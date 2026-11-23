#!/usr/bin/env python3
"""Write the logo directories that the scatter-plot tooltips read.

    export COLLEGE_FOOTBALL_API_KEY='your-key'
    python3 build_logos.py

One small file per section -- docs/logos.json, docs/ncaam/logos.json and
docs/ncaaw/logos.json -- mapping each top-division team name, spelled exactly
as that section's season files spell it, to its ESPN team id. Only the top
division, because only it is ever plotted: the scatters take FBS or Division I
teams and the polls rank nobody else. The page builds the image
address from the id, so the file stays small and the size and light/dark
variant are chosen where the logo is drawn:

    {src}/64/{id}.png          {src}-dark/64/{id}.png in dark mode

The images are CollegeFootballData's, served from its own CDN, which is where
both of its APIs point their `logos` fields. Football uses /logos and both
basketball sections /cbb-logos. ESPN ids are per school rather than per sport,
so the women's side can use the men's images.

WHERE THE IDS COME FROM, section by section, because each source names teams
its own way and a logo is only found if the name matches the season file:

  football  CFBD /teams, every school it knows, by `school`.
  NCAAM     CBBD /teams for each season on the site, by `school`, id from
            `sourceId` (the ESPN id; `id` is CBBD's own).
  NCAAW     wehoop's schedule files, which carry the ESPN id beside the name
            the fetcher uses (`home_location` / `home_id`). Anything still
            missing falls back to the NCAAM directory by name.

A team with no entry simply gets a tooltip without a picture. Re-run this when
a new team joins a division; nothing else changes it, and a pull does not need
it.
"""
import glob
import json
import os
import sys
import tempfile

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


def write(section_dir, src, ids, label):
    _, top = names_in(section_dir)
    kept = {n: ids[n] for n in sorted(top) if n in ids}
    missing_top = sorted(top - set(kept))
    out = os.path.join(section_dir, "logos.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"src": src, "ids": kept}, f, separators=(",", ":"),
                  ensure_ascii=False)
    print(f"{label:9} {len(kept):4} of {len(top):4} top-division teams  -> "
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


def season_years(section_dir):
    return sorted(int(os.path.basename(p)[7:11]) for p in
                  glob.glob(os.path.join(section_dir, "season-20*.json")))


def ncaam(key):
    d = os.path.join(DOCS, "ncaam")
    ids = {}
    # Oldest first, so a school's current id wins if it ever changed.
    for year in season_years(d):
        for t in fetch_basketball.get("/teams", {"season": year}, key):
            if t.get("logos") and t.get("school") and t.get("sourceId"):
                ids[t["school"]] = str(t["sourceId"])
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
    for name, tid in fallback.items():
        ids.setdefault(name, tid)
    write(d, CDN + "/cbb-logos", ids, "NCAAW")


def main():
    key = os.environ.get("COLLEGE_FOOTBALL_API_KEY")
    if not key:
        sys.exit("COLLEGE_FOOTBALL_API_KEY is not set (the same key serves "
                 "both CollegeFootballData and CollegeBasketballData).")
    football(key)
    men = ncaam(key)
    ncaaw(men)


if __name__ == "__main__":
    main()
