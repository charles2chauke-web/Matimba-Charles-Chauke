# Strict daily update

All new model generation is now limited to today in SAST. Previous references to
seven-day targets are historical workflow documentation, not active prediction
permissions. See [strict daily policy](strict-daily-policy.md) for the exact market,
major-competition and corner/yellow-card modelling contracts.

# Working data-to-model workflow

The model can now produce experimental estimates from reported football data,
without requiring a subscription for the public fallback. This does not establish
statistical calibration, executable betting value or profitability.

## Use it

1. Run `start-windows.cmd` on Windows (Python/Node prerequisites are in README.md).
2. Open the private local address shown by the launcher.
3. Choose **Run Model → Load data & run model**.
4. Open **View predictions** next to a match with available fitted models.

The workflow fetches up to three season files, validates them before database
writes, imports results and listed fixtures, and attempts a bounded batch of up to
12 research analyses. Repeating it preserves unchanged result receipt times and
appends new model snapshots. Insufficient coverage is reported per match. The
maximum batch is 30, and the default upcoming horizon is seven days.

## Public feed documentation and permission

Reviewed 2 November 2026:

- https://github.com/openfootball/football.json — documented public raw JSON API.
- https://github.com/openfootball/football.json/blob/master/LICENSE.md — CC0 1.0.
- https://github.com/openfootball/england — upstream English competition sources.

The publisher dedicates its schema, data and scripts to the public domain and
explicitly documents raw GitHub URLs as an API requiring no key. The importer uses
only fixed `raw.githubusercontent.com/openfootball/football.json/master/{season}/en.1.json`
destinations with validated season strings. There is no arbitrary remote fetch or
website scraping, no redirect following, and no football-provider credential header.
TLS verification remains enabled.

The published season files contain both nested `score.ft` / optional `score.ht`
arrays and legacy flat `[home,away]` full-time arrays. Both are validated. Missing
scores are not zero, missing times are skipped, and future full-time scores are
rejected. Only the explicitly mapped English Premier League preset is supported.
Season labels are mapped to one competition for training; literal team names are
preserved without fuzzy aliases.

## Data quality and time limitations

The feed is community-maintained and is not independently verified. Retrieval
today does not make its results current. During real-data validation, its most
recent reported result was **20 September 2026**, although the workflow ran on
**2 November 2026**. Its schedule listed Newcastle–Everton on 2 November. Neither
the result coverage nor that fixture was independently confirmed against an official
live provider. The UI shows the latest reported result and its age explicitly.

The feed supplies local-looking match times without timezone offsets or authoritative
status flags. **Europe/London is an explicit import assumption**, not a verified
timezone statement from the JSON. Python's IANA timezone database applies UK daylight
saving, then the UI displays SAST. The pinned `tzdata` dependency supports Windows.
This assumption remains in source provenance and warnings.

Future unscored entries are stored with status **listed**, not confirmed scheduled.
They can be analysed for research but do not become eligible betting fixtures.
Current-value evaluation still requires scheduled status, and historical replay
still requires a recorded scheduled identity. A public-feed refresh does not
overwrite a manually asserted fixture status. Old, unscored entries do not get
invented final results or live statuses.

Source per-match update timestamps are absent. Observation time is the application's
documented retrieval time, not a fabricated upstream publication time; receipt time
is the actual database import time. Historical backtests cannot treat these new
imports as information received in the past. Repeated unchanged statistics retain
their original receipt, while actual corrections append observations.

xG, corners, cards, referee data, injuries, line-ups and bookmaker prices are not
provided by this feed. They remain missing. The xG-based model is withheld without
reported xG; no fake odds, EV, calibration or Top Five selections are generated.

## Observed working result

On 2 November 2026, the workflow imported **810 reported historical results** from
the 2024/25, 2025/26 and 2026/27 files. The configured 730-day training window left
**720 eligible competition matches** for the first source-listed research fixture.
Poisson and conditional Dixon–Coles models produced normalised outcome estimates.
A fixture involving a team with insufficient effective coverage was withheld.
These counts demonstrate execution on source data, not prediction accuracy.

A complete default refresh then evaluated all 11 listed fixtures in the seven-day
window: **9 produced research analyses and 2 were withheld** for coverage. All 810
previously imported results remained unchanged; no duplicate result observations
were created by the refresh. Saved predictions were opened through the actual
desktop/mobile browser workflow. No synthetic fixtures were used in that run.

## Repeatability and failures

Downloads are limited to 2 MB per season file and time out. Cached bodies are hashed,
schema-validated and revalidated using ETags when available. Invalid new payloads
do not replace the last valid cache. An offline cached fallback is labelled as such
with the original retrieval time; it is never presented as fresh data. Raw data and
cache metadata stay in ignored `data/source-cache/`. The code contains no bundled
fake fixtures or manufactured performance records.

`GET /api/workflow/status` reports actual counts, presence (never value) of a
configured API-Football key, and the last workflow result. `POST /api/workflow/run`
with JSON `{}` automatically uses API-Football when a key is saved, otherwise
the public fallback. `source` can explicitly select `api-football` or `public`.
API controls are `league_id` (default 39) and `api_seasons` (start-year integers);
public `seasons` uses YYYY-YY strings. Shared controls are `horizon_days` and
`max_fixtures`. Only one workflow runs at a time in this private
single-process application.

## API-Football primary integration

The official v3.9.3 documentation was reviewed from the user's uploaded PDF.
The adapter now supports **API Settings → Test connection** and automatic
API-Football results/schedules in Run Model when a key is present. Default
league 39 uses the current season and two preceding season start years.
Namespaced provider history stays separate from community data. Detailed stats,
xG remains missing; pre-match odds are now imported by Top Five. The daily Top
Five screens experimental selections across all leagues available on the current
SAST date. Independent production qualification remains unavailable.

Live access must still be tested on the user's laptop; the development cloud
has neither that local key nor current network access to the provider. See
[provider integration](provider-integration.md) for supported endpoints,
validation, error handling and exact limitations.
