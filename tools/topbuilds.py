"""Learn each hero's meta build from what top players (Ascendant and up) actually buy.

Keeps the item purchases of recent Ascendant+ matches in data/top_purchases.json (fetching only new
3-hour windows on each refresh), then writes data/top_builds.json. For each hero:
  1. Every game is sorted into a playstyle by where its souls went (gun, spirit or hybrid), and
     the style top players win with is chosen, so a build never mixes two styles.
  2. Within that style, the items most top players buy become the core build, in their real buy
     order and split into early, mid and late game; the common optional picks become situational.

Usage (normally run by serve.py):
    python tools/topbuilds.py
"""
import bisect
import calendar
import json
import os
import re
import statistics
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict

API_ROOT = "https://api.deadlock-api.com"
API = f"{API_ROOT}/v1"
USER_AGENT = "deadlock-build-viewer/1.0 (personal fan site)"
TOP_BADGE = 100            # Ascendant and up
KEEP_DAYS = 7
PATCH_MIN_HOURS = 36       # after a patch, wait this long before using only post-patch games
WINDOW_SECONDS = 3 * 3600
SETTLE_SECONDS = 8 * 3600
REQUEST_GAP_SECONDS = 1.0

CORE_RATE = 0.5            # bought in at least half of the style's games
CORE_MIN_IMPACT = -0.025   # ...and not losing more than 2.5 points of win rate when bought
SITUATIONAL_RATE = 0.15
MAX_SITUATIONAL = 10
STYLE_MIN_SHARE = 0.25     # a style must be at least this common to be "the meta"
STYLE_LEAD = 0.15          # gun/spirit share lead that makes a game gun or spirit rather than hybrid
PHASES = (("Early Game", 0, 10 * 60), ("Mid Game", 10 * 60, 22 * 60), ("Late Game", 22 * 60, 10 ** 9))


def fetch_json(path, tries=4):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(path if path.startswith("http") else f"{API}{path}", headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=180) as res:
                return json.load(res)
        except urllib.error.HTTPError as err:
            if err.code not in (429, 500, 502, 503, 504) or attempt == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == tries - 1:
                raise
        time.sleep(15 * (attempt + 1))
    return []


def shop_items():
    """id -> {slot, cost, name} for every item sold in the shop."""
    out = {}
    for item in fetch_json("/assets/items"):
        if item.get("type") == "upgrade" and item.get("shopable"):
            out[item["id"]] = {"slot": item.get("item_slot_type"), "cost": item.get("cost") or 0, "name": item.get("name")}
    return out


NAMED_UPDATE = re.compile(r"\b(updates?|patch(es)?|changelog)\b", re.I)


def latest_patch_time(now):
    """Start time of the newest game update.

    Balance patches carry their date in the title ('10-05-2026'). A big update announced under a
    name ('City Never Sleeps') counts too, from when it was posted, because its dated changelog
    entry can follow days later. New-hero announcements change no numbers and aren't patches.
    """
    best = 0
    for post in fetch_json(f"{API_ROOT}/v2/patches"):
        title = post.get("title") or ""
        posted = calendar.timegm(time.strptime(post["pub_date"][:19], "%Y-%m-%dT%H:%M:%S")) if post.get("pub_date") else 0
        m = re.search(r"(\d{2})-(\d{2})-(\d{4})", title)
        if m:
            day = calendar.timegm((int(m[3]), int(m[1]), int(m[2]), 0, 0, 0))
            t = posted if day <= posted < day + 36 * 3600 else day + 20 * 3600
        elif posted and NAMED_UPDATE.search(title + " " + re.sub(r"<[^>]+>", " ", post.get("content") or "")):
            t = posted
        else:
            continue
        if t <= now:
            best = max(best, t)
    return best


def fetch_window(start, items):
    try:
        rows = fetch_json(f"/matches/metadata?include_info=true&include_player_kda=true&include_player_items=true&game_mode=normal"
                          f"&min_average_badge={TOP_BADGE}&min_unix_timestamp={start}&max_unix_timestamp={start + WINDOW_SECONDS - 1}&limit=2000")
    except urllib.error.HTTPError as err:
        if err.code == 404:  # the API answers 404 when a window has no matches yet
            return []
        raise
    games = []
    for m in rows:
        if m.get("not_scored") or m.get("match_outcome") != "TeamWin" or len(m.get("players") or []) != 12:
            continue
        for p in m["players"]:
            bought = {}
            for it in p.get("items") or []:
                iid = it.get("item_id")
                if iid in items and iid not in bought:
                    bought[iid] = [iid, it.get("game_time_s") or 0, 1 if it.get("sold_time_s") else 0]
            games.append([m["match_id"], start, p.get("hero_id"), p.get("account_id"),
                          1 if p.get("team") == m.get("winning_team") else 0, sorted(bought.values(), key=lambda x: x[1])])
    return games


def refresh_purchases(path, items, log=print):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {"windows": {}, "games": []}
    now = int(time.time())
    first = now - KEEP_DAYS * 86400
    first -= first % WINDOW_SECONDS
    windows = {int(k): v for k, v in data["windows"].items() if int(k) >= first}
    games = [g for g in data["games"] if g[1] >= first]
    have = {(g[0], g[3]) for g in games}
    added = requests = 0
    for start in range(first, now, WINDOW_SECONDS):
        settled = now - (start + WINDOW_SECONDS) > SETTLE_SECONDS
        if settled and windows.get(start):
            continue
        try:
            got = fetch_window(start, items)
            requests += 1
        except (urllib.error.URLError, OSError, ValueError) as err:
            log(f"Top builds: couldn't fetch a window ({err}); will retry next refresh.")
            continue
        for g in got:
            if (g[0], g[3]) not in have:
                have.add((g[0], g[3]))
                games.append(g)
                added += 1
        windows[start] = True
        time.sleep(REQUEST_GAP_SECONDS)
    data = {"windows": {str(k): v for k, v in windows.items()}, "games": games, "updated": now}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(path + ".tmp", path)
    log(f"Top builds: {added} new player-games from {requests} requests; {len(games)} kept.")
    return data


def style_of(game, items):
    souls = Counter()
    for iid, _t, sold in game[5]:
        if not sold:
            souls[items[iid]["slot"]] += items[iid]["cost"]
    total = souls["weapon"] + souls["spirit"] + souls["vitality"]
    if not total:
        return None
    lead = (souls["weapon"] - souls["spirit"]) / total
    return "gun" if lead >= STYLE_LEAD else "spirit" if lead <= -STYLE_LEAD else "hybrid"


def item_impact(games, iid, when, ends):
    """Win rate with vs without an item, among games still going when it's usually bought (so
    late items aren't compared against games that ended before anyone could buy them)."""
    yes = no = wy = wn = 0
    for g in games:
        if ends[id(g)] < when:
            continue
        if iid in g[6]:
            yes += 1
            wy += g[4]
        else:
            no += 1
            wn += g[4]
    if yes < 25 or no < 25:
        return None
    a, b = wy / yes, wn / no
    se = (a * (1 - a) / yes + b * (1 - b) / no) ** 0.5
    return {"diff": a - b, "se": se, "with": a, "without": b, "n": yes}


def matchup_counters(games, items, enemies, base):
    """For the hero's worst common matchups: the items that most improve their win rate there."""
    against = defaultdict(list)
    for g in games:
        for e in enemies.get((g[0], g[3]), ()):
            against[e].append(g)
    out = []
    for enemy, gs in against.items():
        if len(gs) < 150:
            continue
        rate = sum(g[4] for g in gs) / len(gs)
        if rate > base - 0.03:
            continue
        ends = {id(g): max(t for _i, t, _s in g[5]) for g in gs}
        times = defaultdict(list)
        for g in gs:
            for iid, t, _s in g[5]:
                times[iid].append(t)
        helps = []
        for iid, ts in times.items():
            if len(ts) < 25:
                continue
            imp = item_impact(gs, iid, statistics.median(ts), ends)
            # Clearly helpful: at least +6 points and more than its own margin of error.
            if imp and imp["diff"] >= 0.06 and imp["diff"] > imp["se"]:
                helps.append({"id": iid, "time": int(statistics.median(ts)), "rate": len(ts) / len(gs), **imp})
        if helps:
            helps.sort(key=lambda h: -h["diff"])
            out.append({"enemy": enemy, "games": len(gs), "winRate": rate, "items": helps[:5]})
    out.sort(key=lambda m: m["winRate"])
    return out[:2]


def build_for_hero(games, items, enemies):
    """The meta style and its build for one hero's games, or None with too little data."""
    styled = [(g, style_of(g, items)) for g in games]
    styled = [(g, s) for g, s in styled if s]
    if len(styled) < 40:
        return None
    wins_all = sum(g[4] for g, _ in styled)
    base = wins_all / len(styled)
    styles = {}
    for name in ("gun", "spirit", "hybrid"):
        sub = [g for g, s in styled if s == name]
        if sub:
            w = sum(g[4] for g in sub)
            styles[name] = {"games": len(sub), "wins": w, "share": len(sub) / len(styled),
                            "score": (w + 50 * base) / (len(sub) + 50)}
    # The style top players win with most, among styles common enough to be a real meta choice.
    common = {k: v for k, v in styles.items() if v["share"] >= STYLE_MIN_SHARE} or styles
    style = max(common, key=lambda k: common[k]["score"])
    chosen = [g for g, s in styled if s == style]

    bought = defaultdict(list)
    sold = Counter()
    for g in chosen:
        for iid, t, was_sold in g[5]:
            bought[iid].append(t)
            sold[iid] += was_sold
    n = len(chosen)
    # Many games end before late game, so an item's rate counts only games still going when it's
    # usually bought (a player's last purchase stands in for how long their game lasted).
    ends = sorted(max(t for _i, t, _s in g[5]) for g in chosen)

    def still_going(t):
        lo, hi = 0, len(ends)
        while lo < hi:
            mid = (lo + hi) // 2
            if ends[mid] < t:
                lo = mid + 1
            else:
                hi = mid
        return len(ends) - lo

    end_of = {id(g): max(t for _i, t, _s in g[5]) for g in chosen}
    stats = {}
    for iid, ts in bought.items():
        when = int(statistics.median(ts))
        stats[iid] = {"id": iid, "rate": min(1.0, len(ts) / max(1, still_going(when), n // 4)), "time": when,
                      "soldRate": sold[iid] / len(ts), "buyers": len(ts)}
        imp = item_impact(chosen, iid, when, end_of)
        if imp:
            stats[iid]["impact"] = round(imp["diff"], 4)
            stats[iid]["impactSe"] = round(imp["se"], 4)

    # Popular isn't enough: items whose buyers clearly lose more (by more than 1.5× the margin of
    # error) are left out, however many players buy them.
    def losing(s):
        return "impact" in s and s["impact"] + 1.5 * s["impactSe"] < 0

    # Core items must also not cost wins: popular items that do worse than skipping them by more
    # than 2.5 points drop to situational (where items are ordered by how much they help).
    def core_worthy(s):
        return s["rate"] >= CORE_RATE and not losing(s) and s.get("impact", 0) >= CORE_MIN_IMPACT

    core = sorted((s for s in stats.values() if core_worthy(s)), key=lambda s: s["time"])
    situational = sorted((s for s in stats.values() if s["rate"] >= SITUATIONAL_RATE and not core_worthy(s) and not losing(s)),
                         key=lambda s: -(s.get("impact", 0)))[:MAX_SITUATIONAL]
    dropped = sorted((s for s in stats.values() if s["rate"] >= SITUATIONAL_RATE and losing(s)), key=lambda s: s["impact"])
    phases = []
    for label, lo, hi in PHASES:
        part = [s for s in core if lo <= s["time"] < hi]
        if part:
            phases.append({"name": label, "items": part})
    return {
        "style": style, "styles": styles, "games": n, "wins": sum(g[4] for g in chosen),
        "players": len({g[3] for g in chosen}), "heroGames": len(styled), "heroWinRate": base,
        "phases": phases, "situational": sorted(situational, key=lambda s: s["time"]),
        "avoid": dropped[:6], "matchups": matchup_counters(chosen, items, enemies, base),
        # Measured win-rate impact of every item with enough data, for judging saved builds.
        "impacts": {str(s["id"]): s["impact"] for s in stats.values() if "impact" in s},
        # How often this hero's top players buy each item at all (for the counter-pick page).
        "rates": {str(s["id"]): round(s["rate"], 3) for s in stats.values() if s["rate"] >= 0.03},
    }


def counter_stats(games, enemies, min_games=150):
    """Measured counter value of items against each enemy hero.

    For an item and an enemy hero: how much more buyers out-win non-buyers when that hero is on the
    other team than when they aren't. Subtracting the "enemy absent" gap removes how good the item
    is in general, leaving what is specific to the matchup. Only clear effects are kept (at least
    1.5 margins of error); the page uses them as supporting evidence, not as its only source.
    """
    times = defaultdict(list)
    for g in games:
        for iid, t, _s in g[5]:
            times[iid].append(t)
    median = {iid: statistics.median(ts) for iid, ts in times.items() if len(ts) >= 200}
    ends_all = [[], []]
    ends_vs = defaultdict(lambda: [[], []])
    with_all = defaultdict(lambda: [0, 0])
    with_vs = defaultdict(lambda: [0, 0])
    for g in games:
        end, won = max(t for _i, t, _s in g[5]), g[4]
        foes = enemies.get((g[0], g[3]), ())
        ends_all[won].append(end)
        for e in foes:
            ends_vs[e][won].append(end)
        for iid in g[6]:
            m = median.get(iid)
            if m is None or end < m:
                continue
            w = with_all[iid]
            w[0] += 1
            w[1] += won
            for e in foes:
                w = with_vs[(e, iid)]
                w[0] += 1
                w[1] += won
    for pools in [ends_all, *ends_vs.values()]:
        for lst in pools:
            lst.sort()

    def pool(ends, m):
        losses = len(ends[0]) - bisect.bisect_left(ends[0], m)
        wins = len(ends[1]) - bisect.bisect_left(ends[1], m)
        return losses + wins, wins

    overall, lifts = {}, defaultdict(dict)
    for iid, m in median.items():
        total_n, total_w = pool(ends_all, m)
        have_n, have_w = with_all[iid]
        if have_n >= min_games and total_n - have_n >= min_games:
            overall[str(iid)] = {"rate": round(have_n / total_n, 3),
                                 "impact": round(have_w / have_n - (total_w - have_w) / (total_n - have_n), 4)}
        for e, pools in ends_vs.items():
            vs_n, vs_w = pool(pools, m)
            a = with_vs.get((e, iid), (0, 0))                    # enemy present, item bought
            b = (vs_n - a[0], vs_w - a[1])                       # enemy present, not bought
            c = (have_n - a[0], have_w - a[1])                   # enemy absent, bought
            d = ((total_n - vs_n) - c[0], (total_w - vs_w) - c[1])  # enemy absent, not bought
            if min(a[0], b[0], c[0], d[0]) < min_games:
                continue
            r = [x[1] / x[0] for x in (a, b, c, d)]
            se = sum(p * (1 - p) / x[0] for p, x in zip(r, (a, b, c, d))) ** 0.5
            lift = (r[0] - r[1]) - (r[2] - r[3])
            if lift >= 0.02 and lift >= 1.5 * se:
                lifts[str(e)][str(iid)] = round(lift, 4)
    return {"items": overall, "lifts": lifts}


def refresh(root, log=print):
    items = shop_items()
    data = refresh_purchases(os.path.join(root, "data", "top_purchases.json"), items, log)
    now = int(time.time())
    patch = latest_patch_time(now)
    # Right after a patch, older games describe the old game; once there's enough, use only new ones.
    since = patch if now - patch >= PATCH_MIN_HOURS * 3600 else 0
    by_hero = defaultdict(list)
    by_match = defaultdict(list)
    for g in data["games"]:
        g.append({i for i, _t, _s in g[5]})  # g[6]: the items bought, for quick lookups
        if g[1] >= since and g[5]:
            by_hero[g[2]].append(g)
        by_match[g[0]].append(g)
    # Each player's enemies: the players in the same match with the other result.
    enemies = {(g[0], g[3]): {p[2] for p in by_match[g[0]] if p[4] != g[4]} for g in data["games"]}
    heroes = {}
    for hero_id, games in by_hero.items():
        b = build_for_hero(games, items, enemies)
        if b:
            heroes[str(hero_id)] = b
    recent = [g for games in by_hero.values() for g in games]
    out = {"updated": now, "minBadge": TOP_BADGE, "days": KEEP_DAYS, "sincePatch": since or None,
           "games": len(recent), "heroes": heroes, "counters": counter_stats(recent, enemies)}
    path = os.path.join(root, "data", "top_builds.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    os.replace(path + ".tmp", path)
    log(f"Top builds: wrote meta builds for {len(heroes)} heroes from {out['games']} top-player games.")
    return out


if __name__ == "__main__":
    refresh(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
