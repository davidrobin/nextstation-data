#!/usr/bin/env python3
"""Génère lines.json (lignes, branches, gares, desserte) à partir des horaires GTFS.

Usage, depuis le dossier du projet :
    python3 Tools/build_gtfs.py              # télécharge les GTFS (cache build/cache/)
    python3 Tools/build_shapes.py            # puis ajoute le tracé réel des voies

Sources (horaires théoriques, licence ODbL / CGU SNCF) :
  - IDFM : RER A à E et Transilien H J K L N P R U V
  - SNCF : TER desservant une gare parisienne (Nord, Est, Lyon, Bercy, Austerlitz,
           Montparnasse, Saint-Lazare)

Une ligne = une ligne commerciale (RER, Transilien) ou une relation TER.
Une branche = un parcours type (suite ordonnée de gares desservies) ; les parcours
inclus dans un autre (semi-directs, parcours partiels) sont fusionnés.
Python 3.9+, bibliothèque standard uniquement (téléchargement via curl).
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
# « Réseau SNCF TGV, Intercités et TER » (transport.data.gouv.fr, ressource 83582).
# L'ancien export-opendata-sncf-gtfs.zip n'est plus mis à jour (horaires figés à juin 2026).
SNCF_URL = "https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip"

# Lignes IDFM retenues : nom court GTFS → (identifiant, nom, groupe)
IDFM_LINES = {
    "A": ("RER-A", "RER A", "RER"), "B": ("RER-B", "RER B", "RER"), "C": ("RER-C", "RER C", "RER"),
    "D": ("RER-D", "RER D", "RER"), "E": ("RER-E", "RER E", "RER"),
    "H": ("H", "Ligne H", "Transilien"), "J": ("J", "Ligne J", "Transilien"),
    "K": ("K", "Ligne K", "Transilien"), "L": ("L", "Ligne L", "Transilien"),
    "N": ("N", "Ligne N", "Transilien"), "P": ("P", "Ligne P", "Transilien"),
    "R": ("R", "Ligne R", "Transilien"), "U": ("U", "Ligne U", "Transilien"),
    "V": ("V", "Ligne V", "Transilien"),
}

# Gares terminus parisiennes (codes UIC)
PARIS_TERMINI = {
    "87271007": "Paris-Nord", "87113001": "Paris-Est", "87686006": "Paris-Gare-de-Lyon",
    "87686667": "Paris-Bercy", "87547000": "Paris-Austerlitz", "87391003": "Paris-Montparnasse",
    "87391102": "Paris-Montparnasse",   # hall 3 Vaugirard (TER Centre-Val de Loire)
    "87384008": "Paris-Saint-Lazare", "87271023": "Paris-Nord", "87686030": "Paris-Gare-de-Lyon",
}

MERGE_DIST = 150.0     # deux arrêts à moins de 150 m = la même gare
MIN_TRIPS = 4          # parcours trop rares ignorés (travaux, détournements…)
MIN_SHARE = 0.01       # … ou moins de 1 % des circulations de la ligne
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
    """Nom du fichier dans l'archive (gère un éventuel sous-dossier)."""
    for n in z.namelist():
        if n == name or n.endswith("/" + name):
            return n
    if required:
        sys.exit("%s absent de l'archive GTFS" % name)
    return None


def nice_name(s):
    s = s.strip()
    return s[:1].upper() + s[1:] if s else s


# --- Registre des gares (fusion des arrêts proches entre jeux de données) ----

class Registry:
    CELL = 0.005

    def __init__(self):
        self.stations = {}
        self.grid = defaultdict(list)

    def _key(self, lat, lon):
        return (int(lat // self.CELL), int(lon // self.CELL))

    def resolve(self, sid, name, lat, lon):
        """Renvoie l'identifiant de la gare (une gare existante à moins de MERGE_DIST, sinon sid)."""
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
            return best[0]
        self.stations[sid] = {"id": sid, "name": name, "lat": round(lat, 6), "lon": round(lon, 6)}
        self.grid[(ki, kj)].append(sid)
        return sid


def load_stops(z):
    """stop_id → (id de la zone d'arrêt, nom, lat, lon)."""
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
    """{route_id: {tuple(stop_id…): nb_circulations}} pour les courses retenues.

    stop_times.txt peut dépasser le Go (bus IDFM inclus) : lecture brute, ligne à ligne,
    en ne décodant que les courses retenues."""
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
    """{service_id: set(date « AAAAMMJJ »)} d'après calendar.txt et calendar_dates.txt."""
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
    """Arrêts d'une course → [(gare, arrivée s, départ s)], deux quais d'une même gare fusionnés."""
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
    """Garde les courses dont la desserte suit une branche de la ligne, dans un sens ou l'autre."""
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
    """« HH:MM:SS » (heures > 24 admises) → secondes."""
    try:
        h, m, sec = v.strip().split(":")
        return int(h) * 3600 + int(m) * 60 + int(sec)
    except (ValueError, AttributeError):
        return None


def leg_times(line, pool):
    """Temps de parcours théorique médian (s), hors arrêt, entre gares consécutives desservies.

    Clé « idA>idB » dans le sens de circulation : la marche n'est pas symétrique
    (rampes, limitations, approche des grandes gares)."""
    ids = {s["id"] for b in line["branches"] for s in b["stations"]}
    out = {}
    for (a, b), vals in pool.items():
        if a in ids and b in ids and a != b:
            v = sorted(vals)
            out["%s>%s" % (a, b)] = int(round(v[len(v) // 2]))
    return dict(sorted(out.items()))


# --- Parcours types → branches ------------------------------------------------

def is_subseq(p, q):
    it = iter(q)
    return all(x in it for x in p)


def maximal_patterns(counted, with_subsumed=False):
    """Garde les parcours fréquents qui ne sont inclus dans aucun autre (dans un sens ou l'autre).

    with_subsumed : renvoie aussi {parcours gardé: {extrémités des parcours inclus}}."""
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
    """Sens canonique : du terminus le plus proche de Paris vers l'autre."""
    centre = (48.8566, 2.3522)
    a, b = stations[p[0]], stations[p[-1]]
    da, db = hav(centre, (a["lat"], a["lon"])), hav(centre, (b["lat"], b["lon"]))
    return p if da <= db else p[::-1]


def align(patterns, stations):
    """Oriente tous les parcours d'une ligne dans le même sens que le plus long
    (sinon « aller » voudrait dire est → ouest pour une branche du RER A et l'inverse pour une autre)."""
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
                s["name"] = PARIS_TERMINI[s["id"]]   # nom unique pour les halls d'une même gare
        branches.append({
            "id": "%s-%d" % (line_id, k + 1),
            "name": "%s ↔ %s" % (st[0]["name"], st[-1]["name"]),
            "stations": st,
        })
    branches.sort(key=lambda b: (b["stations"][0]["name"], b["stations"][-1]["name"]))
    for k, b in enumerate(branches):
        b["id"] = "%s-%d" % (line_id, k + 1)
    # Deux parcours aux mêmes terminus : on les distingue par une gare propre à chacun
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


# --- SNCF : TER vers Paris ----------------------------------------------------

UIC_RE = re.compile(r"(87\d{6})")

REGIONAL_RAIL = "106"   # GTFS étendu : Regional Rail Service (NeTEx : RailSubmode regionalRail)


def is_ter(route_type, stop_ids):
    """Un parcours est-il un TER ?

    1. Type de route GTFS étendu 106 (Regional Rail), s'il est publié ;
       un autre type étendu (102 grandes lignes, 109 banlieue…) n'est pas un TER.
    2. Sinon (type 2 générique, cas actuel du GTFS SNCF) : le mode commercial
       encodé dans les points d'arrêt, « StopPoint:OCETrain TER-<UIC> »."""
    if route_type == REGIONAL_RAIL:
        return True
    if route_type.isdigit() and 100 <= int(route_type) <= 199:
        return False
    return all(s.startswith("StopPoint:OCETrain TER") for s in stop_ids)


def sncf_lines(path, reg):
    z = zipfile.ZipFile(path)
    agencies = {r["agency_id"]: r.get("agency_name", "") for r in read_csv(z, zip_member(z, "agency.txt"))}
    # Trains (type 2 générique ou types ferroviaires étendus) ; le tri TER se fait par is_ter()
    routes = {}
    for r in read_csv(z, zip_member(z, "routes.txt")):
        rt = r.get("route_type", "")
        if rt == "2" or (rt.isdigit() and 100 <= int(rt) <= 199):   # train, ou types ferroviaires étendus
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

    # Temps de parcours théoriques des TER, par couple de gares (toutes relations confondues)
    ter_legs = defaultdict(list)
    for rid, by_pair in raw_legs.items():
        rtype = routes[rid].get("route_type", "")
        for (a, b), vals in by_pair.items():
            if not is_ter(rtype, (a, b)):
                continue
            sa, sb = station_id(a), station_id(b)
            if sa != sb:
                ter_legs[(sa, sb)] += vals

    # Une ligne TER par couple (gare parisienne, destination finale) :
    #  - les relations qui suivent le même chemin (C3, K3, P13… vers Chartres / Le Mans)
    #    sont indiscernables sans connaître le train : elles forment une seule ligne ;
    #  - les directions qui se séparent (Le Mans / Granville) sont des lignes distinctes.
    # Un parcours inclus dans un plus long (Paris → Chartres dans Paris → Le Mans) rejoint
    # la ligne du plus long ; ses terminus apparaissent en sous-titre (« Aussi : Chartres »).
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

        # Une destination desservie en chemin par une ligne plus longue rejoint cette ligne
        # (Nogent-le-Rotrou → ligne du Mans, Argentan → ligne de Granville) : même chemin,
        # seule la desserte diffère (omnibus / semi-direct).
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
            # Ordre des « aussi » : le long de la ligne, depuis Paris
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

    # Circulations TER (numéro de train, jours, horaires) pour reconnaître le train en cours
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


# --- IDFM : RER et Transilien -------------------------------------------------

def idfm_lines(path, reg):
    z = zipfile.ZipFile(path)
    routes = {}
    for r in read_csv(z, zip_member(z, "routes.txt")):
        short = (r.get("route_short_name") or "").strip().upper()
        if r.get("route_type") in ("2", "1", "100", "109") and short in IDFM_LINES:
            # Plusieurs route_id peuvent porter le même nom court : on regroupe
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

    pools = defaultdict(lambda: defaultdict(list))   # {nom court: {(gareA, gareB): [s…]}}
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

        # Circulations : numéro (trip_short_name) et code mission (trip_headsign, ex. « ROPO »)
        trips = []
        for tid, seq in seqs.items():
            if routes[trip_route[tid]][0] != short:
                continue
            tstops = trip_stations(seq, idfm_station_id)
            m = meta.get(tid, {})
            number = (m.get("trip_short_name") or "").strip()
            if tstops and number.isdigit():
                trips.append({"number": number, "mission": (m.get("trip_headsign") or "").strip()[:8],
                              "days": calendar.get(m.get("service_id"), set()), "stops": tstops})
        attach_trips(lines[-1], trips)
    return lines


def build_timetables(lines, ndays):
    """Horaires embarqués : pour chaque ligne, ses circulations des `ndays` prochains jours.

    Format compact (JSON) :
      from, to     : période couverte (AAAAMMJJ)
      services     : jours de circulation, un bit par jour depuis `from` (chaîne hexadécimale)
      lines[id]    : st (gares), trips = [numéro, mission, service, [gare, arrivée, départ, …]]
                     heures en minutes depuis minuit du jour de circulation (> 1440 après minuit)."""
    import datetime as dt
    ndays = max(1, min(ndays, 64))   # un service = un entier 64 bits côté app
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
    """Identifiants IDFM de chaque gare d'Île-de-France, pour l'API temps réel PRIM.

    Champ `idfm` : zones d'arrêt ferrées (« monomodalStopPlace ») à moins de `radius` m,
    de la plus proche à la plus lointaine, puis leurs zones de correspondance. L'app essaie
    `STIF:StopArea:SP:<id>:` dans cet ordre jusqu'à trouver le train."""
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


def _idfm_station(stop):
    area, name, lat, lon = stop
    return (area, nice_name(name), lat, lon)


# --- Programme principal ------------------------------------------------------

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

    reg = Registry()   # SNCF d'abord : ses gares portent le code UIC
    print("→ Lecture du GTFS SNCF (TER)…")
    ter = sncf_lines(sncf, reg)
    print("→ Lecture du GTFS IDFM (RER, Transilien)… (peut prendre une minute)")
    idf = idfm_lines(idfm, reg)

    lines = idf + sorted(ter, key=lambda l: (l["group"], l["name"]))
    n_refs = attach_idfm_refs(idfm, lines)
    timetables = build_timetables(lines, args.days)
    tt_path = os.path.join(os.path.dirname(args.out), "timetables.json")
    with open(tt_path, "w", encoding="utf-8") as f:
        json.dump(timetables, f, ensure_ascii=False, separators=(",", ":"))
    for l in lines:
        l.pop("_trips", None)
    catalog = {
        "version": 2,
        "source": "Horaires théoriques IDFM et SNCF (GTFS) · Tracé : SNCF Réseau (ODbL)",
        "lines": lines,
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
    n_tr = sum(len(t["trips"]) for t in timetables["lines"].values())
    print("✓ %d circulations du %s au %s → %s (%d Ko)" % (
        n_tr, timetables["from"], timetables["to"], tt_path, os.path.getsize(tt_path) // 1024))
    if not n_tr:
        print("  ⚠︎ Aucune circulation dans la période : GTFS périmé ? (rm build/cache/*-gtfs.zip)")
    print("  Étape suivante : python3 Tools/build_shapes.py")


if __name__ == "__main__":
    main()
