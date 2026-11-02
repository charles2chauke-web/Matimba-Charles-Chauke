"""Synthetic odds/software validation, not bookmaker quotes or profit evidence."""
from datetime import datetime,timedelta,timezone
import json

import pytest
from backend import main,models,odds
from tests.test_foundation import client
from tests.test_models import fixture_and_history


def import_quote(client,market='1x2',selection='home',price=2.2,line='',observed=None,
                 bookmaker='Synthetic Book A',source='Synthetic odds test source',settlement='regulation_90'):
    observed=observed or (main.now()-timedelta(minutes=5)).isoformat()
    text=','.join(odds.HEADER)+f'\n1,{bookmaker},{market},{selection},{line},{price},{observed},{settlement}\n'
    return client.post('/api/odds/import',json={'csv':text,'source':source})


@pytest.mark.parametrize('p,price,push,fair,ev',[(.8,1.4,0,1.25,.12),(.4,2,.3,1.75,.1),(.2,2,0,5,-.6),(0,2,1,None,0)])
def test_fair_odds_and_ev_with_pushes(p,price,push,fair,ev):
    value=odds.calculate(p,price,push)
    assert value['fair_odds'] == pytest.approx(fair) if fair is not None else value['fair_odds'] is None
    assert value['expected_value'] == pytest.approx(ev)
    assert value['implied_probability'] == pytest.approx(1/price)


def test_missing_odds_cannot_claim_value():
    value=odds.calculate(.8)
    assert value['fair_odds']==1.25
    assert value['expected_value'] is None


@pytest.mark.parametrize('p,price,push',[(float('nan'),2,0),(.8,1,0),(.8,2,.3),(-.1,2,0)])
def test_invalid_value_input(p,price,push):
    with pytest.raises(ValueError):
        odds.calculate(p,price,push)


def test_odds_import_deduplication_and_immutable_receipt(client):
    fixture_and_history(client)
    observed=(main.now()-timedelta(minutes=5)).isoformat()
    assert import_quote(client,observed=observed).json()=={'inserted':1,'duplicates':0}
    first=client.get('/api/fixtures/1/odds').json()['items'][0]
    assert import_quote(client,observed=observed).json()=={'inserted':0,'duplicates':1}
    assert client.get('/api/fixtures/1/odds').json()['items']==[first]
    assert client.get('/api/odds/status').json()['observations']==1


@pytest.mark.parametrize('change',[{'price':'NaN'},{'price':1},{'price':10001},{'market':'corners'},
    {'selection':'unknown'},{'market':'goals','selection':'over','line':2},
    {'line':2.5},{'settlement':'includes_extra_time'},
    {'observed':'2099-01-01T00:00:00Z'},{'observed':'2025-01-01T00:00:00'}])
def test_invalid_odds_rejected(client,change):
    fixture_and_history(client)
    assert import_quote(client,**change).status_code==422
    assert client.get('/api/fixtures/1/odds').json()['items']==[]


def test_atomic_odds_batch_and_unknown_fixture(client):
    fixture_and_history(client)
    at=(main.now()-timedelta(minutes=5)).isoformat()
    data=','.join(odds.HEADER)+f'\n1,Synthetic Book,1x2,home,,2,{at},regulation_90\n999,Other,1x2,home,,2,{at},regulation_90'
    assert client.post('/api/odds/import',json={'csv':data,'source':'Synthetic source'}).status_code==422
    assert client.get('/api/odds/status').json()['observations']==0


def test_best_odds_are_only_fresh_unconflicted_quotes(client):
    fixture_and_history(client)
    import_quote(client,price=2.2)
    import_quote(client,price=2.4,bookmaker='Synthetic Book B')
    import_quote(client,price=9,bookmaker='Synthetic Stale Book',observed=(main.now()-timedelta(hours=3)).isoformat())
    run=client.post('/api/fixtures/1/analysis',json={}).json()
    response=client.post(f"/api/analysis/{run['id']}/value",json={})
    assert response.status_code==200
    result=response.json()
    row=next(r for r in result['rows'] if r['market']=='1x2' and r['selection']=='home')
    assert row['best_quote']['decimal_odds']==2.4
    assert row['expected_value']==pytest.approx(row['probability']*2.4-1)
    assert row['recommendation'] is None
    assert result['recommendation'] is None and result['uncertainty'] is None
    assert client.get('/api/dashboard').json()['selections']==[]
    assert client.get('/api/value/'+str(result['id'])).json()==result


def test_quotes_received_after_prediction_cannot_reprice_its_original_time(client):
    fixture_and_history(client)
    run=client.post('/api/fixtures/1/analysis',json={}).json()
    import_quote(client)
    historical=client.post(f"/api/analysis/{run['id']}/value",json={'pricing_time':'at_prediction'}).json()
    assert historical['odds_observation_ids']==[]
    assert all(row['expected_value'] is None for row in historical['rows'])
    current=client.post(f"/api/analysis/{run['id']}/value",json={}).json()
    assert current['odds_observation_ids']


def test_value_snapshot_not_overwritten_by_new_prices(client):
    fixture_and_history(client)
    import_quote(client,price=2)
    run=client.post('/api/fixtures/1/analysis',json={}).json()
    first=client.post(f"/api/analysis/{run['id']}/value",json={}).json()
    import_quote(client,price=2.5,observed=(main.now()-timedelta(minutes=1)).isoformat())
    second=client.post(f"/api/analysis/{run['id']}/value",json={}).json()
    assert first['id']!=second['id']
    assert client.get('/api/value/'+str(first['id'])).json()==first
    assert len(client.get(f"/api/analysis/{run['id']}/values").json())==2


def test_stale_model_withholds_ev(client):
    fixture_and_history(client)
    import_quote(client)
    run=client.post('/api/fixtures/1/analysis',json={}).json()
    run['generated_at']=(main.now()-timedelta(hours=25)).isoformat()
    with main.db() as connection:
        # Isolated synthetic setup exercises the stale-snapshot guard.
        connection.execute('UPDATE model_runs SET result=? WHERE id=?',(json.dumps({k:v for k,v in run.items() if k!='id'}),run['id']))
    result=client.post(f"/api/analysis/{run['id']}/value",json={}).json()
    assert all(row['status']=='model_stale' and row['expected_value'] is None for row in result['rows'])


def test_current_value_rejects_live_match_but_archived_pricing_available(client):
    _,fixture_csv=fixture_and_history(client)
    import_quote(client)
    run=client.post('/api/fixtures/1/analysis',json={}).json()
    client.post('/api/fixtures/import',json={'csv':fixture_csv.replace('scheduled','live')})
    assert client.post(f"/api/analysis/{run['id']}/value",json={}).status_code==409
    assert client.post(f"/api/analysis/{run['id']}/value",json={'pricing_time':'at_prediction'}).status_code==200


def test_dnb_uses_push_formula(client):
    fixture_and_history(client)
    import_quote(client,market='draw_no_bet',selection='home',price=1.8)
    run=client.post('/api/fixtures/1/analysis',json={}).json()
    result=client.post(f"/api/analysis/{run['id']}/value",json={}).json()
    row=next(r for r in result['rows'] if r['market']=='draw_no_bet' and r['selection']=='home')
    assert row['fair_odds']==pytest.approx((1-row['push_probability'])/row['probability'])
    assert row['expected_value']==pytest.approx(row['probability']*1.8+row['push_probability']-1)


def test_complete_same_book_margin_and_no_cross_book_margin(client):
    fixture_and_history(client)
    observed=(main.now()-timedelta(minutes=5)).isoformat()
    for selection,price in [('home',2),('draw',3),('away',4)]:
        import_quote(client,selection=selection,price=price,observed=observed)
    import_quote(client,bookmaker='Synthetic Incomplete Book',price=2.2,observed=observed)
    run=client.post('/api/fixtures/1/analysis',json={}).json()
    result=client.post(f"/api/analysis/{run['id']}/value",json={}).json()
    row=next(r for r in result['rows'] if r['market']=='1x2' and r['selection']=='home')
    complete=next(q for q in row['bookmaker_quotes'] if q['bookmaker']=='Synthetic Book A')
    incomplete=next(q for q in row['bookmaker_quotes'] if q['bookmaker']=='Synthetic Incomplete Book')
    assert complete['overround']==pytest.approx(1/2+1/3+1/4-1)
    assert complete['margin_removed_implied']==pytest.approx(.5/(1/2+1/3+1/4))
    assert incomplete['overround'] is None


def test_double_chance_overlapping_outcomes_never_sum_as_margin(client):
    fixture_and_history(client)
    observed=(main.now()-timedelta(minutes=5)).isoformat()
    for selection in odds.OUTCOMES['double_chance']:
        import_quote(client,market='double_chance',selection=selection,price=1.5,observed=observed)
    run=client.post('/api/fixtures/1/analysis',json={}).json()
    result=client.post(f"/api/analysis/{run['id']}/value",json={}).json()
    for row in result['rows']:
        if row['market']=='double_chance':
            assert row['best_quote']['overround'] is None
            assert 'overlap' in row['best_quote']['margin_status']


def test_tied_conflicting_quote_withheld(client):
    fixture_and_history(client)
    observed=(main.now()-timedelta(minutes=5)).isoformat()
    import_quote(client,price=2,observed=observed)
    import_quote(client,price=3,observed=observed,source='Synthetic conflicting source')
    run=client.post('/api/fixtures/1/analysis',json={}).json()
    result=client.post(f"/api/analysis/{run['id']}/value",json={}).json()
    row=next(r for r in result['rows'] if r['market']=='1x2' and r['selection']=='home')
    assert row['status']=='conflicting_odds'
    assert row['best_quote'] is None and row['expected_value'] is None


def test_first_stored_movement_not_closing_line(client):
    fixture_and_history(client)
    import_quote(client,price=2,observed=(main.now()-timedelta(minutes=30)).isoformat())
    import_quote(client,price=2.2,observed=(main.now()-timedelta(minutes=5)).isoformat())
    run=client.post('/api/fixtures/1/analysis',json={}).json()
    result=client.post(f"/api/analysis/{run['id']}/value",json={}).json()
    row=next(r for r in result['rows'] if r['market']=='1x2' and r['selection']=='home')
    assert row['best_quote']['odds_change']==pytest.approx(.2)
    assert row['closing_line_value'] is None


def test_calculator_api_is_explicitly_user_input(client):
    response=client.post('/api/value/calculator',json={'probability':.8,'decimal_odds':1.4}).json()
    assert response['expected_value']==pytest.approx(.12)
    assert response['recommendation'] is None
    assert 'User-entered' in response['source']
    assert client.post('/api/value/calculator',json={'probability':.8,'decimal_odds':1.4,'push':.3}).status_code==422


def test_stale_only_quotes_withhold_value(client):
    fixture_and_history(client)
    import_quote(client,observed=(main.now()-timedelta(hours=2)).isoformat())
    run=client.post('/api/fixtures/1/analysis',json={}).json()
    result=client.post(f"/api/analysis/{run['id']}/value",json={}).json()
    row=next(r for r in result['rows'] if r['market']=='1x2' and r['selection']=='home')
    assert row['status']=='stale_odds'
    assert row['expected_value'] is None and row['best_quote'] is None


def test_unsynchronised_outcomes_do_not_claim_market_margin(client):
    fixture_and_history(client)
    for index,(selection,price) in enumerate([('home',2),('draw',3),('away',4)]):
        import_quote(client,selection=selection,price=price,observed=(main.now()-timedelta(minutes=5+index*2)).isoformat())
    run=client.post('/api/fixtures/1/analysis',json={}).json()
    result=client.post(f"/api/analysis/{run['id']}/value",json={}).json()
    rows=[r for r in result['rows'] if r['market']=='1x2']
    assert all(r['best_quote']['overround'] is None for r in rows)
    assert all(r['expected_value'] is not None for r in rows)


def test_after_kickoff_odds_rejected_even_when_timestamp_is_past(client):
    current=main.now()
    csv='home,away,competition,kickoff_sast,status\nSynthetic A,Synthetic B,Synthetic League,'+(current-timedelta(hours=1)).astimezone(main.SAST).isoformat()+',live'
    client.post('/api/fixtures/import',json={'csv':csv})
    response=import_quote(client,observed=(current-timedelta(minutes=5)).isoformat())
    assert response.status_code==422
    assert 'precede fixture kickoff' in response.json()['detail']


def test_value_requires_an_available_model(client):
    current=main.now()
    csv='home,away,competition,kickoff_sast,status\nSynthetic A,Synthetic B,Synthetic League,'+(current+timedelta(days=1)).astimezone(main.SAST).isoformat()+',scheduled'
    client.post('/api/fixtures/import',json={'csv':csv})
    run=client.post('/api/fixtures/1/analysis',json={}).json()
    assert client.post(f"/api/analysis/{run['id']}/value",json={}).status_code==409
