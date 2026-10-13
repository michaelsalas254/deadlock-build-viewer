"""Checks the published site after a deploy and fails when the automation has broken.

Everything here updates itself, so nobody is watching it. This is the alarm: it reads what the
live site is actually serving and exits with an error (which GitHub emails to the repository's
owner) when something needs a person, for example the precomputed data has stopped being rebuilt,
or a game update renamed a stat and one kind of counter item came up empty.

Usage (run by the deploy workflow):
    python tools/check_site.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from topbuilds import fetch_json  # noqa: E402
from update_due import site_url  # noqa: E402

MAX_AGE_HOURS = 24
MIN_HEROES = 30
MIN_ITEMS = 150


def problems(snap, now):
    """What is wrong with the published data, as sentences."""
    found = []

    def hours(t):
        return (now - (t or 0)) / 3600

    if hours(snap.get("built")) > MAX_AGE_HOURS:
        found.append(f"the instant-load data was last rebuilt {hours(snap.get('built')):.0f} hours ago")
    heroes = snap.get("heroes") or []
    if len(heroes) < MIN_HEROES:
        found.append(f"only {len(heroes)} heroes are listed")
    if len(snap.get("items") or []) < MIN_ITEMS:
        found.append(f"only {len(snap.get('items') or [])} items are listed")
    tiers = (snap.get("tiers") or {}).get("all")
    if not tiers or len(tiers.get("stats") or []) < MIN_HEROES:
        found.append("the tier list is missing or covers too few heroes")
    tops = snap.get("topBuilds") or {}
    if len(tops.get("heroes") or {}) < MIN_HEROES:
        found.append(f"only {len(tops.get('heroes') or {})} heroes have a meta build")
    elif hours(tops.get("updated")) > MAX_AGE_HOURS:
        found.append(f"the meta builds were last rebuilt {hours(tops.get('updated')):.0f} hours ago")
    counters = snap.get("counters") or {}
    empty = sorted(kind for kind, items in (counters.get("types") or {}).items() if not items)
    if not counters.get("types"):
        found.append("the counter page has no data")
    elif empty:
        found.append(f"the counter page found no items for: {', '.join(empty)} (a game update may have renamed a stat)")
    if len(counters.get("traits") or {}) < len(heroes):
        found.append("the counter page is missing some heroes")
    return found


def main():
    try:
        snap = fetch_json(f"{site_url()}data/snapshot.json?t={int(time.time())}", tries=3)
    except Exception as err:
        print(f"PROBLEM: the site's instant-load data couldn't be read ({err}).")
        return 1
    found = problems(snap, time.time())
    try:
        live = fetch_json("/assets/steam-info", tries=2).get("client_version")
        art = int((snap.get("manifest") or {}).get("game_version") or 0)
        if live and art and live > art:
            print(f"Note: the game is on version {live} but the site's art is from version {art}; images come from deadlock-api.com until the next rebuild.")
    except Exception:
        pass
    for line in found:
        print(f"PROBLEM: {line}.")
    if not found:
        print(f"Site is healthy: {len(snap['heroes'])} heroes, {len(snap['items'])} items, "
              f"data rebuilt {(time.time() - snap['built']) / 60:.0f} minutes ago for game version {(snap.get('steam') or {}).get('client_version')}.")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
