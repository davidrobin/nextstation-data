# nextstation-data

Données de l'app iOS **Prochaine gare** (suivi du train par GPS, distance jusqu'à la prochaine gare, horaires).

Chaque nuit, une tâche GitHub Actions télécharge les horaires officiels et publie sur GitHub Pages :

| Fichier | Contenu |
|---|---|
| `manifest.json` | date de génération, période couverte par les horaires, empreinte SHA-256 de chaque fichier |
| `lines.json` | lignes (RER, Transilien, TER vers Paris), branches, gares, desserte, tracé des voies, temps de parcours théoriques, identifiants IDFM |
| `timetables.json` | circulations des 60 prochains jours : numéro de train, jours de circulation, heures à chaque gare |

L'app lit `manifest.json` au lancement (au plus une fois par jour) et ne télécharge que les fichiers modifiés.

## Sources (licences ouvertes)

- Horaires : SNCF Voyageurs, « Réseau SNCF TGV, Intercités et TER » (GTFS) ; Île-de-France Mobilités, offre horaires (GTFS)
- Tracé des voies : SNCF Réseau, formes des lignes du RFN (ODbL)

## Générer à la main

```sh
python3 tools/build_gtfs.py --out site/lines.json --cache build/cache
python3 tools/build_shapes.py --lines site/lines.json --cache build/cache
python3 tools/make_manifest.py site
```

Python 3.9+, bibliothèque standard uniquement (téléchargements via `curl`).
