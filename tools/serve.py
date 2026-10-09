"""Serve the build viewer, refreshing the extracted game art whenever Deadlock has been patched.

Usage:
    python tools/serve.py [--port 8765] [--game "<Deadlock>/game/citadel"]

On startup, and every 30 minutes while running, this compares the installed game's version
(steam.inf) with the version the art in assets/game was extracted from, and re-runs
extract_assets.py when they differ. Until the refresh finishes the page uses online images,
so it never shows outdated art. Every 30 minutes it also updates the tier list's match sample
(tools/matches.py) and the meta builds learned from top players' purchases (tools/topbuilds.py),
and every 6 hours it re-reads community balance talk (tools/community.py).
"""
import argparse
import functools
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

TOOLS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS)
ASSETS = os.path.join(ROOT, "assets", "game")
COMMUNITY = os.path.join(ROOT, "data", "community.json")
CHECK_EVERY_SECONDS = 30 * 60
COMMUNITY_EVERY_SECONDS = 6 * 3600

MATCHES = os.path.join(ROOT, "data", "matches.json")

sys.path.insert(0, TOOLS)
import community  # noqa: E402
import matches  # noqa: E402
import topbuilds  # noqa: E402
from vpk import DEFAULT_GAME_DIR  # noqa: E402


def installed_version(game_dir):
    try:
        with open(os.path.join(game_dir, "steam.inf"), encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("ClientVersion="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def extracted_version():
    try:
        with open(os.path.join(ASSETS, "manifest.json"), encoding="utf-8") as f:
            return str(json.load(f).get("game_version"))
    except (OSError, ValueError):
        return None


def python_with_pillow():
    """The extractor needs Pillow; use whichever installed Python has it."""
    candidates = [[sys.executable]]
    if os.name == "nt":
        candidates += [["py", "-3"], ["py"]]
    candidates += [["python3"], ["python"]]
    for cmd in candidates:
        try:
            if subprocess.run([*cmd, "-c", "import PIL"], capture_output=True, timeout=60).returncode == 0:
                return cmd
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def refresh_assets(game_dir):
    python = python_with_pillow()
    if not python:
        print("Game art is out of date, but no Python with Pillow was found. Install it with: python -m pip install pillow")
        return False
    # Extract from a temporary copy of the tools: some Python installs (like the Microsoft Store
    # one) can't reliably read or write files under AppData, but always can in the temp folder.
    work = tempfile.mkdtemp(prefix="deadlock-art-")
    try:
        for name in ("vpk.py", "source2.py", "extract_assets.py"):
            shutil.copy2(os.path.join(TOOLS, name), work)
        out = os.path.join(work, "out", "assets", "game")
        result = subprocess.run([*python, os.path.join(work, "extract_assets.py"), "--game", game_dir, "--out", out], cwd=work)
        if result.returncode != 0 or not os.path.exists(os.path.join(out, "manifest.json")):
            print("Refreshing game art failed; the viewer keeps using online images.")
            return False
        staged, old = ASSETS + ".new", ASSETS + ".old"
        shutil.rmtree(staged, ignore_errors=True)
        shutil.copytree(out, staged)
        shutil.rmtree(old, ignore_errors=True)
        try:
            if os.path.exists(ASSETS):
                os.replace(ASSETS, old)
            os.replace(staged, ASSETS)
        except OSError:
            # A file may be open by the server; fall back to copying over the old folder.
            shutil.copytree(staged, ASSETS, dirs_exist_ok=True)
            shutil.rmtree(staged, ignore_errors=True)
        shutil.rmtree(old, ignore_errors=True)
        return True
    finally:
        shutil.rmtree(work, ignore_errors=True)


def check_and_refresh(game_dir):
    current, extracted = installed_version(game_dir), extracted_version()
    if not current:
        print(f"Deadlock not found at {game_dir}; serving without refreshing game art.")
        return
    if current == extracted:
        print(f"Game art is up to date (Deadlock version {current}).")
        return
    print(f"Deadlock is on version {current} but the game art is from {extracted or 'no extraction yet'}. Re-extracting...")
    if refresh_assets(game_dir):
        print(f"Game art refreshed to version {current}.")


def refresh_community():
    """Re-read community balance talk for the tier list, unless the saved copy is still fresh."""
    try:
        if time.time() - os.path.getmtime(COMMUNITY) < COMMUNITY_EVERY_SECONDS:
            return
    except OSError:
        pass
    try:
        result = community.collect()
    except Exception as err:  # the tier list just leaves this factor out
        print(f"Couldn't read community discussion ({err}).")
        return
    os.makedirs(os.path.dirname(COMMUNITY), exist_ok=True)
    with open(COMMUNITY + ".tmp", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1)
    os.replace(COMMUNITY + ".tmp", COMMUNITY)
    print(f"Community discussion refreshed: {result['posts']} posts, {result['comments']} comments.")


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        # Always revalidate, so refreshed art and page updates show up on the next load.
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--game", default=DEFAULT_GAME_DIR, help="path to Deadlock/game/citadel")
    args = parser.parse_args()

    check_and_refresh(args.game)

    def watch():
        while True:
            time.sleep(CHECK_EVERY_SECONDS)
            check_and_refresh(args.game)

    def watch_community():
        while True:
            refresh_community()
            time.sleep(COMMUNITY_EVERY_SECONDS)

    def watch_matches():
        while True:
            try:
                matches.refresh(MATCHES)
            except Exception as err:  # keep serving the last good sample
                print(f"Couldn't refresh the match sample ({err}).")
            try:
                topbuilds.refresh(ROOT)
            except Exception as err:  # keep serving the last good builds
                print(f"Couldn't refresh the top players' builds ({err}).")
            time.sleep(CHECK_EVERY_SECONDS)

    threading.Thread(target=watch, daemon=True).start()
    threading.Thread(target=watch_community, daemon=True).start()
    threading.Thread(target=watch_matches, daemon=True).start()
    handler = functools.partial(Handler, directory=ROOT)
    with http.server.ThreadingHTTPServer(("", args.port), handler) as server:
        print(f"Build viewer running at http://localhost:{args.port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
