# Daily Top Five

## User contract

The daily list contains **only fixtures still to be played on its generation day
in Africa/Johannesburg (SAST)**. It discovers fixtures across any league returned
by the user's API, not a preset of five major leagues. Tomorrow's fixtures never
fill vacant places. Up to five markets are selected, with one per match and a
configurable maximum per league (default two). The list can contain fewer than five.

## Use

1. Confirm the saved API-Football key in API Settings.
2. Open **Top Five → Build Top Five**. Keep the local app terminal open.
3. The background job discovers today's fixtures, obtains available historical
   seasons for each league, imports results/schedules, fits models for today's
   fixtures, retrieves paginated pre-match bookmaker prices and ranks the list.
4. Inspect the cards and coverage/exclusion messages. A large matchday may take
   several minutes. A league failure is reported without discarding other leagues.
5. Refresh the list to remove started games and stale evidence. This is a manual
   daily action, not an automatic scheduler.

The job stops if the SAST date changes. No future window option exists on this
page. The older generic research endpoint can still inspect other windows, but
`POST /api/top-five/build` accepts only `window=today`.

## Two explicit list types

**Value screened** (default) requires:

- An API-Football schedule refreshed in the past 24 hours, status NS/scheduled,
  and a future kickoff on the current SAST date.
- A saved, unchanged-identity analysis no more than 24 hours old.
- At least the saved minimum sample (never below 30), both raw and effective.
- The selected Poisson/Dixon–Coles model and the other comparison model.
- Probability at least the chosen minimum (default 75%) in **both** models.
- Model probability disagreement no greater than ten percentage points.
- A non-conflicting bookmaker quote within the chosen age limit (default 180
  minutes, visibly configurable). The source documents updates about every three
  hours; these are provider-reported pre-match quotes, not real-time guaranteed prices.
- Strictly positive estimated EV meeting the selected minimum (default 3%) under
  **both** models. Rank by the lower of their EV estimates, then apply league/five caps.

Model agreement is not independent confirmation or a predictive confidence
interval: Dixon–Coles shares Poisson strengths. Probabilities are not inflated,
calibration is not invented, and unavailable statistics stay missing.

**Probability research** does not require odds/positive value. It is separately
labelled and cannot be mistaken for value-screened selections. It uses the selected
model, sample/freshness filters and the same daily-only window.

Each card shows the market, raw model probability, fair price, bookmaker/quote time
and age, theoretical EV, comparison-model evidence and full analysis. Prices can
be stale or unavailable at the bookmaker; costs and account restrictions are not
modelled. A high-probability market is not automatically a good bet.

## Actual qualification versus experimental selections

The ranked list is operational: `selections`/`selection_count` contain genuine
screened candidates rather than a permanently hardcoded empty list. Home and
sidebar counts use the recalculated list. Models, odds and reports are preserved.

These selections remain **experimental**. The legacy `qualified` field stays empty
because independent calibration/deployment approval and predictive uncertainty
have not been established. It is not used to hide the ranked experimental list.
No independent profitability, confidence percentage or guaranteed win claim is made.
The new UI does not present an unearned validated recommendation badge.

## Forward paper tracking

Completed builds are archived immutably in `top_five_runs`. The paper audit takes
only the **first archived value-screened selection per fixture**, preventing
repeated refreshes or changing markets from multiplying simulated performance.
A saved selection must precede kickoff. It is settled only after a matching,
non-conflicting regulation result arrives; prior source receipt cannot be treated
as later out-of-sample evidence. Changed schedules are withheld pending review.

One unit is simulated at the recorded bookmaker price: win = odds minus one,
loss = minus one, push = zero. No settled observations means unavailable profit
and ROI, not zero-loss performance. Unpriced probability research is not treated
as a bet. This is forward **paper** tracking, not real wagering, executable odds
proof, independent statistical validation or a claim that the model is profitable.

## Bounds and API provenance

Documentation reviewed from the supplied official v3.9.3 PDF:
fixtures pages 61–66; pre-match odds pages 130–133. Daily fixture request uses
`/fixtures?date=YYYY-MM-DD&timezone=Africa/Johannesburg`. Available league seasons
come from `/leagues?id=…`; up to three recent available years are imported.
Only FT regulation results train the model. The per-league batch is capped at
30 analyses; any remainder is reported, not silently declared evaluated.

Odds request uses `/odds` with league, season, **date**, timezone and page filters.
Pagination (10 results/page) is validated and bounded to five complete pages per
league. Changes/incomplete pagination are refused atomically. No odds from an
unknown fixture, changed kickoff or future/naive update timestamp are accepted.
The original provider update time is retained; refreshing the app cannot make
an old price fresh. Only ID-and-name-whitelisted full-match 1x2, half-line goals,
BTTS and double-chance markets are imported. Half-time, extra-time and unknown
markets are skipped. Raw response errors/credentials never enter cards or archives.

The all-day fixture response is bounded to 1,500 entries / 4 MB. Odds validation
is bounded to 50,000 supported observations per league and validated in chunks
before a single transaction. Excessive collections fail visibly. Rate spacing,
quota stops, timeouts and Retry-After handling apply; the job does not bypass
provider coverage or network restrictions.

Automated and browser validation use synthetic responses and real model fitting.
The user previously confirmed live account access and successful result/model
imports on Windows. The newly added odds/daily-discovery paths still require a
real run on that laptop; the cloud holds no copy of the user's key.
