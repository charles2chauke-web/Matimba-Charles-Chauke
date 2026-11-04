# API-Football integration — 2 November 2026

## Documentation reviewed

The user supplied **API-Football - Documentation.pdf**, a 139-page print of
https://www.api-football.com/documentation-v3, API version **3.9.3**, printed
2 November 2026 at 20:45. Reviewed authentication/account/limits (pages 1–3),
league coverage/seasons (36–40) and fixture statuses/queries (61–66).
PDF SHA-256: `7d5cad6d7fbe7780ef5be3e5706a34efc0a058e52490e9fcb8a7a2bd589ab2a1`.
The official site remains blocked by the development cloud proxy; the supplied
copy resolves the documentation prerequisite. It does not prove live credentials
or response compatibility. Collapsed response examples in the PDF do not establish
all nested fields; the adapter validates the expected structure and fails closed.

Documented direct API-Sports base: `https://v3.football.api-sports.io`.
Authentication: GET requests with the server-side `x-apisports-key` header.
This implementation supports direct API-Sports subscriptions, not RapidAPI keys.

## Implemented

| Capability | Endpoint / behaviour |
| --- | --- |
| Account connection test | `GET /status`; documented as not consuming daily quota. Shows plan, subscription status/end and request usage; discards account name/email. |
| League/season access | `GET /leagues?id=…&season=…`; requires the selected league and season to be returned before importing. Coverage flags are provider statements, not guarantees. |
| Schedules and results | `GET /fixtures?league=…&season=…`; validates complete collections and timezone-aware fixture dates; UI displays SAST. |
| Status mapping | NS scheduled; TBD listed/unconfirmed; PST postponed; CANC/ABD/AWD/WO withheld from pre-match research; live statuses excluded; completed statuses excluded from pre-match research. |
| Regulation-time history | Only FT with non-null `score.fulltime` goals; optional halftime; other statistics remain missing. AET/PEN results do not train this adapter. |
| Stable identities | Persistent provider fixture/team/league mappings; team names pinned on first receipt. Reschedules retain the local fixture ID and append schedule observations. Old prediction/odds identities are never rewritten. |
| Corrections and receipt time | Identical statistics keep their original receipt. Changed results append observations. Removed/rescheduled/withdrawn results receive missing-metric observations; old evidence remains available as of its original receipt. |
| Removed fixtures | A previously imported fixture omitted from a complete response becomes `unavailable`, not a claimed cancellation. It cannot be selected or analysed until returned again. |
| Quota/rate safety | Serial requests, minimum spacing, documented daily/minute rate headers, numeric/date Retry-After, bounded cooldowns, exhausted-quota stop; no automatic retries. |

The collection envelope must contain matching `get`, empty `errors`, consistent
`results`, `response`, and `paging.current=1,total=1`. These league/fixtures
queries have no documented page parameter in the supplied PDF. Multi-page or
inconsistent responses are refused instead of importing partial data or inventing
pagination. Each response is limited to 4 MB / 1,500 fixtures. All selected seasons
are downloaded and validated before a single import transaction. A failed later
season or identity conflict rolls back data imports; a successful account test may
still be retained independently.

Upstream failures can be HTTP failures **or HTTP 200 with nonempty errors**.
Only static sanitised error categories reach the browser. No response bodies,
headers, credentials or upstream exception messages are logged or exported.
TLS verification stays enabled; destinations and paths are fixed; redirects are
not followed. No arbitrary URL input is accepted. No keys are sent to the public feed.

## Use on Windows

1. Update the **whole application**, preserving your entire `data` folder as
   described in [UPDATE-WINDOWS.md](../UPDATE-WINDOWS.md). Updating just the launcher
   does not update the backend or dashboard.
2. Open **API Settings → API-Football → Test connection**. Your saved encrypted key
   is reused. Saving a replacement key clears the previous test. No key goes in chat.
3. After a successful test, open **Run Model → Load data & run model**.
4. Automatic source uses API-Football when a key is present. It never silently
   falls back to public data after a provider failure. The public fallback can be
   selected explicitly. Default API league is 39 (English Premier League); default
   seasons are the current season start year and two previous years. Adjust league
   ID/season start years if necessary for your coverage.
5. Open **View predictions** for a usable analysis. Insufficient sample and no
   upcoming fixtures are valid outcomes; they do not mean the API key failed.

A default three-season run makes one account-status call and up to six data calls.
It analyses up to 12 scheduled matches today in SAST. Limits are configurable
through the existing workflow request. API history is namespaced separately from
manual/community competitions; no fuzzy reconciliation or mixed-source training.
First import can therefore need a full historical window even if public data exists.

## Validation and limits

Automated tests use explicitly synthetic provider responses and real model fitting.
They cover connection/account/quota errors, HTTP-200 errors, privacy, environment
and encrypted credentials, idempotency, corrections, schedule changes, immutable
as-of evidence, missing fields, unsupported pagination, atomic failure, removed
fixtures, stable-ID conflicts, and the import-to-model workflow. Desktop/mobile
browser testing also uses an isolated synthetic store.

**The user has confirmed live account access and fixture/model imports on Windows.**
New odds and daily-discovery paths still require local live validation. The development
cloud has no user key and cannot reach the official API host under its current
network policy. A successful mock test must never be described as a working live
connection. The app stores the actual local connection result and timestamp so
failures can be diagnosed without sharing credentials.

Pre-match bookmaker odds and all-league daily discovery are now available through
Top Five; see [daily shortlist](daily-shortlist.md) for timestamps, market mapping,
pagination bounds and live-validation limitations. The source updates odds about
every three hours; no real-time executable-price guarantee is made.

Reported FT corners/yellow/red-card statistics are now imported for strict daily
count modelling; see [strict daily policy](strict-daily-policy.md).

Not implemented: xG, injuries,
line-ups, automatic scheduling, other provider adapters, independent calibration
approval or production Top Five qualification. Account success does not imply any
of these capabilities or profitable forecasts.

Cloud-only optional configuration still requires applying the saved official-host
allowlist and securely binding `API_FOOTBALL_KEY`; Windows networking and saved local
keys are separate. Never disable TLS verification or bypass a policy denial.
