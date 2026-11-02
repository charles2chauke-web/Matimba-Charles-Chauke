# Provider integration status — 2 November 2026

## Implemented versus blocked

The local historical pipeline and documented CC0 OpenFootball raw-JSON fallback
now work end to end; see [working-model guide](working-model.md). No live
API-Football, Sportmonks or Football-Data.org adapter has been enabled.
The requirement to inspect current official documentation before implementation
remains in force. Access to both documentation sites below returned HTTP 403 from
the cloud's configured proxy. The environment has no API-Football, Sportmonks or
Football-Data key in local encrypted storage or the relevant environment variables.
These failures do not establish that any API subscription is invalid.

Required cloud network additions have been saved in the configuration draft:

- `www.api-football.com`
- `v3.football.api-sports.io`
- `docs.football-data.org`
- `api.football-data.org`

Review and save the network settings to enable documentation access in the cloud.
Saving a draft alone does not apply a runtime policy. Do not bypass the proxy or
disable TLS verification. These cloud settings are separate from Windows networking.

## Documentation to review before the first adapter

First candidate: API-Football / API-Sports, already the default credential slot.
Alternative to evaluate: Football-Data.org. Candidate domains and documentation
locations are references for review, not confirmation of endpoint availability.

- https://www.api-football.com/documentation-v3
- https://docs.football-data.org/general/v4/index.html

For the selected provider, record actual documented endpoint paths, authentication
headers, response schemas, timezones, status codes, pagination, rate-limit headers,
and error envelopes. Confirm with a valid credential using non-destructive requests.
Never put credentials in URLs, logs, exported source references or frontend responses.
Mask upstream response messages; never return a raw provider error containing a key.

Coverage checks still required:

| Capability | Current evidence |
| --- | --- |
| Competitions / league and season access | Not verified against current documentation or subscription |
| Upcoming fixtures and postponed/cancelled/live statuses | Not verified |
| Historical results and team identities | Not verified |
| Goals and half-time scores | Not verified |
| xG / xGA and non-penalty xG | Not verified; never derive from goals and label as reported xG |
| Corners, cards and referee statistics | Not verified |
| Injuries, suspensions and line-ups | Not verified |
| Bookmaker odds and historical timestamps | Not verified |
| Daily/minute usage limits and retry semantics | Not verified |
| Paid plan restrictions and historical depth | Not verified |

Some of these capabilities may be unavailable or require paid plans. No specific
plan promises can be made until the current documentation and account access are checked.
FBref, Understat, Soccerway, Sofascore, Flashscore, WhoScored, Transfermarkt and
FootyStats have no integrations in this project; permitted automated access has
not been established. Do not scrape them or treat visibility as permission.

## Adapter delivery requirements

1. Read and save the verified capability matrix and documentation review date.
2. Use a fixed official HTTPS base URL and documented endpoints; no user-controlled
   destination URLs, redirects to other hosts, or arbitrary remote imports.
3. Retrieve credentials server-side from the existing encrypted store. An account
   connection check must be clearly distinguished from fixture/statistics coverage.
4. Bound response size and pagination; respect usage limits, `Retry-After`, timeouts,
   and quota exhaustion without repeated unchanged retries.
5. Validate the full response before a transaction. Stable provider fixture/team IDs
   must govern reschedules and team aliases. Keep conflicting manual records separate
   until exact reconciliation is established.
   Record immutable fixture-status observations through the same storage contract
   as CSV imports; historical replay must know which schedule was available then.
6. Preserve source, response retrieval time, source update time when available,
   per-statistic missingness and revision history. Never assign invented values.
7. Test authentication failures, quota limits, pagination, unavailable statistics,
   postponed fixtures, reschedules, malformed responses, and idempotent refreshes.
8. Validate one real connection and representative data retrieval without exposing
   credentials. Mock-response tests alone do not prove live integration readiness.

No automatic real-money betting, public deployment or unvalidated model outputs
are authorised by the data collection workflow.
