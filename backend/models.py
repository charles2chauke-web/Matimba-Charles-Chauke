"""Experimental statistical baselines, never betting recommendations.

Poisson strengths are fitted jointly against opponents. Dixon–Coles rho is
fitted conditionally on those same strengths, so the two are not independent.
"""
import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
from scipy.optimize import minimize, minimize_scalar
from scipy.stats import poisson

from backend import history

VERSION = 'experimental-0.1.0'


class ModelUnavailable(ValueError):
    pass


def score_distribution(home_rate, away_rate, rho=0.0):
    """Normalised joint distribution with an explicitly bounded omitted tail."""
    if any(not math.isfinite(x) for x in (home_rate, away_rate, rho)):
        raise ModelUnavailable('Nonfinite model parameter')
    if not 0 <= home_rate <= 20 or not 0 <= away_rate <= 20:
        raise ModelUnavailable('Expected goals are outside the supported numerical range [0, 20]')
    adjustments = np.array([[1-home_rate*away_rate*rho, 1+home_rate*rho],
                            [1+away_rate*rho, 1-rho]])
    if np.any(adjustments < 0):
        raise ModelUnavailable('Fitted low-score adjustment is invalid for these team rates')
    limit = max(2, int(max(poisson.ppf(1-1e-12, home_rate), poisson.ppf(1-1e-12, away_rate))))
    grid = np.outer(poisson.pmf(np.arange(limit+1), home_rate), poisson.pmf(np.arange(limit+1), away_rate))
    grid[:2, :2] *= adjustments
    mass = float(grid.sum())
    if not math.isfinite(mass) or mass <= 0 or abs(1-mass) > 1e-9:
        raise ModelUnavailable('Score distribution failed numerical mass validation')
    return grid/mass, max(0., 1-mass)


def markets(home_rate, away_rate, rho=0.0):
    grid, tail = score_distribution(home_rate, away_rate, rho)
    home, draw, away = float(np.tril(grid, -1).sum()), float(np.trace(grid)), float(np.triu(grid, 1).sum())
    indices = np.indices(grid.shape)
    total = indices.sum(axis=0)
    goals = {}
    for line in (.5, 1.5, 2.5, 3.5):
        under = float(grid[total < line].sum())
        goals[f'under_{line}'] = under
        goals[f'over_{line}'] = 1-under
    both = float(grid[1:, 1:].sum())
    order = np.argsort(grid.ravel())[-8:][::-1]
    return {'expected_goals': {'home': home_rate, 'away': away_rate},
            'result': {'home': home, 'draw': draw, 'away': away}, 'goals': goals,
            'btts': {'yes': both, 'no': 1-both},
            'double_chance': {'home_or_draw': home+draw, 'away_or_draw': away+draw, 'home_or_away': home+away},
            'draw_no_bet': {'home': {'win': home, 'push': draw, 'loss': away},
                            'away': {'win': away, 'push': draw, 'loss': home}},
            'team_goals': {'home_over_0.5': float(grid[1:, :].sum()), 'away_over_0.5': float(grid[:, 1:].sum())},
            'scorelines': [{'home': int(i//grid.shape[1]), 'away': int(i%grid.shape[1]),
                           'probability': float(grid.ravel()[i])} for i in order],
            'omitted_tail': tail, 'rho': rho}


def recency_weights(rows, cutoff, half_life):
    age = np.array([(cutoff-datetime.fromisoformat(r['kickoff_utc'])).total_seconds()/86400 for r in rows])
    return np.exp(-math.log(2)*age/half_life)


@dataclass
class StrengthFit:
    teams: list
    coefficients: np.ndarray
    sample: int
    effective_sample: float
    objective: float

    def design(self, home, away):
        if home not in self.teams or away not in self.teams:
            raise ModelUnavailable('Both teams must be present in the fitted competition dataset')
        return design(self.teams, [(home, away)])

    def rates(self, home, away):
        rates = np.exp(self.design(home, away) @ self.coefficients)
        return float(rates[0]), float(rates[1])

    def diagnostics(self):
        return {'sample': self.sample, 'effective_sample': self.effective_sample,
                'teams': len(self.teams), 'home_goal_multiplier': float(np.exp(self.coefficients[1])),
                'objective': self.objective, 'out_of_sample_validated': False}


def design(teams, fixtures):
    count = len(teams)
    lookup = {team: i for i, team in enumerate(teams)}
    matrix = np.zeros((2*len(fixtures), 2+2*count))
    matrix[:, 0] = 1
    matrix[::2, 1] = 1
    # Centred attack and defensive-concession effects have sum zero.
    matrix[:, 2:] = -1/count
    for i, (home, away) in enumerate(fixtures):
        h, a = lookup[home], lookup[away]
        matrix[2*i, 2+h] += 1
        matrix[2*i, 2+count+a] += 1
        matrix[2*i+1, 2+a] += 1
        matrix[2*i+1, 2+count+h] += 1
    return matrix


def check_coverage(rows, home, away, minimum, min_team):
    if len(rows) < minimum:
        raise ModelUnavailable(f'Need at least {minimum} eligible competition matches; found {len(rows)}')
    counts = Counter(team for r in rows for team in (r['home_key'], r['away_key']))
    if counts[home] < min_team or counts[away] < min_team:
        raise ModelUnavailable(f'Each selected team needs at least {min_team} historical appearances; found {counts[home]} and {counts[away]}')
    if len(counts) > 100:
        raise ModelUnavailable('Dataset exceeds the 100-team fitting limit; reduce the training window')
    graph = {team: set() for team in counts}
    for row in rows:
        graph[row['home_key']].add(row['away_key'])
        graph[row['away_key']].add(row['home_key'])
    connected, pending = set(), [home]
    while pending:
        team = pending.pop()
        if team not in connected:
            connected.add(team)
            pending.extend(graph[team]-connected)
    if len(connected) != len(counts):
        raise ModelUnavailable('Competition match graph is disconnected; team strengths cannot be compared reliably')


def fit_strength(rows, cutoff, half_life=180, ridge=1.0, metric='goals'):
    teams = sorted({team for r in rows for team in (r['home_key'], r['away_key'])})
    matrix = design(teams, [(r['home_key'], r['away_key']) for r in rows])
    response = np.array([r['metrics'][f'{metric}_{side}'] for r in rows for side in ('home', 'away')], dtype=float)
    weights = recency_weights(rows, cutoff, half_life)
    if float(weights.sum()) <= 0 or not np.all(np.isfinite(response)) or np.any(response < 0):
        raise ModelUnavailable('Invalid response data or recency weights')
    effective = float(weights.sum()**2 / np.square(weights).sum())
    weights = np.repeat(weights/weights.mean(), 2)
    initial = np.zeros(matrix.shape[1])
    initial[0] = np.log(max(float(np.average(response, weights=weights)), .05))
    penalty = np.ones(matrix.shape[1])*ridge
    penalty[:2] = 0

    def objective(coefficients):
        linear = matrix @ coefficients
        means = np.exp(linear)
        loss = float(np.sum(weights*(means-response*linear)) + .5*np.sum(penalty*coefficients**2))
        gradient = matrix.T @ (weights*(means-response)) + penalty*coefficients
        return loss, gradient

    result = minimize(objective, initial, jac=True, method='L-BFGS-B',
                      bounds=[(-4, 3), (-2, 2)] + [(-2, 2)]*(len(initial)-2),
                      options={'maxiter': 500, 'ftol': 1e-10, 'gtol': 1e-6})
    if not result.success or not math.isfinite(result.fun):
        raise ModelUnavailable('Strength optimisation did not converge; no probabilities were released')
    if any(abs(value-bound) < 1e-5 for value, pair in zip(result.x, [(-4,3),(-2,2)]+[(-2,2)]*(len(initial)-2)) for bound in pair):
        raise ModelUnavailable('Strength fit reached a numerical parameter boundary; inspect the dataset')
    return StrengthFit(teams, result.x, len(rows), effective, float(result.fun))


def fit_rho(rows, fit, cutoff, half_life):
    rates = np.array([fit.rates(r['home_key'], r['away_key']) for r in rows])
    h, a = rates[:, 0], rates[:, 1]
    # Strict positivity for all four adjusted cells, including unobserved cells.
    low = float(max(np.max(-1/h), np.max(-1/a))) + 1e-8
    high = float(min(1., np.min(1/(h*a)))) - 1e-8
    if low >= high:
        raise ModelUnavailable('No feasible low-score correlation interval')
    weights = recency_weights(rows, cutoff, half_life)
    scores = np.array([[r['metrics']['goals_home'], r['metrics']['goals_away']] for r in rows])

    def objective(rho):
        tau = np.ones(len(rows))
        for score, values in [((0,0), 1-h*a*rho), ((0,1), 1+h*rho), ((1,0), 1+a*rho), ((1,1), np.full(len(rows), 1-rho))]:
            mask = (scores[:,0] == score[0]) & (scores[:,1] == score[1])
            tau[mask] = values[mask]
        return -float(np.sum(weights*np.log(tau)))

    low_count = int(np.sum((scores[:,0] <= 1) & (scores[:,1] <= 1)))
    if low_count < 10:
        raise ModelUnavailable(f'Need at least 10 low-score observations for rho; found {low_count}')
    result = minimize_scalar(objective, bounds=(low, high), method='bounded', options={'xatol':1e-8})
    if not result.success or min(result.x-low, high-result.x) < 1e-5:
        raise ModelUnavailable('Low-score correlation fit is unstable or reached its boundary')
    return float(result.x), low_count


def elo_update(home, away, home_goals, away_goals, k=20.):
    expected = 1/(1+10**((away-home)/400))
    actual = 1. if home_goals > away_goals else .5 if home_goals == away_goals else 0.
    change = k*(actual-expected)
    return home+change, away-change


def elo_context(rows, home, away, k=20.):
    graph = {}
    for row in rows:
        graph.setdefault(row['home_key'],set()).add(row['away_key'])
        graph.setdefault(row['away_key'],set()).add(row['home_key'])
    connected, pending = set(), [home]
    while pending:
        team = pending.pop()
        if team not in connected:
            connected.add(team)
            pending.extend(graph.get(team,set())-connected)
    if away not in connected:
        return None
    ratings, counts = {}, Counter()
    for row in sorted(rows, key=lambda r: (r['kickoff_utc'], r['id'])):
        h, a = row['home_key'], row['away_key']
        ratings[h], ratings[a] = elo_update(ratings.get(h,1500.), ratings.get(a,1500.), row['metrics']['goals_home'], row['metrics']['goals_away'], k)
        counts.update((h,a))
    return {'home': ratings.get(home), 'away': ratings.get(away), 'home_sample': counts[home], 'away_sample': counts[away],
            'k': k, 'initial_rating': 1500, 'home_advantage': 0,
            'note': 'Descriptive Elo baseline. K and scale are research hyperparameters; no calibrated 1X2 probabilities or cross-competition adjustments.'}


def form_context(rows, team, fit, cutoff, half_life):
    selected = sorted([r for r in rows if team in (r['home_key'], r['away_key'])], key=lambda r:r['kickoff_utc'], reverse=True)[:20]
    weights = recency_weights(selected, cutoff, half_life)
    outcomes, gf, ga, expected_for, expected_against, xg = [], [], [], [], [], []
    for row in selected:
        side = 'home' if row['home_key'] == team else 'away'
        other = 'away' if side == 'home' else 'home'
        scored, conceded = row['metrics']['goals_'+side], row['metrics']['goals_'+other]
        rates = fit.rates(row['home_key'], row['away_key'])
        expected_for.append(rates[0 if side == 'home' else 1])
        expected_against.append(rates[1 if side == 'home' else 0])
        gf.append(scored); ga.append(conceded)
        outcomes.append('W' if scored > conceded else 'L' if scored < conceded else 'D')
        xg.append(row['metrics']['xg_'+side])
    paired = [(g,x) for g,x in zip(gf,xg) if x is not None]
    return {'sample': len(selected), 'form': outcomes, 'effective_sample': float(weights.sum()**2/(weights**2).sum()),
            'weighted_goals_for': float(np.average(gf,weights=weights)), 'weighted_goals_against': float(np.average(ga,weights=weights)),
            'opponent_adjusted_scoring_residual': float(np.average(np.array(gf)-expected_for,weights=weights)),
            'opponent_adjusted_conceding_residual': float(np.average(np.array(ga)-expected_against,weights=weights)),
            'reported_xg_sample': len(paired),
            'goals_minus_reported_xg': sum(g-x for g,x in paired)/len(paired) if paired else None,
            'days_since_last_match': (cutoff-datetime.fromisoformat(selected[0]['kickoff_utc'])).total_seconds()/86400,
            'note': 'Time-weighted descriptive form. Opponent residuals use the in-sample Poisson fit; they are not independent or validated predictive effects.'}


def load_dataset(connection, competition, cutoff, training_days):
    start = (cutoff-timedelta(days=training_days)).isoformat()
    end = cutoff.isoformat()
    records = connection.execute('''SELECT * FROM historical_matches WHERE competition_key=?
      AND kickoff_utc>=? AND kickoff_utc<? AND EXISTS (SELECT 1 FROM historical_observations o
      WHERE o.match_id=historical_matches.id AND o.observed_at<=? AND o.imported_at<=?)
      ORDER BY kickoff_utc DESC,id DESC LIMIT 5001''', (history.normalized(competition), start, end, end, end)).fetchall()
    if len(records) > 5000:
        raise ModelUnavailable('More than 5,000 eligible matches; reduce the training window')
    rows, evidence = [], []
    for record in records:
        items = history.observations(connection, record['id'], end)
        resolved = history.resolve(items)
        rows.append({**dict(record), 'metrics': resolved['metrics'], 'conflicts': resolved['conflicts']})
        evidence.extend({'id': item['id'], 'match_identity': record['identity'], 'metrics': item['metrics'], 'source': item['source'],
                         'observed_at': item['observed_at'], 'imported_at': item['imported_at']} for item in items)
    fingerprint = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    return rows, fingerprint, [item['id'] for item in evidence]


def analyse(rows, fixture, cutoff, options, minimum, include_context=True):
    home, away = history.normalized(fixture['home']), history.normalized(fixture['away'])
    scored = [r for r in rows if all(r['metrics'][f'goals_{side}'] is not None for side in ('home','away'))]
    result = {'status': 'insufficient_data', 'models': {}, 'unavailable': {}, 'elo': None, 'form': None,
              'eligible_matches': len(scored), 'excluded_goal_conflicts': len(rows)-len(scored),
              'calibration': None, 'uncertainty': None, 'ensemble': None, 'recommendation': None,
              'reliability': 'Insufficient for betting · sources unverified; models unvalidated',
              'warnings': ['Experimental probabilities are uncalibrated; no recommendation or betting-value assessment.',
                           'Poisson and Dixon–Coles share strengths and are not independent evidence.',
                           'Injuries, line-ups, weather and other unavailable factors have no numerical effects.']}
    minimum = max(30, minimum)
    def prepare(data, metric='goals'):
        check_coverage(data, home, away, minimum, options['min_team_matches'])
        weights = recency_weights(data,cutoff,options['half_life_days'])
        effective = float(weights.sum()**2/(weights**2).sum())
        if effective < minimum:
            raise ModelUnavailable(f'Time-weighted effective sample {effective:.1f} is below the required {minimum}')
        for team in (home,away):
            selected = [row for row in data if team in (row['home_key'],row['away_key'])]
            weights = recency_weights(selected,cutoff,options['half_life_days'])
            effective = float(weights.sum()**2/(weights**2).sum())
            if effective < options['min_team_matches']:
                raise ModelUnavailable('A selected team has insufficient effective historical sample after time weighting')
        return fit_strength(data,cutoff,options['half_life_days'],options['ridge'],metric)
    try:
        fit = prepare(scored)
        h, a = fit.rates(home, away)
        result['models']['poisson'] = {**markets(h,a), 'diagnostics': fit.diagnostics()}
    except ModelUnavailable as error:
        result['unavailable']['poisson'] = str(error)
        result['unavailable']['dixon_coles'] = 'Requires a stable Poisson strength fit'
    else:
        result['status'] = 'experimental'
        if include_context:
            result['form'] = {'home': form_context(scored,home,fit,cutoff,options['half_life_days']),
                              'away': form_context(scored,away,fit,cutoff,options['half_life_days'])}
        try:
            rho, low_count = fit_rho(scored, fit, cutoff, options['half_life_days'])
            result['models']['dixon_coles'] = {**markets(h,a,rho), 'diagnostics': {**fit.diagnostics(), 'low_score_sample': low_count,
                'method': 'Conditional rho maximum likelihood; shares Poisson goal strengths'}}
        except ModelUnavailable as error:
            result['unavailable']['dixon_coles'] = str(error)
    if include_context and home in {r['home_key'] for r in scored}|{r['away_key'] for r in scored} and away in {r['home_key'] for r in scored}|{r['away_key'] for r in scored}:
        result['elo'] = elo_context(scored,home,away,options['elo_k'])
    xg_rows = [r for r in rows if all(r['metrics'][f'xg_{side}'] is not None for side in ('home','away'))]
    try:
        xg_fit = prepare(xg_rows,metric='xg')
        h,a = xg_fit.rates(home,away)
        result['models']['xg_poisson'] = {**markets(h,a), 'diagnostics': {**xg_fit.diagnostics(),
            'method': 'Poisson quasi-likelihood on reported fractional xG; opponent and venue adjusted'}}
        result['status'] = 'experimental'
    except ModelUnavailable as error:
        result['unavailable']['xg_poisson'] = str(error)
    return result
