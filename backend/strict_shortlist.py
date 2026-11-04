"""Strict today-only requested markets, major priority, one stable outcome/match."""
import json
from collections import Counter
from datetime import datetime,timedelta,timezone
from backend import daily_policy,odds,history


def build(connection,settings,current,model='poisson',mode='research',max_odds_age_minutes=180,league_ids=None):
    current=current.astimezone(history.SAST);utc=current.astimezone(timezone.utc)
    candidates=[];excluded=[];in_window=0
    fixtures=connection.execute('''SELECT f.*,p.provider_id,p.league_id,p.last_received_at FROM fixtures f
        JOIN api_football_fixtures p ON p.fixture_id=f.id WHERE f.status='scheduled' AND f.kickoff_utc>? ORDER BY f.kickoff_utc,f.id''',(utc.isoformat(),)).fetchall()
    minimum=max(30,settings['min_sample']);threshold=max(.75,settings['min_probability'])
    for fixture in map(dict,fixtures):
        if league_ids is not None and fixture['league_id'] not in league_ids:continue
        if not daily_policy.is_today(fixture['kickoff_utc'],current):continue
        in_window+=1
        def omit(reason):excluded.append({'fixture_id':fixture['id'],'home':fixture['home'],'away':fixture['away'],'reason':reason})
        daily=connection.execute('SELECT checked_at FROM top_five_daily_fixtures WHERE provider_id=? AND day=?',(fixture['provider_id'],current.date().isoformat())).fetchone()
        if not daily or datetime.fromisoformat(daily['checked_at'])>current or not timedelta(0)<=current-datetime.fromisoformat(fixture['last_received_at'])<=timedelta(hours=24):
            omit('Build today’s list to verify the current API schedule.');continue
        row=connection.execute('SELECT id,result FROM model_runs WHERE fixture_id=? ORDER BY generated_at DESC,id DESC LIMIT 1',(fixture['id'],)).fetchone()
        if not row:omit('No saved analysis for today.');continue
        snapshot=json.loads(row['result']);generated=datetime.fromisoformat(snapshot['generated_at'])
        if generated>current or generated.astimezone(history.SAST).date()!=current.date():omit('The prediction was not generated today in SAST.');continue
        if odds.fixture_identity(snapshot['fixture'])!=fixture['identity']:omit('Schedule changed after prediction; rebuild.');continue
        quotes=connection.execute('''SELECT * FROM odds_observations WHERE fixture_id=? AND fixture_identity=?
            AND observed_at>=? AND observed_at<=? AND imported_at<=? ORDER BY observed_at,id LIMIT 10001''',
            (fixture['id'],fixture['identity'],(utc-timedelta(minutes=max_odds_age_minutes)).isoformat(),utc.isoformat(),utc.isoformat())).fetchall()
        if len(quotes)>10000:omit('Recent odds evidence exceeds the evaluation bound.');continue
        resolved=odds.resolve_quotes([dict(q) for q in quotes],utc,max_odds_age_minutes)
        eligible=[]
        for market in daily_policy.candidates(snapshot,model):
            d=market['diagnostics'];p=market['probability'];alt=market['comparison_probability']
            if min(d.get('sample',0),d.get('effective_sample',0))<minimum or min(p,alt)<threshold or abs(p-alt)>.10:continue
            key=(market['market'],market['selection'],market['line'])
            prices=[q for q in resolved if q['status']=='fresh' and (q['market'],q['selection'],q['line'])==key]
            best=max(prices,key=lambda q:q['decimal_odds']) if prices else None
            conservative=min(p,alt)
            calculation=odds.calculate(conservative,best['decimal_odds'] if best else None)
            if mode=='value' and (best is None or calculation['expected_value']<=0 or calculation['expected_value']<settings['min_ev']):continue
            eligible.append({**market,**calculation,'raw_probability':p,'comparison_probability':alt,'best':best})
        if not eligible:
            missing=', '.join(snapshot.get('count_unavailable',{}))
            omit('No requested market meets strict sample, probability and model-stability'+('/price/EV' if mode=='value' else '')+' filters.'+(f' Count statistics unavailable: {missing}.' if missing else ''));continue
        eligible.sort(key=lambda r:(r['probability'],r['expected_value'] if r['expected_value'] is not None else -1),reverse=True)
        best=eligible[0];quote=best.pop('best');d=best.pop('diagnostics')
        blockers=['Probabilities are experimental, not independently calibrated accuracy.','Model agreement is not a confidence interval.']
        if quote is None:blockers.append('No compatible current bookmaker price; betting value unavailable.')
        candidates.append({**best,'fixture_id':fixture['id'],'run_id':row['id'],'home':fixture['home'],'away':fixture['away'],
            'competition':fixture['competition'],'league_id':fixture['league_id'],'major_competition':fixture['league_id'] in daily_policy.MAJOR_LEAGUES,
            'kickoff_sast':datetime.fromisoformat(fixture['kickoff_utc']).astimezone(history.SAST).isoformat(),
            'model':best['market']+'_dispersion_count' if best['market'] in ('corners','yellow_cards') else model,'model_generated_at':generated.isoformat(),'fixture_status':'scheduled',
            'decimal_odds':quote['decimal_odds'] if quote else None,'bookmaker':quote['bookmaker'] if quote else None,
            'odds_observed_at':quote['observed_at'] if quote else None,'odds_age_minutes':quote['age_minutes'] if quote else None,
            'agreement_probability':best['comparison_probability'],'lower_model_ev':best['expected_value'],
            'sample':d['sample'],'effective_sample':d['effective_sample'],'qualified':False,
            'selection_stage':'value_screened' if mode=='value' else 'probability_research','data_quality':'Provider-reported · not independently verified',
            'qualification_blockers':blockers,'reasons':[best['method'],f"{d['sample']} relevant matches; effective sample {d['effective_sample']:.1f}.",
                'Only today’s requested full-match markets are considered; one outcome per match.',
                'Ranked by the lower of two model estimates, not by advertised win certainty.']})
    candidates.sort(key=lambda c:(c['major_competition'],c['probability'],c['effective_sample']),reverse=True)
    first_major=[];remaining=[];major_seen=set()
    for candidate in candidates:
        if candidate['major_competition'] and candidate['competition'] not in major_seen:
            first_major.append(candidate);major_seen.add(candidate['competition'])
        else:remaining.append(candidate)
    candidates=first_major+remaining
    selections=[];counts=Counter()
    for candidate in candidates:
        if len(selections)>=5 or counts[candidate['competition']]>=settings['max_per_league']:
            excluded.append({'fixture_id':candidate['fixture_id'],'home':candidate['home'],'away':candidate['away'],'reason':'Outside the five-place or per-league cap.'});continue
        counts[candidate['competition']]+=1;selections.append({'rank':len(selections)+1,**candidate})
    selections.sort(key=lambda c:(c['major_competition'],c['probability'],c['effective_sample']),reverse=True)
    for rank,candidate in enumerate(selections,1):candidate['rank']=rank
    return {'generated_at':current.isoformat(),'timezone':'Africa/Johannesburg','window':'today','model':model,'mode':mode,
        'policy':daily_policy.policy(),'selections':selections,'selection_count':len(selections),'research_watchlist':selections,
        'qualified':[],'qualified_count':0,'qualification_available':False,'qualification_explanation':'Independent calibration and predictive uncertainty remain unavailable.',
        'max_odds_age_minutes':max_odds_age_minutes,'fixtures_in_window':in_window,'excluded':excluded,
        'settings':{**{k:settings[k] for k in ('min_ev','max_per_league')},'min_probability':threshold,'min_sample':minimum},
        'message':f'{len(selections)} strict daily experimental selections. Only requested markets; eligible major competitions prioritised.'}
