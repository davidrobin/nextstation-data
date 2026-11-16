#!/usr/bin/env python3
"""Historique des longueurs de train (PRIM, « Prochains passages – requête globale »).

À chaque passage (toutes les heures en journée), lit les circulations des ~3 prochaines heures
et note, par numéro de train et par jour de circulation, la longueur annoncée
(VehicleFeatureRef : longTrain → L, shortTrain → C). Garde 42 jours.

  PRIM_API_KEY=… python3 tools/collect_lengths.py history/lengths.json

Format :
  {"updatedAt": "…", "trains": {"862404": {"line": "STIF:Line::C01744:", "days": {"2026-11-17": "L"}}}}
"""
import datetime as dt
import gzip
import json
import os
import sys
import urllib.request
from zoneinfo import ZoneInfo

URL = "https://prim.iledefrance-mobilites.fr/marketplace/estimated-timetable"
PARIS = ZoneInfo("Europe/Paris")
KEEP_DAYS = 42
CODES = {"longTrain": "L", "shortTrain": "C"}


def text(x):
    if isinstance(x, list):
        x = x[0] if x else None
    if isinstance(x, dict):
        x = x.get("value")
    return x if isinstance(x, str) else None


def journeys(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "EstimatedVehicleJourney":
                yield from (v if isinstance(v, list) else [v])
            else:
                yield from journeys(v)
    elif isinstance(node, list):
        for v in node:
            yield from journeys(v)


def first_time(j):
    calls = (j.get("EstimatedCalls") or {}).get("EstimatedCall") or []
    for c in calls:
        for k in ("AimedDepartureTime", "AimedArrivalTime"):
            s = text(c.get(k))
            if s:
                try:
                    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(PARIS)
                except ValueError:
                    pass
    return None


def main():
    path = sys.argv[1]
    key = os.environ["PRIM_API_KEY"]
    req = urllib.request.Request(URL, headers={"apikey": key, "Accept": "application/json",
                                               "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=300) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    root = json.loads(raw)

    hist = {"trains": {}}
    if os.path.exists(path):
        with open(path) as f:
            hist = json.load(f)
    trains = hist.setdefault("trains", {})

    seen = new = 0
    for j in journeys(root):
        code = CODES.get(text(j.get("VehicleFeatureRef")) or "")
        number = text((j.get("TrainNumbers") or {}).get("TrainNumberRef")) or text(j.get("VehicleJourneyName")) or ""
        when = first_time(j)
        if not code or not number.isdigit() or not when:
            continue
        seen += 1
        # Jour de circulation : un train parti avant 3 h appartient à la veille
        day = (when - dt.timedelta(hours=3)).date().isoformat()
        t = trains.setdefault(number, {"days": {}})
        t["line"] = text(j.get("LineRef")) or t.get("line", "")
        if t["days"].get(day) != code:
            new += t["days"].get(day) is None
            t["days"][day] = code   # la dernière annonce l'emporte (changement de dernière minute)

    limit = (dt.datetime.now(PARIS).date() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    for number in list(trains):
        days = {d: v for d, v in trains[number]["days"].items() if d >= limit}
        if days:
            trains[number]["days"] = dict(sorted(days.items()))
        else:
            del trains[number]
    hist["updatedAt"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(hist, f, separators=(",", ":"), sort_keys=True)
    print("%d trains avec longueur dans la réponse, %d nouvelles observations, %d trains suivis"
          % (seen, new, len(trains)))


if __name__ == "__main__":
    main()
