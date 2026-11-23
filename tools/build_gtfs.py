#!/usr/bin/env python3
"""Generates lines.json (lines, branches, stations, stopping patterns) from the GTFS timetables.

Usage, from the project folder:
    python3 Tools/build_gtfs.py              # downloads the GTFS files (cache build/cache/)
    python3 Tools/build_shapes.py            # then adds the real track geometry

Sources (scheduled timetables, ODbL license / SNCF terms of use):
  - IDFM: RER A to E and Transilien H J K L N P R U V
  - SNCF: TER serving a Paris station (Nord, Est, Lyon, Bercy, Austerlitz,
           Montparnasse, Saint-Lazare)

A line = a commercial line (RER, Transilien) or a TER service.
A branch = a typical route (ordered sequence of served stations); routes
included in another (semi-fast, partial routes) are merged.
Python 3.9+, standard library only (downloads via curl).
"""
import argparse
import csv
import io
import json
import math
import os
import re
import subprocess
import sys
import unicodedata
import zipfile
from collections import defaultdict

IDFM_URL = ("https://data.iledefrance-mobilites.fr/explore/dataset/offre-horaires-tc-gtfs-idfm/"
            "files/a925e164271e4bca93433756d6a340d1/download/")
# « Réseau SNCF TGV, Intercités et TER » (transport.data.gouv.fr, resource 83582).
# The old export-opendata-sncf-gtfs.zip is no longer updated (timetables frozen at June 2026).
SNCF_URL = "https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip"

# IDFM lines kept: GTFS short name → (identifier, name, group)
IDFM_LINES = {
    "A": ("RER-A", "RER A", "RER"), "B": ("RER-B", "RER B", "RER"), "C": ("RER-C", "RER C", "RER"),
    "D": ("RER-D", "RER D", "RER"), "E": ("RER-E", "RER E", "RER"),
    "H": ("H", "Ligne H", "Transilien"), "J": ("J", "Ligne J", "Transilien"),
    "K": ("K", "Ligne K", "Transilien"), "L": ("L", "Ligne L", "Transilien"),
    "N": ("N", "Ligne N", "Transilien"), "P": ("P", "Ligne P", "Transilien"),
    "R": ("R", "Ligne R", "Transilien"), "U": ("U", "Ligne U", "Transilien"),
    "V": ("V", "Ligne V", "Transilien"),
}

# Paris terminus stations (UIC codes)
PARIS_TERMINI = {
    "87271007": "Paris-Nord", "87113001": "Paris-Est", "87686006": "Paris-Gare-de-Lyon",
    "87686667": "Paris-Bercy", "87547000": "Paris-Austerlitz", "87391003": "Paris-Montparnasse",
    "87391102": "Paris-Montparnasse",   # hall 3 Vaugirard (TER Centre-Val de Loire)
    "87384008": "Paris-Saint-Lazare", "87271023": "Paris-Nord", "87686030": "Paris-Gare-de-Lyon",
}

MERGE_DIST = 150.0     # two stops less than 150 m apart = the same station
MIN_TRIPS = 4          # routes too rare are ignored (engineering works, diversions…)
MIN_SHARE = 0.01       # … or less than 1 % of the line's trips
R = 6371008.8


def hav(a, b):
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(h)))


def download(url, path):
    if os.path.exists(path):
        return path
    print("→ Téléchargement %s" % url)
    tmp = path + ".part"
    r = subprocess.run(["curl", "-fL", "--progress-bar", "-o", tmp, url])
    if r.returncode != 0:
        sys.exit("Échec du téléchargement (curl %d) : %s" % (r.returncode, url))
    os.replace(tmp, path)
    return path


def read_csv(z, name):
    with z.open(name) as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline="")):
            yield row


def zip_member(z, name, required=True):
    """File name in the archive (handles a possible subfolder)."""
    for n in z.namelist():
        if n == name or n.endswith("/" + name):
            return n
    if required:
        sys.exit("%s absent de l'archive GTFS" % name)
    return None


def nice_name(s):
    s = s.strip()
    return s[:1].upper() + s[1:] if s else s


# --- Station registry (merging nearby stops across datasets) ----------------

class Registry:
    CELL = 0.005

    def __init__(self):
        self.stations = {}
        self.grid = defaultdict(list)
        # UIC codes merged into another station (halls of the same station, neighboring stops):
        # SNCF real time may mention them, the app finds them through the "uic" field
        self.aliases = defaultdict(set)

    def _key(self, lat, lon):
        return (int(lat // self.CELL), int(lon // self.CELL))

    def resolve(self, sid, name, lat, lon):
        """Returns the station identifier (an existing station within MERGE_DIST, otherwise sid)."""
        if sid in self.stations:
            return sid
        ki, kj = self._key(lat, lon)
        best = None
        for i in range(ki - 1, ki + 2):
            for j in range(kj - 1, kj + 2):
                for other in self.grid[(i, j)]:
                    s = self.stations[other]
                    d = hav((lat, lon), (s["lat"], s["lon"]))
                    if d < MERGE_DIST and (best is None or d < best[1]):
                        best = (other, d)
        if best:
            if UIC_RE.fullmatch(sid) and sid != best[0]:
                self.aliases[best[0]].add(sid)
            return best[0]
        self.stations[sid] = {"id": sid, "name": name, "lat": round(lat, 6), "lon": round(lon, 6)}
        self.grid[(ki, kj)].append(sid)
        return sid


def load_stops(z):
    """stop_id → (stop zone id, name, lat, lon)."""
    rows = {r["stop_id"]: r for r in read_csv(z, zip_member(z, "stops.txt"))}
    out = {}
    for sid, r in rows.items():
        parent = r.get("parent_station") or ""
        area = rows.get(parent, r) if parent else r
        try:
            lat, lon = float(area["stop_lat"]), float(area["stop_lon"])
        except (KeyError, ValueError):
            lat, lon = float(r["stop_lat"]), float(r["stop_lon"])
        out[sid] = (area["stop_id"], area.get("stop_name") or r.get("stop_name", ""), lat, lon)
    return out


def load_patterns(z, trip_route):
    """{route_id: {tuple(stop_id…): trip_count}} for the kept trips.

    stop_times.txt can exceed 1 GB (IDFM buses included): raw reading, line by line,
    decoding only the kept trips."""
    name = zip_member(z, "stop_times.txt")
    seqs = defaultdict(list)
    with z.open(name) as f:
        header = f.readline().decode("utf-8-sig").strip().split(",")
        i_trip, i_stop, i_seq = (header.index(c) for c in ("trip_id", "stop_id", "stop_sequence"))
        i_arr = header.index("arrival_time") if "arrival_time" in header else None
        i_dep = header.index("departure_time") if "departure_time" in header else None
        quoted = False
        wanted = {t.encode() for t in trip_route}
        for raw in f:
            if b'"' in raw:
                quoted = True
                row = next(csv.reader([raw.decode("utf-8")]))
                tid = row[i_trip].encode()
            else:
                row = None
                tid = raw.split(b",", i_trip + 1)[i_trip] if i_trip else raw[:raw.find(b",")]
            if tid not in wanted:
                continue
            if row is None:
                row = raw.decode("utf-8").rstrip("\r\n").split(",")
            arr = hms(row[i_arr]) if i_arr is not None else None
            dep = hms(row[i_dep]) if i_dep is not None else None
            seqs[row[i_trip]].append((int(row[i_seq]), row[i_stop], arr, dep))
        del quoted
    patterns = defaultdict(lambda: defaultdict(int))
    legs = defaultdict(lambda: defaultdict(list))   # {route_id: {(stopA, stopB): [s…]}}
    for tid, s in seqs.items():
        s.sort()
        rid = trip_route[tid]
        patterns[rid][tuple(x[1] for x in s)] += 1
        for a, b in zip(s, s[1:]):
            start = a[3] if a[3] is not None else a[2]
            end = b[2] if b[2] is not None else b[3]
            if start is not None and end is not None and 0 < end - start < 4 * 3600:
                legs[rid][(a[1], b[1])].append(end - start)
    return patterns, legs, seqs


def load_calendar(z):
    """{service_id: set(date "YYYYMMDD")} from calendar.txt and calendar_dates.txt."""
    import datetime as dt
    days = defaultdict(set)
    name = zip_member(z, "calendar.txt", required=False)
    if name:
        cols = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        for r in read_csv(z, name):
            d = dt.datetime.strptime(r["start_date"], "%Y%m%d").date()
            end = dt.datetime.strptime(r["end_date"], "%Y%m%d").date()
            week = [r.get(c) == "1" for c in cols]
            while d <= end:
                if week[d.weekday()]:
                    days[r["service_id"]].add(d.strftime("%Y%m%d"))
                d += dt.timedelta(days=1)
    name = zip_member(z, "calendar_dates.txt", required=False)
    if name:
        for r in read_csv(z, name):
            if r.get("exception_type") == "1":
                days[r["service_id"]].add(r["date"])
            elif r.get("exception_type") == "2":
                days[r["service_id"]].discard(r["date"])
    return days


def trip_stations(seq, to_station):
    """Stops of a trip → [(station, arrival s, departure s)], two platforms of the same station merged."""
    out = []
    for _, stop, arr, dep in seq:
        sid = to_station(stop)
        if arr is None:
            arr = dep
        if dep is None:
            dep = arr
        if arr is None:
            return None
        if out and out[-1][0] == sid:
            out[-1] = (sid, out[-1][1], dep)
        else:
            out.append((sid, arr, dep))
    return out if len(out) >= 2 else None


def attach_trips(line, trips):
    """Keeps the trips whose stopping pattern follows a branch of the line, in either direction."""
    index = [{s["id"]: i for i, s in enumerate(b["stations"])} for b in line["branches"]]
    kept = []
    for t in trips:
        ids = [x[0] for x in t["stops"]]
        for ix in index:
            if ids[0] not in ix or ids[-1] not in ix:
                continue
            pos = [ix.get(i) for i in ids]
            if None in pos:
                continue
            if all(a < b for a, b in zip(pos, pos[1:])) or all(a > b for a, b in zip(pos, pos[1:])):
                kept.append(t)
                break
    line["_trips"] = kept


def hms(v):
    """"HH:MM:SS" (hours > 24 allowed) → seconds."""
    try:
        h, m, sec = v.strip().split(":")
        return int(h) * 3600 + int(m) * 60 + int(sec)
    except (ValueError, AttributeError):
        return None


def leg_times(line, pool):
    """Median scheduled travel time (s), excluding stops, between consecutive served stations.

    Key "idA>idB" in the direction of travel: running is not symmetric
    (gradients, speed limits, approach to large stations)."""
    ids = {s["id"] for b in line["branches"] for s in b["stations"]}
    out = {}
    for (a, b), vals in pool.items():
        if a in ids and b in ids and a != b:
            v = sorted(vals)
            out["%s>%s" % (a, b)] = int(round(v[len(v) // 2]))
    return dict(sorted(out.items()))


# --- Typical routes → branches -------------------------------------------------

def is_subseq(p, q):
    it = iter(q)
    return all(x in it for x in p)


def maximal_patterns(counted, with_subsumed=False):
    """Keeps the frequent routes that are not included in any other (in either direction).

    with_subsumed: also returns {kept route: {ends of the included routes}}."""
    total = sum(counted.values())
    merged = defaultdict(int)
    for p, n in counted.items():
        if len(p) >= 2:
            merged[min(p, p[::-1])] += n
    keep = [p for p, n in merged.items() if n >= MIN_TRIPS and n >= MIN_SHARE * total]
    keep.sort(key=len, reverse=True)
    out = []
    subsumed = defaultdict(set)
    for p in keep:
        host = next((q for q in out if is_subseq(p, q) or is_subseq(p[::-1], q)), None)
        if host is None:
            out.append(p)
        else:
            subsumed[host].update((p[0], p[-1]))
    return (out, subsumed) if with_subsumed else out


def orient(p, stations):
    """Canonical direction: from the terminus nearest to Paris towards the other."""
    centre = (48.8566, 2.3522)
    a, b = stations[p[0]], stations[p[-1]]
    da, db = hav(centre, (a["lat"], a["lon"])), hav(centre, (b["lat"], b["lon"]))
    return p if da <= db else p[::-1]


def align(patterns, stations):
    """Orients all the routes of a line in the same direction as the longest one
    (otherwise "outbound" would mean east → west for one RER A branch and the opposite for another)."""
    oriented = [orient(p, stations) for p in patterns]
    ref = max(oriented, key=len)
    rank = {s: i for i, s in enumerate(ref)}
    out = []
    for p in oriented:
        common = [s for s in p if s in rank]
        if len(common) >= 2 and rank[common[0]] > rank[common[-1]]:
            p = p[::-1]
        out.append(p)
    return out


def make_branches(line_id, patterns, reg):
    branches = []
    for k, p in enumerate(align(patterns, reg.stations)):
        st = [dict(reg.stations[s], stop=True) for s in p]
        for s in st:
            if s["id"] in PARIS_TERMINI:
                s["name"] = PARIS_TERMINI[s["id"]]   # unique name for the halls of the same station
        branches.append({
            "id": "%s-%d" % (line_id, k + 1),
            "name": "%s ↔ %s" % (st[0]["name"], st[-1]["name"]),
            "stations": st,
        })
    branches.sort(key=lambda b: (b["stations"][0]["name"], b["stations"][-1]["name"]))
    for k, b in enumerate(branches):
        b["id"] = "%s-%d" % (line_id, k + 1)
    # Two routes with the same terminuses: tell them apart by a station specific to each
    by_name = defaultdict(list)
    for b in branches:
        by_name[b["name"]].append(b)
    for same in by_name.values():
        if len(same) < 2:
            continue
        for b in same:
            others = {s["id"] for o in same if o is not b for s in o["stations"]}
            own = [s for s in b["stations"][1:-1] if s["id"] not in others]
            if own:
                b["name"] += " (via %s)" % own[len(own) // 2]["name"]
    return branches


def dedupe_station_ids(pattern):
    out = []
    for s in pattern:
        if not out or out[-1] != s:
            out.append(s)
    return tuple(out)


# --- SNCF: TER to Paris --------------------------------------------------------

UIC_RE = re.compile(r"(87\d{6})")

REGIONAL_RAIL = "106"   # Extended GTFS: Regional Rail Service (NeTEx: RailSubmode regionalRail)


def is_ter(route_type, stop_ids):
    """Is a route a TER?

    1. Extended GTFS route type 106 (Regional Rail), if published;
       another extended type (102 long distance, 109 suburban…) is not a TER.
    2. Otherwise (generic type 2, the current case of the SNCF GTFS): the commercial mode
       encoded in the stop points, "StopPoint:OCETrain TER-<UIC>"."""
    if route_type == REGIONAL_RAIL:
        return True
    if route_type.isdigit() and 100 <= int(route_type) <= 199:
        return False
    return all(s.startswith("StopPoint:OCETrain TER") for s in stop_ids)


def sncf_lines(path, reg):
    z = zipfile.ZipFile(path)
    agencies = {r["agency_id"]: r.get("agency_name", "") for r in read_csv(z, zip_member(z, "agency.txt"))}
    # Trains (generic type 2 or extended rail types); TER sorting is done by is_ter()
    routes = {}
    for r in read_csv(z, zip_member(z, "routes.txt")):
        rt = r.get("route_type", "")
        if rt == "2" or (rt.isdigit() and 100 <= int(rt) <= 199):   # train, or extended rail types
            routes[r["route_id"]] = r
    trip_route = {r["trip_id"]: r["route_id"] for r in read_csv(z, zip_member(z, "trips.txt"))
                  if r["route_id"] in routes}
    stops = load_stops(z)

    def uic(stop_id):
        area = stops[stop_id][0]
        m = UIC_RE.search(area) or UIC_RE.search(stop_id)
        return m.group(1) if m else area

    def station_id(stop_id):
        area, name, lat, lon = stops[stop_id]
        return reg.resolve(uic(stop_id), nice_name(name), lat, lon)

    raw, raw_legs, seqs = load_patterns(z, trip_route)
    calendar = load_calendar(z)
    meta = {r["trip_id"]: r for r in read_csv(z, zip_member(z, "trips.txt")) if r["trip_id"] in trip_route}

    # Scheduled TER travel times, per pair of stations (all services combined)
    ter_legs = defaultdict(list)
    for rid, by_pair in raw_legs.items():
        rtype = routes[rid].get("route_type", "")
        for (a, b), vals in by_pair.items():
            if not is_ter(rtype, (a, b)):
                continue
            sa, sb = station_id(a), station_id(b)
            if sa != sb:
                ter_legs[(sa, sb)] += vals

    # One TER line per pair (Paris station, final destination):
    #  - services following the same path (C3, K3, P13… to Chartres / Le Mans)
    #    can't be told apart without knowing the train: they form a single line;
    #  - directions that split (Le Mans / Granville) are separate lines.
    # A route included in a longer one (Paris → Chartres in Paris → Le Mans) joins
    # the longer one's line; its terminuses appear as a subtitle (« Aussi : Chartres »).
    corridors = defaultdict(lambda: defaultdict(int))
    colors = defaultdict(lambda: defaultdict(int))
    for rid, counted in raw.items():
        r = routes[rid]
        rtype = r.get("route_type", "")
        for p, n in counted.items():
            if not is_ter(rtype, p):
                continue
            raw_uic = [uic(s) for s in p]
            paris = [PARIS_TERMINI[u] for u in raw_uic if u in PARIS_TERMINI]
            if not paris:
                continue
            term = paris[0]
            sp = dedupe_station_ids(tuple(station_id(s) for s in p))
            corridors[term][sp] += n
            colors[(term, sp[0], sp[-1])][(r.get("route_color") or "1E73BE").lstrip("#")[:6]] += n

    def far_end(p):
        return orient(p, reg.stations)[-1]

    lines = []
    for term in sorted(corridors):
        maximal, subsumed = maximal_patterns(corridors[term], with_subsumed=True)
        by_dest = defaultdict(list)
        for p in maximal:
            by_dest[far_end(p)].append(p)

        # A destination served along the way by a longer line joins that line
        # (Nogent-le-Rotrou → Le Mans line, Argentan → Granville line): same path,
        # only the stopping pattern differs (all stations / semi-fast).
        def km(p):
            return sum(hav((reg.stations[a]["lat"], reg.stations[a]["lon"]),
                           (reg.stations[b]["lat"], reg.stations[b]["lon"])) for a, b in zip(p, p[1:]))
        order = sorted(by_dest, key=lambda d: -max(km(p) for p in by_dest[d]))
        merged_into = {}
        for d in order:
            host = next((h for h in order if h != d and h not in merged_into
                         and max(km(p) for p in by_dest[h]) > max(km(p) for p in by_dest[d])
                         and any(d in q for q in by_dest[h])), None)
            if host is not None:
                merged_into[d] = host
        for d, h in merged_into.items():
            while h in merged_into:
                h = merged_into[h]
            by_dest[h] += by_dest.pop(d)
            subsumed_dest = subsumed.setdefault(by_dest[h][0], set())
            subsumed_dest.add(d)

        for dest, pats in sorted(by_dest.items(), key=lambda x: reg.stations[x[0]]["name"]):
            dest_name = reg.stations[dest]["name"]
            slug = re.sub(r"[^A-Za-z]+", "", term.replace("Paris-", "")).upper()
            dslug = re.sub(r"[^A-Za-z]+", "", unicodedata.normalize("NFKD", dest_name)
                           .encode("ascii", "ignore").decode()).upper()[:20]
            lid = "TER-%s-%s" % (slug, dslug)
            also = []
            for p in pats:
                for e in subsumed.get(p, ()):
                    name = reg.stations[e]["name"]
                    if e != dest and e not in PARIS_TERMINI and name not in also:
                        also.append(name)
            # Order of the "also" terminuses: along the line, from Paris
            ref = orient(max(pats, key=len), reg.stations)
            rank = {s: i for i, s in enumerate(ref)}
            ids_by_name = {reg.stations[s]["name"]: s for p in pats for s in p}
            also.sort(key=lambda nm: rank.get(ids_by_name.get(nm, ""), 10 ** 6))
            col = defaultdict(int)
            for (t, a_, b_), counts in colors.items():
                if t == term and (a_ == dest or b_ == dest):
                    for c, n in counts.items():
                        col[c] += n
            lines.append({
                "id": lid, "name": "TER %s – %s" % (term, dest_name), "shortName": "TER",
                "subtitle": ("Aussi : " + ", ".join(also)) if also else None,
                "color": "#" + (max(col, key=col.get) if col else "1E73BE"),
                "group": "TER · %s" % term,
                "branches": make_branches(lid, pats, reg),
            })
            lines[-1]["legTimes"] = leg_times(lines[-1], ter_legs)

    # TER trips (train number, days, times) to recognize the current train
    ter_trips = []
    for tid, seq in seqs.items():
        rid = trip_route[tid]
        if not is_ter(routes[rid].get("route_type", ""), [x[1] for x in seq]):
            continue
        tstops = trip_stations(seq, station_id)
        m = meta.get(tid, {})
        number = (m.get("trip_short_name") or m.get("trip_headsign") or "").strip()
        if tstops and number:
            ter_trips.append({"number": number, "mission": "", "days": calendar.get(m.get("service_id"), set()),
                              "stops": tstops})
    for line in lines:
        attach_trips(line, ter_trips)
    return lines


# --- IDFM: RER and Transilien ----------------------------------------------------

def idfm_lines(path, reg):
    z = zipfile.ZipFile(path)
    routes = {}
    for r in read_csv(z, zip_member(z, "routes.txt")):
        short = (r.get("route_short_name") or "").strip().upper()
        if r.get("route_type") in ("2", "1", "100", "109") and short in IDFM_LINES:
            # Several route_id may have the same short name: they are grouped
            routes[r["route_id"]] = (short, r)
    trip_route = {r["trip_id"]: r["route_id"] for r in read_csv(z, zip_member(z, "trips.txt"))
                  if r["route_id"] in routes}
    stops = load_stops(z)
    stops_by_id = stops
    resolved = {}

    def idfm_station_id(stop_id):
        if stop_id not in resolved:
            resolved[stop_id] = reg.resolve(*_idfm_station(stops_by_id[stop_id]))
        return resolved[stop_id]
    raw, raw_legs, seqs = load_patterns(z, trip_route)
    calendar = load_calendar(z)
    meta = {r["trip_id"]: r for r in read_csv(z, zip_member(z, "trips.txt")) if r["trip_id"] in trip_route}

    pools = defaultdict(lambda: defaultdict(list))   # {short name: {(stationA, stationB): [s…]}}
    for rid, by_pair in raw_legs.items():
        short = routes[rid][0]
        for (a, b), vals in by_pair.items():
            sa = reg.resolve(*_idfm_station(stops[a]))
            sb = reg.resolve(*_idfm_station(stops[b]))
            if sa != sb:
                pools[short][(sa, sb)] += vals

    grouped = defaultdict(lambda: defaultdict(int))
    colors = {}
    for rid, counted in raw.items():
        short, r = routes[rid]
        colors.setdefault(short, r.get("route_color") or "")
        for p, n in counted.items():
            sp = dedupe_station_ids(tuple(reg.resolve(*_idfm_station(stops[s])) for s in p))
            grouped[short][sp] += n

    lines = []
    for short, counted in sorted(grouped.items()):
        lid, name, group = IDFM_LINES[short]
        pats = maximal_patterns(counted)
        if not pats:
            continue
        lines.append({
            "id": lid, "name": name, "shortName": short, "group": group,
            "color": "#" + (colors.get(short) or "888888").lstrip("#")[:6],
            "branches": make_branches(lid, pats, reg),
        })
        lines[-1]["legTimes"] = leg_times(lines[-1], pools[short])

        # Trips: number (trip_short_name) and mission code (trip_headsign, e.g. « ROPO »)
        trips = []
        for tid, seq in seqs.items():
            if routes[trip_route[tid]][0] != short:
                continue
            tstops = trip_stations(seq, idfm_station_id)
            m = meta.get(tid, {})
            number = (m.get("trip_short_name") or "").strip()
            headsign = (m.get("trip_headsign") or "").strip()
            if not tstops:
                continue
            if number.isdigit():
                trips.append({"number": number, "mission": headsign[:8],
                              "days": calendar.get(m.get("service_id"), set()), "stops": tstops})
            else:
                # RER A / B (RATP): no public train number, only a mission code
                # (« QIKI »). Internal identifier "~CODE+departure time", never shown as is.
                code = number if number.isalpha() else (headsign if headsign.isalpha() and len(headsign) <= 6 else "")
                code = code.upper()[:8]
                synthetic = "~%s%04d" % (code or "X", tstops[0][2] // 60)
                trips.append({"number": synthetic, "mission": code,
                              "days": calendar.get(m.get("service_id"), set()), "stops": tstops})
        attach_trips(lines[-1], trips)
        print("  %-8s %5d circulations, %5d rattachées" % (short, len(trips), len(lines[-1]["_trips"])))
    return lines


def build_timetables(lines, ndays):
    """Bundled timetables: for each line, its trips over the next `ndays` days.

    Compact format (JSON):
      from, to     : period covered (YYYYMMDD)
      services     : days of operation, one bit per day from `from` (hexadecimal string)
      lines[id]    : st (stations), trips = [number, mission, service, [station, arrival, departure, …]]
                     times in minutes since midnight of the day of operation (> 1440 after midnight)."""
    import datetime as dt
    ndays = max(1, min(ndays, 64))   # a service = a 64-bit integer on the app side
    start = dt.date.today()
    window = [(start + dt.timedelta(days=i)).strftime("%Y%m%d") for i in range(ndays)]
    pos = {d: i for i, d in enumerate(window)}
    services, service_index = [], {}
    out = {}
    for line in lines:
        merged = {}
        for t in line.get("_trips", []):
            bits = 0
            for d in t["days"]:
                if d in pos:
                    bits |= 1 << pos[d]
            if not bits:
                continue
            stops = tuple((sid, arr // 60, dep // 60) for sid, arr, dep in t["stops"])
            key = (t["number"], t["mission"], stops)
            merged[key] = merged.get(key, 0) | bits
        if not merged:
            continue
        st, st_index, trips = [], {}, []
        for (number, mission, stops), bits in sorted(merged.items(), key=lambda x: (x[0][2][0][2], x[0][0])):
            hexbits = format(bits, "x")
            if hexbits not in service_index:
                service_index[hexbits] = len(services)
                services.append(hexbits)
            flat = []
            for sid, arr, dep in stops:
                if sid not in st_index:
                    st_index[sid] = len(st)
                    st.append(sid)
                flat += [st_index[sid], arr, dep]
            trips.append([number, mission, service_index[hexbits], flat])
        out[line["id"]] = {"st": st, "trips": trips}
    return {"version": 1, "from": window[0], "to": window[-1], "services": services, "lines": out}


def attach_idfm_refs(path, lines, radius=600):
    """IDFM identifiers of each Île-de-France station, for the PRIM real-time API.

    Field `idfm`: rail stop zones ("monomodalStopPlace") within `radius` m,
    from nearest to farthest, then their interchange zones. The app tries
    `STIF:StopArea:SP:<id>:` in this order until it finds the train."""
    z = zipfile.ZipFile(path)
    monos = []
    for r in read_csv(z, zip_member(z, "stops.txt")):
        sid = r["stop_id"]
        if not sid.startswith("IDFM:monomodalStopPlace:"):
            continue
        try:
            lat, lon = float(r["stop_lat"]), float(r["stop_lon"])
        except ValueError:
            continue
        parent = (r.get("parent_station") or "").replace("IDFM:", "")
        monos.append((sid.rsplit(":", 1)[1], parent, lat, lon))
    n = 0
    for line in lines:
        for b in line["branches"]:
            for st in b["stations"]:
                near = sorted((hav((st["lat"], st["lon"]), (la, lo)), m, p) for m, p, la, lo in monos
                              if abs(la - st["lat"]) < 0.01 and abs(lo - st["lon"]) < 0.015)
                near = [x for x in near if x[0] <= radius]
                refs = []
                for _, m, _p in near[:2]:
                    if m not in refs:
                        refs.append(m)
                for _, _m, p in near[:2]:
                    if p and p not in refs:
                        refs.append(p)
                if refs:
                    st["idfm"] = refs
                    n += 1
    return n


def attach_exits(path, lines, radius=250):
    """Entrances / exits of Île-de-France stations (IDFM GTFS, location_type = 2).

    Field `exits`: [[name, lat, lon], …], entrances attached to a stop zone of the station
    (field `idfm`), failing that those within `radius` m. Order: by name (the GTFS does not give
    a main entrance)."""
    z = zipfile.ZipFile(path)
    by_parent, allx = defaultdict(list), []
    for r in read_csv(z, zip_member(z, "stops.txt")):
        if (r.get("location_type") or "") != "2":
            continue
        try:
            lat, lon = round(float(r["stop_lat"]), 6), round(float(r["stop_lon"]), 6)
        except ValueError:
            continue
        e = [(r.get("stop_name") or "").strip(), lat, lon]
        by_parent[(r.get("parent_station") or "").replace("IDFM:", "")].append(e)
        allx.append(e)
    n = 0
    for line in lines:
        for b in line["branches"]:
            for st in b["stations"]:
                if st.get("wp"):
                    continue
                found = [e for ref in st.get("idfm", []) for e in by_parent.get(ref, [])]
                if not found and st.get("idfm"):
                    found = [e for e in allx if abs(e[1] - st["lat"]) < 0.005 and abs(e[2] - st["lon"]) < 0.007
                             and hav((st["lat"], st["lon"]), (e[1], e[2])) <= radius]
                uniq = sorted({(e[0], e[1], e[2]) for e in found})
                if uniq:
                    st["exits"] = [list(e) for e in uniq]
                    n += 1
    return n


URBAN_MODES = {"1": "metro", "0": "tram"}


def urban_network(path):
    """Île-de-France metro and tram, for real-time (PRIM) boards only.

    No detailed timetables: per line, a summary per type of day (first and last
    departure, median interval at peak, off-peak and evening times) for the fallback without
    network. Per station (the mode's stop zone): name, position, IDFM zones for PRIM, lines."""
    import datetime as dt
    from statistics import median
    z = zipfile.ZipFile(path)
    rows = {r["stop_id"]: r for r in read_csv(z, zip_member(z, "stops.txt"))}
    # Single-mode stop zones (one per mode) attached to each interchange zone
    monos = defaultdict(list)
    for r in rows.values():
        if r["stop_id"].startswith("IDFM:monomodalStopPlace:") and r.get("parent_station"):
            monos[r["parent_station"]].append(r["stop_id"].rsplit(":", 1)[-1])
    routes = {}
    for r in read_csv(z, zip_member(z, "routes.txt")):
        mode = URBAN_MODES.get(r.get("route_type", ""))
        short = (r.get("route_short_name") or "").strip()
        if mode and short:
            routes[r["route_id"]] = (mode, short, r)
    trips = {r["trip_id"]: r for r in read_csv(z, zip_member(z, "trips.txt")) if r["route_id"] in routes}
    trip_route = {t: r["route_id"] for t, r in trips.items()}
    _, _, seqs = load_patterns(z, trip_route)
    calendar = load_calendar(z)

    def tail(sid):
        return sid.rsplit(":", 1)[-1]

    # Reference dates: next Tuesday, Saturday and Sunday
    today = dt.date.today()
    def next_day(wd):
        return (today + dt.timedelta(days=(wd - today.weekday()) % 7)).strftime("%Y%m%d")
    ref_days = {"wd": next_day(1), "sa": next_day(5), "su": next_day(6)}

    lines, stations = {}, {}
    times = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))   # {line: {day: {(stop, direction): [min]}}}
    patterns = defaultdict(lambda: defaultdict(int))   # {line: {(station…): count}}
    for tid, seq in seqs.items():
        visited = []
        mode, short, r = routes[trip_route[tid]]
        lid = ("M" if mode == "metro" else "") + short
        line = lines.setdefault(lid, {
            "id": lid, "idfmLine": tail(r["route_id"]), "mode": mode, "shortName": short,
            "name": ("Métro " if mode == "metro" else "Tram ") + short,
            "color": "#" + ((r.get("route_color") or "888888").lstrip("#")[:6]),
            "textColor": "#" + ((r.get("route_text_color") or "FFFFFF").lstrip("#")[:6]),
        })
        days = calendar.get(trips[tid].get("service_id"), set())
        direction = trips[tid].get("direction_id") or "0"
        for _, stop, arr, dep in seq:
            quai = rows.get(stop, {})
            parent = rows.get(quai.get("parent_station") or "", {})
            area = parent or quai
            if not area:
                continue
            sid = tail(area["stop_id"])
            st = stations.get(sid)
            if st is None:
                grand = rows.get(area.get("parent_station") or "", {})
                try:
                    lat, lon = round(float(area["stop_lat"]), 6), round(float(area["stop_lon"]), 6)
                except (KeyError, ValueError):
                    continue
                # PRIM: the interchange zone, then its single-mode zones (metro, tram…)
                refs = [sid] + sorted(set(monos.get(area["stop_id"], [])))[:6] + ([tail(grand["stop_id"])] if grand else [])
                st = stations[sid] = {"id": sid, "name": nice_name(area.get("stop_name") or ""),
                                      "lat": lat, "lon": lon, "refs": refs, "lines": []}
            if lid not in st["lines"]:
                st["lines"].append(lid)
            st.setdefault("_quais", set()).add(tail(stop))
            if not visited or visited[-1] != sid:
                visited.append(sid)
            t = dep if dep is not None else arr
            if t is None:
                continue
            for key, day in ref_days.items():
                if day in days:
                    times[lid][key][(sid, direction)].append(t // 60)
        if len(visited) >= 2:
            patterns[lid][tuple(visited)] += 1

    for lid, line in lines.items():
        summary = {}
        for key in ref_days:
            by_stop = times[lid].get(key) or {}
            if not by_stop:
                continue
            # Departures before 4 am: end of the previous night (counted after midnight)
            all_t = [t + 1440 if t < 240 else t for ts in by_stop.values() for t in ts]
            ref = sorted(max(by_stop.values(), key=len))
            def gap(a, b):
                g = [y - x for x, y in zip(ref, ref[1:]) if a * 60 <= x < b * 60 and y > x]
                return int(round(median(g))) if g else None
            summary[key] = [min(all_t), max(all_t), gap(7, 9), gap(10, 16), gap(21, 24)]
        line["summary"] = summary
        # Routes (direction of travel): the most frequent ones, without those included in another
        counted = patterns[lid]
        total = sum(counted.values()) or 1
        keep = [p for p, n in counted.items() if n >= 10 and n >= 0.01 * total]
        keep.sort(key=len, reverse=True)
        maximal = []
        for p in keep:
            if not any(is_subseq(p, q) for q in maximal):
                maximal.append(p)
        line["patterns"] = [list(p) for p in maximal]

    def order(l):
        num = re.match(r"\d+", l["shortName"].lstrip("T"))
        return (l["mode"] != "metro", int(num.group()) if num else 99, l["shortName"])
    out_lines = sorted(lines.values(), key=order)
    rank = {l["id"]: i for i, l in enumerate(out_lines)}
    out_st = []
    for st in stations.values():
        st["lines"].sort(key=lambda x: rank.get(x, 99))
        st["quais"] = sorted(st.pop("_quais", set()))
        out_st.append(st)
    out_st.sort(key=lambda s: s["name"])
    return {"lines": out_lines, "stations": out_st}


def attach_uic_aliases(lines, reg):
    """Adds to the stations their secondary UIC codes ("uic" field), used by SNCF real time."""
    n = 0
    for line in lines:
        for b in line["branches"]:
            for s in b["stations"]:
                extra = sorted(reg.aliases.get(s["id"], ()))
                if extra:
                    s["uic"] = extra
                    n += 1
    return n


def _idfm_station(stop):
    area, name, lat, lon = stop
    return (area, nice_name(name), lat, lon)


# --- Main program --------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="NextStation/Resources/lines.json")
    ap.add_argument("--cache", default="build/cache")
    ap.add_argument("--idfm", help="GTFS IDFM déjà téléchargé (zip)")
    ap.add_argument("--sncf", help="GTFS SNCF déjà téléchargé (zip)")
    ap.add_argument("--days", type=int, default=60, help="jours d'horaires embarqués à partir d'aujourd'hui (64 max)")
    args = ap.parse_args()

    os.makedirs(args.cache, exist_ok=True)
    sncf = args.sncf or download(SNCF_URL, os.path.join(args.cache, "sncf-gtfs.zip"))
    idfm = args.idfm or download(IDFM_URL, os.path.join(args.cache, "idfm-gtfs.zip"))

    reg = Registry()   # SNCF first: its stations carry the UIC code
    print("→ Lecture du GTFS SNCF (TER)…")
    ter = sncf_lines(sncf, reg)
    print("→ Lecture du GTFS IDFM (RER, Transilien)… (peut prendre une minute)")
    idf = idfm_lines(idfm, reg)

    lines = idf + sorted(ter, key=lambda l: (l["group"], l["name"]))
    n_alias = attach_uic_aliases(lines, reg)
    n_refs = attach_idfm_refs(idfm, lines)
    n_exits = attach_exits(idfm, lines)
    print("✓ entrées de gare pour %d gares (Île-de-France)" % n_exits)
    timetables = build_timetables(lines, args.days)
    tt_path = os.path.join(os.path.dirname(args.out), "timetables.json")
    with open(tt_path, "w", encoding="utf-8") as f:
        json.dump(timetables, f, ensure_ascii=False, separators=(",", ":"))
    for l in lines:
        l.pop("_trips", None)
    print("→ Métro et tramway (temps réel seulement)…")
    urban = urban_network(idfm)
    print("✓ %d lignes de métro et de tram, %d stations" % (len(urban["lines"]), len(urban["stations"])))
    for l in urban["lines"]:
        print("    %-5s %-10s %s · %d parcours %s" % (l["id"], l["idfmLine"], l["summary"].get("wd"),
                                                  len(l["patterns"]), [len(p) for p in l["patterns"]]))
    for st in urban["stations"]:
        if st["name"].startswith(("Châtelet", "Montparnasse", "La Défense", "Porte de Versailles")):
            print("    ·", {k: v for k, v in st.items() if k != "quais"}, len(st["quais"]), "quais")
    catalog = {
        "version": 2,
        "source": "Horaires théoriques IDFM et SNCF (GTFS) · Tracé : SNCF Réseau (ODbL)",
        "lines": lines,
        "urban": urban,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=1)
        f.write("\n")

    for l in lines:
        print("  %-28s %-26s %2d branche(s) : %s" % (
            l["id"][:28], l["group"][:26], len(l["branches"]),
            " | ".join(b["name"] for b in l["branches"])[:110]))
    n_st = len({s["id"] for l in lines for b in l["branches"] for s in b["stations"]})
    print("\n✓ %d lignes, %d gares → %s" % (len(lines), n_st, args.out))
    print("✓ identifiants IDFM (temps réel PRIM) pour %d arrêts de branche" % n_refs)
    print("✓ codes UIC secondaires (temps réel SNCF) pour %d arrêts de branche" % n_alias)
    n_tr = sum(len(t["trips"]) for t in timetables["lines"].values())
    print("✓ %d circulations du %s au %s → %s (%d Ko)" % (
        n_tr, timetables["from"], timetables["to"], tt_path, os.path.getsize(tt_path) // 1024))
    if not n_tr:
        print("  ⚠︎ Aucune circulation dans la période : GTFS périmé ? (rm build/cache/*-gtfs.zip)")
    print("  Étape suivante : python3 Tools/build_shapes.py")


if __name__ == "__main__":
    main()
