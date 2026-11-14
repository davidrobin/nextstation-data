#!/usr/bin/env python3
"""Écrit site/manifest.json : version, période couverte et empreinte de chaque fichier publié.

L'app lit ce manifeste au lancement (au plus une fois par jour) et ne télécharge
que les fichiers dont l'empreinte a changé."""
import datetime
import hashlib
import json
import os
import sys

site = sys.argv[1] if len(sys.argv) > 1 else "site"
files = {}
for name in ("lines.json", "timetables.json"):
    path = os.path.join(site, name)
    with open(path, "rb") as f:
        data = f.read()
    files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
with open(os.path.join(site, "timetables.json"), encoding="utf-8") as f:
    tt = json.load(f)
manifest = {
    "version": 1,
    "generatedAt": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "timetables": {"from": tt["from"], "to": tt["to"],
                   "trips": sum(len(l["trips"]) for l in tt["lines"].values())},
    "files": files,
}
with open(os.path.join(site, "manifest.json"), "w", encoding="utf-8") as f:
    json.dump(manifest, f, ensure_ascii=False, indent=1)
print(json.dumps(manifest, ensure_ascii=False, indent=1))
