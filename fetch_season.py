#!/usr/bin/env python3
"""
Pull a college football season from CollegeFootballData.com and write the
season.json that the recursive-rankings app reads.

Setup (once):
    1. Get a free key at https://collegefootballdata.com/key
    2. export COLLEGE_FOOTBALL_API_KEY='your-key-here'

Use (re-run any time; each run costs ~4 of your 1,000 monthly calls):
    python3 fetch_season.py                    # current season, regular only
    python3 fetch_season.py --year 2025        # a past season
    python3 fetch_season.py --postseason       # include bowls/playoff
    python3 fetch_season.py --cfbd-weeks       # keep CFBD's own week numbers
    python3 fetch_season.py --selftest         # verify logic, no key needed

The AP and Coaches polls ride along in a "polls" key so the app can grade itself
against them; each snapshot records the week of data it reflects. Pass --no-polls
to skip that call.

Weeks are renumbered from kickoff dates by default. CFBD files Week 0 and Labor
Day weekend under a single "week 1", which gives any team that played both two
games in one bucket -- and the app holds one game per team per week, so it drops
the second. Calendar weeks run Tuesday-Monday, so a Saturday and the Labor Day
Monday after it stay together. Pass --cfbd-weeks to keep the raw numbering.

Then open the app, go to the Data tab, and load season.json with the file picker.
"""

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.collegefootballdata.com"
KEY_ENV = "COLLEGE_FOOTBALL_API_KEY"


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
def get(path, params, key, retries=4):
    """GET with Bearer auth and backoff on transient failures."""
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": "recursive-rankings/1.0",
    })
    delay = 1.5
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                die(f"CFBD rejected the key ({e.code}). Check {KEY_ENV} is set "
                    f"to a valid key from https://collegefootballdata.com/key")
            if e.code == 429:
                warn(f"rate limited, waiting {delay:.0f}s")
            elif e.code >= 500:
                warn(f"CFBD returned {e.code}, retrying")
            else:
                die(f"CFBD returned {e.code} for {path}: {e.read()[:300].decode('utf-8', 'replace')}")
        except (urllib.error.URLError, TimeoutError) as e:
            warn(f"network error ({e}), retrying")
        if attempt < retries - 1:
            time.sleep(delay)
            delay *= 2
    die(f"gave up on {path} after {retries} attempts")


def warn(msg):
    print(f"  ! {msg}", file=sys.stderr)


def die(msg):
    print(f"\nerror: {msg}", file=sys.stderr)
    sys.exit(1)


# ----------------------------------------------------------------------------
# Field access — CFBD has used both camelCase and snake_case over time
# ----------------------------------------------------------------------------
def pick(d, *names, default=None):
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    return default


# ----------------------------------------------------------------------------
# Week numbering from kickoff dates
# ----------------------------------------------------------------------------
# Kickoffs are stamped in UTC and run from about 14:00 to 05:00 the next day, so
# a night game in the US carries the following day's UTC date. Shifting back 8
# hours maps every kickoff onto its own US calendar day; the shifted times all
# land between 06:00 and 21:00, so no game sits near a date boundary.
UTC_SHIFT = dt.timedelta(hours=8)
_TUESDAY_EPOCH = dt.date(1970, 1, 6)        # a Tuesday, so weeks run Tue-Mon


def game_date(g):
    """US calendar date of the kickoff, or None if the row carries no usable date."""
    raw = pick(g, "startDate", "start_date")
    if not raw:
        return None
    s = str(raw).strip().replace("Z", "+0000")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            t = dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
        if t.tzinfo is not None:
            t = t.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return (t - UTC_SHIFT).date()
    try:                                     # plain YYYY-MM-DD
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def calendar_week(d):
    """Index of the Tuesday-Monday week containing d. Monotone in d."""
    return (d - _TUESDAY_EPOCH).days // 7


def assign_weeks(games, use_dates=True):
    """Renumber games 1..N by calendar week. Returns (games, how).

    Falls back to CFBD's own numbering when asked, or when any row lacks a date
    (the self-test fixtures have none). Numbering is compacted to consecutive
    integers so the app's week slider has no empty rows in the middle.
    """
    if not games:
        return games, "none"
    if use_dates and all(g.get("_date") for g in games):
        keyed = [(calendar_week(g["_date"]), g) for g in games]
        how = "kickoff dates"
    else:
        keyed = [(g["week"], g) for g in games]
        how = "CFBD week numbers"
    order = {k: i for i, k in enumerate(sorted({k for k, _ in keyed}), start=1)}
    for k, g in keyed:
        g["week"] = order[k]
    return games, how


def drop_duplicate_rows(games):
    """Remove rows that describe the same game twice.

    CFBD sometimes files one game under two ids with different kickoff times --
    2025's Augsburg-Hamline is listed at both 01:30 and 18:00 UTC with the same
    43-3 score at the same venue. Identical teams and an identical score within a
    few days is one game, so keep the earliest row. Returns (games, removed).
    """
    WINDOW = dt.timedelta(days=3)
    out, removed, keep = [], [], {}
    for g in sorted(games, key=lambda x: (x.get("_date") or dt.date.min,
                                          x["home"], x["away"])):
        sig = (g["home"], g["away"], g["homePoints"], g["awayPoints"])
        prev, d = keep.get(sig), g.get("_date")
        if prev is not None and d is not None and prev.get("_date") is not None \
                and abs(d - prev["_date"]) <= WINDOW:
            removed.append(f"{g['away']} at {g['home']} "
                           f"{g['awayPoints']}-{g['homePoints']} on {d}")
            continue
        keep[sig] = g
        out.append(g)
    return out, removed


def double_booked(games):
    """[(week, team, [labels])] for any team holding two games in one week."""
    from collections import defaultdict
    seen = defaultdict(list)
    for g in games:
        for t in (g["home"], g["away"]):
            seen[(g["week"], t)].append(f"{g['away']} at {g['home']}")
    return [(w, t, v) for (w, t), v in sorted(seen.items()) if len(v) > 1]


# ----------------------------------------------------------------------------
# Transform
# ----------------------------------------------------------------------------
def build_teams(raw_teams, division):
    out = {}
    for t in raw_teams:
        name = pick(t, "school", "team")
        if not name:
            continue
        out[name] = {
            "name": name,
            "conference": pick(t, "conference", default="") or "",
            "division": division,
        }
    return out


def is_title_game(g):
    """Conference championship games are filed as regular season by CFBD.

    Detected from the notes field, which reads like 'SEC Championship' or
    'Big Ten Championship Game'. Army-Navy, played after championship weekend,
    has no such note and is correctly kept as an ordinary regular-season game.
    """
    notes = (pick(g, "notes", default="") or "")
    return "championship" in notes.lower()


def build_games(raw_games, teams, week_offset=0, drop_titles=True, tag_cfbd=True):
    """Return (games, skipped, titles_dropped).

    Only completed, non-tied games with two known teams.
    """
    games, skipped, dropped = [], [], []
    for g in raw_games:
        home = pick(g, "homeTeam", "home_team")
        away = pick(g, "awayTeam", "away_team")
        week = pick(g, "week")
        hp = pick(g, "homePoints", "home_points")
        ap = pick(g, "awayPoints", "away_points")
        completed = pick(g, "completed", default=False)

        label = f"wk{week} {away} at {home}"
        if not home or not away:
            skipped.append(f"{label}: missing a team name")
            continue
        if not completed:
            continue                                  # not played yet — silent
        if drop_titles and is_title_game(g):
            dropped.append(f"{away} vs {home} — {pick(g, 'notes', default='title game')}")
            continue
        if hp is None or ap is None:
            skipped.append(f"{label}: completed but no score")
            continue
        if hp == ap:
            skipped.append(f"{label}: tie, cannot be ordered")
            continue
        if week is None:
            skipped.append(f"{label}: no week number")
            continue

        for n in (home, away):
            if n not in teams:
                # D2/D3 or NAIA opponents show up here; keep them as their own tier
                teams[n] = {"name": n, "conference": "", "division": "OTHER"}

        games.append({
            "week": int(week) + week_offset,
            "home": home,
            "away": away,
            "homePoints": int(hp),
            "awayPoints": int(ap),
            "_date": game_date(g),
            "_cfbd": int(week) if tag_cfbd else None,
        })
    return games, skipped, dropped


POLLS_WANTED = ("AP Top 25", "Coaches Poll")


def build_polls(raw, through_of):
    """[{week, poll, throughWeek, ranks:[{rank, team}]}] for the two human polls.

    CFBD files a poll under the week it precedes, so the poll labelled week N
    reflects games through CFBD week N-1. through_of converts that into the
    renumbered week its last game landed in, which is the horizon the app has to
    be set to for the comparison to be fair. A preseason poll maps to 0.
    """
    out = []
    for row in raw:
        wk = pick(row, "week")
        if wk is None:
            continue
        for p in (pick(row, "polls", default=[]) or []):
            name = pick(p, "poll", default="")
            if name not in POLLS_WANTED:
                continue
            ranks = []
            for r in (pick(p, "ranks", default=[]) or []):
                team, rk = pick(r, "school", "team"), pick(r, "rank")
                if team and rk is not None:
                    ranks.append({"rank": int(rk), "team": team})
            if not ranks:
                continue
            ranks.sort(key=lambda r: r["rank"])
            out.append({"week": int(wk), "poll": name,
                        "throughWeek": through_of(int(wk) - 1),
                        "ranks": ranks})
    out.sort(key=lambda p: (p["week"], p["poll"]))
    return out


def through_week_map(games):
    """CFBD week -> the renumbered week that the last of its games landed in."""
    last = {}
    for g in games:
        c = g.get("_cfbd")
        if c is not None:
            last[c] = max(last.get(c, 0), g["week"])

    def through_of(cfbd_week):
        return max([w for c, w in last.items() if c <= cfbd_week] or [0])
    return through_of


def summarize(teams, games):
    """Print the diagnostics that matter for this ranking system."""
    from collections import defaultdict
    div = defaultdict(int)
    for t in teams.values():
        div[t["division"]] += 1

    played = defaultdict(int)
    cross = 0
    conf_games = defaultdict(lambda: [0, 0])          # conference -> [outside, total]
    for g in games:
        dh = teams[g["home"]]["division"]
        da = teams[g["away"]]["division"]
        if dh != da:
            cross += 1
        for me, them in ((g["home"], g["away"]), (g["away"], g["home"])):
            played[me] += 1
            c, oc = teams[me]["conference"], teams[them]["conference"]
            conf_games[c][1] += 1
            if c != oc:
                conf_games[c][0] += 1

    weeks = sorted({g["week"] for g in games})
    print(f"\n  teams        {len(teams)}  ({', '.join(f'{k}: {v}' for k, v in sorted(div.items()))})")
    print(f"  games        {len(games)}")
    print(f"  weeks        {min(weeks)}–{max(weeks)}" if weeks else "  weeks        none")
    if games:
        pct = cross / len(games) * 100
        print(f"  cross-division games   {cross}  ({pct:.1f}% of all games)")
        if pct < 12:
            print("     ^ thin. Divisions are only weakly linked, so cross-division")
            print("       comparisons in the ranking will be unreliable.")

    thin = sorted((v[0] / v[1] * 100, k, v[1])
                  for k, v in conf_games.items() if v[1] >= 20)[:5]
    if thin:
        print("\n  least-connected conferences (share of games played outside the conference):")
        for pctv, name, n in thin:
            flag = "   <- too few links to rank reliably" if pctv < 20 else ""
            print(f"     {pctv:5.1f}%  {name or '(none)'}  [{n} games]{flag}")


# ----------------------------------------------------------------------------
# Self test — verifies the transform without touching the network
# ----------------------------------------------------------------------------
FIXTURE_TEAMS_FBS = [
    {"school": "Florida", "conference": "SEC"},
    {"school": "Florida Atlantic", "conference": "American Athletic"},
    {"school": "Army", "conference": "American Athletic"},
]
FIXTURE_TEAMS_FCS = [
    {"school": "Tarleton State", "conference": "United Athletic"},
    {"school": "Mercer", "conference": "Southern"},
]
FIXTURE_GAMES = [
    # normal completed game, camelCase
    {"week": 1, "homeTeam": "Florida", "awayTeam": "Florida Atlantic",
     "homePoints": 31, "awayPoints": 17, "completed": True},
    # snake_case variant
    {"week": 1, "home_team": "Army", "away_team": "Tarleton State",
     "home_points": 27, "away_points": 30, "completed": True},
    # unplayed — should vanish silently
    {"week": 5, "homeTeam": "Florida", "awayTeam": "Mercer",
     "homePoints": None, "awayPoints": None, "completed": False},
    # tie — should be skipped and reported
    {"week": 2, "homeTeam": "Mercer", "awayTeam": "Florida Atlantic",
     "homePoints": 21, "awayPoints": 21, "completed": True},
    # unknown opponent (D2) — should be admitted as OTHER
    {"week": 2, "homeTeam": "Mercer", "awayTeam": "Shorter",
     "homePoints": 45, "awayPoints": 3, "completed": True},
    # conference title game — filed as regular season, must be dropped by default
    {"week": 15, "homeTeam": "Florida", "awayTeam": "Georgia",
     "homePoints": 24, "awayPoints": 21, "completed": True,
     "notes": "SEC Championship Game"},
    # Army-Navy: played after title weekend, no note, must be KEPT
    {"week": 16, "homeTeam": "Army", "awayTeam": "Navy",
     "homePoints": 17, "awayPoints": 14, "completed": True},
]


# Two weekends that CFBD files under a single "week 1". Tarleton plays both, so
# raw numbering costs Florida Atlantic its game; date numbering keeps everything.
FIXTURE_SPLIT = [
    {"week": 1, "homeTeam": "Florida", "awayTeam": "Tarleton State",
     "homePoints": 42, "awayPoints": 7, "completed": True,
     "startDate": "2026-08-29T23:00:00.000Z"},
    {"week": 1, "homeTeam": "Florida Atlantic", "awayTeam": "Tarleton State",
     "homePoints": 20, "awayPoints": 17, "completed": True,
     "startDate": "2026-09-05T16:00:00.000Z"},
    # Labor Day Monday night: 01:00 UTC Sep 8 is the evening of Sep 7, and must
    # stay in the Sep 1-7 week rather than opening a new one.
    {"week": 1, "homeTeam": "Army", "awayTeam": "Mercer",
     "homePoints": 24, "awayPoints": 21, "completed": True,
     "startDate": "2026-09-08T01:00:00.000Z"},
]

# One game, filed twice by CFBD under different ids and kickoff times.
FIXTURE_DUPE = [
    {"week": 12, "homeTeam": "Florida", "awayTeam": "Mercer",
     "homePoints": 43, "awayPoints": 3, "completed": True,
     "startDate": "2025-11-15T01:30:00.000Z"},
    {"week": 12, "homeTeam": "Florida", "awayTeam": "Mercer",
     "homePoints": 43, "awayPoints": 3, "completed": True,
     "startDate": "2025-11-15T18:00:00.000Z"},
    # a real rematch, same score, months later -- must be KEPT
    {"week": 1, "homeTeam": "Florida", "awayTeam": "Mercer",
     "homePoints": 43, "awayPoints": 3, "completed": True,
     "startDate": "2025-08-30T18:00:00.000Z"},
]


# CFBD files a poll under the week it precedes: the week-2 poll reflects week 1.
FIXTURE_RANKINGS = [
    {"week": 1, "polls": [
        {"poll": "AP Top 25", "ranks": [{"school": "Florida", "rank": 1}]},
        {"poll": "FCS Coaches Poll", "ranks": [{"school": "Mercer", "rank": 1}]}]},
    {"week": 2, "polls": [
        {"poll": "AP Top 25", "ranks": [{"school": "Army", "rank": 2},
                                        {"school": "Florida", "rank": 1}]},
        {"poll": "Coaches Poll", "ranks": [{"school": "Florida", "rank": 3}]}]},
]


def selftest():
    teams = {}
    teams.update(build_teams(FIXTURE_TEAMS_FBS, "FBS"))
    teams.update(build_teams(FIXTURE_TEAMS_FCS, "FCS"))
    games, skipped, dropped = build_games(FIXTURE_GAMES, teams)
    kept_all, _, _ = build_games(FIXTURE_GAMES, dict(teams), drop_titles=False)

    checks = []
    checks.append(("completed games kept", len(games) == 4))
    checks.append(("conference title game dropped by default",
                   not any(g["home"] == "Florida" and g["away"] == "Georgia" for g in games)))
    checks.append(("title game kept when asked",
                   any(g["home"] == "Florida" and g["away"] == "Georgia" for g in kept_all)))
    checks.append(("Army-Navy kept (no title note, plays after title week)",
                   any(g["home"] == "Army" and g["away"] == "Navy" for g in games)))
    checks.append(("dropped title game reported", len(dropped) == 1))
    checks.append(("unplayed game dropped silently",
                   not any("Mercer" in s and "wk5" in s for s in skipped)))
    checks.append(("tie skipped and reported", any("tie" in s for s in skipped)))
    checks.append(("snake_case parsed",
                   any(g["home"] == "Army" and g["homePoints"] == 27 for g in games)))
    checks.append(("upset preserved (Tarleton 30, Army 27)",
                   any(g["away"] == "Tarleton State" and g["awayPoints"] == 30 for g in games)))
    checks.append(("unknown opponent tiered as OTHER",
                   teams.get("Shorter", {}).get("division") == "OTHER"))
    checks.append(("divisions labelled",
                   teams["Florida"]["division"] == "FBS"
                   and teams["Tarleton State"]["division"] == "FCS"))
    checks.append(("conference carried through",
                   teams["Florida"]["conference"] == "SEC"))

    # --- week numbering ---
    checks.append(("no date -> falls back to CFBD numbering",
                   assign_weeks([dict(g) for g in games])[1] == "CFBD week numbers"))
    checks.append(("fallback numbering is compacted to 1..N",
                   sorted({g["week"] for g in assign_weeks([dict(g) for g in games])[0]})
                   == list(range(1, len({g["week"] for g in games}) + 1))))

    st = {}
    st.update(build_teams(FIXTURE_TEAMS_FBS, "FBS"))
    st.update(build_teams(FIXTURE_TEAMS_FCS, "FCS"))
    sg, _, _ = build_games(FIXTURE_SPLIT, st)
    raw_clash = double_booked([dict(g) for g in sg])
    sg, show = assign_weeks(sg)
    byteam = {}
    for g in sg:
        for t in (g["home"], g["away"]):
            byteam.setdefault(t, []).append(g["week"])

    checks.append(("kickoff date parsed with the UTC shift",
                   game_date(FIXTURE_SPLIT[0]) == dt.date(2026, 8, 29)))
    checks.append(("late-night kickoff stays on its US game day",
                   game_date(FIXTURE_SPLIT[2]) == dt.date(2026, 9, 7)))
    checks.append(("weeks run Tuesday-Monday, so Labor Day Monday joins its weekend",
                   calendar_week(dt.date(2026, 9, 5)) == calendar_week(dt.date(2026, 9, 7))
                   and calendar_week(dt.date(2026, 9, 7)) != calendar_week(dt.date(2026, 9, 8))))
    checks.append(("CFBD's single 'week 1' double-books a team", len(raw_clash) == 1))
    checks.append(("dates used when every row has one", show == "kickoff dates"))
    checks.append(("date numbering splits the two weekends",
                   sorted(byteam["Tarleton State"]) == [1, 2]))
    checks.append(("no team double-booked after renumbering",
                   double_booked(sg) == []))
    checks.append(("every game survives the split", len(sg) == len(FIXTURE_SPLIT)))
    checks.append(("Labor Day game shares the second week",
                   byteam["Army"] == [2]))

    dt_ = {}
    dt_.update(build_teams(FIXTURE_TEAMS_FBS, "FBS"))
    dt_.update(build_teams(FIXTURE_TEAMS_FCS, "FCS"))
    dg, _, _ = build_games(FIXTURE_DUPE, dt_)
    deduped, removed = drop_duplicate_rows(dg)
    checks.append(("CFBD row filed twice is removed", len(removed) == 1))
    checks.append(("a genuine rematch with the same score is kept",
                   len(deduped) == 2))
    checks.append(("dedupe leaves no team double-booked",
                   double_booked(assign_weeks(deduped)[0]) == []))

    # --- polls ---
    sg2, _, _ = build_games(FIXTURE_SPLIT, dict(st))
    sg2, _ = assign_weeks(sg2)
    polls = build_polls(FIXTURE_RANKINGS, through_week_map(sg2))
    ap2 = [p for p in polls if p["poll"] == "AP Top 25" and p["week"] == 2][0]
    checks.append(("only the two FBS human polls are kept",
                   {p["poll"] for p in polls} == {"AP Top 25", "Coaches Poll"}))
    checks.append(("both snapshots of a week are kept", len(polls) == 3))
    checks.append(("ranks parsed and sorted",
                   [r["rank"] for r in ap2["ranks"]] == [1, 2]
                   and ap2["ranks"][0]["team"] == "Florida"))
    checks.append(("preseason poll maps to no data",
                   [p for p in polls if p["week"] == 1][0]["throughWeek"] == 0))
    checks.append(("week-2 poll maps to the renumbered week holding CFBD week 1",
                   ap2["throughWeek"] == 2))
    checks.append(("postseason games are not tagged for poll alignment",
                   build_games(FIXTURE_GAMES, dict(st), tag_cfbd=False)[0][0]["_cfbd"]
                   is None))

    print("\nself test")
    ok = True
    for name, passed in checks:
        print(f"  {'pass' if passed else 'FAIL'}  {name}")
        ok &= passed
    summarize(teams, games)
    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


# ----------------------------------------------------------------------------
def main():
    default_year = dt.date.today().year
    if dt.date.today().month < 6:          # Jan–May belongs to the prior season
        default_year -= 1

    p = argparse.ArgumentParser(description="Fetch a season from CollegeFootballData.com")
    p.add_argument("--year", type=int, default=default_year)
    p.add_argument("--out", default="season.json")
    p.add_argument("--postseason", action="store_true",
                   help="also include bowls and playoff, numbered after the regular season")
    p.add_argument("--no-fcs", action="store_true", help="top division only")
    p.add_argument("--no-polls", action="store_true",
                   help="skip the AP and Coaches poll fetch (saves one API call)")
    p.add_argument("--cfbd-weeks", action="store_true",
                   help="keep CFBD's own week numbers instead of renumbering from "
                        "kickoff dates (CFBD folds Week 0 and Labor Day weekend into "
                        "one 'week 1', which costs the app one game per affected team)")
    p.add_argument("--include-title-games", action="store_true",
                   help="include conference championship games (excluded by default; "
                        "they are filed as regular season by CFBD)")
    p.add_argument("--selftest", action="store_true", help="verify logic without a key")
    a = p.parse_args()

    if a.selftest:
        sys.exit(selftest())

    key = os.environ.get(KEY_ENV)
    if not key:
        die(f"{KEY_ENV} is not set.\n"
            f"  Get a free key at https://collegefootballdata.com/key, then:\n"
            f"    export {KEY_ENV}='your-key-here'")

    print(f"fetching {a.year} season")

    teams = {}
    print("  teams, top division")
    teams.update(build_teams(get("/teams/fbs", {"year": a.year}, key), "FBS"))
    if not a.no_fcs:
        print("  teams, second division")
        # the parameter has been called both 'classification' and 'division'
        raw = get("/teams", {"year": a.year, "classification": "fcs"}, key)
        fcs = build_teams(raw, "FCS")
        for name, info in fcs.items():
            teams.setdefault(name, info)

    print("  games, regular season")
    raw = get("/games", {"year": a.year, "seasonType": "regular"}, key)
    games, skipped, dropped = build_games(raw, teams, drop_titles=not a.include_title_games)

    if a.postseason:
        offset = max((g["week"] for g in games), default=0)
        print("  games, postseason")
        raw = get("/games", {"year": a.year, "seasonType": "postseason"}, key)
        pg, ps, pd = build_games(raw, teams, week_offset=offset, drop_titles=False,
                                 tag_cfbd=False)
        games += pg
        skipped += ps
        dropped += pd

    if not games:
        die(f"no completed games found for {a.year}. If the season hasn't started, "
            f"try --year {a.year - 1}.")

    games, dupes = drop_duplicate_rows(games)
    if dupes:
        print(f"  removed {len(dupes)} duplicate row(s) filed twice by CFBD:")
        for d in dupes[:4]:
            print(f"     {d}")

    games, how = assign_weeks(games, use_dates=not a.cfbd_weeks)
    print(f"  weeks numbered from {how}")
    clashes = double_booked(games)
    if clashes:
        warn(f"{len(clashes)} team-weeks hold two games; the app keeps only the first "
             f"of each and reports the rest as skipped rows")
        for w, t, v in clashes[:5]:
            warn(f"    week {w}: {t} -- {'; '.join(v)}")
        if not a.cfbd_weeks:
            warn("    unexpected with date numbering; please report this season/year")

    polls = []
    if not a.no_polls:
        print("  polls, AP and Coaches")
        try:
            polls = build_polls(get("/rankings", {"year": a.year,
                                                  "seasonType": "regular"}, key),
                                through_week_map(games))
        except SystemExit:
            raise
        except Exception as e:                       # a missing poll is not fatal
            warn(f"could not read polls ({e}); writing the season without them")
        if polls:
            graded = {p["poll"] for p in polls}
            wks = sorted({p["week"] for p in polls})
            print(f"     {len(polls)} poll snapshots, weeks {wks[0]}-{wks[-1]}"
                  f"  ({', '.join(sorted(graded))})")
            unknown = sorted({r["team"] for p in polls for r in p["ranks"]
                              if r["team"] not in teams})
            if unknown:
                warn(f"{len(unknown)} ranked teams are not in the team list: "
                     f"{', '.join(unknown[:5])}")

    for g in games:
        g.pop("_date", None)
        g.pop("_cfbd", None)
    games.sort(key=lambda g: (g["week"], g["home"]))
    payload = {"teams": list(teams.values()), "games": games}
    if polls:
        payload["polls"] = polls
    with open(a.out, "w") as f:
        json.dump(payload, f, indent=1)

    summarize(teams, games)
    if dropped:
        print(f"\n  excluded {len(dropped)} conference championship games:")
        for d in dropped[:6]:
            print(f"     {d}")
        if len(dropped) > 6:
            print(f"     ... and {len(dropped) - 6} more")
        print("     (pass --include-title-games to keep them)")
    if skipped:
        print(f"\n  skipped {len(skipped)} rows:")
        for s in skipped[:8]:
            print(f"     {s}")
        if len(skipped) > 8:
            print(f"     ... and {len(skipped) - 8} more")

    print(f"\nwrote {a.out}")
    print("Load it in the app's Data tab with the file picker.")


if __name__ == "__main__":
    main()
