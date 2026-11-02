# Chronological research backtesting

This engine evaluates a specified research workflow. It does not certify a model,
automatically deploy a calibrator, fill the Top Five or establish real-world
profitability. Sources, manual settlement results and executable prices remain
unverified. All automated test worlds are explicitly synthetic and isolated.

## Information-time contract

Every target is a completed historical result in the selected normalised competition.
Decision time is kickoff minus the configured lead (default 60 minutes). Date windows
use **decision dates in SAST**. A 00:30 SAST kickoff with a 60-minute lead belongs to
the previous day's 23:30 decision period.

The calibration window runs from `calibration_start` to the start of `test_start`.
The holdout runs from `test_start` through `test_end` inclusive. Target kickoffs must
already be in the past at report creation. Calibration labels must be observed and
received before the holdout begins. A result arriving late or remaining conflicting
at that freeze is excluded from calibration even if it is known when the report runs.
Holdout outcomes are resolved from evidence known at report creation.

### Walk-forward replay

The statistical baselines refit sequentially at each target decision cutoff.
Training matches must have earlier kickoffs and both observation and receipt times
at or before the cutoff. The target's own result cannot enter its fit. Earlier
holdout results may enter later rolling fits **only after becoming known**; future
labels and corrections cannot. Hyperparameters remain fixed for the study.

Replay also requires a recorded **scheduled fixture identity** by the decision
cutoff. CSV fixture imports now append immutable status observations when a fixture
is created or its status changes. Identical refreshes preserve the original receipt.
The final schedule is never assumed to have been known earlier. Unknown historical
schedules or a known postponed/cancelled/live status withhold replay.

An older installation can preserve only the latest fixture row's asserted status
and existing receipt time during migration. Missing earlier schedule states are
not reconstructed or backdated. Existing pre-match model snapshots can still be
audited; a later non-scheduled observation at/before the decision withholds that
forecast. Tied timestamps are handled conservatively rather than assuming an
ambiguous status change occurred after prediction.

Existing model gates apply: minimum raw/effective samples, per-team coverage,
connected competition graph, convergence and numerical limits. Missing xG does
not become goal-derived xG. Missing models are reported rather than replaced by
uniform probabilities. Replayed forecasts are labelled simulations; they are not
inserted into the original live model-run history.

A historical dataset first imported today is not evidence that this computer knew
it years ago. Such a replay can correctly produce no evaluable predictions. There
is no production flag or API to backdate receipt times. An audited provider/data
availability contract is required before relaxing this conservative constraint.

### Saved prediction audit

The auditor selects the latest original model snapshot whose generation time was
at or before the target decision cutoff, within the configured age limit and with
the exact fixture identity/kickoff. It never selects a snapshot by how well it
predicted the result. A snapshot generated after the decision cannot enter merely
because it still preceded kickoff. No probabilities are refitted or overwritten.

This path becomes useful as the private application accumulates genuine pre-match
predictions and later results. The currently installed engine does not yet retrieve
results automatically; permitted manual imports supply settlement evidence.

## Calibration and holdout scores

Each model independently fits one temperature parameter on its eligible earlier
calibration forecasts, using multinomial log loss. At least 20 labels and two
outcome classes are required as engineering guards, not scientific adequacy claims.
The temperature range is 0.25–4. A failed or boundary fit is withheld. Scaling is
`softmax(log(p) / T)`, with a numerical 1e-15 log floor.

Temperature is frozen at the holdout start. Holdout outcomes never fit it. The raw
and scaled holdout scores are both reported, including deterioration. This changes
probabilities only in the saved study; it never edits original model snapshots or
activates a production calibrator. No ensemble is built from arbitrary weights.

Saved audits retain the original model version, research parameters and sample
guard for each forecast. Raw audit scores can describe a sequence of changing
research settings; they are not evidence for one fixed candidate. Temperature is
withheld when original configurations differ across the eligible calibration and
holdout cohorts, or their settings are unknown. Narrow the audit or use a fixed
walk-forward configuration instead; the engine never chooses a favourable subset
based on holdout outcomes.

The 1X2 metrics are:

- Accuracy: argmax home/draw/away class against the reported result.
- Multiclass Brier: mean sum of three squared errors, range 0–2.
- Log loss: mean negative log probability assigned to the observed class, with
  a 1e-15 scoring floor for numerical stability.
- Mean classwise ECE: mean of home/draw/away expected calibration errors over ten
  fixed probability bins. Each class's bin errors are weighted by its sample share.
- Calibration curves: mean predicted probability versus observed frequency in
  those ten bins, with counts. Empty bins are missing, not fabricated points.

Models can have different coverage. Paired comparison tables recompute raw metrics
on exactly the same holdout matches for each model pair. They do not automatically
declare a winner. Zero evaluated predictions produce unavailable scores and the
explicit status `no_evaluable_predictions`, never a successful zero-test validation.

## Prespecified simulation strategy

Each study records one model and one fixed market/selection/line, probability and
EV filters, flat stake, quote/model age limits and probability basis. These options
are fixed before the engine evaluates the study, but user selection after observing
past results/repeatedly inspecting holdouts can still create selection bias. There
is no claim of a preregistered, untouched independent test.

The default strategy uses raw experimental probabilities. An explicit alternative
uses frozen temperature-scaled 1X2 probabilities for 1X2, double chance or draw-no-bet.
If the requested calibrator is unavailable, that strategy is withheld; it does not
silently fall back to raw probabilities. Goals, BTTS and team-goal strategies use
their original raw model probabilities, since a 1X2 temperature does not calibrate
the entire score distribution.

The probability filter uses unconditional win probability; pushes are separate.
The default 75% filter is a configurable preference, not a validated cutoff and
never modifies the estimates. At most one simulated selection is made per match.
There are no accumulators, dependent-probability multiplication or loss-recovery stakes.

## Historical odds, settlement and finances

Only matching regulation-time quotes observed **and received** by the historical
decision cutoff can be used. Best price means the largest fresh, non-conflicting
imported price at that time. Missing prices, stale quotes, mismatched identities
and tied price conflicts cannot become a trade. Present-day odds never fill a
historical gap. An imported quote does not prove actual executable availability.

The price already contains its offered bookmaker margin; no invented margin or
current replacement is imposed. The simulation excludes taxes, commission, stake
limits, account restrictions, bankroll and exposure limits. It uses constant stake
units, not an actual betting account or a bankroll management module.

Wins return `stake × (odds − 1)` net profit; losses lose the stake; pushes return it
with zero net profit. ROI is net profit / total staked, **including push stakes**.
Win rate excludes pushes, with wins/losses/pushes reported separately. Zero bets
leave profit, ROI, win rate and drawdown unavailable. They do not prove break-even.

The P&L curve orders simulations by the recorded result-evidence availability time,
not by an invented immediate settlement time. Latest evidence/late corrections can
make that time later than a real bookmaker's unknown settlement. For tied timestamps,
losses precede wins as an explicit conservative within-batch ordering assumption.
Maximum drawdown is the largest peak-to-trough decline in cumulative unit profit,
starting from zero. It is not a bankroll percentage or proof of real cash-flow risk.
Losing trades and periods remain visible. Closing-line value remains unavailable
without verified closing data.

Financial subgroup tables show only the chosen strategy's actual simulated trades
by competition, market and unconditional probability decile. They do not invent
returns for unpriced or untraded markets. The selected-market binary score uses
all eligible holdout forecasts before betting filters, excluding actual pushes and
using conditional non-push probabilities where appropriate. Its Brier range is 0–1,
distinct from the multiclass 1X2 Brier.

## Bounds, persistence and local API

Studies evaluate at most 100 combined calibration/holdout targets, sequentially.
Larger windows are rejected before fitting; no targets are silently truncated.
Replay inherits the 5,000-training-match/100-team limit. Saved audits cap candidate
runs at 5,000, and each historical price lookup caps evidence at 10,000 observations.
These limits bound local-machine work, not statistical reliability.

- `GET /api/backtest-context`: competition coverage, earliest history receipt and
  stored model-run count to help choose feasible studies.
- `POST /api/backtests`: explicit study configuration. The dashboard supplies complete
  defaults and date/strategy controls.
- `GET /api/backtests?limit=50&offset=0`: saved report metadata, maximum 100 per page.
- `GET /api/backtests/{id}`: original immutable report.

Reports retain options, model versions, decisions, raw predictions, original run IDs
or replay evidence references, result references, calibration fit/freeze, holdout
metrics, paired samples, exclusions, simulated prices/trades and a report fingerprint.
New runs append records. Existing model/value/history snapshots are not edited.

Back up all of `data/` with the app stopped before upgrading. The backtest table is
created additively. Live provider integration and independent real-data validation
remain outstanding; no report automatically approves a model for daily recommendations.
