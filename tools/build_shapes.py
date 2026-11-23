#!/usr/bin/env python3
"""Adds the real track geometry (SNCF Réseau, open data) to the branches of lines.json.

Usage, from the project folder:
    python3 Tools/build_shapes.py
    python3 Tools/build_shapes.py --rfn path/to/lignes_rfn.json   # file already downloaded

Source: « Fichier de formes des lignes du Réseau Ferré National » (SNCF Réseau,
data.gouv.fr, ODbL license), operated lines only. Cached in build/cache/.

Steps:
  1. Builds a graph of the rail network (RFN lines connected at their junctions).
  2. For each pair of consecutive stations of a branch, looks for the shortest
     route on this graph (A*), simplifies it (Douglas-Peucker, 5 m) and inserts it
     as track points {"id": "wp", "stop": false, "wp": true}.
     Each station is snapped to the track (original position kept in olat/olon).
  3. Adds the stations located on the track but missing from the branch:
     served by the line → stop: true; otherwise passed → stop: false ("ins": true).
  4. Removes the branches that became redundant.
Without a reliable route, a section stays a straight line (reported).

Idempotent. Python 3.9+, standard library only (downloads via curl).
"""
import argparse
import csv
import heapq
import io
import json
import math
import os
import subprocess
import sys
from collections import defaultdict

RFN_URLS = [
    "https://www.data.gouv.fr/api/1/datasets/r/93aa5f9f-07c7-4c01-a201-1b7e4e03f075",
    "https://www.data.gouv.fr/api/1/datasets/r/bdedd313-bbb6-48f4-988a-48cda4b70ecf",
    "https://www.data.gouv.fr/api/1/datasets/r/a9c45a65-fba8-443c-a860-c959d1b5fb52",
]

SNAP_DIST = 300.0       # max station ↔ track distance (m)
JUNCTION_DIST = 40.0    # a line end less than 40 m from another line = junction
SIMPLIFY_TOL = 5.0      # Douglas-Peucker tolerance (m)
MAX_RATIO = 1.8         # route / straight-line length ratio above which the route is rejected
INSERT_DIST = 80.0      # a station less than 80 m from the track is on the branch
CELL = 0.01             # spatial index (degrees)
R = 6371008.8


# --- Geometry --------------------------------------------------------------------

def hav(a, b):
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(h)))


def local(p, o):
    k = math.pi / 180 * R
    return ((p[1] - o[1]) * math.cos(math.radians(o[0])) * k, (p[0] - o[0]) * k)


def project_on_segment(p, a, b):
    ax, ay = local(a, p)
    bx, by = local(b, p)
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / l2))
    return t, math.hypot(ax + t * dx, ay + t * dy)


def lerp(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def simplify(pts, tol):
    if len(pts) < 3:
        return list(pts)
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        best, idx = 0.0, None
        for k in range(i + 1, j):
            _, d = project_on_segment(pts[k], pts[i], pts[j])
            if d > best:
                best, idx = d, k
        if idx is not None and best > tol:
            keep[idx] = True
            stack += [(i, idx), (idx, j)]
    return [p for p, k in zip(pts, keep) if k]


def length(pts):
    return sum(hav(a, b) for a, b in zip(pts, pts[1:]))


def cells(a, b, pad=0):
    i0, i1 = sorted((int(math.floor(a[0] / CELL)), int(math.floor(b[0] / CELL))))
    j0, j1 = sorted((int(math.floor(a[1] / CELL)), int(math.floor(b[1] / CELL))))
    return [(i, j) for i in range(i0 - pad, i1 + pad + 1) for j in range(j0 - pad, j1 + pad + 1)]


# --- Loading the RFN -------------------------------------------------------------

def find_geometries(obj, out):
    """Collects the (Multi)LineStrings of the operated lines, whatever the JSON format."""
    if isinstance(obj, dict):
        status = obj.get("mnemo") or (obj.get("properties") or {}).get("mnemo")
        if status and status != "EXPLOITE":
            return
        t = obj.get("type")
        if t == "LineString" and "coordinates" in obj:
            out.append(obj["coordinates"])
            return
        if t == "MultiLineString" and "coordinates" in obj:
            out.extend(obj["coordinates"])
            return
        for v in obj.values():
            find_geometries(v, out)
    elif isinstance(obj, list):
        for v in obj:
            find_geometries(v, out)
    elif isinstance(obj, str) and obj.startswith("{") and "coordinates" in obj:
        try:
            find_geometries(json.loads(obj), out)
        except ValueError:
            pass


def parse_rfn(raw):
    text = raw.decode("utf-8-sig", errors="replace")
    lines = []
    try:
        find_geometries(json.loads(text), lines)
    except ValueError:
        sample = text[:4096]
        delim = ";" if sample.count(";") > sample.count(",") else ","
        for row in csv.reader(io.StringIO(text), delimiter=delim):
            for cell in row:
                if cell.startswith("{") and "coordinates" in cell:
                    find_geometries(cell, lines)
    return [[(c[1], c[0]) for c in line] for line in lines if len(line) >= 2]


def load_rfn(path_arg, cache_dir):
    if path_arg:
        with open(path_arg, "rb") as f:
            return parse_rfn(f.read())
    os.makedirs(cache_dir, exist_ok=True)
    for i, url in enumerate(RFN_URLS):
        cache = os.path.join(cache_dir, "rfn_%d.dat" % i)
        if not os.path.exists(cache):
            print("→ Téléchargement %s" % url)
            r = subprocess.run(["curl", "-fsSL", "-o", cache, url])
            if r.returncode != 0:
                print("  échec (curl %d)" % r.returncode)
                if os.path.exists(cache):
                    os.remove(cache)
                continue
        with open(cache, "rb") as f:
            lines = parse_rfn(f.read())
        if lines:
            print("✓ %d tronçons de lignes RFN exploitées (%s)" % (len(lines), os.path.basename(cache)))
            return lines
        print("  aucun tracé exploitable dans %s" % cache)
    sys.exit("Impossible d'obtenir le fichier RFN. Télécharge-le à la main et passe --rfn <fichier>.")


# --- Rail network graph ----------------------------------------------------------

class Graph:
    def __init__(self, polylines, bbox):
        lat0, lat1, lon0, lon1 = bbox
        self.pos = []                       # node → (lat, lon)
        self.adj = defaultdict(list)        # node → [(neighbor, length)]
        self.edges = []                     # (u, v)
        self.edge_line = []                 # edge → index of the RFN line
        self.grid = defaultdict(list)       # cell → [edge index]
        self.splits = {}                    # edge → [(t, node)] intermediate points, sorted
        ends = []
        for li, pl in enumerate(polylines):
            if not any(lat0 <= p[0] <= lat1 and lon0 <= p[1] <= lon1 for p in pl):
                continue
            ids = []
            for p in pl:
                ids.append(len(self.pos))
                self.pos.append(p)
            for u, v in zip(ids, ids[1:]):
                self._add_edge(u, v, li)
            ends += [ids[0], ids[-1]]
        # Junctions: end of a line lying on another line
        n_edges = len(self.edges)
        for e in ends:
            for ei, t, d in self._near_edges(self.pos[e], JUNCTION_DIST, limit=n_edges):
                u, v = self.edges[ei]
                if e in (u, v):
                    continue
                w = self._split(ei, t)
                if w != e:
                    self._link(e, w, d)

    def _add_edge(self, u, v, line):
        w = hav(self.pos[u], self.pos[v])
        self.adj[u].append((v, w))
        self.adj[v].append((u, w))
        ei = len(self.edges)
        self.edges.append((u, v))
        self.edge_line.append(line)
        for c in cells(self.pos[u], self.pos[v]):
            self.grid[c].append(ei)
        return ei

    def _link(self, u, v, w):
        self.adj[u].append((v, w))
        self.adj[v].append((u, w))

    def _split(self, ei, t):
        """Node at point t of edge ei (reuses an end if t ≈ 0 or 1).

        Points of the same edge are chained in order (u … w … v) so that a
        route between two stations lying on the same edge does not go back and forth."""
        u, v = self.edges[ei]
        if t <= 0.001:
            return u
        if t >= 0.999:
            return v
        chain = self.splits.setdefault(ei, [(0.0, u), (1.0, v)])
        for k in range(len(chain) - 1):
            t0, n0 = chain[k]
            t1, n1 = chain[k + 1]
            if abs(t - t0) < 1e-6:
                return n0
            if t0 < t < t1:
                w = len(self.pos)
                self.pos.append(lerp(self.pos[u], self.pos[v], t))
                self._link(n0, w, hav(self.pos[n0], self.pos[w]))
                self._link(w, n1, hav(self.pos[w], self.pos[n1]))
                chain.insert(k + 1, (t, w))
                return w
        return v

    def _near_edges(self, p, radius, limit=None):
        r = int(math.ceil(radius / 1000.0 / (CELL * 70))) + 1
        ci, cj = int(math.floor(p[0] / CELL)), int(math.floor(p[1] / CELL))
        seen, out = set(), []
        for i in range(ci - r, ci + r + 1):
            for j in range(cj - r, cj + r + 1):
                for ei in self.grid.get((i, j), ()):
                    if ei in seen or (limit is not None and ei >= limit):
                        continue
                    seen.add(ei)
                    u, v = self.edges[ei]
                    t, d = project_on_segment(p, self.pos[u], self.pos[v])
                    if d <= radius:
                        out.append((ei, t, d))
        out.sort(key=lambda x: x[2])
        return out

    def snap(self, p, max_candidates=4):
        """Candidate nodes for a station: projection on each nearby RFN line
        (several parallel lines often run a few meters from each other).
        Returns [(node, distance)], nearest first."""
        out, lines = [], set()
        for ei, t, d in self._near_edges(p, SNAP_DIST):
            line = self.edge_line[ei]
            if line in lines:
                continue
            lines.add(line)
            out.append((self._split(ei, t), d))
            if len(out) >= max_candidates:
                break
        return out

    def shortest(self, a, b, max_len):
        """A* between nodes a and b; returns the list of positions or None."""
        goal = self.pos[b]
        dist = {a: 0.0}
        prev = {}
        heap = [(hav(self.pos[a], goal), 0.0, a)]
        while heap:
            f, g, u = heapq.heappop(heap)
            if u == b:
                path = [u]
                while u in prev:
                    u = prev[u]
                    path.append(u)
                return [self.pos[n] for n in reversed(path)]
            if g > dist.get(u, math.inf) or f > max_len:
                continue
            for v, w in self.adj[u]:
                nd = g + w
                if nd < dist.get(v, math.inf):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(heap, (nd + hav(self.pos[v], goal), nd, v))
        return None


def best_route(graph, cands_a, cands_b, direct):
    """Shortest route between two stations, among their candidate tracks."""
    best, best_len = None, math.inf
    combos = sorted(((da + db, na, nb) for na, da in cands_a for nb, db in cands_b), key=lambda x: x[0])
    for snap_cost, na, nb in combos:
        if na == nb:
            continue
        limit = min(direct * MAX_RATIO + 2000, best_len)
        path = graph.shortest(na, nb, limit)
        if path:
            L = length(path) + 2 * snap_cost
            if L < best_len:
                best, best_len = path, L
            if L <= direct * 1.15 + 300:
                break   # good enough: no need to try the other tracks
    return best


# --- Main program ----------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--lines", default="NextStation/Resources/lines.json")
    ap.add_argument("--rfn", help="fichier RFN déjà téléchargé (JSON, GeoJSON ou CSV)")
    ap.add_argument("--cache", default="build/cache")
    args = ap.parse_args()

    with open(args.lines, encoding="utf-8") as f:
        catalog = json.load(f)

    # Start again from the original stations (idempotence)
    for line in catalog["lines"]:
        for br in line["branches"]:
            st = []
            for s in br["stations"]:
                if s.get("wp") or s.get("ins"):
                    continue
                s = dict(s)
                s["lat"], s["lon"] = s.pop("olat", s["lat"]), s.pop("olon", s["lon"])
                st.append(s)
            br["stations"] = st

    all_st = {}
    for line in catalog["lines"]:
        for br in line["branches"]:
            for s in br["stations"]:
                all_st.setdefault(s["id"], s)
    lats = [s["lat"] for s in all_st.values()]
    lons = [s["lon"] for s in all_st.values()]
    bbox = (min(lats) - 0.1, max(lats) + 0.1, min(lons) - 0.15, max(lons) + 0.15)

    print("→ Construction du graphe ferroviaire…")
    graph = Graph(load_rfn(args.rfn, args.cache), bbox)
    print("✓ %d nœuds, %d arêtes" % (len(graph.pos), len(graph.edges)))

    nodes = {sid: graph.snap((s["lat"], s["lon"])) for sid, s in all_st.items()}
    no_track = sorted(all_st[sid]["name"] for sid, n in nodes.items() if not n)
    if no_track:
        print("⚠ %d gares à plus de %d m d'une voie RFN (souterrain, réseau RATP…) : %s" % (
            len(no_track), SNAP_DIST, ", ".join(no_track[:15]) + (" …" if len(no_track) > 15 else "")))

    # Stations indexed for step 3
    st_grid = defaultdict(list)
    for sid, s in all_st.items():
        st_grid[(int(math.floor(s["lat"] / CELL)), int(math.floor(s["lon"] / CELL)))].append(sid)

    routes = {}
    fallbacks = []
    for line in catalog["lines"]:
        served = {s["id"] for br in line["branches"] for s in br["stations"] if s.get("stop", True)}
        for br in line["branches"]:
            real = br["stations"]
            for s in real:
                s["olat"], s["olon"] = s["lat"], s["lon"]
            orig = [(s["lat"], s["lon"]) for s in real]

            # 1-2. Route between consecutive stations
            paths = []
            for k in range(len(real) - 1):
                key = (real[k]["id"], real[k + 1]["id"])
                if key not in routes:
                    rev = routes.get((key[1], key[0]))
                    if rev is not None or (key[1], key[0]) in routes:
                        routes[key] = rev[::-1] if rev else None
                    else:
                        routes[key] = best_route(graph, nodes.get(key[0]) or [], nodes.get(key[1]) or [],
                                                 hav(orig[k], orig[k + 1]))
                if routes[key] is None:
                    fallbacks.append("%s : %s → %s" % (br["id"], real[k]["name"], real[k + 1]["name"]))
                paths.append(routes[key])

            # Station snapped to the track
            snap = []
            for k in range(len(real)):
                inc = paths[k - 1] if k > 0 else None
                out = paths[k] if k < len(paths) else None
                snap.append(inc[-1] if inc else out[0] if out else orig[k])

            poly = [snap[0]]          # full track geometry (for step 3)
            seq = [("st", 0)]
            for k, path in enumerate(paths):
                a, b = snap[k], snap[k + 1]
                inner = list(path[1:-1]) if path else []
                inner = simplify([a] + inner + [b], SIMPLIFY_TOL)[1:-1]
                for p in inner:
                    poly.append(p)
                    seq.append(("wp", p))
                poly.append(b)
                seq.append(("st", k + 1))

            # 3. Stations on the track but missing from the branch
            own = {s["id"] for s in real}
            cum = [0.0]
            for p, q in zip(poly, poly[1:]):
                cum.append(cum[-1] + hav(p, q))
            extra = {}
            for i in range(len(poly) - 1):
                for c in cells(poly[i], poly[i + 1], pad=1):
                    for sid in st_grid.get(c, ()):
                        if sid in own:
                            continue
                        s = all_st[sid]
                        t, d = project_on_segment((s["lat"], s["lon"]), poly[i], poly[i + 1])
                        if d <= INSERT_DIST and (sid not in extra or d < extra[sid][1]):
                            along = cum[i] + t * (cum[i + 1] - cum[i])
                            if 1.0 < along < cum[-1] - 1.0:
                                extra[sid] = (along, d, lerp(poly[i], poly[i + 1], t))

            # Assembly in track order
            items = []
            pos = 0.0
            pi = 0
            for kind, val in seq:
                pos = cum[pi]
                if kind == "st":
                    s = real[val]
                    s["lat"], s["lon"] = round(snap[val][0], 6), round(snap[val][1], 6)
                    items.append((pos, 0, s))
                else:
                    items.append((pos, 1, {"id": "wp", "name": "", "lat": round(val[0], 6),
                                           "lon": round(val[1], 6), "stop": False, "wp": True}))
                pi += 1
            for sid, (along, _, p) in extra.items():
                s = all_st[sid]
                ins = {"id": sid, "name": s["name"], "lat": round(p[0], 6),
                       "lon": round(p[1], 6), "stop": sid in served, "ins": True,
                       "olat": s.get("olat", s["lat"]), "olon": s.get("olon", s["lon"])}
                if s.get("idfm"):
                    ins["idfm"] = s["idfm"]   # IDFM stop zones (PRIM real time)
                if s.get("uic"):
                    ins["uic"] = s["uic"]   # secondary UIC codes (SNCF real time)
                if s.get("exits"):
                    ins["exits"] = s["exits"]   # station entrances (map)
                items.append((along, 0, ins))
            items.sort(key=lambda x: (x[0], x[1]))
            br["stations"] = [x[2] for x in items]

        # 4. Redundant branches (stopping pattern included in another branch of the line)
        def stops_of(b):
            return [s["id"] for s in b["stations"] if s.get("stop") and not s.get("wp")]

        def is_subseq(p, q):
            it = iter(q)
            return all(x in it for x in p)

        kept = []
        for b in sorted(line["branches"], key=lambda b: -len(stops_of(b))):
            p = stops_of(b)
            if not any(is_subseq(p, stops_of(q)) or is_subseq(p[::-1], stops_of(q)) for q in kept):
                kept.append(b)
        line["branches"] = kept

    for line in catalog["lines"]:
        for br in line["branches"]:
            st = br["stations"]
            total = length([(s["lat"], s["lon"]) for s in st])
            n_stop = sum(1 for s in st if s.get("stop") and not s.get("wp"))
            n_pass = sum(1 for s in st if not s.get("stop") and not s.get("wp"))
            print("  %-22s %6.1f km  %3d arrêts  %3d gares traversées  %4d points de tracé" % (
                br["id"][:22], total / 1000, n_stop, n_pass, sum(1 for s in st if s.get("wp"))))

    catalog["source"] = ((catalog.get("source") or "").split(" · Tracé")[0] +
                         " · Tracé des voies : SNCF Réseau, formes des lignes du RFN (ODbL)").strip(" ·")
    with open(args.lines, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")

    if fallbacks:
        uniq = sorted(set(fallbacks))
        report = os.path.join(args.cache, "..", "shapes-lignes-droites.txt")
        with open(report, "w", encoding="utf-8") as f:
            f.write("\n".join(uniq) + "\n")
        print("\n⚠ %d tronçons restés en ligne droite (aucun itinéraire RFN trouvé) :" % len(uniq))
        for x in uniq[:40]:
            print("  - " + x)
        if len(uniq) > 40:
            print("  … (%d autres, liste complète : build/shapes-lignes-droites.txt)" % (len(uniq) - 40))
    print("\n✓ %s mis à jour (%d Ko)" % (args.lines, os.path.getsize(args.lines) // 1024))


if __name__ == "__main__":
    main()
