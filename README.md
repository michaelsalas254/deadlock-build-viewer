# Deadlock Build Viewer

A fan site for Deadlock: view any build by its ID in an in-game–style shop layout, meta builds learned
from what top players actually buy, a tier list measured from real matches, duel matchups, a
recommended hero roster, and counter picks (choose the enemy team, see what to buy against it).

Fan-made and not affiliated with Valve. Hero, item and build data come from
[deadlock-api.com](https://deadlock-api.com).

## How it stays current

Nothing needs running by hand. A GitHub Action (`.github/workflows/deploy.yml`) is triggered every
15 minutes and rebuilds the site when the published data is about half an hour old, or straight away
when the game has been updated (`tools/update_due.py`). GitHub runs scheduled workflows when it has
capacity, so in practice a rebuild lands every few hours. Each rebuild:

- refreshes a sample of recent matches for the tier list's strength model (`tools/matches.py`)
- rebuilds each hero's meta build from Ascendant+ players' purchases (`tools/topbuilds.py`)
- re-reads community balance talk every 6 hours (`tools/community.py`)
- mirrors hero, item and ability art that changed in a game update (`tools/mirror_art.py`)
- precomputes what the first screens show, so visits draw instantly (`tools/snapshot.mjs`)
- publishes the site to GitHub Pages, then checks what it published (`tools/check_site.py`) and
  fails the run, which GitHub emails to the repository owner, if something needs a person

Stats are measured since the latest balance patch, which the tools find from the game's update posts.
They also read each patch's notes: heroes the notes name, and heroes whose top players rely on an
item the notes change, move in the tier list as soon as games on the new patch come in, and their
meta builds switch to post-patch games early. If the notes ever stop being readable, the health
check says so.
Between rebuilds the page checks the live game version itself on every visit and, if the game has
moved on, reads item numbers and art from the live game data instead of the precomputed copy.

The workflow also re-enables its own schedule on each scheduled run, because GitHub switches off
scheduled workflows in repositories that go 60 days without a commit.

## Running it locally

```
python tools/serve.py
```

Then open http://localhost:8765. Locally, `serve.py` also extracts the game's art and fonts from your
Deadlock install (`tools/extract_assets.py`) and refreshes them after each patch. The public site
doesn't depend on that: it mirrors changed art by itself. Committing a refreshed `assets/game` folder
is optional (it also updates the shop's UI art and hero backdrops; fonts are never committed).
