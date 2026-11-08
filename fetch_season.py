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
import statistics
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


def upcoming_date(g):
    """US calendar date a fixture is expected on, or None.

    A played kickoff goes through game_date(), whose eight-hour shift puts a
    night game on its own US day. A fixture whose kickoff time is not set yet
    cannot: the feed stamps those 05:00Z -- midnight Eastern on the date it
    means -- and shifting that back eight hours would move every one of them a
    day early. So a TBD fixture is read as the plain date in its stamp. CFBD
    spells the flag `startTimeTBD` here and `startTimeTbd` in the basketball
    feed, so both are accepted.
    """
    if not pick(g, "startTimeTBD", "startTimeTbd", "start_time_tbd", default=False):
        return game_date(g)
    raw = pick(g, "startDate", "start_date")
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def dedupe_fixtures(rows):
    """Sort fixtures and drop the ones the feed filed twice.

    CFBD sometimes lists one fixture under two ids with home and away swapped
    -- 2026-27 has Morgan State and Towson both ways on 14 November -- so the
    pair is treated as unordered and only the first is kept. Sorting before
    the test rather than after makes which one survives depend on the fixture
    itself and not on the order the feed happened to return, so a refetch does
    not churn the file.

    Two fixtures for one team on one day against DIFFERENT opponents are left
    alone: those are the feed's claim about a real schedule, not a duplicate,
    and dropping one would quietly invent a bye.
    """
    rows.sort(key=lambda r: (r["date"], r["home"], r["away"]))
    out, seen = [], set()
    for r in rows:
        k = (r["date"], frozenset((r["home"], r["away"])))
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def build_upcoming(raw_games, teams, today=None, top="FBS"):
    """Fixtures still to be played, flat and plainly named.

    Kept out of `games` and written to their own file. The ranking reads only
    `games`, so a fixture can never reach a rating, and a visitor to the table
    does not pay to download a schedule only the team pages read.

    No week number is attached. Weeks here are renumbered from kickoff dates
    and then compacted to consecutive integers over the games actually played,
    so the mapping shifts as the season fills in; a fixture carries the date
    instead, which is both stable and what a reader wants to see anyway.

    A row that is neither completed nor in the future is an abandoned game
    rather than a fixture -- 2026 has several among the lower divisions -- so
    anything dated before today is dropped instead of advertised as to come.
    """
    if today is None:
        today = dt.date.today()

    def ranked(n):
        return (teams.get(n) or {}).get("division") == top

    out = []
    for g in raw_games:
        home = pick(g, "homeTeam", "home_team")
        away = pick(g, "awayTeam", "away_team")
        if not home or not away:
            continue
        if pick(g, "completed", default=False):
            continue
        if not ranked(home) and not ranked(away):
            continue
        d = upcoming_date(g)
        if d is None or d < today:
            continue
        row = {"home": home, "away": away, "date": d.isoformat()}
        if pick(g, "neutralSite", "neutral_site", default=False):
            row["neutral"] = 1
        if pick(g, "conferenceGame", "conference_game", default=False):
            row["conf"] = 1
        if pick(g, "startTimeTBD", "startTimeTbd", "start_time_tbd", default=False):
            row["tbd"] = 1
        out.append(row)
    return dedupe_fixtures(out)


def upcoming_path(out):
    """season-2026.json -> upcoming-2026.json, so the pair sits side by side."""
    base = os.path.basename(out)
    head = os.path.dirname(out)
    name = base.replace("season", "upcoming", 1) if "season" in base \
        else (base[:-5] + ".upcoming.json" if base.endswith(".json")
              else base + ".upcoming.json")
    return os.path.join(head, name) if head else name


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


# The feed's own label for the committee's ranking is not fixed across seasons or
# documented reliably, so match it rather than hard-code it, and store one stable
# name the app can ask for. Lower divisions run their own AP and coaches polls,
# which must not be mistaken for these -- hence the explicit reject list.
POLL_CANON = (
    ("AP Top 25", ("ap top 25", "ap poll")),
    ("Coaches Poll", ("coaches poll", "afca coaches poll", "usa today coaches poll")),
    ("CFP Rankings", ("playoff committee rankings", "cfp rankings", "cfp",
                      "college football playoff rankings", "playoff committee")),
)
POLL_REJECT = ("fcs", "division i-aa", "division ii", "division iii", "d2", "d3",
               "naia", "junior college", "juco")


def canon_poll(name):
    """The app's name for a feed poll, or None if it is not one we carry."""
    low = " ".join(str(name or "").lower().split())
    if not low:
        return None
    for bad in POLL_REJECT:
        if bad in low:
            return None
    for canon, forms in POLL_CANON:
        if low in forms or low == canon.lower():
            return canon
    return None


def build_polls(raw, through_of):
    """[{week, poll, throughWeek, ranks:[{rank, team}]}] for the polls we carry.

    The two human polls run all season; the committee's ranking only starts
    around week 10 and only exists from 2014 on, so seasons before that simply
    carry fewer poll names.

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
            name = canon_poll(pick(p, "poll", default=""))
            if name is None:
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



# ----------------------------------------------------------------------------
# ESPN's Football Power Index
# ----------------------------------------------------------------------------
def build_fpi(raw, teams, top="FBS"):
    """[{team, fpi, rank, sos, sor}] from ESPN's Football Power Index.

    These are the figures ESPN publishes on its own FPI page: the rating, the
    team's place by that rating, strength of schedule and strength of record.
    All three places are places in the field; `fpi` alone is a rating on ESPN's
    scale -- roughly the margin it expects against an average opponent -- and is
    carried as a number so the page can show it beside the place.

    Two cautions are built into the shape of what this returns.

    The feed has no week dimension. /ratings/fpi takes a year and hands back one
    snapshot -- today's -- and passing &week=N changes nothing in the response,
    which was checked rather than assumed. There is no way to ask what FPI said
    in week 6, so the app must not imply it knows: the caller stamps the payload
    with the day it was fetched, and the page shows these figures only at the
    live horizon.

    FPI is a margin-of-victory model and the ranking it sits beside refuses
    margin on purpose, so the two disagree by construction rather than by
    accident. Showing them together is the point. It is also exactly why none
    of this is allowed to reach the grader: it rides in its own key, which
    ingest() carries through to the page untouched.

    Rows outside the top division are dropped. FPI only rates FBS, and `teams`
    holds the lower divisions too by this point, so the test is on the division
    rather than on mere membership.
    """
    out = []
    for row in raw:
        name = pick(row, "team", "school")
        if not name or (teams.get(name) or {}).get("division") != top:
            continue
        res = pick(row, "resumeRanks", "resume_ranks", default={}) or {}
        rec = {"team": name}
        rating = pick(row, "fpi")
        if rating is not None:
            try:
                rec["fpi"] = round(float(rating), 2)
            except (TypeError, ValueError):
                pass
        # resumeRanks.fpi is the team's PLACE by FPI, not the rating again.
        for key, *names in (("rank", "fpi"),
                            ("sos", "strengthOfSchedule", "strength_of_schedule"),
                            ("sor", "strengthOfRecord", "strength_of_record")):
            v = pick(res, *names)
            if v is None:
                continue
            try:
                rec[key] = int(v)
            except (TypeError, ValueError):
                pass
        # A row carrying a name and nothing else says nothing. Dropping it is
        # better than three dashes, which would read as a team FPI declined to
        # rate rather than as a row that arrived empty.
        if len(rec) > 1:
            out.append(rec)
    out.sort(key=lambda r: (r.get("rank") if r.get("rank") is not None else 10 ** 6,
                            r["team"]))
    return out


def through_week_map(games):
    """CFBD week -> the renumbered week by which that week's games had been played.

    Uses the median of each week's renumbered weeks rather than the maximum. The
    feed occasionally files a stray game under the wrong week -- 2025 lists four
    D2/D3 playoff games played on 13 December as week 1 -- and taking the maximum
    let a single mis-filed row drag every poll's alignment to the end of the
    season, so a preseason ballot would be compared against a finished ranking.
    """
    buckets = {}
    for g in games:
        c = g.get("_cfbd")
        if c is not None:
            buckets.setdefault(c, []).append(g["week"])
    last = {c: statistics.median_low(v) for c, v in buckets.items()}

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
FIXTURE_UPCOMING = [
    # a plain fixture with a real kickoff time
    {"week": 12, "homeTeam": "Florida", "awayTeam": "Army",
     "homePoints": None, "awayPoints": None, "completed": False,
     "startDate": "2026-11-21T18:00:00.000Z", "conferenceGame": True},
    # a TBD fixture: 05:00Z is a placeholder for the stated date and must not
    # be shifted back a day the way a real night kickoff is
    {"week": 12, "homeTeam": "Florida Atlantic", "awayTeam": "Mercer",
     "homePoints": None, "awayPoints": None, "completed": False,
     "startDate": "2026-11-22T05:00:00.000Z", "startTimeTBD": True},
    # neutral site
    {"week": 13, "homeTeam": "Army", "awayTeam": "Florida Atlantic",
     "homePoints": None, "awayPoints": None, "completed": False,
     "startDate": "2026-11-28T20:00:00.000Z", "neutralSite": True},
    # already played: belongs in games, never here
    {"week": 1, "homeTeam": "Florida", "awayTeam": "Florida Atlantic",
     "homePoints": 31, "awayPoints": 17, "completed": True,
     "startDate": "2026-09-05T18:00:00.000Z"},
    # abandoned: unplayed but dated before today
    {"week": 3, "homeTeam": "Army", "awayTeam": "Florida",
     "homePoints": None, "awayPoints": None, "completed": False,
     "startDate": "2026-09-19T18:00:00.000Z"},
    # no FBS side, so nobody has a page for it
    {"week": 12, "homeTeam": "Tarleton State", "awayTeam": "Mercer",
     "homePoints": None, "awayPoints": None, "completed": False,
     "startDate": "2026-11-21T18:00:00.000Z"},
]

FIXTURE_FPI = [
    # a complete row: rating, place by rating, and the two resume places
    {"team": "Florida", "conference": "SEC", "fpi": 18.69,
     "resumeRanks": {"fpi": 4, "strengthOfSchedule": 69,
                     "strengthOfRecord": 11, "gameControl": 7}},
    # snake_case spellings, which the feed has used for these fields before
    {"team": "Army", "conference": "American Athletic", "fpi": -3.5,
     "resume_ranks": {"fpi": 70, "strength_of_schedule": 120,
                      "strength_of_record": 65}},
    # a negative rating and a missing SOR: the cell goes blank, the row stays
    {"team": "Florida Atlantic", "conference": "American Athletic",
     "fpi": -13.774, "resumeRanks": {"fpi": 118, "strengthOfSchedule": 104}},
    # FPI does not rate the lower divisions, but if a row ever appears it is
    # not ours to show: there is no page for it in the table
    {"team": "Mercer", "conference": "Southern", "fpi": -25.0,
     "resumeRanks": {"fpi": 140, "strengthOfSchedule": 9,
                     "strengthOfRecord": 130}},
    # nothing but a name: an empty row is dropped rather than rendered as a
    # team FPI declined to rate
    {"team": "Tarleton State"},
    # no name at all
    {"fpi": 1.0, "resumeRanks": {"fpi": 1}},
]

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
        {"poll": "Coaches Poll", "ranks": [{"school": "Florida", "rank": 3}]},
        {"poll": "Playoff Committee Rankings",
         "ranks": [{"school": "Army", "rank": 1}]},
        {"poll": "AFCA Division II Coaches Poll",
         "ranks": [{"school": "Ferris State", "rank": 1}]}]},
]


def selftest():
    teams = {}
    teams.update(build_teams(FIXTURE_TEAMS_FBS, "FBS"))
    teams.update(build_teams(FIXTURE_TEAMS_FCS, "FCS"))
    games, skipped, dropped = build_games(FIXTURE_GAMES, teams)
    kept_all, _, _ = build_games(FIXTURE_GAMES, dict(teams), drop_titles=False)
    soon = build_upcoming(FIXTURE_UPCOMING, teams, today=dt.date(2026, 11, 10))
    power = build_fpi(FIXTURE_FPI, teams)

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
    checks.append(("only top-division polls are kept, under canonical names",
                   {p["poll"] for p in polls}
                   == {"AP Top 25", "Coaches Poll", "CFP Rankings"}))
    checks.append(("both snapshots of a week are kept", len(polls) == 4))
    checks.append(("the committee ranking is filed under one stable name",
                   [p for p in polls if p["poll"] == "CFP Rankings"][0]["ranks"][0]["team"]
                   == "Army"))
    checks.append(("every documented spelling of the committee poll is matched",
                   all(canon_poll(n) == "CFP Rankings" for n in
                       ("CFP", "cfp rankings", "Playoff Committee Rankings",
                        "College Football Playoff Rankings", " Playoff  Committee "))))
    checks.append(("lower-division polls are never mistaken for their FBS namesakes",
                   all(canon_poll(n) is None for n in
                       ("FCS Coaches Poll", "AFCA Division II Coaches Poll",
                        "AP Division III Top 25", "NAIA Coaches Poll"))))
    checks.append(("an unknown poll is dropped rather than guessed at",
                   canon_poll("Some New Poll") is None and canon_poll("") is None))
    checks.append(("ranks parsed and sorted",
                   [r["rank"] for r in ap2["ranks"]] == [1, 2]
                   and ap2["ranks"][0]["team"] == "Florida"))
    checks.append(("preseason poll maps to no data",
                   [p for p in polls if p["week"] == 1][0]["throughWeek"] == 0))
    checks.append(("week-2 poll maps to the renumbered week holding CFBD week 1",
                   ap2["throughWeek"] == 2))
    # one row filed under the wrong week must not move anybody's alignment
    stray = [dict(g) for g in sg2]
    stray.append({"week": 99, "_cfbd": 1, "home": "X", "away": "Y",
                  "homePoints": 1, "awayPoints": 0, "_date": dt.date(2026, 12, 13)})
    checks.append(("one mis-filed game cannot drag the poll alignment",
                   through_week_map(stray)(1) == through_week_map(sg2)(1)))
    checks.append(("a played game never appears as a fixture",
                   not any(r["home"] == "Florida" and r["away"] == "Florida Atlantic"
                           for r in soon)))
    checks.append(("an unplayed row dated in the past is dropped, not advertised",
                   not any(r["date"] == "2026-09-19" for r in soon)))
    checks.append(("a fixture with no FBS side is left out",
                   not any(r["home"] == "Tarleton State" for r in soon)))
    checks.append(("the three real fixtures are kept", len(soon) == 3))
    checks.append(("a TBD fixture keeps its own date rather than shifting a day back",
                   any(r["away"] == "Mercer" and r["date"] == "2026-11-22" for r in soon)))
    checks.append(("a fixture with a real kickoff still goes through the shift",
                   any(r["away"] == "Army" and r["date"] == "2026-11-21" for r in soon)))
    checks.append(("neutral and conference games are marked",
                   any(r.get("neutral") for r in soon) and any(r.get("conf") for r in soon)))
    checks.append(("fixtures are sorted by date",
                   [r["date"] for r in soon] == sorted(r["date"] for r in soon)))
    checks.append(("a fixture carries no score and no week, so it cannot be graded",
                   all("week" not in r and "homePoints" not in r for r in soon)))
    checks.append(("one fixture filed both ways is kept once",
                   len(dedupe_fixtures([{"date": "2026-11-21", "home": "Florida",
                                         "away": "Army"},
                                        {"date": "2026-11-21", "home": "Army",
                                         "away": "Florida"}])) == 1))
    checks.append(("two different opponents on one day are both kept",
                   len(dedupe_fixtures([{"date": "2026-11-21", "home": "Florida",
                                         "away": "Army"},
                                        {"date": "2026-11-21", "home": "Florida",
                                         "away": "Mercer"}])) == 2))
    checks.append(("the power index keeps only top-division teams",
                   [r["team"] for r in power]
                   == ["Florida", "Army", "Florida Atlantic"]))
    checks.append(("the rating, its place, and both resume places are read",
                   power[0] == {"team": "Florida", "fpi": 18.69, "rank": 4,
                                "sos": 69, "sor": 11}))
    checks.append(("resumeRanks.fpi is read as a place, not as the rating again",
                   power[0]["rank"] == 4 and power[0]["fpi"] == 18.69))
    checks.append(("snake_case field names are read too",
                   power[1] == {"team": "Army", "fpi": -3.5, "rank": 70,
                                "sos": 120, "sor": 65}))
    checks.append(("a negative rating survives rounding",
                   power[2]["fpi"] == -13.77))
    checks.append(("a missing figure leaves its key out rather than guessing",
                   "sor" not in power[2] and "sos" in power[2]))
    checks.append(("a row with nothing but a name is dropped",
                   all(r["team"] != "Tarleton State" for r in power)))
    checks.append(("a row with no team name is dropped",
                   all(r.get("team") for r in power)))
    checks.append(("the power index is ordered by its own place",
                   [r["rank"] for r in power] == [4, 70, 118]))
    checks.append(("an unrated team sorts last rather than first",
                   [r["team"] for r in build_fpi(
                       [{"team": "Army", "fpi": 1.0},
                        {"team": "Florida", "fpi": 2.0,
                         "resumeRanks": {"fpi": 9}}], teams)]
                   == ["Florida", "Army"]))
    checks.append(("an empty feed gives an empty index, not an error",
                   build_fpi([], teams) == []))
    # The page hides these columns off the live horizon because of this: the
    # feed serves one snapshot per season and ignores a week parameter, so
    # there is nothing to align a rewound horizon against.
    checks.append(("the power index carries no week of its own",
                   all("week" not in r for r in power)))
    checks.append(("the power index never reaches the game list",
                   all(not (set(g) & {"fpi", "rank", "sos", "sor"}) for g in games)))
    checks.append(("the fixture file is named beside the season file",
                   upcoming_path("docs/season-2026.json") == "docs/upcoming-2026.json"))
    checks.append(("an oddly named output still gets a fixture file",
                   upcoming_path("out.json") == "out.upcoming.json"))
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
def ordered_teams(teams, top):
    """Deterministic team order: top division first, then the rest, each by name.

    The feed returns teams in no fixed order, and writing them out in insertion
    order made every refetch produce a diff of shuffled names even when nothing
    had changed. Games are already sorted; this gives the rest of the file the
    same property, so a diff only ever shows real news.
    """
    return sorted(teams.values(),
                  key=lambda t: (0 if t.get("division") == top else 1,
                                 t.get("division") or "",
                                 t.get("name") or ""))


def main():
    default_year = dt.date.today().year
    if dt.date.today().month < 6:          # Jan–May belongs to the prior season
        default_year -= 1

    p = argparse.ArgumentParser(description="Fetch a season from CollegeFootballData.com")
    p.add_argument("--year", type=int, default=default_year)
    p.add_argument("--out", default="season.json")
    p.add_argument("--upcoming-out", default=None,
                   help="where to write the fixtures (default: beside --out, "
                        "season-X.json -> upcoming-X.json)")
    p.add_argument("--no-upcoming", action="store_true",
                   help="skip the fixture file")
    p.add_argument("--postseason", action="store_true",
                   help="also include bowls and playoff, numbered after the regular season")
    p.add_argument("--no-fcs", action="store_true", help="top division only")
    p.add_argument("--no-polls", action="store_true",
                   help="skip the AP and Coaches poll fetch (saves one API call)")
    p.add_argument("--no-fpi", action="store_true",
                   help="skip the ESPN power index fetch (saves one API call)")
    p.add_argument("--cfbd-weeks", action="store_true",
                   help="keep CFBD's own week numbers instead of renumbering from "
                        "kickoff dates (CFBD folds Week 0 and Labor Day weekend into "
                        "one 'week 1', which costs the app one game per affected team)")
    p.add_argument("--include-title-games", action="store_true",
                   help="include conference championship games (excluded by default; "
                        "they are filed as regular season by CFBD)")
    p.add_argument("--list-polls", action="store_true",
                   help="print every poll name the feed returns for the year and exit, "
                        "with the name this script would file each one under")
    p.add_argument("--selftest", action="store_true", help="verify logic without a key")
    a = p.parse_args()

    if a.selftest:
        sys.exit(selftest())

    key = os.environ.get(KEY_ENV)
    if not key:
        die(f"{KEY_ENV} is not set.\n"
            f"  Get a free key at https://collegefootballdata.com/key, then:\n"
            f"    export {KEY_ENV}='your-key-here'")

    if a.list_polls:
        # The committee ranking's label is not stable enough to trust from
        # documentation, so this reads it straight out of the feed.
        rows = get("/rankings", {"year": a.year, "seasonType": "regular"}, key)
        seen = {}
        for row in rows:
            for poll in (pick(row, "polls", default=[]) or []):
                nm = pick(poll, "poll", default="")
                if not nm:
                    continue
                rec = seen.setdefault(nm, {"n": 0, "weeks": set(), "teams": 0})
                rec["n"] += 1
                rec["weeks"].add(pick(row, "week"))
                rec["teams"] = max(rec["teams"], len(pick(poll, "ranks", default=[]) or []))
        if not seen:
            print(f"{a.year}: the feed returned no polls at all")
            return
        print(f"{a.year}: {len(seen)} poll name(s) in the feed\n")
        width = max(len(n) for n in seen)
        for nm in sorted(seen):
            rec = seen[nm]
            wk = sorted(w for w in rec["weeks"] if w is not None)
            canon = canon_poll(nm)
            span = f"weeks {wk[0]}-{wk[-1]}" if wk else "no weeks"
            print(f"  {nm:<{width}}  {rec['n']:>3} snapshots, {span}, "
                  f"up to {rec['teams']} teams  ->  "
                  f"{canon if canon else 'not carried'}")
        return

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

    # Built here, from the same rows, before `raw` is reused for the postseason.
    soon = []
    if not a.no_upcoming:
        print("  fixtures still to play")
        soon = build_upcoming(raw, teams)
        if soon:
            print(f"     {len(soon)} fixtures, {soon[0]['date']} .. {soon[-1]['date']}"
                  f", {sum(1 for r in soon if r.get('tbd'))} without a kickoff time")
        else:
            warn("the feed listed no fixtures ahead of today")

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


    fpi = []
    if not a.no_fpi:
        print("  ESPN power index")
        try:
            fpi = build_fpi(get("/ratings/fpi", {"year": a.year}, key), teams)
        except SystemExit:
            raise
        except Exception as e:                  # a missing rating is not fatal
            warn(f"could not read the power index ({e}); "
                 f"writing the season without it")
        if fpi:
            whole = sum(1 for r in fpi
                        if all(k in r for k in ("fpi", "rank", "sos", "sor")))
            print(f"     {len(fpi)} teams rated, {whole} with all four figures")
            if whole < len(fpi):
                warn(f"{len(fpi) - whole} rated teams are missing a figure; "
                     f"those cells will read as blank")
        else:
            warn(f"the feed returned no power index for {a.year}; FPI data "
                 f"starts in 2005")
    for g in games:
        g.pop("_date", None)
        g.pop("_cfbd", None)
    games.sort(key=lambda g: (g["week"], g["home"]))
    payload = {"teams": ordered_teams(teams, 'FBS'), "games": games}
    if polls:
        payload["polls"] = polls
    if fpi:
        # Its own key, beside the polls and well clear of `games`. The day is
        # stamped because the feed has no week: without it the page cannot tell
        # a reader how current these figures are, and cannot know to hide them
        # when the horizon is rewound behind them.
        payload["fpi"] = {"asOf": dt.date.today().isoformat(), "teams": fpi}
    with open(a.out, "w") as f:
        json.dump(payload, f, indent=1)

    if not a.no_upcoming:
        # Written even when empty. A finished season has no fixtures left, and
        # saying so in a file is better than leaving none: the page can ask for
        # it unconditionally instead of guessing which seasons have one, and a
        # missing file stops meaning "not supported yet" and starts meaning
        # something is actually wrong.
        where = a.upcoming_out or upcoming_path(a.out)
        with open(where, "w") as f:
            json.dump({"sport": "football", "season": a.year,
                       "generated": dt.date.today().isoformat(),
                       "upcoming": soon}, f, separators=(",", ":"))
        print(f"wrote {where} ({len(soon)} fixtures)")

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
