"""Reported regulation-time FT statistics with stable-team-ID checks and caching."""
import json
from datetime import datetime,timedelta,timezone
from backend import api_football as provider,history,odds


def parse(payload,home_id,away_id):
    if not isinstance(payload,list):raise provider.ProviderError('statistics_schema','Statistics response must contain team objects.')
    if not payload:return None
    if len(payload)!=2:raise provider.ProviderError('statistics_teams','Statistics require both fixture teams; no partial team pair is assumed.')
    teams={}
    for item in payload:
        try:
            tid=provider.integer(item['team']['id'],1)
            if tid not in (home_id,away_id) or tid in teams:raise ValueError()
            values=item['statistics']
            if not isinstance(values,list) or len(values)>100:raise ValueError()
            metrics={};seen=set()
            for stat in values:
                name=stat['type']
                if name not in ('Corner Kicks','Yellow Cards','Red Cards'):continue
                if name in seen:raise ValueError()
                seen.add(name)
                value=stat.get('value')
                # Null/missing statistics remain missing, including zero-valued fields.
                if value is not None:value=provider.integer(value,0,100)
                metrics[{'Corner Kicks':'corners','Yellow Cards':'yellow','Red Cards':'red'}[name]]=value
            teams[tid]=metrics
        except (KeyError,TypeError,ValueError):
            raise provider.ProviderError('statistics_schema','Fixture statistics have invalid team identities or count fields.') from None
    return {metric+'_'+side:teams[tid].get(metric) for side,tid in [('home',home_id),('away',away_id)] for metric in ('corners','yellow','red')}


def enrich(db,client,key,league_id,current,budget,progress=lambda message:None,clock=lambda:datetime.now(timezone.utc)):
    with db() as c:
        targets=c.execute('''SELECT f.*,p.provider_id,p.home_id,p.away_id FROM fixtures f JOIN api_football_fixtures p ON p.fixture_id=f.id
            WHERE p.league_id=? AND f.status='completed' AND f.kickoff_utc<?
            AND (SELECT provider_status FROM api_football_observations o WHERE o.provider_id=p.provider_id ORDER BY o.id DESC LIMIT 1)='FT'
            ORDER BY f.kickoff_utc DESC LIMIT 120''',(league_id,current.isoformat())).fetchall()
    calls=cached=available=0;errors=[]
    for target in targets:
        target=dict(target)
        with db() as c:
            match=c.execute('SELECT id FROM historical_matches WHERE identity=?',(target['identity'],)).fetchone()
            if not match:continue
            state=history.resolve(history.observations(c,match['id']))['metrics']
            score=[state['goals_home'],state['goals_away']]
            if any(v is None for v in score):continue
            previous=c.execute('SELECT * FROM api_football_statistics WHERE provider_id=?',(target['provider_id'],)).fetchone()
        stats=None
        if previous and json.loads(previous['score'])==score:
            age=current-datetime.fromisoformat(previous['observed_at'])
            prior=json.loads(previous['metrics'])
            available_stats=prior is not None and any(v is not None for v in prior.values())
            complete=prior is not None and all(prior.get(metric+'_'+side) is not None for metric in ('corners','yellow') for side in ('home','away'))
            if timedelta(0)<=age<timedelta(days=7 if complete else 1):
                cached+=1;available+=int(available_stats);continue
        if calls>=budget:break
        progress(f'Loading reported corners/cards for league {league_id}: {calls+1}/{budget} statistics calls')
        calls+=1
        try:
            payload,_=provider.collection(client,key,'/fixtures/statistics',{'fixture':target['provider_id']})
            stats=parse(payload,target['home_id'],target['away_id'])
            received=clock()
            with db() as c:
                c.execute('INSERT OR REPLACE INTO api_football_statistics VALUES (?,?,?,?)',
                          (target['provider_id'],received.isoformat(),json.dumps(score),json.dumps(stats)))
                if stats is not None:
                    provider.record_history(c,target,{**state,**stats},received)
                    available+=1
        except provider.ProviderError as error:
            if calls==0 or not errors or errors[-1]!=error.code:errors.append(error.code)
            # Failed requests consume the budget too; rate/quota failures stop this pass.
            if error.code in ('rate_limited','quota_exhausted','quota_or_rate_limit','access_denied','authentication_failed'):break
    return {'calls':calls,'cached':cached,'available':available,'errors':errors[:12],
            'missing_statistics':'Unreported corners/cards are withheld, never filled with zero.'}
