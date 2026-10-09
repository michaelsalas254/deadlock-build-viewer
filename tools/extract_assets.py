"""Extract Deadlock images, UI art and fonts from a local game install for the build viewer.

Usage:
    python tools/extract_assets.py [--game "<Deadlock>/game/citadel"] [--out assets/game]

Pixels come from the local game files. deadlock-api.com is only asked which
image file belongs to which item, ability and hero.

Re-run after a game patch to pick up new or changed art.
"""
import argparse
import json
import os
import re
import shutil
import sys
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from source2 import decode_vsvg, decode_vtex  # noqa: E402
from vpk import DEFAULT_GAME_DIR, VPK  # noqa: E402

API = "https://api.deadlock-api.com/v1"
API_IMAGE_PREFIX = "/assets-api-res/"
API_KEY_PATTERN = re.compile(r"(images|icons)/[\w./-]+")
TEXTURE_SUFFIXES = ("_psd.vtex_c", "_png.vtex_c", "_tga.vtex_c", ".vtex_c")

# Whole folders of UI art (relative to panorama/images/) that the page styles itself with.
# main_menu/pick_screen/ is the Create Roster screen (card frames, priority icons, smoke backdrop).
UI_FOLDERS = ("shop/", "tooltips/", "builds/", "main_menu/pick_screen/")
UI_SKIP = re.compile(r"/text_images/")
UI_FILES = (
    "hud/icons/icon_soul.vsvg_c",
    "hud/core/icon_soul.vsvg_c",
    "upgrades/tier_numbers_small_01.vsvg_c",
    "upgrades/tier_numbers_small_02.vsvg_c",
    "upgrades/tier_numbers_small_03.vsvg_c",
    "upgrades/tier_numbers_small_04.vsvg_c",
    "masks/card_edge_blur_png.vtex_c",
    "control_icons/lock_medium_png.vtex_c",
    "icons/roster_icon_png.vtex_c",
)
FONT_PATTERN = re.compile(r"^(radiance|reaver|retail|valveoracle-|valvepulp).*\.(otf|ttf)$", re.I)

LOSSLESS_MAX = 512        # images up to this size are stored losslessly
MAX_DIMENSION = 1920      # larger art (hero backgrounds) is scaled down to this


def fetch_json(path):
    req = urllib.request.Request(f"{API}{path}", headers={"User-Agent": "deadlock-build-viewer-extractor"})
    with urllib.request.urlopen(req, timeout=60) as res:
        return json.load(res)


def api_key(url):
    """'https://.../assets-api-res/images/upgrades/x/y.png' -> 'images/upgrades/x/y'"""
    if not url or API_IMAGE_PREFIX not in url:
        return None
    key = os.path.splitext(url.split(API_IMAGE_PREFIX, 1)[1])[0]
    return key if API_KEY_PATTERN.fullmatch(key) else None


def stem_of(path):
    name = path.rsplit("/", 1)[-1]
    for suffix in TEXTURE_SUFFIXES + (".vsvg_c",):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return None


class GameImages:
    def __init__(self, vpk):
        self.vpk = vpk
        self.by_stem = defaultdict(list)
        for path in vpk:
            if path.startswith("panorama/images/"):
                stem = stem_of(path)
                if stem:
                    self.by_stem[stem].append(path)

    def resolve(self, key):
        """Find the game file for an API image key, preferring an exact path match."""
        key = key.split("/", 1)[1]  # drop the API's images/ or icons/ prefix
        base = f"panorama/images/{key}"
        for suffix in TEXTURE_SUFFIXES + (".vsvg_c",):
            if base + suffix in self.vpk:
                return base + suffix
        folders = key.split("/")[:-1]
        candidates = self.by_stem.get(key.rsplit("/", 1)[-1], [])
        if not candidates:
            return None

        def score(path):
            parts = path.split("/")[2:-1]
            shared = sum(1 for f in folders if f in parts)
            return (shared, -len(path))

        return max(candidates, key=score)


def save_texture(img, out_base):
    if max(img.size) > MAX_DIMENSION:
        img.thumbnail((MAX_DIMENSION, MAX_DIMENSION))
    lossless = max(img.size) <= LOSSLESS_MAX
    out = out_base + ".webp"
    img.save(out, "WEBP", lossless=lossless, quality=100 if lossless else 88, method=4)
    return out


def convert(vpk, game_path, out_base):
    os.makedirs(os.path.dirname(out_base), exist_ok=True)
    data = vpk.read(game_path)
    if game_path.endswith(".vsvg_c"):
        out = out_base + ".svg"
        with open(out, "w", encoding="utf-8") as f:
            f.write(decode_vsvg(data))
        return out
    return save_texture(decode_vtex(data), out_base)


def read_game_version(game_dir):
    try:
        with open(os.path.join(game_dir, "steam.inf"), encoding="utf-8", errors="replace") as f:
            info = dict(line.strip().split("=", 1) for line in f if "=" in line)
        return info.get("ClientVersion") or info.get("PatchVersion")
    except OSError:
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--game", default=DEFAULT_GAME_DIR, help="path to Deadlock/game/citadel")
    parser.add_argument("--out", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "game"))
    args = parser.parse_args()

    started = time.time()
    vpk = VPK(os.path.join(args.game, "pak01_dir.vpk"))
    images = GameImages(vpk)
    print(f"Opened game archive: {len(vpk.entries):,} files")

    print("Asking deadlock-api.com which image belongs to which item and hero...")
    items = fetch_json("/assets/items")
    heroes = fetch_json("/assets/heroes?only_active=true")
    keys = set()
    for item in items:
        for field in ("image", "shop_image"):
            k = api_key(item.get(field))
            if k:
                keys.add(k)
    for hero in heroes:
        imgs = hero.get("images") or {}
        for field in ("icon_hero_card", "icon_image_small", "background_image", "top_bar_vertical_image", "name_image"):
            k = api_key(imgs.get(field))
            if k:
                keys.add(k)

    if os.path.isdir(args.out):
        shutil.rmtree(args.out)
    os.makedirs(args.out)

    jobs = []  # (game_path, output base without extension, manifest section, manifest key)
    missing = []
    for k in sorted(keys):
        game_path = images.resolve(k)
        if game_path:
            jobs.append((game_path, os.path.join(args.out, *k.split("/")), "images", k))
        else:
            missing.append(k)

    ui_paths = [p for p in vpk if p.startswith("panorama/images/") and p[len("panorama/images/"):].startswith(UI_FOLDERS)
                and stem_of(p) and not UI_SKIP.search(p)]
    ui_paths += ["panorama/images/" + p for p in UI_FILES if "panorama/images/" + p in vpk]
    ui_paths = sorted(set(ui_paths))
    # Name UI files by stem, but keep the _png/_psd suffix when two sources share a stem.
    stem_counts = defaultdict(int)
    for game_path in ui_paths:
        stem_counts[(game_path.rsplit("/", 1)[0], stem_of(game_path))] += 1
    for game_path in ui_paths:
        folder = game_path.rsplit("/", 1)[0]
        name = stem_of(game_path)
        if stem_counts[(folder, name)] > 1:
            name = game_path.rsplit("/", 1)[1].split(".")[0]
        key = f"{folder[len('panorama/images/'):]}/{name}"
        jobs.append((game_path, os.path.join(args.out, "ui", *key.split("/")), "ui", key))

    manifest = {"source": "Deadlock game files", "game_version": read_game_version(args.game),
                "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "images": {}, "ui": {}, "fonts": []}
    failures = []
    site_root = os.path.dirname(os.path.dirname(os.path.abspath(args.out)))

    def site_path(path):
        return os.path.relpath(path, site_root).replace("\\", "/")

    def run(job):
        game_path, out_base, section, key = job
        try:
            out = convert(vpk, game_path, out_base)
            return section, key, site_path(out), None
        except Exception as exc:  # keep going; report at the end
            return section, key, None, f"{game_path}: {exc}"

    print(f"Converting {len(jobs)} images...")
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
        for section, key, out, err in pool.map(run, jobs):
            if err:
                failures.append(err)
            else:
                manifest[section][key] = out

    font_dir = os.path.join(args.game, "panorama", "fonts")
    os.makedirs(os.path.join(args.out, "fonts"), exist_ok=True)
    for name in sorted(os.listdir(font_dir)):
        if FONT_PATTERN.match(name):
            out = os.path.join(args.out, "fonts", name)
            shutil.copy2(os.path.join(font_dir, name), out)
            manifest["fonts"].append(site_path(out))

    manifest["missing"] = missing
    with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)

    total = sum(os.path.getsize(os.path.join(dp, fn)) for dp, _, fns in os.walk(args.out) for fn in fns)
    print(f"Done in {time.time() - started:.0f}s: {len(manifest['images'])} item/hero images, "
          f"{len(manifest['ui'])} UI images, {len(manifest['fonts'])} fonts, {total / 1e6:.1f} MB")
    if missing:
        print(f"{len(missing)} API images had no match in the game files (the page falls back to deadlock-api.com for these):")
        for k in missing[:20]:
            print("   ", k)
    if failures:
        print(f"{len(failures)} files failed to convert:")
        for f in failures[:20]:
            print("   ", f)


if __name__ == "__main__":
    main()
