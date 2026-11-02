# Daily selection review

Today's Top Five now opens a functional review page instead of the development
placeholder. `GET /api/shortlist` recalculates the current list from saved analyses
without rewriting model/value history or making external requests.

The page explicitly separates **qualified betting selections** from a **research
watchlist**. Production qualification is still unavailable: no independent source
approval, validated uncertainty or approved model-deployment contract exists yet.
The current app therefore returns zero qualified selections and explains those
blockers. It never treats a saved research backtest, API-key presence or a lower
probability threshold as automatic approval. This update is not a completed
production betting-recommendation engine or an API-Football integration.

Research candidates use the selected Poisson, Dixon–Coles or xG-based model from
each fixture's latest saved analysis. There is no automatic switch to whichever
model offers the largest estimate and no invented ensemble weight. Missing models,
changed fixtures, stale snapshots and inadequate historical samples are excluded.

The saved SAST-day/next-24-hours window, probability filter, minimum raw/effective
sample and maximum selections per league apply. Up to five candidates are retained,
with one primary market per match. Fewer candidates remain fewer; probabilities
are never adjusted to fill places. Minimum research sample remains at least 30.

Positive estimated EV meeting the saved EV filter takes ordering precedence only
when a fresh, non-conflicting imported quote exists. Otherwise research ordering
is probability-only and does not establish betting value. A negative-EV quote does
not outrank an unpriced candidate simply because it is priced. Price freshness is
60 minutes and model freshness is 24 hours, matching the initial research defaults.

Each card includes probability, theoretical fair odds, fresh imported odds/EV or
an explicit unavailable marker, source quality, sample evidence, blockers and a
full-analysis button. Listed/unconfirmed community fixtures remain research only.
Unknown uncertainty, missing odds and unverified data are never replaced by scores.

The page's refresh recalculates from local stored data; **Run data & models** opens
the actual data workflow. It is not an automatic scheduler. Original models, value
assessments and backtests remain immutable. The home page/sidebar qualified count
remains zero while production qualification is unavailable.
