# CollegeFootballRankings

Every game is graded by what happened next. A Week 1 result is scored by how your
opponent did in Week 2, which is scored by how their opponent did in Week 3, and so
on to the end of the season. Nothing is assumed. No preseason poll, no margin of
victory, no human vote — only results, read backward from the last week played.

## What's here

| Path | What it is |
|---|---|
| `docs/` | The public site. Static HTML, no build step, no dependencies. |
| `docs/index.html` | Rankings, with the AP and Coaches polls alongside. |
| `docs/about.html` | How the method works, and what it can't do. |
| `docs/season-20XX.json` | Game results and polls, one file per season (2024–2026). |
| `recursive-rankings.html` | The research build — weekly slates, the draft triangle, per-game revision history, poll comparison, backtesting. Open it directly; it loads a season through its own file picker. |
| `fetch_season.py` | Pulls a season from CollegeFootballData.com and writes the JSON both apps read. |

## The victors gate

A record-only system can't tell an unbeaten team that played nobody from an unbeaten
team that played everybody. The gate is a separate check applied *after* the ranking
is computed: to be listed, you must have beaten someone inside a chosen tier.

- **One pass** reads the tier off the finished order once. Stable, but it launders —
  you can qualify on a win over a team that is itself disqualified.
- **Self-consistent** also requires that opponent to qualify, and iterates to a fixed
  point. Stricter, but it needs enough cross-conference play to close a chain, so it
  is switched off before week 8.

The gate never changes a rating or an order. It only decides who is shown; excluded
teams still count as opponents for everyone else.

## Refreshing the data

```sh
export COLLEGE_FOOTBALL_API_KEY='your-key'   # free at collegefootballdata.com/key
python3 fetch_season.py --year 2026 --out docs/season-2026.json
```

About four API calls per run. `python3 fetch_season.py --selftest` verifies the
transform without a key or a network connection (32 checks).

Weeks are renumbered from kickoff dates rather than trusting the feed's own week
field, which folds Week 0 and Labor Day weekend into a single "week 1" — that would
otherwise cost the app one game for every team that played in both.

`docs/` holds the published data and is the source of truth. Root-level copies, if
you keep any for the research build, are deliberately git-ignored so the two cannot
drift apart.

## Deploying

**GitHub Pages** — Settings → Pages → Source: `main`, folder: `/docs`. Nothing else.

**Netlify** — connect the repo; `netlify.toml` already sets `publish = "docs"` with
no build command. Or drag the `docs/` folder onto app.netlify.com/drop.

## Data

Game results and polls from [collegefootballdata.com](https://collegefootballdata.com).

## License

MIT — see [LICENSE](LICENSE).
