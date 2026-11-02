"""Daily research shortlist. Research ranking never implies qualification."""
import json
from collections import Counter
from datetime import datetime,timedelta,timezone
from backend import odds


def research_order(row,minimum_ev):
    ev=row['expected_value']
    priced_value=ev is not None and ev>0 and ev>=minimum_ev
    return (priced_value,ev if priced_value else row['probability'],row['probability'])


def build(connection,settings,current,model='poisson',mode='research',window=None,max_odds_age_minutes=60,league_ids=None):
    settings={**settings,'window':window or settings['window']}
    utc=current.astimezone(timezone.utc)
    rows=connection.execute("SELECT * FROM fixtures WHERE status IN ('scheduled','listed') AND kickoff_utc>? ORDER BY kickoff_utc,id",(utc.isoformat(),)).fetchall()
    in_window=[]
    for row in rows:
        if league_ids is not None:
            provider=connection.execute('SELECT league_id FROM api_football_fixtures WHERE fixture_id=?',(row['id'],)).fetchone()
            if not provider or provider['league_id'] not in league_ids:continue
        kickoff=datetime.fromisoformat(row['kickoff_utc'])
        if (settings['window']=='7d' and kickoff<current+timedelta(days=7)) or (settings['window']=='24h' and kickoff<current+timedelta(hours=24)) or (settings['window']=='today' and kickoff.astimezone(current.tzinfo).date()==current.date()):
            in_window.append(dict(row))
    candidates=[];excluded=[]
    for fixture in in_window:
        saved=connection.execute('SELECT id,result FROM model_runs WHERE fixture_id=? ORDER BY generated_at DESC,id DESC LIMIT 1',(fixture['id'],)).fetchone()
        def omit(reason):excluded.append({'fixture_id':fixture['id'],'home':fixture['home'],'away':fixture['away'],'reason':reason})
        if saved is None:
            omit('No saved model analysis. Run the data-and-model workflow first.');continue
        snapshot={'id':saved['id'],**json.loads(saved['result'])}
        if model not in snapshot['models']:
            omit('Selected model unavailable in the latest analysis; inspect its coverage report.');continue
        if odds.fixture_identity(snapshot['fixture'])!=odds.fixture_identity(fixture):
            omit('Fixture changed since analysis; generate a new snapshot.');continue
        generated=datetime.fromisoformat(snapshot['generated_at'])
        if generated>current or current-generated>timedelta(hours=24):
            omit('Model snapshot is outside the 24-hour research freshness window.');continue
        diagnostics=snapshot['models'][model].get('diagnostics',{})
        minimum=max(30,settings['min_sample'])
        if diagnostics.get('sample',0)<minimum or diagnostics.get('effective_sample',0)<minimum:
            omit('Historical sample is below the saved research minimum.');continue
        if mode=='value':
            if fixture['status']!='scheduled':
                omit('Value screening requires a scheduled fixture; unconfirmed listings are excluded.');continue
            evidence=connection.execute('SELECT provider_id,last_received_at FROM api_football_fixtures WHERE fixture_id=?',(fixture['id'],)).fetchone()
            if not evidence or current-datetime.fromisoformat(evidence['last_received_at'])>timedelta(hours=24) or datetime.fromisoformat(evidence['last_received_at'])>current:
                omit('Refresh the API-Football schedule; value screening requires provider evidence from the past 24 hours.');continue
            daily=connection.execute('SELECT checked_at FROM top_five_daily_fixtures WHERE provider_id=? AND day=?',
                                     (evidence['provider_id'],current.date().isoformat())).fetchone()
            if not daily or datetime.fromisoformat(daily['checked_at'])>current:
                omit('Build Top Five to confirm this fixture in today’s API schedule; it is absent from the latest daily discovery.');continue
        try:
            values=odds.assess(connection,snapshot,model,utc,utc,{'pricing_time':'current','model':model,'max_odds_age_minutes':max_odds_age_minutes,'max_model_age_hours':24,'fresh_only':True})
        except ValueError:
            omit('Odds evidence exceeded the evaluation limit. Inspect the price history.');continue
        eligible=[r for r in values['rows'] if r['probability']>=settings['min_probability']]
        if not eligible:
            omit('No market reaches your minimum probability; estimates were not adjusted.');continue
        if mode=='value':
            # Agreement is a diagnostic, not an independently validated ensemble.
            alternate='dixon_coles' if model=='poisson' else 'poisson'
            other=snapshot['models'].get(alternate)
            comparison={(r['market'],r['selection'],r['line']):r for r in odds.model_markets(other)} if other else {}
            screened=[]
            for r in eligible:
                alt=comparison.get((r['market'],r['selection'],r['line']))
                quote=r['best_quote']
                if quote is None or alt is None or abs(r['probability']-alt['probability'])>.10:continue
                conservative=min(r['probability'],alt['probability'])
                lower_ev=odds.calculate(conservative,quote['decimal_odds'],min(r['push_probability'],alt['push']))['expected_value']
                if conservative>=settings['min_probability'] and lower_ev>0 and lower_ev>=settings['min_ev']:
                    screened.append({**r,'agreement_probability':alt['probability'],'lower_model_ev':lower_ev})
            eligible=screened
            if not eligible:
                omit('No market meets probability, positive EV, fresh non-conflicting odds and model-agreement filters.');continue
        # Positive-EV rows take precedence only when a genuine imported price exists.
        # Without one, ranking is explicitly probability-only research, not value.
        eligible.sort(key=lambda r:r['lower_model_ev'] if mode=='value' else research_order(r,settings['min_ev']),reverse=True)
        best=eligible[0]
        blockers=['Model has no independently approved calibration/deployment evidence.',
                  'Validated predictive uncertainty is unavailable.',
                  'Source statistics and fixture status have not been independently verified.']
        if fixture['status']!='scheduled':blockers.append('Fixture is listed/unconfirmed, not verified scheduled.')
        if best['best_quote'] is None:blockers.append('No fresh, non-conflicting bookmaker odds; betting value is unverified.')
        elif best['expected_value'] is None or best['expected_value']<settings['min_ev']:blockers.append('Estimated value does not meet the saved EV requirement.')
        candidates.append({'fixture_id':fixture['id'],'run_id':saved['id'],'home':fixture['home'],'away':fixture['away'],
            'competition':fixture['competition'],'kickoff_sast':datetime.fromisoformat(fixture['kickoff_utc']).astimezone(current.tzinfo).isoformat(),
            'fixture_status':fixture['status'],'model':model,'model_generated_at':snapshot['generated_at'],
            'market':best['market'],'selection':best['selection'],'line':best['line'],'probability':best['probability'],
            'push_probability':best['push_probability'],'fair_odds':best['fair_odds'],
            'decimal_odds':best['best_quote']['decimal_odds'] if best['best_quote'] else None,'expected_value':best['expected_value'],
            'bookmaker':best['best_quote']['bookmaker'] if best['best_quote'] else None,
            'odds_observed_at':best['best_quote']['observed_at'] if best['best_quote'] else None,
            'odds_age_minutes':best['best_quote']['age_minutes'] if best['best_quote'] else None,
            'lower_model_ev':best.get('lower_model_ev'),'agreement_probability':best.get('agreement_probability'),
            'selection_stage':'value_screened' if mode=='value' else 'probability_research',
            'sample':diagnostics['sample'],'effective_sample':diagnostics['effective_sample'],
            'data_quality':'Unverified source · research only','qualification_blockers':blockers,'qualified':False,
            'reasons':[f"{diagnostics['sample']} historical matches; effective time-weighted sample {diagnostics['effective_sample']:.1f}.",
                       'One primary research market per match. No dependent probabilities were multiplied.',
                       'Fresh priced value takes priority; otherwise ordering is probability-only, not a value recommendation.']})
    candidates.sort(key=lambda r:r['lower_model_ev'] if mode=='value' else research_order(r,settings['min_ev']),reverse=True)
    counts=Counter();watchlist=[]
    for candidate in candidates:
        if counts[candidate['competition']]>=settings['max_per_league']:
            excluded.append({'fixture_id':candidate['fixture_id'],'home':candidate['home'],'away':candidate['away'],'reason':'Maximum selections per league reached; no extra correlated exposure was added.'});continue
        if len(watchlist)>=5:
            excluded.append({'fixture_id':candidate['fixture_id'],'home':candidate['home'],'away':candidate['away'],'reason':'Outside the five-place research shortlist.'});continue
        counts[candidate['competition']]+=1
        watchlist.append({'rank':len(watchlist)+1,**candidate})
    return {'generated_at':current.isoformat(),'timezone':'Africa/Johannesburg','window':settings['window'],'model':model,
            'qualified':[],'qualified_count':0,'research_watchlist':watchlist,'selections':watchlist,'selection_count':len(watchlist),
            'mode':mode,'max_odds_age_minutes':max_odds_age_minutes,'fixtures_in_window':len(in_window),
            'excluded':excluded,'settings':{**{k:settings[k] for k in ('min_probability','min_ev','max_per_league')},'min_sample':max(30,settings['min_sample'])},
            'message':f'{len(watchlist)} experimental selections met the '+('probability, odds, positive-EV, model-agreement and diversification filters.' if mode=='value' else 'probability/sample/diversification filters; betting value may be unknown.'),
            'qualification_available':False,
            'qualification_explanation':'Production qualification remains unavailable until independent calibration, uncertainty and source-validation evidence is implemented. Research candidates never bypass those requirements.'}
