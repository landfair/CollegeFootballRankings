#!/usr/bin/env python3
"""
Pull a college basketball season from CollegeBasketballData.com and write the
JSON the rankings app reads.

Setup (once):
    1. Get a key at https://collegebasketballdata.com  (the CollegeFootballData
       key works here too -- it is the same account system)
    2. export COLLEGE_FOOTBALL_API_KEY='your-key-here'

Use:
    python3 fetch_basketball.py --season 2025 --out docs/basketball/season-2025.json
    python3 fetch_basketball.py --selftest

A season is named for the year it ends in: 2025 is the 2024-25 season.

Unlike football, the bucket is a DAY, not a week. Basketball teams play two or
three times a week -- 45% of team-weeks hold more than one game -- and the app's
data model keeps one game per team per bucket, so weekly buckets would discard
nearly half the season. No team played twice in a day in any season checked.
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

BASE = "https://api.collegebasketballdata.com"
KEY_ENV = "COLLEGE_FOOTBALL_API_KEY"
POLLS_WANTED = ("AP Top 25", "Coaches Poll")
# Kickoffs are stamped UTC; tip-offs run late evening US time, so shifting back
# 8 hours puts every game on the calendar day it was actually played.
UTC_SHIFT = dt.timedelta(hours=8)


def warn(msg):
    print(f"  ! {msg}", file=sys.stderr)


def die(msg):
    print(f"\nerror: {msg}", file=sys.stderr)
    sys.exit(1)


def get(path, params, key, retries=4):
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": "recursive-rankings/1.0",
    })
    delay = 1.5
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                die(f"the API rejected the key ({e.code}). Check {KEY_ENV}.")
            if e.code == 429:
                warn(f"rate limited, waiting {delay:.0f}s")
            elif e.code >= 500:
                warn(f"server returned {e.code}, retrying")
            else:
                die(f"{e.code} for {path}: {e.read()[:300].decode('utf-8','replace')}")
        except (urllib.error.URLError, TimeoutError) as e:
            warn(f"network error ({e}), retrying")
        if attempt < retries - 1:
            time.sleep(delay)
            delay *= 2
    die(f"gave up on {path}")


def game_day(g):
    """Calendar day the game was played, or None."""
    raw = g.get("startDate")
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
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def fetch_games(season, key):
    """Page the games endpoint; it caps a response at 3000 rows."""
    out, seen = [], set()
    cur, stop = dt.date(season - 1, 10, 15), dt.date(season, 5, 1)
    while cur < stop:
        nxt = min(cur + dt.timedelta(days=20), stop)
        for g in get("/games", {"season": season, "seasonType": "regular",
                                "startDateRange": cur.isoformat(),
                                "endDateRange": nxt.isoformat()}, key):
            if g.get("id") in seen:
                continue
            seen.add(g.get("id"))
            out.append(g)
        cur = nxt
    return out


def fetch_postseason(season, key):
    """NCAA tournament and the other postseason events, as played."""
    return get("/games", {"season": season, "seasonType": "postseason"}, key)


def build_postseason(raw):
    """Finished postseason games, flat and plainly named.

    These are deliberately NOT merged into `games`. The ranking reads only
    `games`, so keeping the bracket in its own key means a tournament result
    can never feed back into a rating that was supposed to predict it --
    the ranking stays a report on the regular season, which is the whole
    basis on which anyone would ask it who will reach a Final Four.
    """
    out = []
    for g in raw:
        home, away = g.get("homeTeam"), g.get("awayTeam")
        hp, ap = g.get("homePoints"), g.get("awayPoints")
        if not home or not away or g.get("status") != "final":
            continue
        if hp is None or ap is None or hp == ap:
            continue
        d = game_day(g)
        if d is None:
            continue
        out.append({"home": home, "away": away,
                    "homePoints": int(hp), "awayPoints": int(ap),
                    "date": d.isoformat(),
                    "tournament": g.get("tournament") or ""})
    out.sort(key=lambda g: (g["date"], g["home"], g["away"]))
    return out


def final_four(post):
    """The four teams of the last NCAA weekend, from the bracket alone.

    The last three NCAA games are the two semi-finals and the final, and the
    teams in them are exactly the Final Four. Derived rather than stored, so
    it cannot drift from the games it is drawn from.
    """
    ncaa = [g for g in post if g.get("tournament") == "NCAA"]
    if len(ncaa) < 3:
        return []
    names = []
    for g in ncaa[-3:]:
        for t in (g["home"], g["away"]):
            if t not in names:
                names.append(t)
    return sorted(names)


def upcoming_day(g):
    """Calendar day a fixture is expected on, or None.

    A played game goes through game_day(), whose eight-hour shift exists to
    keep a late West Coast tip on its own evening. A fixture whose tip time is
    not set yet cannot go through it: the feed stamps those with a placeholder
    of 05:00Z -- midnight Eastern on the date it means -- and every one of the
    2,229 such rows in 2026-27 carries exactly that time. Shifting them back
    eight hours would move all of them a day early, so a TBD fixture is read
    as the plain date in its stamp and only a real tip time is shifted.
    """
    tbd = g.get("startTimeTbd") or g.get("startTimeTBD")
    if not tbd:
        return game_day(g)
    raw = g.get("startDate")
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


def build_upcoming(raw, teams, today=None, top="D1"):
    """Fixtures still to be played, flat and plainly named.

    These are deliberately NOT merged into `games`, and they are written to
    their own file rather than into the season payload. The ranking reads only
    `games`, so a fixture can never reach a rating -- the discipline the
    postseason already follows -- and a visitor to the table does not pay to
    download a schedule that only the team pages read.

    Two traps the feed sets here. It sends `homePoints: 0` on an unplayed row
    rather than null, so completion is decided by `status` alone; a test on the
    score would admit every fixture in the season as a nil-nil draw. And a row
    that is neither final nor in the future is an abandoned game, not a
    fixture, so anything dated before today is dropped rather than advertised
    as still to come.

    Only fixtures involving a team in the top division are kept, since those
    are the only teams with a page to show them on. Note that `teams` holds the
    lower divisions too by this point -- build_games() adds every opponent it
    meets -- so the test is on the division, not on mere membership.
    """
    if today is None:
        today = dt.date.today()

    def ranked(n):
        return (teams.get(n) or {}).get("division") == top

    out = []
    for g in raw:
        home, away = g.get("homeTeam"), g.get("awayTeam")
        if not home or not away or g.get("status") == "final":
            continue
        if not ranked(home) and not ranked(away):
            continue
        d = upcoming_day(g)
        if d is None or d < today:
            continue
        row = {"home": home, "away": away, "date": d.isoformat()}
        if g.get("neutralSite"):
            row["neutral"] = 1
        if g.get("conferenceGame"):
            row["conf"] = 1
        if g.get("startTimeTbd") or g.get("startTimeTBD"):
            row["tbd"] = 1
        out.append(row)
    return dedupe_fixtures(out)


def upcoming_path(out):
    """Where the fixtures for this season file belong.

    season-2027.json -> upcoming-2027.json, so the pair sits side by side in
    docs/ and the page can derive one name from the other. Anything not named
    that way simply gains a suffix.
    """
    base = os.path.basename(out)
    head = os.path.dirname(out)
    name = base.replace("season", "upcoming", 1) if "season" in base \
        else (base[:-5] + ".upcoming.json" if base.endswith(".json")
              else base + ".upcoming.json")
    return os.path.join(head, name) if head else name


def build_games(raw, teams):
    """Usable games plus the day each was played. Returns (games, skipped)."""
    games, skipped = [], []
    for g in raw:
        home, away = g.get("homeTeam"), g.get("awayTeam")
        hp, ap = g.get("homePoints"), g.get("awayPoints")
        label = f"{away} at {home}"
        if not home or not away:
            skipped.append(f"{label}: missing a team"); continue
        if g.get("status") != "final":
            continue
        if hp is None or ap is None:
            skipped.append(f"{label}: final but no score"); continue
        if hp == ap:
            skipped.append(f"{label}: tie, cannot be ordered"); continue
        d = game_day(g)
        if d is None:
            skipped.append(f"{label}: no usable date"); continue
        for n, conf in ((home, g.get("homeConference")), (away, g.get("awayConference"))):
            if n not in teams:
                teams[n] = {"name": n, "conference": conf or "", "division": "OTHER"}
        games.append({"home": home, "away": away,
                      "homePoints": int(hp), "awayPoints": int(ap), "_date": d})
    return games, skipped


def assign_days(games):
    """Number each distinct game day 1..N in order. Returns (games, dates)."""
    days = sorted({g["_date"] for g in games})
    index = {d: i + 1 for i, d in enumerate(days)}
    for g in games:
        g["week"] = index[g["_date"]]
    return games, [d.isoformat() for d in days]


def double_booked(games):
    """[(bucket, team)] for any team holding two games in one bucket."""
    from collections import defaultdict
    seen = defaultdict(int)
    for g in games:
        seen[(g["week"], g["home"])] += 1
        seen[(g["week"], g["away"])] += 1
    return [k for k, v in sorted(seen.items()) if v > 1]


def build_polls(raw, dates):
    """[{poll, week, pollDate, throughWeek, ranks}] for the two human polls.

    A poll published on a given date reflects every game played before it, so
    throughWeek is the last day bucket that falls strictly earlier. Keying off
    the published date avoids inferring alignment from week numbers.
    """
    from collections import defaultdict
    days = [dt.date.fromisoformat(d) for d in dates]

    def through(pd):
        n = 0
        for i, d in enumerate(days, start=1):
            if d < pd:
                n = i
            else:
                break
        return n

    grouped = defaultdict(list)
    for r in raw:
        name = r.get("pollType")
        if name not in POLLS_WANTED:
            continue
        # Regular season only, to match what the ranking covers. The feed also
        # tags the post-tournament poll with the same week and date as the final
        # regular-season one, so seasonType has to be part of the key or the two
        # merge into a single 50-team poll.
        if r.get("seasonType") != "regular":
            continue
        team, rank = r.get("team"), r.get("ranking")
        # "others receiving votes" carry points but no ranking
        if not team or rank is None:
            continue
        grouped[(name, r.get("seasonType"), r.get("week"),
                 (r.get("pollDate") or "")[:10])].append(
            {"rank": int(rank), "team": team})

    out = []
    for (name, _stype, week, pdate), ranks in grouped.items():
        if not ranks or not pdate:
            continue
        ranks.sort(key=lambda r: r["rank"])
        if len(ranks) != len({r["rank"] for r in ranks}):
            warn(f"{name} {pdate}: duplicate ranks, snapshot may be merged")
        out.append({"poll": name, "week": int(week or 0), "pollDate": pdate,
                    "throughWeek": through(dt.date.fromisoformat(pdate)),
                    "ranks": ranks})
    out.sort(key=lambda p: (p["pollDate"], p["poll"]))
    return out


def summarize(teams, games, dates):
    from collections import defaultdict
    div = defaultdict(int)
    for t in teams.values():
        div[t["division"]] += 1
    played = defaultdict(int)
    for g in games:
        played[g["home"]] += 1
        played[g["away"]] += 1
    d1 = [t for t, i in teams.items() if i["division"] == "D1"]
    counts = sorted(played[t] for t in d1 if played[t])
    per = defaultdict(int)
    for g in games:
        per[g["week"]] += 1
    sizes = sorted(per.values())
    print(f"\n  teams        {len(teams)}  ({', '.join(f'{k}: {v}' for k, v in sorted(div.items()))})")
    print(f"  games        {len(games)}")
    print(f"  game days    {len(dates)}   {dates[0]} .. {dates[-1]}")
    print(f"  games/day    min {sizes[0]}, median {sizes[len(sizes)//2]}, max {sizes[-1]}")
    if counts:
        print(f"  top-division games played   min {counts[0]}, median "
              f"{counts[len(counts)//2]}, max {counts[-1]}")


# ---------------------------------------------------------------------------
FIXTURE_GAMES = [
    {"homeTeam": "Duke", "awayTeam": "Kansas", "homePoints": 80, "awayPoints": 70,
     "status": "final", "startDate": "2024-11-05T23:00:00.000Z"},
    # late tip: 02:00 UTC on the 6th is the evening of the 5th
    {"homeTeam": "Gonzaga", "awayTeam": "Baylor", "homePoints": 77, "awayPoints": 75,
     "status": "final", "startDate": "2024-11-06T02:00:00.000Z"},
    {"homeTeam": "Duke", "awayTeam": "Baylor", "homePoints": 66, "awayPoints": 60,
     "status": "final", "startDate": "2024-11-08T23:00:00.000Z"},
    # a non-D1 opponent, admitted on the strength of a real game
    {"homeTeam": "Siena", "awayTeam": "Mercyhurst", "homePoints": 88, "awayPoints": 61,
     "status": "final", "startDate": "2024-11-08T23:00:00.000Z"},
    # unplayed and tied rows must not survive, and must not register anybody
    {"homeTeam": "Duke", "awayTeam": "Hartford", "homePoints": None, "awayPoints": None,
     "status": "scheduled", "startDate": "2024-12-01T23:00:00.000Z"},
    {"homeTeam": "Canisius", "awayTeam": "Kansas", "homePoints": 70, "awayPoints": 70,
     "status": "final", "startDate": "2024-11-08T23:00:00.000Z"},
]
FIXTURE_POLLS = [
    {"pollType": "AP Top 25", "seasonType": "regular", "week": 1,
     "pollDate": "2024-11-04T00:00:00.000Z", "team": "Kansas", "ranking": 1},
    {"pollType": "AP Top 25", "seasonType": "regular", "week": 2,
     "pollDate": "2024-11-07T00:00:00.000Z", "team": "Duke", "ranking": 1},
    # others receiving votes: points but no ranking
    {"pollType": "AP Top 25", "seasonType": "regular", "week": 2,
     "pollDate": "2024-11-07T00:00:00.000Z", "team": "Siena", "ranking": None},
    # the post-tournament poll reuses the same week and date
    {"pollType": "AP Top 25", "seasonType": "postseason", "week": 2,
     "pollDate": "2024-11-07T00:00:00.000Z", "team": "Gonzaga", "ranking": 1},
    {"pollType": "FCS Coaches Poll", "seasonType": "regular", "week": 2,
     "pollDate": "2024-11-07T00:00:00.000Z", "team": "Nobody", "ranking": 1},
]


FIXTURE_POST = [
    # a four-team bracket: two semi-finals then the final, out of date order
    {"homeTeam": "Duke", "awayTeam": "Baylor", "homePoints": 70, "awayPoints": 60,
     "status": "final", "tournament": "NCAA", "startDate": "2024-04-08T23:00:00.000Z"},
    {"homeTeam": "Duke", "awayTeam": "Kansas", "homePoints": 75, "awayPoints": 70,
     "status": "final", "tournament": "NCAA", "startDate": "2024-04-06T23:00:00.000Z"},
    {"homeTeam": "Baylor", "awayTeam": "Gonzaga", "homePoints": 80, "awayPoints": 78,
     "status": "final", "tournament": "NCAA", "startDate": "2024-04-06T20:00:00.000Z"},
    # an earlier round, which must not be mistaken for the Final Four
    {"homeTeam": "Duke", "awayTeam": "Siena", "homePoints": 90, "awayPoints": 50,
     "status": "final", "tournament": "NCAA", "startDate": "2024-03-21T23:00:00.000Z"},
    # other events and unfinished rows
    {"homeTeam": "Mercyhurst", "awayTeam": "Siena", "homePoints": 70, "awayPoints": 68,
     "status": "final", "tournament": "NIT", "startDate": "2024-04-04T23:00:00.000Z"},
    {"homeTeam": "Kansas", "awayTeam": "Gonzaga", "homePoints": None, "awayPoints": None,
     "status": "scheduled", "tournament": "NCAA", "startDate": "2024-04-09T23:00:00.000Z"},
    {"homeTeam": "Duke", "awayTeam": "Kansas", "homePoints": 60, "awayPoints": 60,
     "status": "final", "tournament": "NCAA", "startDate": "2024-03-22T23:00:00.000Z"},
]


FIXTURE_UPCOMING = [
    # a plain fixture with a real tip time
    {"homeTeam": "Duke", "awayTeam": "Kansas", "homePoints": 0, "awayPoints": 0,
     "status": "scheduled", "startDate": "2024-12-01T23:00:00.000Z",
     "conferenceGame": False},
    # a TBD fixture: 05:00Z is a placeholder for the stated date, and must not
    # be shifted back an evening the way a real late tip is
    {"homeTeam": "Gonzaga", "awayTeam": "Baylor", "homePoints": 0, "awayPoints": 0,
     "status": "scheduled", "startDate": "2024-12-05T05:00:00.000Z",
     "startTimeTbd": True, "conferenceGame": True},
    # neutral site
    {"homeTeam": "Duke", "awayTeam": "Gonzaga", "homePoints": 0, "awayPoints": 0,
     "status": "scheduled", "startDate": "2024-12-10T20:00:00.000Z",
     "neutralSite": True},
    # already played: belongs in games, never here
    {"homeTeam": "Duke", "awayTeam": "Baylor", "homePoints": 66, "awayPoints": 60,
     "status": "final", "startDate": "2024-11-08T23:00:00.000Z"},
    # abandoned: unplayed but dated before today
    {"homeTeam": "Kansas", "awayTeam": "Duke", "homePoints": 0, "awayPoints": 0,
     "status": "scheduled", "startDate": "2024-10-20T23:00:00.000Z"},
    # neither side is in the top division, so nobody has a page for it
    {"homeTeam": "Mercyhurst", "awayTeam": "Siena", "homePoints": 0, "awayPoints": 0,
     "status": "scheduled", "startDate": "2024-12-02T23:00:00.000Z"},
]


def selftest():
    teams = {"Duke": {"name": "Duke", "conference": "ACC", "division": "D1"},
             "Kansas": {"name": "Kansas", "conference": "Big 12", "division": "D1"},
             "Gonzaga": {"name": "Gonzaga", "conference": "WCC", "division": "D1"},
             "Baylor": {"name": "Baylor", "conference": "Big 12", "division": "D1"}}
    games, skipped = build_games(FIXTURE_GAMES, teams)
    post = build_postseason(FIXTURE_POST)
    soon = build_upcoming(FIXTURE_UPCOMING, teams, today=dt.date(2024, 11, 20))
    games, dates = assign_days(games)
    polls = build_polls(FIXTURE_POLLS, dates)
    byday = {}
    for g in games:
        byday.setdefault(g["week"], []).append(g)

    checks = [
        ("final games kept", len(games) == 4),
        ("unplayed game dropped", not any(g["away"] == "Hartford" for g in games)),
        ("tie skipped and reported", any("tie" in s for s in skipped)),
        ("late tip stays on its own evening", dates[0] == "2024-11-05"),
        ("two distinct game days", len(dates) == 2),
        ("same-evening games share a bucket", len(byday[1]) == 2),
        ("unknown opponent admitted as OTHER",
         teams.get("Mercyhurst", {}).get("division") == "OTHER"),
        ("a skipped game registers nobody",
         "Canisius" not in teams and "Hartford" not in teams),
        ("no team double-booked in a day", double_booked(games) == []),
        ("only the two human polls kept",
         {p["poll"] for p in polls} == {"AP Top 25"}),
        ("preseason poll maps to no games",
         [p for p in polls if p["week"] == 1][0]["throughWeek"] == 0),
        ("poll maps to the day before it was published",
         [p for p in polls if p["week"] == 2][0]["throughWeek"] == 1),
        ("others receiving votes are not ranked",
         all(r["team"] != "Siena" for p in polls for r in p["ranks"])),
        ("the postseason poll does not merge into the regular one",
         [p for p in polls if p["week"] == 2][0]["ranks"] == [{"rank": 1, "team": "Duke"}]),
        ("postseason keeps only finished, ordered games",
         len(post) == 5),
        ("postseason is sorted by date", [g["date"] for g in post] ==
         sorted(g["date"] for g in post)),
        ("postseason carries its event name",
         {g["tournament"] for g in post} == {"NCAA", "NIT"}),
        ("Final Four is the last three NCAA games, not the NIT or earlier rounds",
         final_four(post) == ["Baylor", "Duke", "Gonzaga", "Kansas"]),
        ("an incomplete bracket yields no Final Four",
         final_four([g for g in post if g["tournament"] == "NCAA"][:2]) == []),
        # the real guarantee: a postseason row carries a plain date and no day
        # index, so even if one were merged into `games` the engine would drop
        # it -- ingest() requires a finite week and skips anything without one.
        ("postseason rows carry no day index, so they cannot be graded",
         all("week" not in g for g in post) and all("date" in g for g in post)),
        ("regular-season rows all carry one",
         all(isinstance(g.get("week"), int) for g in games)),
        ("a played game never appears as a fixture",
         not any(r["home"] == "Duke" and r["away"] == "Baylor" for r in soon)),
        ("an unplayed row dated in the past is dropped, not advertised",
         not any(r["date"] == "2024-10-20" for r in soon)),
        ("a fixture between two lower-division sides is left out",
         not any(r["home"] == "Mercyhurst" for r in soon)),
        ("the three real fixtures are kept", len(soon) == 3),
        ("a TBD fixture keeps its own date rather than shifting an evening back",
         any(r["away"] == "Baylor" and r["date"] == "2024-12-05" for r in soon)),
        ("a TBD fixture is flagged as such",
         all(r.get("tbd") for r in soon if r["away"] == "Baylor")),
        ("a fixture with a real tip time still goes through the shift",
         any(r["away"] == "Kansas" and r["date"] == "2024-12-01" for r in soon)),
        ("neutral and conference games are marked",
         any(r.get("neutral") for r in soon) and any(r.get("conf") for r in soon)),
        ("fixtures are sorted by date",
         [r["date"] for r in soon] == sorted(r["date"] for r in soon)),
        ("a fixture carries no score and no day index, so it cannot be graded",
         all("week" not in r and "homePoints" not in r for r in soon)),
        ("one fixture filed both ways is kept once",
         len(dedupe_fixtures([{"date": "2026-11-14", "home": "Morgan State",
                               "away": "Towson"},
                              {"date": "2026-11-14", "home": "Towson",
                               "away": "Morgan State"}])) == 1),
        ("two different opponents on one day are both kept",
         len(dedupe_fixtures([{"date": "2027-02-25", "home": "Prairie View A&M",
                               "away": "Alcorn State"},
                              {"date": "2027-02-25", "home": "Prairie View A&M",
                               "away": "Jackson State"}])) == 2),
        ("which of a swapped pair survives does not depend on feed order",
         dedupe_fixtures([{"date": "2026-11-14", "home": "Towson",
                           "away": "Morgan State"},
                          {"date": "2026-11-14", "home": "Morgan State",
                           "away": "Towson"}])
         == dedupe_fixtures([{"date": "2026-11-14", "home": "Morgan State",
                              "away": "Towson"},
                             {"date": "2026-11-14", "home": "Towson",
                              "away": "Morgan State"}])),
        ("the fixture file is named beside the season file",
         upcoming_path("docs/basketball/season-2027.json")
         == "docs/basketball/upcoming-2027.json"),
        ("an oddly named output still gets a fixture file",
         upcoming_path("out.json") == "out.upcoming.json"),
    ]
    print("\nself test")
    ok = True
    for name, passed in checks:
        print(f"  {'pass' if passed else 'FAIL'}  {name}")
        ok &= passed
    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


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
    p = argparse.ArgumentParser(description="Fetch a basketball season")
    p.add_argument("--season", type=int, default=dt.date.today().year)
    p.add_argument("--out", default="season.json")
    p.add_argument("--upcoming-out", default=None,
                   help="where to write the fixtures (default: beside --out, "
                        "season-X.json -> upcoming-X.json)")
    p.add_argument("--no-upcoming", action="store_true",
                   help="skip the fixture file")
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args()
    if a.selftest:
        sys.exit(selftest())

    key = os.environ.get(KEY_ENV)
    if not key:
        die(f"{KEY_ENV} is not set.")

    print(f"fetching {a.season-1}-{str(a.season)[2:]} season")
    print("  teams, top division")
    teams = {}
    for t in get("/teams", {"season": a.season}, key):
        n = t.get("school")
        if n:
            teams[n] = {"name": n, "conference": t.get("conference") or "",
                        "division": "D1"}
    print("  games")
    raw = fetch_games(a.season, key)
    games, skipped = build_games(raw, teams)
    if not games:
        die(f"no completed games for {a.season}")
    games, dates = assign_days(games)
    print(f"  bucketed into {len(dates)} game days")

    soon = []
    if not a.no_upcoming:
        print("  fixtures still to play")
        soon = build_upcoming(raw, teams)
        if soon:
            print(f"     {len(soon)} fixtures, {soon[0]['date']} .. {soon[-1]['date']}"
                  f", {sum(1 for r in soon if r.get('tbd'))} without a tip time")
        else:
            warn("the feed listed no fixtures ahead of today")

    clashes = double_booked(games)
    if clashes:
        warn(f"{len(clashes)} team-days hold two games; the app keeps only the first")
        for w, t in clashes[:5]:
            warn(f"    day {w} ({dates[w-1]}): {t}")

    print("  postseason")
    post = []
    try:
        post = build_postseason(fetch_postseason(a.season, key))
        ncaa = [g for g in post if g["tournament"] == "NCAA"]
        f4 = final_four(post)
        print(f"     {len(post)} games, {len(ncaa)} of them NCAA" +
              (f"; Final Four {', '.join(f4)}" if f4 else "; no complete bracket yet"))
    except Exception as e:                                   # noqa: BLE001
        warn(f"postseason unavailable ({e}); continuing without it")

    print("  polls, AP and Coaches")
    polls = []
    try:
        polls = build_polls(get("/rankings", {"season": a.season}, key), dates)
        if polls:
            wks = sorted({p["pollDate"] for p in polls})
            print(f"     {len(polls)} snapshots, {wks[0]} .. {wks[-1]}")
            unknown = sorted({r["team"] for p in polls for r in p["ranks"]
                              if r["team"] not in teams})
            if unknown:
                warn(f"{len(unknown)} ranked teams not in the team list: "
                     f"{', '.join(unknown[:5])}")
    except SystemExit:
        raise
    except Exception as e:
        warn(f"could not read polls ({e}); writing without them")

    for g in games:
        g.pop("_date", None)
    games.sort(key=lambda g: (g["week"], g["home"]))
    payload = {"sport": "basketball", "topDivision": "D1",
               "teams": ordered_teams(teams, 'D1'), "games": games, "dates": dates}
    if polls:
        payload["polls"] = polls
    if post:
        payload["postseason"] = post
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
            json.dump({"sport": "basketball", "season": a.season,
                       "generated": dt.date.today().isoformat(),
                       "upcoming": soon}, f, separators=(",", ":"))
        print(f"wrote {where} ({len(soon)} fixtures)")

    summarize(teams, games, dates)
    if skipped:
        print(f"\n  skipped {len(skipped)} rows:")
        for s in skipped[:6]:
            print(f"     {s}")
        if len(skipped) > 6:
            print(f"     ... and {len(skipped)-6} more")
    print(f"\nwrote {a.out}")


if __name__ == "__main__":
    main()
