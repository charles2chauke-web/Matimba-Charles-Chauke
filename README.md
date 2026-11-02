# CHARLES FOOTBALL BETTING INTELLIGENCE SYSTEM

Data-Driven Football Predictions and Betting Analysis

**Updating an existing download?** Follow [UPDATE-WINDOWS.md](UPDATE-WINDOWS.md)
to preserve your local data. Today's Top Five now has a functional daily research
review and exclusion report; [qualification is still separate](docs/daily-shortlist.md)
and cannot be bypassed by unverified estimates.

## Run the working research model

Start the app with `start-windows.cmd`, open the private local address below, and
choose **Run Model → Load data & run model**. This downloads the documented CC0
OpenFootball English Premier League feed, imports reported results, fits the
available statistical models, and saves analyses for listed upcoming matches.
Click **View predictions** to inspect a real-data research run. Source limitations
and insufficient-coverage cases are shown instead of invented statistics.

This workflow was exercised on 2 November 2026: 810 community-reported results
were imported and Poisson/Dixon–Coles estimates were fitted using 720 eligible
historical matches for the source-listed Newcastle–Everton fixture. The newest
reported result was 20 September 2026 (43 days old). These are **unverified,
uncalibrated research estimates**, not validated betting recommendations. See the
[working-model guide](docs/working-model.md) for provenance and the exact limitations.

You selected API-Football as the primary provider. Its live adapter still requires
current official documentation access and connection validation. Add your key in
local **API Settings**, or securely as `API_FOOTBALL_KEY` in cloud environment
settings and apply the saved network settings. Never send the key in chat. The
public fallback uses no API key and never sends your API-Football credentials to GitHub.

## Current development status

FastAPI + SQLite backend and React/TypeScript dashboard. Includes dark/light themes,
fixture exploration and provenance, atomic CSV imports, status updates and duplicate
detection, encrypted API-key storage, and persistent research preferences.

Phase 2 now includes a historical Data Library: validated CSV imports, optional
xG/corners/cards/half-time statistics, append-only source observations, conflict
detection, source timestamps, and descriptive team form over up to 5/10/20 matches.
Live provider integration is **blocked**, not completed: access to official API
documentation was denied by the cloud network policy, and no provider key is
configured. See [provider integration status](docs/provider-integration.md).

Phase 3 adds **experimental model research**: fitted Poisson goal strengths,
conditional Dixon–Coles low-score adjustment, a reported-xG-based strength model,
Elo context and time-weighted recent form. Generate research from Model Research,
Goals Analysis or Full-Time Analysis. Every run saves an immutable evidence snapshot.
See [model methodology](docs/models.md) for assumptions, fitting limits and exclusions.

Phase 4 adds **manual odds and theoretical value research**: timestamped odds imports,
best fresh imported-price comparisons, fair odds, push-aware expected value,
same-book market overround, observed price movement, a calculator and immutable
comparison snapshots. See [odds and value guide](docs/odds.md). There is no live
bookmaker feed and no independently verified claim that an imported price is obtainable.

Phase 5 adds **chronological research backtesting**: receipt-aware walk-forward
replay, saved pre-match prediction audits, an earlier calibration period, untouched
holdout scoring, raw/temperature-scaled probability comparisons, historical-price
flat-stake simulation, losing periods, drawdown, grouped results and immutable
study reports. See [backtesting methodology](docs/backtesting.md).

There is **no live API-Football integration, calibrated production prediction model, automated
schedule, bankroll simulator, validated betting recommendation or betting execution**.
Those sections show their unavailable status. No sample fixtures or synthetic
performance are loaded. Top Five remains empty until validated models and odds can
support qualification. Schedule and betting thresholds remain reserved for later phases.
No outcomes or profitability are guaranteed.

Backtest reports are research results, not independent validation or permission to
deploy a model. Core prediction snapshots remain experimental. A zero-sample study
does not establish readiness; zero bets leave profit/ROI unavailable.

## Windows startup

1. Install **Python 3.12 for Windows x86-64** (including `py`) and Node.js 22.12+ LTS.
2. Double-click `start-windows.cmd` in this folder. Internet access is required for
   first-time dependencies and refreshes. Setup builds the dashboard and starts the backend.
3. Open **http://127.0.0.1:8000** on your own computer. Keep the terminal open.
4. Stop with Ctrl+C. The application listens on loopback only. Do not expose its
   port publicly; it is a single-user local application without user authentication.

The Windows launcher and DPAPI code require validation on an actual Windows machine;
development validation was performed on Linux.

The launcher explicitly selects 64-bit Python 3.12 and creates `.venv-win312`.
It does not reuse an incompatible older `.venv`. It requires prebuilt Python
packages (`--only-binary=:all:`), so SciPy must not trigger a Fortran build.
If you see `metadata-generation-failed` / `gfortran` / `scipy`, use the updated
launcher and install the required x86-64 Python version; do not install compilers.
Your existing `data` folder remains separate and is not deleted by the launcher.

## Development (Linux/macOS)

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
cd frontend
npm ci --no-audit --no-fund
npm run build
cd ..
.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

For frontend hot reload, start `npm run dev` in `frontend` in a second terminal;
Vite proxies same-origin `/api` requests to port 8000. Refresh the browser after
rebuilding frontend assets. Start the backend from the repository root.

```sh
.venv/bin/python -m pytest -q
```

## Import fixtures

Use **Import fixtures** or Match Explorer. Download the empty CSV template or paste:

```csv
home,away,competition,kickoff_sast,status
```

Add your real fixture rows using ISO datetimes, for example `2026-12-10T19:30:00+02:00`.
Naive datetimes are interpreted as SAST. Explicit offsets must equal +02:00.
Supported statuses: `scheduled`, `postponed`, `cancelled`, `live`, `completed`.
Limits: 1 MB of text, 5,000 rows per import. Validation completes before any writes;
a bad row rejects the entire import. Importing the same normalised teams, competition,
and kickoff skips duplicates or updates the status. A changed kickoff creates a new
fixture: postponed/rescheduled identity reconciliation is a later integration concern.
Names use Unicode NFKC, case folding and whitespace normalisation; fuzzy aliases are
not inferred. CSV input is labelled **unverified**, never verified statistics.
Today's dashboard filters on SAST; the next-24-hours option is in System Settings.
Live, completed, cancelled, postponed and past fixtures cannot enter the upcoming list.

## Historical Data Library

Use **Data Library → Import history**. Enter a source name and optionally a clean
HTTPS reference URL (no credentials, query string or fragment). Import only data
you have permission to use. Source labels are user declarations, not proof of
authorisation, source authenticity or statistical quality.

Required columns (download an empty template in the dashboard):

```csv
home,away,competition,kickoff_sast,goals_home,goals_away,observed_at
```

Optional columns:

```csv
xg_home,xg_away,corners_home,corners_away,yellow_home,yellow_away,red_home,red_away,ht_goals_home,ht_goals_away
```

Only completed regulation-time results belong in this dataset. Goals and count
statistics must be nonnegative integers; xG may be fractional. Empty optional cells
are missing, never zero. Scores including extra time or penalties must not be mixed
with regulation results. Yellow/red counts are not bookmaker booking points.
This importer cannot independently verify a supplied result or its settlement basis.

`kickoff_sast` follows the fixture import timezone rules. `observed_at` must be a
past ISO timestamp with an explicit offset, after kickoff. The whole batch is
validated before insertion. Limits: 5,000 rows / 1 MB. Extra columns and malformed
rows are rejected. No licensed data, synthetic examples or credentials are bundled.

The same match identity is normalised teams + competition + kickoff. Each source
observation is immutable. Reimporting an identical observation skips it without
changing its receipt timestamp. Corrections append new observations. The latest
timestamp per source participates in current resolution; tied differing records or
disagreement between sources mark the field **conflicting** and withhold the value.
Earlier observations remain inspectable. Changing a kickoff creates a distinct
identity; automatic entity/rescheduling reconciliation is not yet implemented.

Search an exact team name to inspect descriptive form and venue averages. These
statistics are not opponent-adjusted or calibrated predictions. Small/incomplete
samples are explicitly counted. Conflicting scores are excluded from form summaries.

`GET /api/teams/summary?team=...&as_of=<offset-aware ISO datetime>` filters to evidence
both observed and received by that time. Source timestamps cannot backdate this
computer's knowledge: earliest usable time is the later of observation and receipt.
This conservative rule means a newly imported historical dataset cannot establish
what a live system knew years ago; historically audited feeds will need a separate
validated availability contract before retrospective backtesting is supported.

API routes: `/api/history/import`, `/api/history` (50/100-row pagination supported),
`/api/history/{id}`, `/api/teams/summary`, and `/api/data/status`. The existing fixture
CSV format remains unchanged. Importing history does not overwrite fixtures or
automatically run models. Back up `data/` before upgrading; new history/run tables are created
additively on startup, preserving settings and credentials.

## Experimental model research

1. Import regulation-time historical results in Data Library and a scheduled future
   fixture through Import fixtures. Use matching competition/team names; aliases are
   not guessed. Research uses one competition, not mixed leagues.
2. Open **Model Research**, choose the fixture and click **Generate research analysis**.
3. Review each available model, its training sample, unavailable-model explanations,
   recent form, Elo context and evidence trail. No recommendation is generated.
4. Use **Saved snapshots** to inspect original runs; new runs never overwrite them.

Both raw and time-weighted effective competition samples must meet the larger of
30 and System Settings' minimum historical sample. Each selected team must meet
the per-team raw/effective sample requirement (default 5). Defaults of 730 training
days, 180-day recency half-life, L2 strength penalty 1 and Elo K=20 are explicit
research starting assumptions, **not validated optimal settings**. Adjust them in
Research hyperparameters. Fitting failures and unsupported data return explanations.
The 75% qualification preference never changes computed probabilities.

Reported xG must be supplied in historical imports for the xG-based model. Missing
xG is not estimated from goals. Poisson and Dixon–Coles share fitted goal strengths
and cannot be treated as independent agreement. No ensemble weights, uncertainty
intervals, calibration scores, ROI or profit claims are produced.

API: `POST /api/fixtures/{id}/analysis` with JSON `{}` for default research assumptions,
`GET /api/fixtures/{id}/analyses` (latest 50 metadata records), and
`GET /api/analysis/{run_id}` for an immutable snapshot. New runs require a scheduled,
not-yet-started fixture. Old snapshots remain available after fixture status changes.

## Credentials and local storage

Use **API Settings** to add, replace or remove keys. Save does not test the key or
contact a provider. Never paste keys in chat or commit them. API responses return
configuration metadata only. Validation errors do not echo submitted values.

Local files live in ignored `data/` (override with `CHARLES_DATA_DIR`). API keys are
Fernet encrypted in SQLite. On Windows the encryption key is protected with current-user
DPAPI; on Unix it is stored in a mode-0600 file inside a mode-0700 directory. An
optional `CHARLES_CREDENTIAL_KEY` environment variable supplies a Fernet key instead.
This protects stored credentials, not against a compromised account or running process.
Keep the same key when restarting. Do not change it after storing credentials.

## Backtesting

Open **Backtesting**, enter an exact competition name and choose a method:

- **Walk-forward replay:** refit at each historical decision using only results
  observed and received by that cutoff. A dataset first imported today cannot
  produce forecasts claiming to have used it months ago.
  Replay also requires a scheduled fixture identity recorded before the decision;
  immutable status observations are now captured on fixture creation/status changes.
  Missing older schedule history is not invented during migration.
- **Saved prediction audit:** evaluate the latest stored pre-match snapshot that
  existed by each decision cutoff, subject to the model-age guard. No refitting
  or replacement of the original probabilities occurs.

Dates refer to **decision dates in SAST**, not kickoff dates. The decision is kickoff
minus the chosen lead (default 60 minutes). The earlier calibration period precedes
the holdout. Temperature scaling uses only earlier-period outcomes already known
at the holdout's start and is never fitted on test labels. Raw and scaled holdout
metrics remain visible even if scaling worsens performance. No calibrator is
automatically deployed and no ensemble weights are invented.

Choose a fixed model, market/selection, filters and flat simulated stake before
running. Only fresh, non-conflicting historical odds observed **and received** by
the decision time can fund a simulated selection. Current prices never replace
missing historical odds. Zero bets mean unavailable ROI, not proven break-even.
Draw-no-bet pushes return the stake and are excluded from non-push win rate.

The engine evaluates at most 100 calibration/holdout targets sequentially; larger
periods must be narrowed and are not silently truncated. Every run saves its
settings, predictions, evidence references, exclusions, calibration fit, holdout
metrics and flat-stake P&L. Changing the form creates a new study rather than
rewriting an existing report. Retrospective tuning/repeated holdout inspection can
still overfit; reports do not establish an independently profitable strategy.

## Odds and value research

Open **Value Betting** to import permitted odds exports and compare them against
a saved model research run. Download the empty template from Import odds:

```csv
fixture_id,bookmaker,market,selection,line,decimal_odds,observed_at,settlement
```

Fixture IDs are shown in the Value Betting fixture selector. Odds must use
`regulation_90` settlement; other rules, corners, cards, Asian lines and extra-time
markets are not accepted. See the guide for exact market/selection codes. Source
timestamps must be past, offset-aware datetimes before kickoff. Imports are atomic,
append-only and deduplicated. Maximum 5,000 rows / 1 MB UTF-8 per odds import.

Choose a model and pricing time, then create a comparison:

- **Current prices** creates a new research decision time using then-known quotes.
  The fixture must still be scheduled before kickoff and match the model's identity.
- **At model generation** uses only odds both observed and received by the original
  model time. Importing a backdated quote later does not make it historically available.

Default freshness guards are 60 minutes for odds and 24 hours for the model. They
are configurable research assumptions, not proof of price availability or model
quality. Stale/conflicting evidence withholds value. Without odds, probabilities and
theoretical fair odds remain visible, but EV is unverified. Each comparison is saved
without altering earlier results. The odds chart displays raw stored observations;
it does not establish actual opening or closing prices. Closing-line value is unavailable.

The calculator accepts your own win/push probability and decimal price. For a
win/lose market, `EV = p × odds − 1` and `fair odds = 1 / p`. With a refunded push,
`EV = p_win × odds + p_push − 1` and `fair odds = (1 − p_push) / p_win`.
Positive calculated EV is theoretical and does not qualify a Top Five selection.

### Manual backup and restore

Stop the app, copy the entire `data/` directory to a secure private location, then restart.
To restore, stop the app and restore that directory as a unit. Back up the existing
directory before replacement. Windows DPAPI backups require the same Windows user
profile on the same machine; after migration, re-enter keys if DPAPI is unavailable.
If an environment key is used, back it up separately in a secure credential manager.
Backups contain sensitive data; do not upload them. An in-app backup workflow is planned.

## Architecture and next phases

`backend/main.py`: local HTTP API, import validation, storage, credential protection.
`backend/history.py`: historical validation, immutable evidence and descriptive summaries.
`backend/models.py`: experimental fitting, probability calculations and model diagnostics.
`backend/odds.py`: manual price evidence, settlement-aware value and margin calculations.
`backend/backtesting.py`: receipt-aware replay, frozen calibration, holdout scoring and simulation.
`backend/public_data.py`: documented CC0 feed, bounded downloads, verified cache hashes and import validation.
`frontend/src`: responsive dashboard; production assets served by FastAPI.
`tests`: labelled synthetic fixtures for automated backend checks only.

Next: inspect current official provider documentation, select one provider and implement
authorised fixtures/statistics integration with coverage, provenance and usage monitoring.
API-Football, Sportmonks and Football-Data.org are credential slots, not implemented
adapters. Historical depth, xG, corners, disciplinary/referee data, injuries, line-ups
and bookmaker odds may require paid subscriptions and vary by league and plan. No
provider coverage has been verified. No scraping integrations exist.

Next work includes independent real-data validation, additional markets, qualifying selections
and scheduling, and immutable performance
tracking in the requested phase order. A trained/validated model is a prerequisite for
recommendations; saved thresholds alone do not establish statistical reliability.
