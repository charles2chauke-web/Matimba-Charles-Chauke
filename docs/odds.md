# Odds and theoretical value research

This is an offline, manual evidence workflow. No live odds API has been integrated,
no price availability/licence has been independently verified, and no betting
strategy has demonstrated profitability. Model probabilities are uncalibrated.
Positive calculated EV is a research result, not a recommendation. There is no
automatic or real-money betting. Top Five remains empty.

## Import contract

Import only exports you are permitted to use. In **Value Betting → Import odds**,
provide a source name and optionally a clean HTTPS reference URL. URLs containing
credentials, query strings or fragments are rejected. API keys never belong in
an odds file, source label or reference URL. No remote URLs are fetched by this importer.

Required CSV header in this order:

```csv
fixture_id,bookmaker,market,selection,line,decimal_odds,observed_at,settlement
```

| Field | Meaning |
| --- | --- |
| fixture_id | Local integer ID from the fixture library; shown in the Value Betting selector |
| bookmaker | Bookmaker name; normalised for matching, not proof of actual availability |
| market | A supported code below |
| selection | A supported outcome code for that market |
| line | Supported numeric goal line, or an empty CSV cell for other markets |
| decimal_odds | Finite decimal price greater than 1, at most 10,000 |
| observed_at | Past ISO timestamp with explicit offset, before the fixture kickoff |
| settlement | Exactly `regulation_90` |

`regulation_90` means 90 minutes plus stoppage time, excluding extra time and
penalties. Regulation results and bookmaker-specific settlement rules must not be
mixed. Counts/cards/booking points, corners, half-time, Asian quarter/integer lines,
cash-out and extra-time markets are not implemented.

| Market code | Selection codes | Line |
| --- | --- | --- |
| 1x2 | home, draw, away | Empty |
| goals | over, under | 0.5, 1.5, 2.5, 3.5 |
| btts | yes, no | Empty |
| team_goals_home | over, under | 0.5 |
| team_goals_away | over, under | 0.5 |
| double_chance | home_or_draw, away_or_draw, home_or_away | Empty |
| draw_no_bet | home, away | Empty |

All rows validate before any are written. Limits: 5,000 rows and 1,000,000 UTF-8
bytes per import. Duplicate observations do not alter the original receipt time.
Different prices/timestamps append evidence. Each quote captures the fixture's
normalised identity and kickoff so an edited/rescheduled fixture cannot silently
reuse incompatible prices. Team names and source permissions are not guessed.

Past fixture odds can be stored for provenance if their observation precedes
kickoff. That does not establish that the system knew the quote historically:
local receipt time is always preserved.

## Compare a saved model with prices

Choose a fixture, saved model research run and one model. No ensemble or automatic
choice of the model giving the largest edge is made. Create a comparison using:

1. **Current pricing:** a new decision time, using only evidence observed and received
   by now. The current fixture must still be scheduled before kickoff and have the
   same identity as the original model snapshot.
2. **At model generation:** the original model timestamp is the pricing cutoff.
   Only quotes both observed and received by that time are eligible. This can inspect
   archived research, but never substitutes today's odds for missing historical odds.

A fresh price imported after a prediction can be compared at a **new current
decision time**, but cannot rewrite the original prediction's pricing availability.
Each assessment saves its creation time, pricing cutoff, options, model/run/version,
source-observation IDs, price details and calculated results. Earlier assessments
are immutable. Reading an older assessment does not assert its prices are still current.

Default guards are quote age <=60 minutes and model age <=24 hours at pricing.
Both are configurable; they are research preferences, not validated optimal settings.
If the model is too old, EV/edge are withheld. Stale quotes cannot become best prices.
Only the latest observed timestamp per bookmaker/market/selection/line/settlement
participates. Tied latest observations with differing prices are conflicting and
withheld instead of choosing the most favourable one. Source details remain inspectable.

The largest fresh, non-conflicting imported price is shown as **best imported odds**.
There is no claim that the bookmaker will accept a stake, that limits are suitable,
or that the source is licensed. The evaluation limit is 10,000 eligible observations
per fixture/cutoff; exceeding it produces an explanation rather than silent truncation.

## Value mathematics and pushes

For a win/lose market:

```
Fair odds = 1 / p_win
EV per unit stake = p_win × decimal_odds − 1
Implied probability = 1 / decimal_odds
```

For draw-no-bet, a draw refunds the stake:

```
EV = p_win × (decimal_odds − 1) − p_loss
   = p_win × decimal_odds + p_push − 1
Fair odds = (1 − p_push) / p_win
Conditional non-push probability = p_win / (1 − p_push)
```

Compare 1/odds with the conditional non-push probability for draw-no-bet, not its
unconditional win probability. The table shows pushes explicitly, and evidence
details show the conditional comparison. When p_win is zero there is no finite
break-even fair price; it is displayed as unavailable. If every outcome is a push,
the expected return is zero, not profit.

The requested calculator example, **80% probability and odds 1.40**, gives fair odds
**1.25** and theoretical EV **+12%**. This is arithmetic on assumptions, not a real
match prediction or observed betting return. No odds means no EV claim. Commission,
taxes, bookmaker limits, account restrictions and execution effects are not modelled.

## Bookmaker margin and movement

Overround is `sum(1 / odds_i) − 1`, computed only for a complete set of mutually
exclusive outcomes at the **same bookmaker, market, line and settlement**, with
fresh, non-conflicting observations within 60 seconds of one another. Outcomes
from different bookmakers are never combined to manufacture a market margin.
Incomplete or unsynchronised markets keep margin unavailable. Negative overround
is retained as an observed underround, not silently replaced.

Proportional margin removal reports `(1/odds_i) / sum(1/odds_j)`. This is an explicit
normalisation assumption, not a calibrated estimate of true probability. For
draw-no-bet its basis is conditional on a non-push. Double-chance outcomes overlap,
so ordinary overround/no-vig calculations are deliberately not performed on them.

Odds movement compares the latest resolved price with the first stored observed
price for that bookmaker/outcome, within the pricing cutoff. Conflicting first
prices withhold that comparison. This is not a verified bookmaker opening price.
The chart shows raw evidence from the latest 100 fixture observations and allows
bookmaker/outcome selection and point inspection. It can include tied source
disagreements. Closing-line value remains unavailable without a verified closing feed.

## Local API

- `POST /api/odds/import` — JSON `csv`, `source`, optional `source_url`.
- `GET /api/odds/status` — stored observation counts and source limitations.
- `GET /api/fixtures/{id}/odds?limit=100&offset=0` — raw paginated evidence (max 500 rows).
- `POST /api/analysis/{run_id}/value` — selected model, `pricing_time` (`current` or
  `at_prediction`), `max_odds_age_minutes`, `max_model_age_hours`.
- `GET /api/analysis/{run_id}/values` — latest 50 comparison metadata records.
- `GET /api/value/{id}` — immutable assessment.
- `POST /api/value/calculator` — user-entered probability (0–1), decimal_odds and
  optional push probability (0–1). Win + push must not exceed one.

All data stays in the local ignored database. Back up the entire `data/` directory
with the app stopped; new odds/assessment tables are created additively on startup.
Live provider documentation/access and account-specific odds coverage remain
blocked as described in [provider integration status](provider-integration.md).
