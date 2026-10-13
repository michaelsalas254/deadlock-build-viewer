"""Keep the site's hero, item and ability art current without the game installed.

The art in assets/game is extracted from a local Deadlock install (extract_assets.py), which only
happens on a computer that has the game. The public site updates itself instead: whenever the game
version moves past the committed art, this mirrors the same images from deadlock-api.com (which
republishes them with each game update) into data/art, and the deploy workflow lays that over the
committed art. Art the extraction never had (a hero or item added since) is mirrored the same way.

Only changed files are downloaded: each image's ETag is remembered and sent back, so an update that
touched three icons fetches three icons.

Usage (normally run by the deploy workflow):
    python tools/mirror_art.py
"""
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from topbuilds import USER_AGENT, fetch_json  # noqa: E402

ART_URL = re.compile(r"/assets-api-res/((?:images|icons)/.+)\.(webp|svg|png)$")
# The kinds of art the site draws; the service also has minimap icons, alternate cards and so on.
WANTED = re.compile(r"^(images/(abilities|items|upgrades)/|images/heroes/[^/]+_(card|sm|vertical)$|icons/heroes/hero_names/)")
# Full-size hero backdrops are about 1.6 MB each; the committed ones are scaled down and stay as they are
# (a new hero's backdrop comes straight from deadlock-api.com until the art is next extracted).
SKIP = re.compile(r"^images/heroes/backgrounds/")
FORMATS = ("webp", "svg", "png")   # first available wins
MAX_FAILED_SHARE = 0.05            # more failures than this and the refresh doesn't count as done
WORKERS = 8


def collect(value, found):
    """Every art URL in the API's hero and item data, grouped as key -> {format: url}."""
    if isinstance(value, str):
        m = ART_URL.search(value)
        if m:
            found.setdefault(m[1], {})[m[2]] = value
    elif isinstance(value, list):
        for v in value:
            collect(v, found)
    elif isinstance(value, dict):
        for v in value.values():
            collect(v, found)
    return found


def download(url, dest, etag):
    """Fetches url to dest unless it still matches etag. Returns (new etag, changed) or raises."""
    headers = {"User-Agent": USER_AGENT}
    if etag and os.path.exists(dest):
        headers["If-None-Match"] = etag
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as res:
                body = res.read()
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest + ".tmp", "wb") as f:
                    f.write(body)
                os.replace(dest + ".tmp", dest)
                return res.headers.get("ETag"), True
        except urllib.error.HTTPError as err:
            if err.code == 304:
                return etag, False
            if err.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(5 * (attempt + 1))


def refresh(root, log=print):
    with open(os.path.join(root, "assets", "game", "manifest.json"), encoding="utf-8") as f:
        base = json.load(f)
    art_dir = os.path.join(root, "data", "art")
    state_path = os.path.join(art_dir, "state.json")
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}

    live = int(fetch_json("/assets/steam-info")["client_version"])
    base_version = int(base.get("game_version") or 0)
    # Mirrored files older than the committed art (it was re-extracted since) would undo it.
    if state.get("version") and int(state["version"]) < base_version:
        log(f"Art: the committed art (version {base_version}) is newer than the mirror; starting the mirror over.")
        shutil.rmtree(os.path.join(art_dir, "game"), ignore_errors=True)
        state = {}
    files, etags = state.get("files", {}), state.get("etags", {})

    found = collect(fetch_json("/assets/items"), collect(fetch_json("/assets/heroes?only_active=true"), {}))
    wanted = {key: formats for key, formats in found.items() if not SKIP.search(key) and (key in base["images"] or WANTED.search(key))}
    if live > base_version and state.get("version") != live:
        targets = list(wanted)                       # the game changed: re-check everything
    elif live > base_version:
        targets = [k for k in wanted if k not in files]   # same version: only what failed or is new
    else:
        targets = [k for k in wanted if k not in base["images"] and k not in files]

    def fetch(key):
        ext = next(e for e in FORMATS if e in wanted[key])
        rel = f"{key}.{ext}"
        try:
            etag, changed = download(wanted[key][ext], os.path.join(art_dir, "game", *rel.split("/")), etags.get(key) if files.get(key) == rel else None)
            return key, rel, etag, changed, None
        except Exception as err:  # one bad image must not stop the rest
            return key, rel, None, False, err

    changed = failed = 0
    with ThreadPoolExecutor(WORKERS) as pool:
        for key, rel, etag, was_changed, err in pool.map(fetch, targets):
            if err:
                failed += 1
                if failed <= 5:
                    log(f"Art: couldn't fetch {key} ({err}).")
                continue
            files[key], etags[key] = rel, etag
            changed += was_changed

    # A refresh with too many gaps isn't recorded, so the next run re-checks everything and the
    # manifest keeps the old version (the page then uses deadlock-api.com's copies meanwhile).
    complete = failed <= MAX_FAILED_SHARE * max(1, len(targets))
    version = live if complete else state.get("version")
    os.makedirs(art_dir, exist_ok=True)
    with open(state_path + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"version": version, "files": files, "etags": etags}, f)
    os.replace(state_path + ".tmp", state_path)

    manifest = dict(base)
    manifest["images"] = {**base["images"], **{key: f"assets/game/{rel}" for key, rel in files.items()}}
    if files:
        manifest["mirrored_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if version and int(version) > base_version:
        manifest["game_version"] = str(version)
    with open(os.path.join(art_dir, "manifest.json.tmp"), "w", encoding="utf-8") as f:
        json.dump(manifest, f)
    os.replace(os.path.join(art_dir, "manifest.json.tmp"), os.path.join(art_dir, "manifest.json"))
    log(f"Art: game version {live}, committed art {base_version}; checked {len(targets)} images, {changed} downloaded, {failed} failed; "
        f"{len(files)} mirrored in all, manifest at version {manifest['game_version']}.")
    return complete


if __name__ == "__main__":
    sys.exit(0 if refresh(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) else 1)
