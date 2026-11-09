# CollegeFootballRankings

College football ranked only by who beat whom — no preseason expectation, no margin
of victory, no votes. AP and Coaches polls and ESPN's power index are shown
alongside for comparison, and feed nothing.

Three sections, one method. Football at the site root, men's basketball at
`/ncaam/`, women's basketball at `/ncaaw/`. The basketball section was a single
`/basketball/` until the men's and women's games were split apart; that path is
still served and redirects to `/ncaam/`, carrying the URL hash with it so a saved
team page survives the move.

## What's here

| Path | What it is |
|---|---|
| `docs/engine.js` | The ranking engine, shared by all three sections. Verified against the research build: 1,974 comparisons, zero differences. |
| `docs/index.html` | Football rankings, with the AP and Coaches polls and ESPN's power index alongside. |
| `docs/about.html` | How the method works, and what it can't do. |
| `docs/season-20XX.json` | Football results, polls and power index, one file per season (2014–2026). |
| `docs/ncaam/` | The same, for men's college basketball (2021–22 through 2026–27). |
| `docs/ncaaw/` | The same, for women's college basketball (2021–22 through 2026–27). No polls — see below. |
| `docs/basketball/` | A redirect stub to `/ncaam/`, so links handed out before the rename still work. |
| `recursive-rankings.html` | The research build — slates, the draft triangle, per-game revision history, poll comparison, backtesting. Open it directly; it loads a season through its own file picker. |
| `fetch_season.py` | Pulls a football season from CollegeFootballData.com. |
| `fetch_basketball.py` | Pulls a men's basketball season from CollegeBasketballData.com. The same API key works for both. |
| `fetch_wbasketball.py` | Pulls a women's basketball season from the wehoop data repository. No API key; needs `pyarrow`. |

## Weeks and days

The engine holds **one game per team per bucket**, and the sports differ in what a
bucket has to be.

Football buckets by week. Basketball cannot: teams play two or three times a week,
and 45% of basketball team-weeks hold more than one game, so weekly buckets would
silently discard nearly half the season. It buckets by **day** instead — no team
played twice in a day in any season checked. Everything downstream is unchanged; a
game's `week` field is just a bucket index.

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

`applyGate()` is also where the per-team résumé figures are attached, which is
easy to miss given the name: `bestWin`, `worstLoss`, `avgWin` and `avgLoss` all
ride out of it on the `all` rows. Only `bestWin` is actually used by the gate.

**`bestWin` and `worstLoss` are not symmetric, on purpose.** `bestWin` skips an
opponent the ranking does not place, and is `Infinity` when there are none.
`worstLoss` counts one, because out there a defeat is the *worst available
result*, not a missing figure — skipping it would give a team whose only loss
came against a lower division an empty cell, reading as no bad loss at all. So
an unranked defeat takes the `rows.length + 1` sentinel, `worstLossNR` says that
is what happened, and the page prints `NR`. An unbeaten team gets `null`, like
`avgLoss` and unlike `bestWin`. `worstLossTeam` names the opponent for the
tooltip; ties keep the earlier game, and an unranked defeat outranks every
ranked one regardless of when it fell.

## Refreshing the data

```sh
export COLLEGE_FOOTBALL_API_KEY='your-key'   # free at collegefootballdata.com/key
python3 fetch_season.py --year 2026 --out docs/season-2026.json --include-title-games
python3 fetch_basketball.py --season 2027 --out docs/ncaam/season-2027.json
python3 fetch_wbasketball.py --season 2027 --out docs/ncaaw/season-2027.json
```

A basketball season is named for the year it ends in, so the 2026–27 season is
`--season 2027`. Adding a season also needs an `<option>` in the `season` select
of the relevant `index.html`; the newest sits first and is what the page opens on.

### The women's game comes from somewhere else

CollegeBasketballData.com has **no women's basketball at all** — the word "women"
does not appear anywhere in its OpenAPI document and no endpoint takes a gender. So
`fetch_wbasketball.py` reads published season files from
[sportsdataverse/wehoop-wbb-data](https://github.com/sportsdataverse/wehoop-wbb-data)
instead. No API key, but parquet, so:

```sh
pip install pyarrow
```

That is this repo's only third-party Python requirement; the other two fetchers are
stdlib-only. `--selftest` runs without it.

Two things about that source are worth knowing before changing the script.

**Division I membership and conference come off the games, not off a team
directory.** Each side of each game carries its own conference id for that season,
so Texas reads Big 12 in 2022–23 and SEC in 2025–26 with nobody having to remember
the realignment, and Stanford still reads Pac-12 in the 2021–22 file. A side with no
conference id was not in Division I that season. Conference *names* are a separate
problem: wehoop publishes standings for only 2003–2007 and 2026–2027, so the names
are merged from those few files and applied to all. An ESPN conference id is stable
across seasons, and the merged map was checked to leave no conference unnamed in any
season here — 2007 is in the list precisely because it names conferences that have
since folded.

**There are no polls, and that is a real gap rather than a cosmetic one.** wehoop
publishes no rankings file. The schedule does record each side's rank at tip-off,
and reconstructing weekly snapshots from it was tried and rejected: over 2025–26
only 8 of 22 weeks recover all 25 places, the week before Christmas recovers 14, and
10 weeks give two teams the same rank because the window straddles two polls. The
page therefore shows no poll rather than a poll-shaped guess — everything poll-shaped
is gated on `SEASON.polls` being non-empty, so the columns, the agreement tiles and
the team-page tiles all reappear by themselves if a poll is ever added. The cost is
not the columns but the yardstick: the defaults on the other two pages are justified
by sweeping every setting against the polls, and on `/ncaaw/` they are inherited and
cannot be checked. ESPN's core API publishes both polls by week and could fill this
later.

The basketball fetcher also stores the postseason — the NCAA tournament, the NIT
and the rest — under its own `postseason` key. **These games are never ranked.**
`ingest()` reads only `games`, and postseason rows carry a plain date rather than
a day index, so even a merge would be dropped. Keeping them apart is the point: a
ranking asked which teams will reach a Final Four must not have seen the Final
Four. Read them with `bracket(season)`, which returns the 68-team field, the four
teams of the last weekend and the champion, all derived from the games rather
than stored, plus the list of events present.

Both fetchers also write a **fixtures** file beside the season file —
`season-2027.json` → `upcoming-2027.json` — holding the games not yet played,
each with a date, the two teams, and flags for neutral site, conference game and
an unset start time. **These are never ranked.** They are not in the season
payload at all, so `ingest()` cannot see them; the page fetches the file lazily,
only on a team page and only for the newest season, so a visitor to the table
does not pay for a schedule they will not read. Read them with
`remaining(upcoming, rows)`, which returns per team the fixtures left and the
mean rating of those opponents, built the same way `schedule()` builds the
backward-looking figure so the two can be read against each other.

Three details worth knowing before touching this code. A fixture with no start
time carries a **placeholder of 05:00Z** — midnight Eastern on the date meant —
so it must not go through the eight-hour shift that keeps a late game on its own
evening, or every such fixture lands a day early. The feed sends
**`homePoints: 0` rather than null** on an unplayed row, so completion is decided
by `status`/`completed` alone; a test on the score would admit the whole season
as nil-nil draws. And a row that is neither played nor in the future is an
**abandoned** game, not a fixture, so anything dated before today is dropped.

Pass `--no-upcoming` to skip the file, or `--upcoming-out PATH` to put it
somewhere else. The file is written even when empty, so a missing one means
something is wrong rather than merely unsupported.

To see what the feed offers for a year without writing anything:

```sh
python3 fetch_season.py --year 2024 --list-polls
```

A basketball season is named for the year it ends in: 2026 is the 2025–26 season.

About five API calls per football run; basketball pages its games endpoint, which
caps responses at 3,000 rows. All three have offline self-tests needing no key, no
network and — for the women's one — no pyarrow: `--selftest` (63 checks for football,
36 for men's basketball, 53 for women's).

Three rankings ride along for comparison: the AP and Coaches polls, and the CFP
selection committee's ranking, which only publishes from about week 10. Poll names
are matched rather than hard-coded, because the feed's label for the committee is
not stable, and each is stored under one canonical name — lower-division polls run
under confusingly similar titles and are rejected explicitly. `--list-polls` prints
every name a season offers and which one each would be filed under. The BCS
standings that preceded the committee ran 1998–2013 and so fall outside every
season here; the BCS and playoff eras do not overlap.

Football also carries **three figures from ESPN's Football Power Index**, under an
`fpi` key beside `polls`: the rating, its own place by that rating (`rank`), and
the two resume places, `sos` and `sor`. **These are never ranked either** — the
key is read straight through `ingest()` to the page and nothing downstream of
`games` can see it, which matters more here than for a poll, since FPI is a
margin-of-victory model and margin is the one input this ranking refuses. An
inertness check lives in the repo's history: 36 before/after comparisons across
every scoring, pool and weighting setting, byte-identical.

Two traps here. The endpoint **has no week dimension** — `/ratings/fpi` takes a
year and returns one snapshot, and passing `&week=N` changes nothing in the
response, which was verified rather than assumed. So the payload is stamped with
an `asOf` date and the page shows these columns only at the live horizon, hiding
them when the week slider is rewound, exactly as it hides the fixture list. And
`resumeRanks.fpi` is the team's **place** by FPI, not the rating repeated; the
rating is the top-level `fpi` field. FPI covers the top division only, from 2005
on, so the archived season files carry none of this until they are re-pulled.
Pass `--no-fpi` to skip the call.

Weeks are renumbered from kickoff dates rather than trusting the feed's own week
field, which folds Week 0 and Labor Day weekend into a single "week 1" — that would
otherwise cost the app one game for every team that played in both.

Conference championship games are included (`--include-title-games`). The feed files
them as regular season but tags them inconsistently, so without the flag they are
dropped in some years and kept in others — which left 2023 Georgia showing 12-0 when
they lost the SEC title game. They are ordinary games, and they are usually the ones
that decide who plays in January. Bowls and the playoff are not included: this ranks
the season that earns a place, not the tournament after it.

2020 was shortened by the pandemic; some teams played three or four games. Its
rankings are honest about what happened but rest on far less evidence.

`docs/` holds the published data and is the source of truth. Root-level copies, if
you keep any for the research build, are deliberately git-ignored so the two cannot
drift apart.

## Deploying

**GitHub Pages** — Settings → Pages → Source: `main`, folder: `/docs`. Nothing else.
Both sports are served from the same deployment; the engine is an ES module, so the
site must be served over http(s) rather than opened as a local file.

**Netlify** — connect the repo; `netlify.toml` already sets `publish = "docs"` with
no build command. Or drag the `docs/` folder onto app.netlify.com/drop.

**After editing `docs/engine.js`, run this before committing:**

```
python3 stamp_engine.py
```

It writes a hash of the engine's bytes into the `?v=` on each page's import.
The pages and the engine are separate files cached separately — Pages serves
both with `max-age=600` — so without it a returning visitor can hold a fresh
`index.html` and a ten-minute-old `engine.js`. If the new page imports something
the old engine lacks, the module never parses and the page is dead, not
degraded: `does not provide an export named 'schedule'`. Pinning the import to
the engine's hash means a page can only ask for the engine it was built against,
and that URL was never in anybody's cache. `--check` exits non-zero when a page
is stamped stale, and `--selftest` runs 16 offline checks.

## Data

Game results and polls from [collegefootballdata.com](https://collegefootballdata.com).

## License

MIT — see [LICENSE](LICENSE).
