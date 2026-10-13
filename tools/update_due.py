"""Decides whether a scheduled run of the deploy workflow has anything to do.

GitHub drops many scheduled runs, so the workflow is triggered more often than the site needs
rebuilding, and this skips the runs with nothing to do. A rebuild is due when the published data
is missing or older than REFRESH_MINUTES, or when the game has changed since it was built (a new
game version, or a new update post). Pushes and manual runs always rebuild.

Prints `needed=true` or `needed=false` for the workflow, and the reason for the log.

Usage (run by the deploy workflow):
    python tools/update_due.py >> "$GITHUB_OUTPUT"
"""
import calendar
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from topbuilds import API_ROOT, fetch_json  # noqa: E402

REFRESH_MINUTES = 25


def site_url():
    """The repository's GitHub Pages address."""
    owner, name = os.environ["GITHUB_REPOSITORY"].split("/")
    return f"https://{owner.lower()}.github.io/{name}/"


def why_due():
    """Why a rebuild is needed, or None when the published site is current."""
    if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        return "this is not a scheduled run"
    try:
        snap = fetch_json(f"{site_url()}data/snapshot.json?t={int(time.time())}", tries=2)
    except Exception as err:
        return f"the published data couldn't be read ({err})"
    built = snap.get("built") or 0
    age = (time.time() - built) / 60
    if age >= REFRESH_MINUTES:
        return f"the published data is {age:.0f} minutes old"
    try:
        live = fetch_json("/assets/steam-info", tries=2).get("client_version")
        if live and live != (snap.get("steam") or {}).get("client_version"):
            return f"the game is now on version {live}"
        posts = [calendar.timegm(time.strptime(p["pub_date"][:19], "%Y-%m-%dT%H:%M:%S"))
                 for p in fetch_json(f"{API_ROOT}/v2/patches", tries=2) if p.get("pub_date")]
        if posts and max(posts) > built:
            return "there is a new game update post"
    except Exception as err:
        print(f"Couldn't check for a game update ({err}); assuming there is none.", file=sys.stderr)
    return None


if __name__ == "__main__":
    reason = why_due()
    print(f"Rebuild: {reason}." if reason else f"Nothing to do: the published data is under {REFRESH_MINUTES} minutes old and the game hasn't changed.", file=sys.stderr)
    print(f"needed={'true' if reason else 'false'}")
