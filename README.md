# Deadlock Build Viewer

A fan site for Deadlock: view any build by its ID in an in-game–style shop layout, meta builds learned
from what top players actually buy, a tier list measured from real matches, duel matchups, and a
recommended hero roster.

Fan-made and not affiliated with Valve. Hero, item and build data come from
[deadlock-api.com](https://deadlock-api.com).

## How it stays current

A GitHub Action (`.github/workflows/deploy.yml`) runs every 30 minutes and:

- refreshes a sample of recent matches for the tier list's strength model (`tools/matches.py`)
- rebuilds each hero's meta build from Ascendant+ players' purchases (`tools/topbuilds.py`)
- re-reads community balance talk every 6 hours (`tools/community.py`)
- publishes the site to GitHub Pages

## Running it locally

```
python tools/serve.py
```

Then open http://localhost:8765. Locally, `serve.py` also extracts the game's art and fonts from your
Deadlock install (`tools/extract_assets.py`) and refreshes them after each patch. To update the art on
the public site after a patch, commit the refreshed `assets/game` folder (fonts are never committed).
