# What beats the seed line?

A search over every team stat the feed carries, and combinations of them, for
something that predicts NCAA tournament games better than the committee's own
seed line. Five seasons, 2021–22 to 2025–26.

## The answer

**Trust the seed line, except when the two teams are seeded within one of each
other — there, take the better raw net rating.** That scores 75.2% against the
seed line's 72.3% on the same 310 games.

| method | accuracy |
|---|---|
| Raw net rating alone | 61.9% |
| This site's recursive ranking | 68.7% |
| Schedule-adjusted margin (KenPom-style) | 71.3% |
| **The seed line** | **72.3%** |
| Seed line + net rating in 1-apart games | **75.2%** |

## Why it works

The committee is excellent at ordering teams that are far apart and close to
useless at ordering teams it has placed side by side. In games seeded one
apart, the higher seed wins only **54.7%** of the time — barely a coin flip.
That is where all the available improvement lives.

Raw net rating fails badly overall (61.9%) because it cannot see schedule
strength: mid-majors post identical raw numbers (13.0 vs 12.9 points) against
schedules nine points weaker, so an unadjusted stat systematically overrates
them. When net rating disagrees with the seed line across all games, it is
right only 32% of the time.

But in 1-apart games that flaw disappears, because the committee has already
matched the teams on schedule: mean |SOS difference| is **2.54** in those games
against **6.90** elsewhere. The adjustment the stat is missing has been done
for it. In exactly that window, net rating hits **71.7%** where the seed line
manages 54.7%.

So the finding is not "net rating is good". It is that seeding and net rating
fail in complementary places, and the seed gap tells you which to trust.

## Evidence it is not noise

- **Never loses a season.** +0, +2, +2, +2, +3 across the five.
- **p = 0.029** by McNemar on the 17 games where it differs from the seed line
  (13 right, 4 wrong).
- **Placebo:** coin-flipping the same 53 close games instead matched it in
  1 of 2000 runs.
- **Not a knife-edge:** N=1 and N=2 both give 75.2%.
- The regression version (`seed + adjusted margin + site ranking`, logistic,
  leave-one-season-out) independently finds the same thing at 74.2%, departs
  from the seed line in only 10 games, goes 8–2, and every departure is the
  same bet: the slightly worse seed in a near-level matchup. Its fitted weights
  are stable in sign and size across all five folds, and a random feature in
  place of adjusted margin matched it in 4 of 200 runs.

## Method

Every figure is out-of-sample where a parameter was fitted: leave-one-season-
out, so a season's games are predicted by a model that never saw them.

Two leakage traps had to be avoided, and both are easy to fall into:

1. **`teamStats` in the season files includes the tournament.** Michigan's 40
   games in 2025–26 are 34 regular + 6 tournament, and `games` played
   correlates +0.80 with tournament wins. Using those stats would be reading
   the answer. This study pulls `/stats/team/season?seasonType=regular`
   instead, which stops at Selection Sunday.
2. **Same-seed games must be dropped.** The committee expresses no opinion on
   a 16-vs-16 play-in, so scoring a system against a shrug is not a
   comparison. All 310 evaluation games have two different seeds, and both
   sides are judged on identical games.

## What upsets have in common

Separately, every feature was tested for whether the favourite's edge in it
predicts the result *beyond what the seed gap already says*. Across ~30
features the strongest reached t = 1.5 — what the best of thirty coin flips
looks like. Nothing survives as a warning sign.

**The big upsets were the least foreseeable.** The correlation between how
improbable an upset was by seed and how much the favourite deserved to win on
adjusted margin is **+0.70** (t = 8.9, n = 86). In the twenty biggest the
favourite was better by 6.7 points; in the twenty smallest the "underdog" was
better by 0.1 and the game was a mis-seed, not an upset.

**A correction.** An earlier pass here found the schedule gap hugely
predictive of upsets (t = −5.2) and that was confounded: the schedule gap
correlates 0.74 with the seed gap, because the committee seeds largely on
schedule, so the test was rediscovering "close-seeded games have more upsets".
Controlling for the seed gap the signal mostly vanishes, and inside the
one-apart band it reverses.

## Caveats

310 games is not many, and 53 of them carry the entire result. The rule should
be re-tested as seasons accumulate. The honest summary is that the effect is
real at conventional significance and consistent in direction, not that it is
nailed down.
