# Recursive Rankings

College football ranked using nothing but who beat whom — no preseason poll, no
margin of victory, no human vote. Every game is graded by what happened next:
your result, then your opponent's season with that game removed, then how *their*
opponents did in turn.

**Live site:** `docs/` (see Deploying below)

## What's here

| Path | What it is |
|---|---|
| `docs/` | The public site. Static HTML, no build step, no dependencies. |
| `docs/index.html` | Rankings, with the AP and Coaches polls alongside. |
| `docs/about.html` | How the method works, and what it can't do. |
| `docs/season-20XX.json` | Game results and polls, one file per season. |
| `recursive-rankings.html` | The full research build — slates, drafts, per-game revision history, backtesting, cost analysis. Not part of the public site. |
| `fetch_season.py` | Pulls a season from CollegeFootballData.com and writes the JSON both apps read. |

## Refreshing the data

```sh
export COLLEGE_FOOTBALL_API_KEY='your-key'   # free at collegefootballdata.com/key
python3 fetch_season.py --year 2026 --out docs/season-2026.json
```

Roughly four API calls per run. `python3 fetch_season.py --selftest` verifies the
transform without a key or a network connection (32 checks).

Weeks are renumbered from kickoff dates rather than trusting the feed's own week
field, which folds Week 0 and Labor Day weekend into a single "week 1" and would
otherwise cost the app one game for every team that played in both.

## Deploying

**GitHub Pages** — Settings → Pages → Source: `main`, folder: `/docs`. Nothing else.

**Netlify** — connect the repo; `netlify.toml` already sets `publish = "docs"` and
no build command. Or drag the `docs/` folder onto app.netlify.com/drop.

Either works. The site is three HTML files and three JSON files; everything is
recomputed client-side.

## Data

Game results and polls from [collegefootballdata.com](https://collegefootballdata.com).
