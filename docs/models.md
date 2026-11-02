# Experimental modelling methodology

These are research baselines, not calibrated betting recommendations. No real-world
accuracy, profitability, confidence interval or optimal hyperparameter claim has
been established. Automated tests use labelled synthetic data solely for software
and mathematical validation. The first live provider integration remains blocked
as documented in [provider integration status](provider-integration.md).

## Dataset and information cutoff

Only matches from the selected fixture's normalised competition enter the fitting
dataset. Historical kickoffs must precede the run cutoff (the current time), and
both the source observation time and this computer's receipt time must be at or
before that cutoff. Results outside the configured historical window are excluded.
No future match or later correction can enter an earlier saved snapshot.

Source resolution is performed as of the cutoff. Conflicting goal scores are
excluded from goal-based models and Elo. The xG model separately requires both
reported home and away xG to be present and non-conflicting. Missing xG is never
replaced by actual goals or a made-up estimate. Manual source authenticity and
regulation-time settlement assumptions have not been independently verified.

Each run stores the original fixture details, cutoff/start dates, options, minimum
sample, evidence observation IDs, a SHA-256 dataset fingerprint including match
identity and evidence, model version, all probabilities or refusal reasons, and
warnings. New runs append records; no API updates or deletes model snapshots.

## Opponent-adjusted Poisson strengths

For home team i and away team j:

```
log(lambda_home) = intercept + home_effect + attack_i + concession_j
log(lambda_away) = intercept + attack_j + concession_i
```

Attack and defensive-concession effects are centred to sum zero. All strengths
and home advantage are fitted jointly by minimising recency-weighted Poisson
negative log-likelihood plus an L2 penalty on team effects. Higher concession
effects mean weaker defence. The fitted home goal multiplier is exp(home_effect),
not a manually imposed football advantage. Likelihood constants independent of
parameters are omitted from the optimisation objective; the objective is not an
out-of-sample log-loss score.

Weights decline exponentially with age: `2^(-age_days / half_life_days)`. Their mean
is normalised to one for fitting so the strength penalty has a consistent scale.
The effective sample is `(sum weights)^2 / sum(weights^2)`.

Defaults: 730-day training window, 180-day half-life, strength L2 penalty 1. These
are visible, editable research hyperparameters. They have not been selected using
chronological validation. Probability filters do not alter the objective or outputs.

Safety/coverage gates: at least max(30, configured minimum historical sample) raw
and effective matches, at least the configured raw/effective appearances for each
selected team (default 5), a connected competition graph, at most 100 teams and
5,000 matches. These engineering gates are not scientific certification of adequacy.
An unobserved team, disconnected comparison, failed optimiser or boundary fit is
withheld rather than replaced by uniform probabilities.

For numerical stability, optimisation bounds are [-4,3] for the log intercept,
[-2,2] for home effect and raw team parameters. A fit reaching a boundary is refused.
Predicted rates outside [0,20] goals are refused; they are never clipped to force
probabilities into a preferred range.

## Dixon–Coles adjustment

The same fitted Poisson rates are used. The low-score factors are:

| Score | Factor |
| --- | --- |
| 0–0 | 1 - lambda_home × lambda_away × rho |
| 0–1 | 1 + lambda_home × rho |
| 1–0 | 1 + lambda_away × rho |
| 1–1 | 1 - rho |
| Other scores | 1 |

Rho maximises weighted likelihood **conditionally on the Poisson strength fit**.
This is not a joint re-estimation of all Dixon–Coles parameters. At least 10
low-score observations are required as a research guard. Bounds enforce strictly
positive factors on all four cells for every training fixture. A boundary rho fit
is withheld. If the fitted rho yields a negative factor for the selected future
fixture's rates, its adjustment is withheld rather than clipped.

Poisson and Dixon–Coles share data and goal strengths; agreement is not independent
confirmation. No assertion that the adjustment outperforms Poisson is made before
out-of-sample evaluation.

## Reported-xG model

The same attack/defence and home-effect design is fitted to supplied fractional
xG through a Poisson quasi-likelihood mean objective. It uses its own eligible
paired-xG subset and the same coverage gates. Conversion of these expected rates
to goal probabilities assumes a Poisson goal distribution and remains unvalidated.
Model-estimated future goals are explicitly distinct from reported historical xG.
Non-penalty xG, shots, big chances and conversion metrics are not supplied or inferred.

## Scores and markets

Independent Poisson home/away score masses form a joint grid; Dixon–Coles adjusts
the four low-score cells. An adaptive grid bounds omitted probability below 1e-9;
the retained grid is normalised to remove only the tiny numerical tail. This is a
numerical operation, not probability calibration. Negative cells or failed mass
validation are refused.

The grid produces 1X2, double chance, goal totals, BTTS, team over 0.5 goals and
draw-no-bet win/push/loss states, plus the eight highest-probability scorelines.
Draw-no-bet pushes are retained explicitly; `1 / win_probability` is not reported
as draw-no-bet fair odds. There is no half-time/full-time joint model, corners or
bookings model yet. Dependent market probabilities are never multiplied together.

## Elo and recent form

Elo starts every competition team at 1500 at the window's beginning, processes
eligible results chronologically, and uses expected match score
`1 / (1 + 10^((opponent_rating - rating)/400))`. Draws contribute 0.5, wins 1,
losses 0; the default K is 20. These conventional scale/update choices are research
hyperparameters. There is no arbitrary home-rating bonus or cross-league strength
adjustment. Selected teams in disconnected result groups are not compared.
Ratings are context, not calibrated home/draw/away probabilities.

Recent form describes up to 20 latest eligible team results. Time-weighted scoring
and conceding rates and effective samples are shown. Residuals compare goals with
the opponent/venue-adjusted fitted Poisson baseline. These are **in-sample**
descriptions, not independent predictive effects; they are not fed back into the
probability model. Goals-minus-reported-xG is computed only on paired observations
and shows its actual sample. Missing player, referee, injury, lineup, travel or
weather information has no invented numerical adjustment.

## Required validation before recommendations

Chronological training/validation/test splits, realistic information availability,
calibration, Brier/log loss, uncertainty estimation, model comparisons and stability
across competitions are still required. Retrospective datasets imported today do
not establish historically available information. An audited provider-availability
contract is needed before credible historical evaluation.

No ensemble is constructed without learned, validated weights. No 75% prediction
filter is applied to manufacture high probabilities. Top Five remains empty and
ROI unavailable until the later validation and odds/value phases establish eligibility.
