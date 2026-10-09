"""Keep a compact local sample of recent Deadlock matches for the tier list's strength model.

The page fits a model to these matches that measures how much each hero changes their team's chance
of winning, adjusted for the teammates and enemies they played with. Each match is stored as
    [match_id, start_minute, average_badge, team0_won, six team-0 hero ids..., six team-1 hero ids...]

Every 3-hour window of the past 4 weeks is sampled: recent days densely (they count most in the
model), older days lightly, plus every Phantom+ match so the high-rank view has enough games.
serve.py refreshes this every 30 minutes and only downloads what's new.

Usage (normally run by serve.py):
    python tools/matches.py [--out data/matches.json]
"""
import argparse
import calendar
import json
import os
import time
import urllib.error
import urllib.request

API = "https://api.deadlock-api.com/v1"
USER_AGENT = "deadlock-build-viewer/1.0 (personal fan site)"
WINDOW_SECONDS = 3 * 3600
KEEP_DAYS = 28
SETTLE_SECONDS = 8 * 3600     # the API keeps ingesting a window's matches for a few hours
REQUEST_GAP_SECONDS = 0.5
HIGH_BADGE = 90               # Phantom and up


def per_window(age_days):
    """How many matches to sample from a 3-hour window this many days old."""
    if age_days <= 3:
        return 1200
    if age_days <= 10:
        return 500
    return 200


def fetch_json(path, tries=4):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(f"{API}{path}", headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=120) as res:
                return json.load(res)
        except urllib.error.HTTPError as err:
            if err.code not in (429, 500, 502, 503, 504) or attempt == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == tries - 1:
                raise
        time.sleep(15 * (attempt + 1))
    return []


def compact(match):
    """[id, start minute, badge, team0 won, 6 team-0 heroes, 6 team-1 heroes], or None if unusable."""
    if match.get("not_scored") or match.get("match_outcome") != "TeamWin":
        return None
    teams = {"Team0": [], "Team1": []}
    for p in match.get("players") or []:
        if p.get("team") in teams and p.get("hero_id"):
            teams[p["team"]].append(int(p["hero_id"]))
    if len(teams["Team0"]) != 6 or len(teams["Team1"]) != 6:
        return None
    start = match.get("start_time")
    minute = calendar.timegm(time.strptime(start, "%Y-%m-%d %H:%M:%S")) // 60 if isinstance(start, str) else 0
    # Only ranked matches have a badge; the per-team fields are usually 0, so prefer the match average.
    badges = [b for b in (match.get("average_badge_team0"), match.get("average_badge_team1")) if b]
    badge = match.get("average_badge") or (round(sum(badges) / len(badges)) if badges else 0)
    return [match["match_id"], minute, badge, 1 if match.get("winning_team") == "Team0" else 0,
            *sorted(teams["Team0"]), *sorted(teams["Team1"])]


def fetch_window(start, limit, high=False):
    query = (f"/matches/metadata?include_info=true&include_player_kda=true&game_mode=normal"
             f"&min_unix_timestamp={start}&max_unix_timestamp={start + WINDOW_SECONDS - 1}&limit={limit}")
    if high:
        query += f"&min_average_badge={HIGH_BADGE}"
    try:
        rows = fetch_json(query)
    except urllib.error.HTTPError as err:
        if err.code == 404:  # the API answers 404 when a window has no matches yet
            return []
        raise
    return [c for c in (compact(m) for m in rows) if c]


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") == 2:
            return data
        if data.get("version") == 1:
            # Version 1 saved no badges, so Phantom+ windows are fetched again to fill them in.
            for v in data["windows"].values():
                v["high"] = False
            data["version"] = 2
            return data
    except (OSError, ValueError):
        pass
    return {"version": 2, "windows": {}, "matches": []}


def refresh(path, log=print):
    """Bring the cache up to date. Returns the number of new matches."""
    data = load(path)
    now = int(time.time())
    oldest = now - KEEP_DAYS * 86400
    first = oldest - oldest % WINDOW_SECONDS
    known = {m[0]: m for m in data["matches"]}
    windows = {int(k): v for k, v in data["windows"].items() if int(k) >= first}
    added = 0
    requests = 0
    for start in range(first, now, WINDOW_SECONDS):
        age_days = (now - start) / 86400
        want = per_window(age_days)
        done = dict(windows.get(start, {}))
        settled = now - (start + WINDOW_SECONDS) > SETTLE_SECONDS
        # Settled windows are fetched once; recent ones again on every refresh while they fill up.
        need_all = not settled or done.get("all", 0) < want
        need_high = not settled or not done.get("high")
        if not need_all and not need_high:
            continue
        got = []
        try:
            if need_all:
                got += fetch_window(start, want)
                done["all"] = want
                requests += 1
                time.sleep(REQUEST_GAP_SECONDS)
            if need_high:
                got += fetch_window(start, 2000, high=True)
                done["high"] = True
                requests += 1
                time.sleep(REQUEST_GAP_SECONDS)
        except (urllib.error.URLError, OSError, ValueError) as err:
            log(f"Matches: couldn't fetch a window ({err}); will retry next refresh.")
        for m in got:
            old = known.get(m[0])
            if old is None:
                known[m[0]] = m
                data["matches"].append(m)
                added += 1
            elif not old[2] and m[2]:
                old[2] = m[2]
        windows[start] = done
    cutoff_minute = oldest // 60
    data["matches"] = sorted((m for m in data["matches"] if m[1] >= cutoff_minute), key=lambda m: m[1])
    data["windows"] = {str(k): v for k, v in windows.items()}
    data["updated"] = now
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(path + ".tmp", path)
    log(f"Matches: {added} new from {requests} requests; {len(data['matches'])} matches cached.")
    return added


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser.add_argument("--out", default=os.path.join(root, "data", "matches.json"))
    args = parser.parse_args()
    refresh(args.out)


if __name__ == "__main__":
    main()
