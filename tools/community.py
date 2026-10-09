"""Gather what the Deadlock community says about each hero's strength.

Reads the past week of balance talk on r/DeadlockTheGame through Reddit's public RSS feeds (posts,
plus the comments of the busiest balance threads) and counts, per hero, how often a sentence that
names them calls them strong ("OP", "broken", "needs a nerf"...) or weak ("useless", "needs a
buff"...). serve.py refreshes this every few hours and the page reads it from data/community.json.

Usage (normally run by serve.py):
    python tools/community.py [--out data/community.json]
"""
import argparse
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.deadlock-api.com/v1"
SUBREDDIT = "https://www.reddit.com/r/DeadlockTheGame"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) deadlock-build-viewer/1.0 (personal fan site)"
REQUEST_GAP_SECONDS = 8      # Reddit's feeds rate-limit quickly; stay well under it
COMMENT_THREADS = 10         # how many of the busiest balance threads to read comments from
MAX_AGE_DAYS = 10

BALANCE_QUERY = ('nerf OR buff OR OP OR broken OR overpowered OR underpowered OR weak OR "tier list" '
                 'OR meta OR busted OR useless')

# Multi-word weak phrases come first, so "nerfed into the ground" isn't read as "nerfed" (strong).
WEAK = re.compile(r"\b(nerfed into the ground|needs? (?:a |some |another )?buffs?|buff|"
                  r"weak(?:est)?|underpowered|under ?tuned|useless|trash|garbage|unplayable|gutted|fell off|"
                  r"worst hero|[cdf][ -]?tier|bad hero|struggles?)\b", re.I)
# On Reddit a bare "OP" usually means "original poster", so only count it (and "meta") as a verdict.
# "They nerfed X" reports a patch rather than calling X strong now, so only asks for a nerf count.
STRONG = re.compile(r"\b((?:is|are|so|too|way|super|still|kinda|really|very|stupidly|insanely)\s+(?:op|meta)|op af|"
                    r"meta (?:pick|hero|defining)|overpowered|over ?tuned|broken|busted|nerf|needs? (?:a |some |another )?nerfs?|"
                    r"too strong|oppressive|cracked|insane|unkillable|unbeatable|s\+?[ -]?tier|god[ -]?tier|"
                    r"free wins?|stomp(?:s|ing|ed)?|dominat\w*|strongest|absurd|ridiculous)\b", re.I)

# Extra names players use; every hero's own name (minus "The") is always included.
ALIASES = {
    "Mo & Krill": ["mo and krill", "mo&krill", "mo & krill", "krill"],
    "Grey Talon": ["talon", "grey talon", "gray talon"],
    "Lady Geist": ["geist", "lady geist"],
    "McGinnis": ["mcginnis", "mcg"],
    "Rat King": ["rat king", "ratking"],
}


def fetch(url, tries=3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as res:
                return res.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as err:
            if err.code != 429 or attempt == tries - 1:
                raise
            time.sleep(30 * (attempt + 1))
    return ""


def feed_entries(xml):
    """(title, link, text, published unix time) for each entry of a Reddit Atom feed."""
    out = []
    for entry in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
        title = html.unescape(re.search(r"<title>(.*?)</title>", entry, re.S).group(1)) if "<title>" in entry else ""
        link = re.search(r'<link href="(.*?)"', entry)
        body = re.search(r"<content[^>]*>(.*?)</content>", entry, re.S)
        text = re.sub(r"<[^>]+>", " ", html.unescape(body.group(1))) if body else ""
        text = re.sub(r"\s+", " ", html.unescape(text))
        # Reddit appends "submitted by /u/x [link] [comments]" to every post body.
        text = re.sub(r"submitted by /u/\S+.*$", "", text).strip()
        when = re.search(r"<(?:published|updated)>(.*?)</", entry)
        ts = time.mktime(time.strptime(when.group(1)[:19], "%Y-%m-%dT%H:%M:%S")) if when else time.time()
        out.append((title, link.group(1) if link else "", text, ts))
    return out


def hero_patterns(heroes):
    pats = {}
    for hero in heroes:
        name = hero["name"]
        names = {name.lower(), re.sub(r"^the\s+", "", name.lower())} | set(ALIASES.get(name, []))
        pats[hero["id"]] = re.compile(r"\b(" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")\b", re.I)
    return pats


def score_text(text, pats, tally):
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        named = [hid for hid, pat in pats.items() if pat.search(sentence)]
        # Sentences naming many heroes are usually lists or matchups; their verdicts are ambiguous.
        if not named or len(named) > 2:
            continue
        rest = sentence
        weak = len(WEAK.findall(rest))
        rest = WEAK.sub(" ", rest)
        strong = len(STRONG.findall(rest))
        for hid in named:
            t = tally.setdefault(hid, {"mentions": 0, "strong": 0, "weak": 0})
            t["mentions"] += 1
            t["strong"] += strong
            t["weak"] += weak


def collect(log=print):
    heroes = json.loads(fetch(f"{API}/assets/heroes?only_active=true"))
    pats = hero_patterns(heroes)
    since = time.time() - MAX_AGE_DAYS * 86400
    query = urllib.parse.quote(BALANCE_QUERY)
    feeds = [
        f"{SUBREDDIT}/search.rss?q={query}&restrict_sr=on&sort=new&t=week&limit=100",
        f"{SUBREDDIT}/search.rss?q={query}&restrict_sr=on&sort=top&t=week&limit=100",
        f"{SUBREDDIT}/top/.rss?t=week&limit=100",
    ]
    posts = {}
    for i, url in enumerate(feeds):
        if i:
            time.sleep(REQUEST_GAP_SECONDS)
        try:
            for title, link, text, ts in feed_entries(fetch(url)):
                if ts >= since and link:
                    posts[link] = (title, text)
        except (urllib.error.URLError, OSError) as err:
            log(f"Community: couldn't read {url.split('?')[0]} ({err})")

    tally = {}
    for title, text in posts.values():
        score_text(f"{title}. {text}", pats, tally)

    # Read comments where the balance arguments happen: threads whose titles talk about it.
    balance = [link for link, (title, text) in posts.items() if STRONG.search(title) or WEAK.search(title)
               or any(p.search(title) for p in pats.values())]
    comments = 0
    for link in balance[:COMMENT_THREADS]:
        time.sleep(REQUEST_GAP_SECONDS)
        try:
            for _title, _link, text, ts in feed_entries(fetch(link.rstrip("/") + "/.rss?limit=100"))[1:]:
                if ts >= since:
                    score_text(text, pats, tally)
                    comments += 1
        except (urllib.error.URLError, OSError) as err:
            log(f"Community: skipped a thread's comments ({err})")

    return {
        "source": "r/DeadlockTheGame",
        "updated": int(time.time()),
        "posts": len(posts),
        "comments": comments,
        "heroes": {str(hid): t for hid, t in tally.items()},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser.add_argument("--out", default=os.path.join(root, "data", "community.json"))
    parser.add_argument("--max-age-hours", type=float, default=0, help="skip if the saved copy is newer than this")
    args = parser.parse_args()
    # Judged by the time saved in the file: restored caches don't always keep file timestamps.
    try:
        with open(args.out, encoding="utf-8") as f:
            updated = json.load(f).get("updated", 0)
        if args.max_age_hours and time.time() - updated < args.max_age_hours * 3600:
            print("Community discussion is still fresh; not re-reading it.")
            return
    except (OSError, ValueError):
        pass
    result = collect()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1)
    print(f"Read {result['posts']} posts and {result['comments']} comments; {len(result['heroes'])} heroes mentioned.")


if __name__ == "__main__":
    main()
