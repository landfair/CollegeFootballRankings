#!/usr/bin/env python3
"""Build the combined football season: every year at once, as one season.

    python3 build_combined.py --out docs/season-all.json

The thirteen football season files are concatenated into a single payload and
graded as though they were one very long season. A team is one node across all
of it, so Alabama's record is its 2014-2026 record and the recursion reaches
through every year a team played rather than stopping at a season boundary.

Three things have to be got right, and only the first is interesting.

WEEKS MUST NOT COLLIDE. The engine holds one game per team per bucket and
scores a result as a percentile among everyone who played that bucket. Leaving
each file's week numbers alone would put 2014 week 1 and 2026 week 1 in the
same bucket, so a 2014 result would be scored against the 2026 field -- not a
merge but a corruption, and a silent one. Each season is therefore offset past
the last bucket of the one before it, which the payload format allows for free:
a game's `week` is documented as a bucket index and nothing requires it to
start at 1 or to mean a calendar week.

POLLS AND THE POWER INDEX ARE DROPPED. Both are statements about one season --
"the AP's 10th in week 8 of 2019" has no referent in a thirteen-year field, and
an agreement figure computed against a poll covering a twelfth of the games
would be measuring the mismatch rather than the rankings. The page hides every
poll-shaped thing when the key is absent, so dropping them is enough.

CONFERENCE IS THE LATEST ONE. Texas is Big 12 for most of this period and SEC
at the end of it; the file reports where a team plays now. The field is display
only -- ingest() never grades on it -- so this is a labelling choice rather
than a modelling one, but it is worth knowing when reading the table that the
conference column describes today and the record describes thirteen years.

What this cannot show is RECENCY. A program strong early and weak lately reads
exactly like its reverse. That is what "as though they were a single season"
means, and it is the first thing to remember when reading the result.
"""

import argparse
import datetime as dt
import json
import os
import sys


def warn(msg):
    print(f"  ! {msg}", file=sys.stderr)


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def load(path):
    if not os.path.exists(path):
        die(f"no such season file: {path}")
    with open(path) as f:
        return json.load(f)


def combine(paths, top="FBS"):
    """(payload, spans). paths oldest first; the last one wins on conference."""
    offset = 0
    teams = {}
    games = []
    spans = []
    for path in paths:
        raw = load(path)
        if not raw.get("games"):
            warn(f"{path} holds no games; skipped")
            continue
        local_max = max(g["week"] for g in raw["games"])
        label = os.path.basename(path)
        for t in raw.get("teams", []):
            name = t.get("name")
            if not name:
                continue
            # Later files overwrite earlier ones, so conference and division
            # end up as the most recent statement about that team.
            teams[name] = {"name": name,
                           "conference": t.get("conference") or "",
                           "division": t.get("division") or "OTHER"}
        for g in raw["games"]:
            games.append({"week": g["week"] + offset,
                          "home": g["home"], "away": g["away"],
                          "homePoints": g["homePoints"],
                          "awayPoints": g["awayPoints"]})
        spans.append({"file": label, "from": offset + 1,
                      "to": offset + local_max, "games": len(raw["games"])})
        offset += local_max

    if not games:
        die("nothing to combine")

    payload = {"sport": "football", "topDivision": top,
               "combined": {"seasons": len(spans), "buckets": offset,
                            "spans": spans,
                            "built": dt.date.today().isoformat()},
               "teams": sorted(teams.values(),
                               key=lambda t: (t["division"] != top, t["name"])),
               "games": games}
    return payload, spans


def check(payload, top="FBS"):
    """Loud assertions about the thing we are about to write."""
    games = payload["games"]
    div = {t["name"]: t["division"] for t in payload["teams"]}
    problems = []

    # The engine holds one game per team per bucket and silently keeps the
    # first when a bucket has two. That is not a merge artefact -- week
    # numbers come from kickoff dates and a team can land twice in one, which
    # season-2014.json does once for Charlotte, a lower-division side the
    # ranking never lists. It is reported rather than refused, because the
    # alternative is this script being stricter than the engine it feeds and
    # rejecting a file the site already serves.
    seen = {}
    for g in games:
        for side in (g["home"], g["away"]):
            k = (g["week"], side)
            seen[k] = seen.get(k, 0) + 1
    doubled = sorted(k for k, v in seen.items() if v > 1)
    notes = []
    if doubled:
        top_hit = [k for k in doubled if div.get(k[1]) == top]
        notes.append(f"{len(doubled)} team-bucket(s) hold two games; the "
                     f"engine keeps the first. "
                     f"{len(top_hit)} involve a top-division team"
                     + (f" ({top_hit[0][1]})" if top_hit else ""))

    # buckets must be contiguous from 1, or the week slider has holes
    weeks = sorted({g["week"] for g in games})
    if weeks[0] != 1:
        problems.append(f"buckets start at {weeks[0]}, not 1")
    gaps = [b for a, b in zip(weeks, weeks[1:]) if b != a + 1]
    if gaps:
        problems.append(f"{len(gaps)} gaps in the bucket sequence "
                        f"(first before {gaps[0]})")

    # spans must tile the range exactly, with no overlap
    spans = payload["combined"]["spans"]
    for a, b in zip(spans, spans[1:]):
        if b["from"] != a["to"] + 1:
            problems.append(f"{a['file']} ends {a['to']}, {b['file']} "
                            f"starts {b['from']} -- seasons overlap or gap")

    # no poll or power index may ride along
    for key in ("polls", "fpi", "postseason"):
        if key in payload:
            problems.append(f"`{key}` must not be in a combined payload")

    top_n = sum(1 for t in payload["teams"] if t["division"] == top)
    return problems, notes, top_n


def main():
    p = argparse.ArgumentParser(
        description="Combine every football season into one")
    p.add_argument("--out", default="docs/season-all.json")
    p.add_argument("--dir", default="docs")
    p.add_argument("--from-year", type=int, default=2014)
    p.add_argument("--to-year", type=int, default=None)
    a = p.parse_args()

    last = a.to_year or dt.date.today().year
    paths = []
    for y in range(a.from_year, last + 1):
        f = os.path.join(a.dir, f"season-{y}.json")
        if os.path.exists(f):
            paths.append(f)
    if not paths:
        die(f"no season-YYYY.json in {a.dir}")

    print(f"combining {len(paths)} seasons: "
          f"{os.path.basename(paths[0])} .. {os.path.basename(paths[-1])}")
    payload, spans = combine(paths)

    problems, notes, top_n = check(payload)
    for msg in notes:
        print(f"  note: {msg}")
    for msg in problems:
        warn(msg)
    if problems:
        die("the combined payload failed its own checks; nothing written")

    with open(a.out, "w") as f:
        json.dump(payload, f, indent=1)

    size = os.path.getsize(a.out)
    print(f"  buckets   {payload['combined']['buckets']}")
    print(f"  games     {len(payload['games'])}")
    print(f"  teams     {len(payload['teams'])}  ({top_n} top-division)")
    print(f"  wrote {a.out}  ({size/1024/1024:.1f} MB)")
    print("\n  season spans:")
    for s in spans:
        print(f"    {s['file']:<20} buckets {s['from']:>4}-{s['to']:<4} "
              f"{s['games']:>5} games")


if __name__ == "__main__":
    main()
