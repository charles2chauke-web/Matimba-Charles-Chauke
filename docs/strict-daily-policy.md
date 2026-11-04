# Strict daily prediction policy

## Exact user rules

All new predictions are for fixtures **not yet started today in Africa/Johannesburg
(SAST)**. Tomorrow, next-24-hour and seven-day prediction windows are not allowed.
Historical results remain training data, not future selections. Previously saved
snapshots remain immutable audit records; they cannot enter today's Top Five unless
both their generation date and fixture date are today and their identities match.

The allowed selection set is exactly:

| Market | Selection | Settlement |
| --- | --- | --- |
| Goals | Over 1.5 total goals | 90 minutes plus stoppage |
| Goals | Over 2.5 total goals | 90 minutes plus stoppage |
| Result | Home win or away win | 90 minutes plus stoppage; draw is a loss |
| Corners | Over 7.5 total corner kicks | Both teams combined, FT only |
| Cards | Over 2.5 total yellow cards | Both teams combined; not booking points or red-card totals |

Over 0.5 goals, unders, draw selections, double chance, BTTS, draw-no-bet,
other lines and half-time markets are excluded from the daily list. The complete
score distribution still exists internally to calculate goal/result probabilities.

The default major-competition set is Premier League, La Liga, Serie A, Bundesliga,
Ligue 1, UEFA Champions League, Europa League and Conference League (API IDs
39,140,135,78,61,2,3,848). All available daily leagues can be considered. Major
competitions are processed first and receive a first pass with at most one
eligible outcome per major competition before extra places are filled. There is
no forced major-league pick when evidence/thresholds fail and no forced market mix.

## What “most reliable” means here

The app cannot claim a verified probability of winning. It chooses **one highest
conservative model estimate per match**, after sample/stability filters, then
applies major priority, five-place and per-league caps. Ranking no longer promotes
larger theoretical EV above estimated outcome probability. Value mode still
requires a compatible price and positive EV meeting the chosen minimum.

The probability floor is 75% and can be raised, not lowered. Both applicable model
estimates must meet the floor, differ by no more than ten percentage points, and
have at least 30 raw/effective observations (or the higher saved minimum). Each
selected team needs the model's effective coverage minimum. Missing comparisons,
changed/stale schedules and inadequate data withhold that market. Estimates are
never increased to fill places; fewer than five is a valid result.

For goals/results the comparison is Poisson versus conditional Dixon–Coles.
These share strengths and are not independent evidence. For corners/yellow cards,
the comparison is a dispersion-aware count tail versus a Poisson tail. The lower
estimate is displayed. This is **not a calibrated confidence interval**, independent
validation, guaranteed accuracy or a promise that an outcome is profitable.

## Actual corner/card statistics

The supplied official API-Football v3.9.3 PDF documents `/fixtures/statistics` on
pages 69–72, including `Corner Kicks`, `Yellow Cards` and `Red Cards`. The adapter
uses the default full-match response, never the half-time option. Only fixtures
whose latest provider status is FT are eligible: AET, PEN, awards, walkovers and
live games are not used for count training.

Both returned teams must match the stored stable provider IDs. Values must be
nonnegative whole counts; explicit zero is retained, missing/null stays missing.
No goals, average goals, red cards or booking-point conversions substitute for a
missing corner/yellow-card observation.

Statistics are appended with the actual retrieval/receipt time. A cached response
is not backdated or relabelled fresh. Complete corner/yellow pairs are cached for
seven days; missing/partial responses are retried after one day. A changed goal
score invalidates reuse of the old cached response. Ordinary unchanged goal
refreshes preserve previously reported count fields. Result withdrawals still
withdraw the corresponding statistics. Historical replay retains original evidence.

Top Five has a visible **new statistics calls per build** budget, default 500,
maximum 750. Each league considers up to 120 recent completed FT fixtures; major
leagues get first access to the budget. The cold first build can take many minutes
because request spacing/quota rules still apply. Subsequent runs reuse cached
statistics. Budget exhaustion means missing markets stay withheld; it never means
zero corners/cards. API coverage may be absent even for a paid account.

## Count model

Each market uses only rows with both teams' reported statistic. The existing
regularised, opponent/venue-adjusted log-link strength model is fitted to corners
or yellow cards, with time decay and team/connectivity/effective-sample checks.
Home/away fitted means are added to form a match-total mean. A weighted Pearson
residual variance factor (at least one) supplies a moment-matched negative-binomial
tail; Poisson supplies the comparison tail. Unstable dispersion, failed optimisation,
numerical bounds or missing statistics withhold the estimate. This is an
experimental count model, not a referee/injury model or validated uncertainty model.

Corners Analysis and Bookings Analysis now show count results or an explicit
withholding reason. They use only today's fixture options and the same saved
analysis snapshots. Build Top Five loads source statistics; clicking a page alone
does not run a hidden import or alter predictions. Half-Time Analysis remains outside
the requested daily market set.

## Bookmaker contracts and paper results

`/odds/bets` (documented pages 138–139) resolves IDs for an exact whitelist of
full-match total-corner and explicitly yellow-card market names. Unknown periods,
generic `Cards Over/Under` and booking-point markets are not assumed equivalent.
Only 7.5 corner and 2.5 yellow-card lines map to these new types. If no compatible
market is available, probability research can show an unpriced estimate; value
mode cannot use it. Existing odds quarantine and timestamp checks remain in force.

Forward paper settlement for these selections requires the corresponding final
reported count. Missing counts leave the result pending; goals never settle a
corner/card prediction. No real-money bets are placed and no prior returns are
fabricated. Independent statistical validation remains outstanding.

## Enforcement and verification

- Direct analysis requests for future-day fixtures return HTTP 409.
- Workflow horizons other than one day and future-window settings are rejected.
- Existing stored future-window preferences migrate to today without deleting data.
- Top Five uses `/api/daily-predictions`; `/api/shortlist` enforces the same policy
  for backward-compatible clients. Broad archived research is separated under
  `/api/research/shortlist`, with no permission to generate future predictions.
- Home recalculates strict selections, so old over-0.5 snapshots cannot populate it.
- Corners/cards have their own sample gates and cannot inherit a large goal sample.

Tests and desktop/mobile browser checks use synthetic source data and real model
fitting. Actual live statistics/catalogue coverage still needs a run on the user's
Windows installation; the development cloud has no copy of that local API key.
