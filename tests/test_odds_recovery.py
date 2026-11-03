"""Synthetic malformed/partly available provider quotes, not live API evidence."""
import copy
import json
from datetime import timedelta
import pytest
from backend import provider_odds,api_football as provider,main
from tests.test_top_five import raw_odds,local_fixture,value_setup
from tests.test_api_football import NOW,reset_rate_state,save
from tests.test_foundation import client


def parse(rows,fixtures=None):
    return provider_odds.parse_with_diagnostics(rows,fixtures or {1:local_fixture()},39,2026,NOW)


def test_unusable_prices_do_not_discard_valid_quotes_from_same_market():
    raw=raw_odds()
    for value in ('1.00',None,'','nan','sensitive-test-key-not-real'):
        raw['bookmakers'][0]['bets'][0]['values'].append({'value':'Home','odd':value})
    records,diagnostic=parse([raw])
    assert len(records)==7
    assert diagnostic['rejection_counts']['quote_price_missing']==2
    assert diagnostic['rejection_counts']['quote_price_not_above_one']==1
    assert diagnostic['rejection_counts']['quote_price_not_finite']==1
    assert diagnostic['rejection_counts']['quote_price_not_numeric']==1
    assert all(r['decimal_odds']>1 for r in records)
    assert 'sensitive-test-key-not-real' not in json.dumps(diagnostic)
    assert all(r['observed_at']==(NOW-timedelta(minutes=10)).isoformat() for r in records)


def test_invalid_fixture_does_not_discard_another_valid_fixture():
    bad=raw_odds();bad['fixture']['id']=2;bad.pop('update')
    fixtures={1:local_fixture(),2:{**local_fixture(),'id':2,'home':'Other synthetic team'}}
    records,diagnostic=parse([raw_odds(),bad],fixtures)
    assert len(records)==7 and all(r['fixture_id']==1 for r in records)
    assert diagnostic['fixtures_skipped']==1
    assert diagnostic['rejection_counts']['odds_quote_update_missing']==1


@pytest.mark.parametrize('field,value,reason',[
    ('update','2026-11-02T12:00:01+00:00','odds_quote_update_in_future'),
    ('update','2026-11-02T11:00:00','odds_quote_update_timezone_missing'),
    ('update','synthetic-private-value','odds_quote_update_invalid'),
])
def test_timestamp_rejections_remain_strict_and_specific(field,value,reason):
    raw=raw_odds();raw[field]=value
    records,diagnostic=parse([raw])
    assert records==[] and diagnostic['rejection_counts']=={reason:1}
    if reason=='odds_quote_update_in_future':
        assert diagnostic['samples'][0]['parsed_times']['quote_update']==value
    else:
        assert value not in json.dumps(diagnostic)


def test_schedule_and_season_mismatch_not_repaired_by_guessing():
    raw=raw_odds();raw['fixture']['date']=(NOW+timedelta(days=1)).isoformat()
    records,d=parse([raw])
    assert records==[] and d['rejection_counts']=={'odds_fixture_kickoff_mismatch':1}
    raw=raw_odds();raw['league']['season']=2025
    records,d=parse([raw])
    assert records==[] and d['rejection_counts']=={'odds_league_season_mismatch':1}


def test_duplicate_fixture_ids_reject_all_copies():
    rows,d=parse([raw_odds(),raw_odds()])
    assert rows==[] and d['rejection_counts']=={'odds_duplicate_fixture_id':2}


def test_duplicate_bookmakers_excluded_without_first_wins():
    raw=raw_odds();other=copy.deepcopy(raw['bookmakers'][0]);other['id']=9
    raw['bookmakers']=[raw['bookmakers'][0],copy.deepcopy(raw['bookmakers'][0]),other]
    rows,d=parse([raw])
    assert len(rows)==7 and all('API 9' in r['bookmaker'] for r in rows)
    assert d['rejection_counts']['odds_duplicate_bookmaker_id']==2


def test_wrong_period_and_unsupported_selection_never_substituted():
    raw=raw_odds();raw['bookmakers'][0]['bets']=[{'id':1,'name':'First Half Winner','values':[{'value':'Home','odd':'2.5'}]}]
    rows,d=parse([raw])
    assert rows==[]
    assert d['rejection_counts']['supported_market_name_mismatch']==1
    assert d['rejection_counts']['no_supported_quotes_returned']==1


def test_malformed_market_excludes_market_not_entire_bookmaker():
    raw=raw_odds();raw['bookmakers'][0]['bets'][0]['values']=None
    rows,d=parse([raw])
    assert len(rows)==4 and all(r['market']!='1x2' for r in rows)
    assert d['rejection_counts']['market_values_invalid']==1


def test_real_job_path_imports_valid_subset_and_retains_diagnostics(client,monkeypatch):
    value_setup(client,monkeypatch);save(client)
    monkeypatch.setattr(provider,'status',lambda *a:{'ok':True,'message':'Synthetic account success'})
    monkeypatch.setattr(provider,'daily_leagues',lambda *a:[{'league_id':999,'season':2026,'fixture_ids':[100]}])
    monkeypatch.setattr(provider,'available_seasons',lambda *a:[2026])
    monkeypatch.setattr(main,'run_api_football_workflow',lambda *a:{'models_generated':1,'competition':'Synthetic League','seasons':[2026]})
    raw=raw_odds();raw['fixture']['id']=100;raw['league']['id']=999
    raw['bookmakers'][0]['bets'][0]['values'].append({'value':'Home','odd':'1.00'})
    monkeypatch.setattr(provider_odds,'pages',lambda *a,**kw:[raw])
    class ImmediateThread:
        def __init__(self,target,args,daemon):self.target=target;self.args=args
        def start(self):self.target(*self.args)
    monkeypatch.setattr(main.threading,'Thread',ImmediateThread)
    assert client.post('/api/top-five/build',json={}).status_code==200
    job=client.get('/api/top-five/jobs/latest').json()
    assert job['status']=='completed'
    item=job['leagues'][0]
    assert item['odds_inserted']==7 and item['error'] is None
    assert item['odds_diagnostics']['rejection_counts']['quote_price_not_above_one']==1
    with main.db() as c:
        assert c.execute("SELECT COUNT(*) FROM odds_observations WHERE source=?",(provider_odds.SOURCE,)).fetchone()[0]==7
