#!/usr/bin/env python3
"""Write the logo directories the pages draw team logos from.

    export COLLEGE_FOOTBALL_API_KEY='your-key'
    python3 build_logos.py

One small file per section -- docs/logos.json, docs/ncaam/logos.json and
docs/ncaaw/logos.json -- mapping each team name, spelled exactly as that
section's season files spell it, to its ESPN team id. Every team in any of
the section's season files is a candidate, opponents from lower divisions
included, since a team page or a slate names them too. The page builds the
image address from the id, so the file stays small and the size and the
light/dark variant are chosen where the logo is drawn:

    {src}/48/{id}.png          {src}-dark/48/{id}.png in dark mode

48 beside a name in a table, 64 on a scatter's hover card, 96 on a team
page's heading -- each about twice the size it is drawn at.

ONLY LOGOS THAT LOAD ARE KEPT. Every candidate is checked against the CDN
before it is written, so the page never asks for an image that is not
there: a team with no logo is simply absent, and the page leaves an empty
space of the same width so the names beside it stay aligned.

The images are CollegeFootballData's, served from its own CDN, which is where
both of its APIs point their `logos` fields. Football uses /logos and both
basketball sections /cbb-logos. ESPN ids are per school rather than per sport,
so the women's side can use the men's images.

WHERE THE IDS COME FROM, section by section, because each source names teams
its own way and a logo is only found if the name matches the season file:

  football  CFBD /teams, every school it knows, by `school`.
  NCAAM     CBBD /teams for each season on the site, by `school`, id from
            `sourceId` (the ESPN id; `id` is CBBD's own). That covers
            Division I only, so lower-division opponents try the football
            directory's id under the same name; a Division I school tries
            it too, after its own.
  NCAAW     wehoop's schedule files, which carry the ESPN id beside the name
            the fetcher uses (`home_location` / `home_id`). The NCAAM
            candidates for the same name are tried after it.

Re-run this when a new team appears; a pull does not need it, and nothing
else changes the files.
"""
import concurrent.futures
import glob
import json
import os
import sys
import tempfile
import urllib.request

import fetch_basketball
import fetch_season
import fetch_wbasketball

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(HERE, "docs")
CDN = "https://cdn.collegefootballdata.com"


def names_in(section_dir):
    """(every team name, the top-division ones) across a section's season files."""
    every, top = set(), set()
    for path in sorted(glob.glob(os.path.join(section_dir, "season-20*.json"))):
        d = json.load(open(path, encoding="utf-8"))
        top_div = d.get("topDivision") or "FBS"
        for t in d.get("teams", []):
            every.add(t["name"])
            if t.get("division") == top_div:
                top.add(t["name"])
        for g in d.get("games", []):
            every.update((g["home"], g["away"]))
    return every, top


def loads(url):
    try:
        req = urllib.request.Request(url, method="HEAD")
        return urllib.request.urlopen(req, timeout=20).status == 200
    except Exception:
        return False


def reachable(src, ids):
    """The subset of ids whose table-sized logo the CDN actually serves."""
    unique = sorted(set(ids))
    with concurrent.futures.ThreadPoolExecutor(16) as pool:
        ok = pool.map(lambda i: loads(f"{src}/48/{i}.png"), unique)
    return {i for i, good in zip(unique, ok) if good}


def write(section_dir, src, ids, label):
    """ids maps a name to one id or to a list of them, best first; the first
    one the CDN serves is kept."""
    every, top = names_in(section_dir)
    cand = {n: (ids[n] if isinstance(ids[n], list) else [ids[n]])
            for n in every if n in ids}
    good = reachable(src, [i for v in cand.values() for i in v])
    kept = {}
    for n in sorted(cand):
        hit = next((i for i in cand[n] if i in good), None)
        if hit:
            kept[n] = hit
    missing_top = sorted(top - set(kept))
    out = os.path.join(section_dir, "logos.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"src": src, "ids": kept}, f, separators=(",", ":"),
                  ensure_ascii=False)
    print(f"{label:9} {len(kept):4} of {len(every):4} teams have a logo "
          f"({len(top) - len(missing_top)} of {len(top)} top-division; "
          f"{len(cand) - len(kept)} ids found but not on the CDN)  -> "
          f"{os.path.relpath(out, HERE)}")
    if missing_top:
        print("          no logo:", ", ".join(missing_top[:20]) +
              (" ..." if len(missing_top) > 20 else ""))


def football(key):
    ids = {}
    for t in fetch_season.get("/teams", {}, key):
        if t.get("logos") and t.get("school") and t.get("id") is not None:
            ids[t["school"]] = str(t["id"])
    write(DOCS, CDN + "/logos", ids, "football")
    return ids


def season_years(section_dir):
    return sorted(int(os.path.basename(p)[7:11]) for p in
                  glob.glob(os.path.join(section_dir, "season-20*.json")))


def ncaam(key, fallback):
    d = os.path.join(DOCS, "ncaam")
    ids = {}
    # Oldest first, so a school's current id wins if it ever changed.
    for year in season_years(d):
        for t in fetch_basketball.get("/teams", {"season": year}, key):
            if t.get("logos") and t.get("school") and t.get("sourceId"):
                ids[t["school"]] = str(t["sourceId"])
    ids = {n: [i] for n, i in ids.items()}
    for name, tid in fallback.items():
        ids.setdefault(name, []).append(tid)
    write(d, CDN + "/cbb-logos", ids, "NCAAM")
    return ids


def ncaaw(fallback):
    d = os.path.join(DOCS, "ncaaw")
    ids = {}
    with tempfile.TemporaryDirectory(prefix="wehoop-logos-") as tmp:
        for year in season_years(d):
            dest = os.path.join(tmp, f"schedule_{year}.parquet")
            try:
                fetch_wbasketball.download(
                    fetch_wbasketball.SCHEDULE.format(season=year), dest)
            except SystemExit:
                print(f"          (no wehoop schedule for {year}, skipped)")
                continue
            cols = fetch_wbasketball.read_parquet(dest)
            for side in ("home", "away"):
                for name, tid in zip(cols.get(f"{side}_location") or [],
                                     cols.get(f"{side}_id") or []):
                    name = (name or "").strip()
                    if not name or tid is None or fetch_wbasketball.PLACEHOLDER(name):
                        continue
                    try:
                        if int(tid) <= 0:
                            continue
                    except (TypeError, ValueError):
                        continue
                    ids[name] = str(int(tid))
    ids = {n: [i] for n, i in ids.items()}
    for name, tids in fallback.items():
        ids.setdefault(name, []).extend(x for x in tids if x not in ids[name])
    write(d, CDN + "/cbb-logos", ids, "NCAAW")


def main():
    key = os.environ.get("COLLEGE_FOOTBALL_API_KEY")
    if not key:
        sys.exit("COLLEGE_FOOTBALL_API_KEY is not set (the same key serves "
                 "both CollegeFootballData and CollegeBasketballData).")
    men = ncaam(key, football(key))
    ncaaw(men)


if __name__ == "__main__":
    main()
