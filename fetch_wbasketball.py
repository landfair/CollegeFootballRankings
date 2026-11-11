#!/usr/bin/env python3
"""Pull a women's college basketball season into the shape the app reads.

Usage:
    python3 fetch_wbasketball.py --season 2027 --out docs/ncaaw/season-2027.json

A season is named for the year it ENDS in, matching fetch_basketball.py: the
2026-27 season is --season 2027.

Where the data comes from, and why it is not the other two fetchers' source.
collegebasketballdata.com feeds football and the men's game, and it has no
women's basketball at all -- its OpenAPI document contains the word "women"
zero times and no endpoint takes a gender. So this reads the published season
files from sportsdataverse's wehoop data repository on GitHub instead:

    https://github.com/sportsdataverse/wehoop-wbb-data

Those files are ESPN-derived and arrive as parquet, one per season, 2001 to
the present. Reading them needs pyarrow, which is this repo's only third-party
Python requirement:

    pip install pyarrow

The schedule file carries the games:

    wbb/schedules/parquet/wbb_schedule_<season>.parquet

Division I membership and conference come off the GAMES, not off a team
directory, and that is deliberate. Each side of each game carries its own
conference id for that season, so Texas reads Big 12 in 2022-23 and SEC in
2025-26 without anybody having to remember the realignment. A current
directory would quietly relabel every archived season instead -- the sort of
error that never announces itself. A side with no conference id is not in
Division I that season.

Conference ids still need names, and those come from:

    wbb/standings/parquet/standings_<season>.parquet

which wehoop publishes for only a handful of seasons (2003-2007 and 2026-2027
at the time of writing), not for all of them. Since an ESPN conference id is
stable across seasons, the names are merged from the few files that exist and
applied to all. Checked rather than hoped: that merged map covers every
conference id appearing in any of 2021-22 through 2026-27, with none left
unnamed, and the old Pac-12 is named out of the 2007 file long after it
stopped existing.

NO POLLS. The AP and Coaches polls are shown beside the football and men's
rankings; they are absent here because wehoop publishes no rankings file. The
schedule does carry each side's rank at tip-off, and reconstructing weekly
snapshots from it was tried and rejected: over 2025-26 only 8 of 22 weeks
recover all 25 places, the week of 22 December recovers 14, and 10 weeks show
two teams sharing a place because the window straddles two polls. A partial
poll compared against a complete ranking would make the agreement figure a
measurement of the gaps rather than of the rankings, so the page shows no poll
rather than a poll-shaped guess. ESPN's core API does publish these per week
and could fill the hole later; that is a separate piece of work.

The output is the payload every other season file uses, so docs/engine.js
reads it unchanged:

    {sport, topDivision, teams[], games[], dates[], postseason[]}
"""

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from collections import Counter, defaultdict

RAW = ("https://raw.githubusercontent.com/sportsdataverse/wehoop-wbb-data/"
       "main/wbb")
SCHEDULE = RAW + "/schedules/parquet/wbb_schedule_{season}.parquet"
STANDINGS = RAW + "/standings/parquet/standings_{season}.parquet"

# Seasons whose standings files are read purely to collect conference NAMES.
# wehoop publishes standings for 2003-2007 and 2026-2027 only, so the recent
# pair names today's conferences and 2007 names the ones that have since
# folded -- the old Pac-12 among them. Verified to leave no conference id
# unnamed across 2021-22 through 2026-27.
MAP_SEASONS = (2027, 2026, 2007)

TOP_DIVISION = "D1"

# Tip-offs are stamped UTC and the late ones run past midnight there: a 10:30pm
# Eastern game on the Pacific coast is stamped 03:30Z the NEXT day. Slicing the
# first ten characters of that string files it under a day nobody played it on,
# which puts it in its own bucket, gives those teams a phantom extra game day
# and -- because the engine holds one game per team per bucket -- can split a
# team's real slate across two. Shifting back 8 hours before taking the date
# lands every game on the calendar day it was actually played. This is the same
# constant and the same reasoning as fetch_basketball.py, which had it from the
# start; this fetcher did not, and quietly misfiled every late West Coast game
# until a Wednesday morning pull turned up 30 games dated that afternoon.
UTC_SHIFT = dt.timedelta(hours=8)

# ESPN's season types, which wehoop carries through unchanged.
REGULAR, POSTSEASON = 2, 3

_warnings = 0


def warn(msg):
    global _warnings
    _warnings += 1
    print(f"  ! {msg}", file=sys.stderr)


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def download(url, dest):
    """Fetch one file to dest. Plain GitHub raw; no key, no rate limit worth
    pacing for. Kept out of the repo tree -- these are somebody else's bytes."""
    try:
        with urllib.request.urlopen(url, timeout=120) as r, \
             open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            die(f"no file at {url}\n       "
                f"(wehoop may not publish that season yet)")
        die(f"{e.code} fetching {url}")
    except Exception as e:                                   # noqa: BLE001
        die(f"could not fetch {url} ({e})")
    return dest


def read_parquet(path):
    """{column: [values]}. Imported here so --selftest runs without pyarrow."""
    try:
        import pyarrow.parquet as pq
    except ImportError:
        die("pyarrow is not installed. This fetcher reads parquet:\n"
            "           pip install pyarrow")
    return pq.read_table(path).to_pydict()


# ---------------------------------------------------------------------------
# the season's shape


def conference_names(cols_list):
    """{conference id: short name}, merged over whatever standings were read."""
    out = {}
    for cols in cols_list:
        n = len(cols.get("group_id") or [])
        for i in range(n):
            gid = cols["group_id"][i]
            nm = (cols.get("group_short_name") or [None] * n)[i] \
                or (cols.get("group_name") or [None] * n)[i]
            if gid is None or not nm:
                continue
            out.setdefault(str(int(gid)), str(nm).strip())
    return out


def build_directory(cols):
    """(teams, by_id) from one season's standings table, when it exists.

    This is a seed, not the authority: it adds the Division I sides that have
    not played yet, so an empty early-season file still lists the division.
    Seasons wehoop publishes no standings for skip this entirely and take
    everything off the games, which is why it must not be the only source.
    """
    teams, by_id = {}, {}
    rows = len(cols.get("team_id") or [])
    for i in range(rows):
        name = (cols["team_location"][i] or "").strip()
        if not name or PLACEHOLDER(name):
            continue
        tid = str(cols["team_id"][i])
        conf = (cols.get("group_short_name") or [None] * rows)[i] \
            or (cols.get("group_name") or [None] * rows)[i] or ""
        by_id[tid] = name
        if name not in teams:
            teams[name] = {"name": name, "conference": str(conf).strip(),
                           "division": TOP_DIVISION}
    return teams, by_id


def parse_schedule(cols, teams, by_id, confnames=None):
    """(regular, postseason, upcoming, skipped) from a schedule table.

    season_type 2 is the regular season and 3 the postseason. Conference
    tournaments sit under 2 -- ESPN tags them TRNMNT but leaves them in the
    regular season -- which is the behaviour this repo wants: football counts
    conference title games as ordinary games and so should this. Only the
    national postseason (NCAA, WNIT, WBIT) is type 3, and the ranking never
    sees those.
    """
    reg, post, soon, skipped = [], [], [], []
    n = len(cols.get("id") or [])

    def col(name, default=None):
        return cols.get(name) or [default] * n

    done = col("status_type_completed", False)
    dates = col("date", "")
    stype = col("season_type", REGULAR)
    heads = col("notes_headline", "")
    for i in range(n):
        home = (col("home_location")[i] or "").strip()
        away = (col("away_location")[i] or "").strip()
        label = f"{away} at {home}" if home and away else f"row {i}"
        if not home or not away:
            skipped.append(f"{label}: missing a side")
            continue
        date = game_day(dates[i])
        if date is None:
            skipped.append(f"{label}: no usable date")
            continue
        day = date.isoformat()

        # A bracket slot whose occupant is not known yet is carried as a team
        # called TBD on a negative id. Two of those would collide into one
        # "team" with a schedule, which is worse than simply not being there.
        if PLACEHOLDER(home) or PLACEHOLDER(away):
            skipped.append(f"{label}: an unfilled bracket slot, not a fixture")
            continue

        if not done[i]:
            soon.append({"date": day, "home": home, "away": away,
                         "tbd": len(dates[i] or "") <= 10})
            continue

        # Teams are registered from games that were actually PLAYED. The
        # standings file already supplies every Division I side; anyone else
        # earns a row by appearing in a result, which keeps the team list from
        # filling up with small colleges a Division I side is merely booked
        # against. It still counts as an opponent when it does play -- the
        # engine scores the game and the worst-loss column prints NR for it.
        for side, tid_key, cid_key in ((home, "home_id", "home_conference_id"),
                                      (away, "away_id", "away_conference_id")):
            by_id.setdefault(str(col(tid_key)[i]), side)
            cid = col(cid_key)[i]
            cid = str(int(cid)) if cid is not None else None
            info = teams.get(side)
            if info is None:
                info = teams[side] = {"name": side, "conference": "",
                                      "division": "OTHER"}
            if cid:
                # A conference id that season IS the Division I membership
                # test. Anything without one is a smaller school, and stays
                # OTHER -- it still counts as an opponent when it plays, and
                # the worst-loss column prints NR against it.
                info["division"] = TOP_DIVISION
                if not info["conference"]:
                    info["conference"] = (confnames or {}).get(cid, "")

        hp, ap = col("home_score")[i], col("away_score")[i]
        if hp is None or ap is None:
            skipped.append(f"{label}: final but no score")
            continue
        hp, ap = int(hp), int(ap)
        if hp == ap:
            # Basketball does not tie. One that reaches here is bad data, and
            # a tie cannot be graded a win or a loss, so it goes loudly.
            skipped.append(f"{label}: tie, cannot be ordered")
            continue
        row = {"home": home, "away": away,
               "homePoints": hp, "awayPoints": ap, "_date": date}
        if stype[i] == POSTSEASON:
            post.append({**row, "date": day,
                         "tournament": tournament_of(heads[i])})
        elif stype[i] == REGULAR:
            reg.append(row)
        else:
            # Type 1 is the preseason: exhibitions against clubs and alumni
            # sides. They are played and they are final, and they are not
            # results anybody means to rank.
            skipped.append(f"{label}: season type {stype[i]}, not a counted game")
    return reg, post, soon, skipped


def game_day(stamp):
    """Calendar day a game was played, from an ISO timestamp, or None.

    Handles both the offset-bearing form ESPN sends through wehoop
    (2026-11-11T03:30Z) and a bare date, and shifts by UTC_SHIFT so a late
    tip-off counts on the day it started rather than the UTC day it ended.
    """
    raw = str(stamp or "").strip()
    if not raw:
        return None
    s = raw.replace("Z", "+0000")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M%z",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            t = dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
        if t.tzinfo is not None:
            t = t.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return (t - UTC_SHIFT).date()
    try:
        return dt.date.fromisoformat(raw[:10])
    except ValueError:
        return None


def PLACEHOLDER(name):
    """True for a stand-in that is not a school.

    wehoop carries unfilled bracket slots as a team literally called TBD on a
    negative id. Several of them share that one name, so left alone they merge
    into a single phantom team with a schedule of its own.
    """
    return (name or "").strip().upper() in ("TBD", "TBA", "")


def tournament_of(headline):
    """'NCAA', 'WNIT', 'WBIT' or the headline's own first phrase.

    bracket() in engine.js filters on this string, so it has to be stable
    rather than whatever ESPN wrote that year.
    """
    h = (headline or "").upper()
    for tag in ("NCAA", "WBIT", "WNIT"):
        if tag in h:
            return tag
    return (headline or "").split(" - ")[0].strip() or "OTHER"


def assign_days(games):
    """Number each distinct game day 1..N in order. Returns (games, dates).

    Buckets are days, not weeks, for the reason the men's file gives: teams
    play two or three times a week, and the engine holds one game per team per
    bucket, so weekly buckets would discard nearly half the season.
    """
    days = sorted({g["_date"] for g in games})
    index = {d: i + 1 for i, d in enumerate(days)}
    for g in games:
        g["week"] = index[g["_date"]]
    return games, [d.isoformat() for d in days]


def double_booked(games):
    seen = Counter()
    for g in games:
        seen[(g["week"], g["home"])] += 1
        seen[(g["week"], g["away"])] += 1
    return [k for k, v in sorted(seen.items()) if v > 1]


def name_collisions(by_id):
    """[(name, [ids])] where two ESPN team ids share one school name."""
    back = defaultdict(list)
    for tid, nm in by_id.items():
        back[nm].append(tid)
    return [(nm, sorted(ids)) for nm, ids in sorted(back.items()) if len(ids) > 1]


def dedupe_fixtures(rows):
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: (r["date"], r["home"], r["away"])):
        k = (r["date"], r["home"], r["away"])
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def upcoming_path(out):
    """season-2027.json -> upcoming-2027.json, beside it."""
    head, tail = os.path.split(out)
    if tail.startswith("season-"):
        tail = "upcoming-" + tail[len("season-"):]
    else:
        root, ext = os.path.splitext(tail)
        tail = root + "-upcoming" + (ext or ".json")
    return os.path.join(head, tail)


def ordered_teams(teams, top):
    """Top division first, then the rest; alphabetical inside each."""
    return sorted(teams.values(),
                  key=lambda t: (t["division"] != top, t["name"]))


def summarize(teams, games, dates):
    div = Counter(t["division"] for t in teams.values())
    played = Counter()
    for g in games:
        played[g["home"]] += 1
        played[g["away"]] += 1
    d1 = [t for t, i in teams.items() if i["division"] == TOP_DIVISION]
    counts = sorted(played[t] for t in d1 if played[t])
    per = Counter(g["week"] for g in games)
    sizes = sorted(per.values())
    print(f"\n  teams        {len(teams)}  "
          f"({', '.join(f'{k}: {v}' for k, v in sorted(div.items()))})")
    print(f"  games        {len(games)}")
    print(f"  game days    {len(dates)}   {dates[0]} .. {dates[-1]}")
    print(f"  games/day    min {sizes[0]}, median {sizes[len(sizes)//2]}, "
          f"max {sizes[-1]}")
    if counts:
        print(f"  top-division games played   min {counts[0]}, median "
              f"{counts[len(counts)//2]}, max {counts[-1]}")


# ---------------------------------------------------------------------------
# offline self-test -- no network, no pyarrow

def _cols(rows, extra=None):
    """Turn a list of row dicts into the column dict read_parquet returns."""
    keys = set(extra or [])
    for r in rows:
        keys |= set(r)
    return {k: [r.get(k) for r in rows] for k in sorted(keys)}


STANDINGS_ROWS = [
    {"team_id": 41, "team_location": "UConn",
     "group_short_name": "Big East", "group_name": "Big East Conference"},
    {"team_id": 41, "team_location": "UConn",          # repeated per stat
     "group_short_name": "Big East", "group_name": "Big East Conference"},
    {"team_id": 251, "team_location": "Texas",
     "group_short_name": "SEC", "group_name": "Southeastern Conference"},
    {"team_id": 26, "team_location": "UCLA",
     "group_short_name": "Big Ten", "group_name": "Big Ten Conference"},
    {"team_id": 2579, "team_location": "South Carolina",
     "group_short_name": "SEC", "group_name": "Southeastern Conference"},
    {"team_id": 68, "team_location": "Boise State",
     "group_short_name": None, "group_name": "Mountain West Conference"},
    {"team_id": 0, "team_location": "", "group_short_name": "X",
     "group_name": "X"},                                # nameless, dropped
]

SCHEDULE_ROWS = [
    {"id": 1, "date": "2026-11-04T00:00Z", "status_type_completed": True,
     "season_type": 2, "home_id": 41, "home_location": "UConn",
     "away_id": 251, "away_location": "Texas",
     "home_score": 88, "away_score": 70, "notes_headline": None},
    {"id": 2, "date": "2026-11-04T00:00Z", "status_type_completed": True,
     "season_type": 2, "home_id": 26, "home_location": "UCLA",
     "away_id": 2579, "away_location": "South Carolina",
     "home_score": 60, "away_score": 75, "notes_headline": None},
    # a Division I side against somebody not in the standings file
    {"id": 3, "date": "2026-11-06T00:00Z", "status_type_completed": True,
     "season_type": 2, "home_id": 68, "home_location": "Boise State",
     "away_id": 9001, "away_location": "Carroll College",
     "home_score": 61, "away_score": 70, "notes_headline": None},
    # not played yet -> a fixture, never a result
    {"id": 4, "date": "2026-11-08", "status_type_completed": False,
     "season_type": 2, "home_id": 150, "home_location": "Duke",
     "away_id": 2305, "away_location": "Kansas",
     "home_score": None, "away_score": None, "notes_headline": None},
    # final but scoreless -> dropped loudly
    {"id": 5, "date": "2026-11-08T00:00Z", "status_type_completed": True,
     "season_type": 2, "home_id": 2294, "home_location": "Iowa",
     "away_id": 194, "away_location": "Ohio State",
     "home_score": None, "away_score": None, "notes_headline": None},
    # a tie, which basketball does not have
    {"id": 6, "date": "2026-11-09T00:00Z", "status_type_completed": True,
     "season_type": 2, "home_id": 239, "home_location": "Baylor",
     "away_id": 2628, "away_location": "TCU",
     "home_score": 70, "away_score": 70, "notes_headline": None},
    # the national postseason, which the ranking must never see
    {"id": 7, "date": "2027-03-20T00:00Z", "status_type_completed": True,
     "season_type": 3, "home_id": 41, "home_location": "UConn",
     "away_id": 150, "away_location": "Duke",
     "home_score": 80, "away_score": 61,
     "notes_headline": "NCAA Women's Basketball Championship - 1st Round"},
    {"id": 8, "date": "2027-03-21T00:00Z", "status_type_completed": True,
     "season_type": 3, "home_id": 2390, "home_location": "Miami",
     "away_id": 108, "away_location": "Harvard",
     "home_score": 66, "away_score": 64, "notes_headline": "WBIT - 2nd Round"},
    # a conference tournament, which IS a regular-season game here
    {"id": 9, "date": "2027-03-07T00:00Z", "status_type_completed": True,
     "season_type": 2, "home_id": 251, "home_location": "Texas",
     "away_id": 99, "away_location": "LSU",
     "home_score": 71, "away_score": 68,
     "notes_headline": "SEC Tournament - Final"},
    # preseason exhibition -> counted by nobody
    {"id": 10, "date": "2026-10-28T00:00Z", "status_type_completed": True,
     "season_type": 1, "home_id": 41, "home_location": "UConn",
     "away_id": 9002, "away_location": "Some Club",
     "home_score": 99, "away_score": 40, "notes_headline": None},
    # missing a side
    {"id": 11, "date": "2026-11-10T00:00Z", "status_type_completed": True,
     "season_type": 2, "home_id": 7, "home_location": "Only One",
     "away_id": None, "away_location": None,
     "home_score": 50, "away_score": None, "notes_headline": None},
]


def selftest():
    checks, failed = 0, 0

    def ok(cond, label):
        nonlocal checks, failed
        checks += 1
        print(("  pass  " if cond else "  FAIL  ") + label)
        if not cond:
            failed += 1

    print("fetch_wbasketball.py self-test\n")

    teams, by_id = build_directory(_cols(STANDINGS_ROWS))
    ok(len(teams) == 5,
       f"five distinct schools come out of seven standings rows (got {len(teams)})")
    ok("" not in teams, "a nameless standings row is dropped")
    ok(teams["UConn"]["conference"] == "Big East",
       "the short conference name is preferred")
    ok(teams["Boise State"]["conference"] == "Mountain West Conference",
       "a missing short name falls back to the long one")
    ok(all(t["division"] == TOP_DIVISION for t in teams.values()),
       "everyone in the standings file is Division I by construction")
    ok(by_id["41"] == "UConn", "team ids map to school names")

    reg, post, soon, skipped = parse_schedule(_cols(SCHEDULE_ROWS), teams, by_id)

    ok(len(reg) == 4, f"four regular-season results survive (got {len(reg)})")
    ok(len(post) == 2, f"two postseason games are set aside (got {len(post)})")
    ok(len(soon) == 1, f"one fixture is still to play (got {len(soon)})")
    ok(len(skipped) == 4, f"four rows are dropped with a reason (got {len(skipped)})")
    ok(not any(g["home"] == "Duke" for g in reg + post),
       "an unplayed game is never a result")
    ok(soon[0]["home"] == "Duke" and soon[0]["tbd"] is True,
       "a date with no tip time is marked tbd")
    ok(any("no score" in s for s in skipped),
       "a final with no score is reported, not silently dropped")
    ok(any("tie" in s for s in skipped), "a tie is reported")
    ok(any("missing a side" in s for s in skipped), "a one-sided row is reported")
    ok(any("season type 1" in s for s in skipped),
       "a preseason exhibition is excluded and said so")

    ok(any(g["home"] == "Texas" and g["away"] == "LSU" for g in reg),
       "a conference tournament final counts as a regular game")
    ok({p["tournament"] for p in post} == {"NCAA", "WBIT"},
       "postseason games are tagged by event")
    ok(all("date" in p for p in post), "a postseason row keeps a plain date")

    ok(teams["Carroll College"]["division"] == "OTHER",
       "an opponent absent from the standings is not Division I")
    ok(teams["Carroll College"]["conference"] == "",
       "and carries no conference name")
    ok(teams["UConn"]["division"] == TOP_DIVISION,
       "a standings team stays Division I after the schedule is read")

    # day bucketing
    g2, dates = assign_days([dict(r) for r in reg])
    ok(dates == sorted(dates), "game days come out in order")
    ok(all(1 <= g["week"] <= len(dates) for g in g2),
       "every game lands in a numbered bucket")
    ok(len(dates) == len({g["_date"] for g in reg}),
       "one bucket per distinct day, no more")

    same = [{"week": 1, "home": "A", "away": "B"},
            {"week": 1, "home": "A", "away": "C"}]
    ok(double_booked(same) == [(1, "A")], "a team twice in one bucket is caught")
    ok(double_booked([{"week": 1, "home": "A", "away": "B"}]) == [],
       "a clean day reports nothing")

    ok(tournament_of("NCAA Women's Basketball Championship - Final Four") == "NCAA",
       "the NCAA tournament is recognised")
    ok(tournament_of("WBIT - Championship") == "WBIT", "the WBIT is recognised")
    ok(tournament_of("WNIT - 1st Round") == "WNIT", "the WNIT is recognised")
    ok(tournament_of("Some Other Event - Round 1") == "Some Other Event",
       "an unknown event keeps its own name")
    ok(tournament_of("") == "OTHER", "a missing headline does not crash")
    ok(tournament_of(None) == "OTHER", "a null headline does not crash")

    cn = conference_names([_cols([
        {"group_id": 7, "group_short_name": "Big Ten",
         "group_name": "Big Ten Conference"},
        {"group_id": 21, "group_short_name": None, "group_name": "Pac-12 Conference"},
        {"group_id": 7, "group_short_name": "IGNORED", "group_name": "x"},
        {"group_id": None, "group_short_name": "nope", "group_name": "nope"}])])
    ok(cn == {"7": "Big Ten", "21": "Pac-12 Conference"},
       f"conference names merge, short wins, first wins, nulls drop ({cn})")

    # division comes off the game's own conference id, not a directory
    t4, b4 = {}, {}
    parse_schedule(_cols([
        {"id": 1, "date": "2023-02-18T00:00Z", "status_type_completed": True,
         "season_type": 2, "home_id": 251, "home_location": "Texas",
         "home_conference_id": 8, "away_id": 26, "away_location": "UCLA",
         "away_conference_id": 21, "home_score": 70, "away_score": 60,
         "notes_headline": None},
        {"id": 2, "date": "2023-02-20T00:00Z", "status_type_completed": True,
         "season_type": 2, "home_id": 251, "home_location": "Texas",
         "home_conference_id": 8, "away_id": 9001, "away_location": "Tiny College",
         "away_conference_id": None, "home_score": 90, "away_score": 40,
         "notes_headline": None}]),
        t4, b4, {"8": "Big 12", "21": "Pac-12"})
    ok(t4["Texas"]["conference"] == "Big 12" and t4["Texas"]["division"] == "D1",
       "a side is placed in the conference its GAME records, not today's")
    ok(t4["UCLA"]["conference"] == "Pac-12",
       "so a 2023 game still says Pac-12 years after it folded")
    ok(t4["Tiny College"]["division"] == "OTHER",
       "a side with no conference id that season is not Division I")

    # UTC shift: a late tip-off must count on the day it started
    ok(game_day("2026-11-11T03:30Z") == dt.date(2026, 11, 10),
       "a 03:30Z game counts on the previous day (10:30pm Eastern)")
    ok(game_day("2026-11-11T02:00Z") == dt.date(2026, 11, 10),
       "so does an 02:00Z one (9pm Eastern)")
    ok(game_day("2026-11-10T17:00Z") == dt.date(2026, 11, 10),
       "an afternoon game stays on its own day")
    ok(game_day("2026-11-11T08:00Z") == dt.date(2026, 11, 11),
       "and 08:00Z is the boundary, landing on the new day")
    ok(game_day("2026-11-10") == dt.date(2026, 11, 10),
       "a bare date with no time is taken as given")
    ok(game_day("") is None and game_day(None) is None,
       "an empty stamp yields no date rather than crashing")
    ok(game_day("not a date") is None, "so does nonsense")

    # the whole point: two games either side of UTC midnight share a bucket
    t5, b5 = build_directory(_cols(STANDINGS_ROWS))
    r5, _, _, _ = parse_schedule(_cols([
        {"id": 1, "date": "2026-11-11T02:00Z", "status_type_completed": True,
         "season_type": 2, "home_id": 41, "home_location": "UConn",
         "home_conference_id": 4, "away_id": 251, "away_location": "Texas",
         "away_conference_id": 23, "home_score": 70, "away_score": 60,
         "notes_headline": None},
        {"id": 2, "date": "2026-11-10T18:00Z", "status_type_completed": True,
         "season_type": 2, "home_id": 26, "home_location": "UCLA",
         "home_conference_id": 7, "away_id": 2579, "away_location": "South Carolina",
         "away_conference_id": 23, "home_score": 80, "away_score": 61,
         "notes_headline": None}]), t5, b5, {"4": "Big East", "7": "Big Ten", "23": "SEC"})
    _, d5 = assign_days(r5)
    ok(len(d5) == 1 and d5[0] == "2026-11-10",
       f"a 02:00Z game and an 18:00Z one the day before share one bucket ({d5})")

    ok(PLACEHOLDER("TBD") and PLACEHOLDER("tba") and PLACEHOLDER(""),
       "an unfilled bracket slot is recognised as not a school")
    ok(not PLACEHOLDER("Texas") and not PLACEHOLDER(None) is False,
       "a real school is not a placeholder")
    tb, tbi = build_directory(_cols(STANDINGS_ROWS))
    r3, p3, s3, k3 = parse_schedule(_cols([
        {"id": 90, "date": "2027-03-25T00:00Z", "status_type_completed": False,
         "season_type": 3, "home_id": -1, "home_location": "TBD",
         "away_id": -2, "away_location": "TBD",
         "home_score": None, "away_score": None, "notes_headline": "NCAA"}]),
        tb, tbi)
    ok(s3 == [] and "TBD" not in tb,
       "a TBD-versus-TBD slot becomes neither a fixture nor a team")

    ok(name_collisions({"1": "Miami", "2": "Miami (OH)"}) == [],
       "two distinctly named schools are not a collision")
    ok(name_collisions({"1": "Miami", "2": "Miami"}) == [("Miami", ["1", "2"])],
       "one name on two ESPN ids IS a collision")

    dd = dedupe_fixtures([{"date": "2026-11-08", "home": "A", "away": "B", "tbd": False},
                          {"date": "2026-11-08", "home": "A", "away": "B", "tbd": True}])
    ok(len(dd) == 1, "a fixture listed twice is written once")

    tt = {"Z": {"name": "Z", "division": "D1"},
          "A": {"name": "A", "division": "OTHER"},
          "M": {"name": "M", "division": "D1"}}
    names = [t["name"] for t in ordered_teams(tt, TOP_DIVISION)]
    ok(names == ["M", "Z", "A"], f"top division sorts first, then the rest ({names})")

    ok(upcoming_path("docs/ncaaw/season-2027.json") ==
       os.path.join("docs/ncaaw", "upcoming-2027.json"),
       "the fixture file is named beside the season file")
    ok(upcoming_path("odd.json") == "odd-upcoming.json",
       "an oddly named output still gets a fixture file")

    payload = {"sport": "wbasketball", "topDivision": TOP_DIVISION,
               "teams": ordered_teams(teams, TOP_DIVISION),
               "games": [{k: v for k, v in g.items() if k != "_date"} for g in g2],
               "dates": dates}
    ok(all(set(g) == {"week", "home", "away", "homePoints", "awayPoints"}
           for g in payload["games"]),
       "a game row carries exactly the five fields the engine reads")
    ok(all(isinstance(g["homePoints"], int) and isinstance(g["awayPoints"], int)
           for g in payload["games"]),
       "scores are numbers")
    ok(not any(k in g for g in payload["games"] for k in ("tournament", "date")),
       "no postseason field reaches the game list")
    ok(payload["topDivision"] == "D1",
       "topDivision matches what the engine filters on")
    ok(all(set(t) == {"name", "conference", "division"} for t in payload["teams"]),
       "a team row carries exactly the three fields the engine reads")

    # the inertness that matters most: nothing unplayed or postseason is graded
    graded = {(g["home"], g["away"]) for g in payload["games"]}
    ok(not (graded & {(p["home"], p["away"]) for p in post}),
       "no postseason game appears in the graded list")
    ok(not (graded & {(s["home"], s["away"]) for s in soon}),
       "no fixture appears in the graded list")

    print(f"\n{checks} checks, {failed} failed")
    if failed:
        return 1
    print("all checks passed")
    return 0


# ---------------------------------------------------------------------------


def main():
    p = argparse.ArgumentParser(
        description="Fetch a women's college basketball season (wehoop/GitHub)")
    p.add_argument("--season", type=int, default=dt.date.today().year,
                   help="the year the season ENDS in (2026-27 is 2027)")
    p.add_argument("--out", default="season.json")
    p.add_argument("--upcoming-out", default=None,
                   help="where to write the fixtures (default: beside --out)")
    p.add_argument("--no-upcoming", action="store_true")
    p.add_argument("--cache-dir", default=None,
                   help="keep the downloaded parquet here instead of a temp "
                        "directory, to avoid re-fetching")
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args()
    if a.selftest:
        sys.exit(selftest())

    tmp = a.cache_dir or tempfile.mkdtemp(prefix="wehoop-")
    os.makedirs(tmp, exist_ok=True)
    print(f"fetching {a.season-1}-{str(a.season)[2:]} women's season "
          f"from the wehoop data repository")

    def fetch(url, dest, what):
        if os.path.exists(dest) and os.path.getsize(dest) > 1024:
            print(f"  {what}: cached")
            return dest
        print(f"  {what}: downloading")
        return download(url, dest)

    sched_f = fetch(SCHEDULE.format(season=a.season),
                    os.path.join(tmp, f"wbb_schedule_{a.season}.parquet"),
                    "schedule")

    # Conference NAMES come from whatever standings files exist, not from this
    # season's -- wehoop publishes them for only a handful of years. The ids
    # themselves are per game and per season, so the names are the only thing
    # borrowed across seasons, and an ESPN conference id does not change
    # meaning. 2007 is in the list to name conferences that have since folded.
    print("  conference names")
    maps = []
    for yr in MAP_SEASONS:
        dest = os.path.join(tmp, f"standings_{yr}.parquet")
        try:
            if not (os.path.exists(dest) and os.path.getsize(dest) > 1024):
                download(STANDINGS.format(season=yr), dest)
            maps.append(read_parquet(dest))
        except SystemExit:
            continue                       # that year is simply not published
    confnames = conference_names(maps)
    if not confnames:
        die("could not name a single conference; wehoop's standings files "
            "may have moved")
    print(f"     {len(confnames)} conference ids named")

    # This season's own standings, when published, seeds the Division I sides
    # that have not played yet. Absent, everything comes off the games.
    teams, by_id = {}, {}
    own = os.path.join(tmp, f"standings_{a.season}.parquet")
    if os.path.exists(own) and os.path.getsize(own) > 1024:
        teams, by_id = build_directory(read_parquet(own))
        print(f"     seeded {len(teams)} teams from this season's standings")

    print("  games")
    reg, post, soon, skipped = parse_schedule(read_parquet(sched_f), teams,
                                              by_id, confnames)
    if not reg:
        die(f"no completed regular-season games for {a.season}")
    if skipped:
        print(f"     {len(skipped)} rows skipped")
        for s in skipped[:5]:
            warn(s)

    collide = name_collisions(by_id)
    if collide:
        # Two ids under one school name would silently merge two teams into
        # one row, which is far worse than a loud stop.
        warn(f"{len(collide)} school names cover two team ids each:")
        for nm, ids in collide[:5]:
            warn(f"    {nm}: {', '.join(ids)}")

    reg, dates = assign_days(reg)
    print(f"     {len(reg)} results over {len(dates)} game days")

    clashes = double_booked(reg)
    if clashes:
        warn(f"{len(clashes)} team-days hold two games; the app keeps only the first")
        for w, t in clashes[:5]:
            warn(f"    day {w} ({dates[w-1]}): {t}")

    if post:
        kinds = Counter(p["tournament"] for p in post)
        print(f"  postseason   {len(post)} games "
              f"({', '.join(f'{k}: {v}' for k, v in sorted(kinds.items()))})")

    soon = dedupe_fixtures([s for s in soon
                            if s["date"] >= dt.date.today().isoformat()])
    if not a.no_upcoming:
        print(f"  fixtures     {len(soon)}" +
              (f", {soon[0]['date']} .. {soon[-1]['date']}" if soon else ""))

    for g in reg:
        g.pop("_date", None)
    for g in post:
        g.pop("_date", None)
    reg.sort(key=lambda g: (g["week"], g["home"]))
    post.sort(key=lambda g: (g["date"], g["home"]))

    # No `polls` key. See the module docstring: wehoop publishes no rankings
    # file, and the page would rather show nothing than a reconstructed poll.
    payload = {"sport": "wbasketball", "topDivision": TOP_DIVISION,
               "teams": ordered_teams(teams, TOP_DIVISION),
               "games": reg, "dates": dates}
    if post:
        payload["postseason"] = post
    with open(a.out, "w") as f:
        json.dump(payload, f, indent=1)

    if not a.no_upcoming:
        where = a.upcoming_out or upcoming_path(a.out)
        with open(where, "w") as f:
            json.dump({"sport": "wbasketball", "season": a.season,
                       "generated": dt.date.today().isoformat(),
                       "games": soon}, f, indent=1)
        print(f"  wrote {where}  ({len(soon)} fixtures)")

    summarize(teams, reg, dates)
    print(f"\n  wrote {a.out}")
    if not a.cache_dir:
        shutil.rmtree(tmp, ignore_errors=True)
    if _warnings:
        print(f"  {_warnings} warning(s) above")


if __name__ == "__main__":
    main()
