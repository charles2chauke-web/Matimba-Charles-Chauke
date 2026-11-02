"""Synthetic historical observations, not real football data or predictions."""
from datetime import timedelta

import pytest
from backend import history, main
from tests.test_foundation import client  # shared isolated database fixture

HEADER = ','.join(history.REQUIRED)
BASE = 'Synthetic Home,Synthetic Away,Synthetic League,2024-01-01T18:00:00+02:00,2,1,2024-01-01T19:00:00+00:00'


def payload(row=BASE, header=HEADER, source='Synthetic Source', url=''):
    return {'csv': header + '\n' + row + '\n', 'source': source, 'source_url': url}


def test_history_missing_metrics_and_provenance(client):
    response = client.post('/api/history/import', json=payload())
    assert response.status_code == 200
    assert response.json() == {'inserted': 1, 'duplicates': 0}
    row = client.get('/api/history').json()['items'][0]
    assert row['metrics']['goals_home'] == 2
    assert row['metrics']['xg_home'] is None
    assert 'xg_home' in row['missing']
    assert not row['conflicts']
    detail = client.get('/api/history/' + str(row['id'])).json()
    observed = detail['observations'][0]
    assert observed['source'] == 'Synthetic Source'
    assert observed['available_at'] == observed['imported_at']
    assert 'unverified' in detail['reliability']
    assert client.get('/api/dashboard').json()['selections'] == []


def test_duplicate_does_not_rewrite_receipt_timestamp(client):
    client.post('/api/history/import', json=payload())
    original = client.get('/api/history/1').json()['observations'][0]
    assert client.post('/api/history/import', json=payload()).json()['duplicates'] == 1
    assert client.get('/api/history/1').json()['observations'] == [original]


def test_conflicts_withheld_and_append_only(client):
    client.post('/api/history/import', json=payload())
    row = BASE.replace(',2,1,', ',3,1,')
    client.post('/api/history/import', json=payload(row, source='Synthetic Alternative Source'))
    result = client.get('/api/history/1').json()
    assert len(result['observations']) == 2
    assert result['metrics']['goals_home'] is None
    assert result['metrics']['goals_away'] == 1
    assert result['conflicts'] == ['goals_home']
    assert 'goals_home' not in result['missing']
    summary = client.get('/api/teams/summary', params={'team': 'Synthetic Home'}).json()
    assert summary['excluded_conflicts'] == 1
    assert summary['windows']['5']['sample'] == 0
    assert summary['windows']['5']['clean_sheets'] is None


def test_later_correction_keeps_old_evidence(client):
    client.post('/api/history/import', json=payload())
    correction = BASE.replace(',2,1,', ',3,1,').replace('19:00:00+00:00', '20:00:00+00:00')
    client.post('/api/history/import', json=payload(correction))
    result = client.get('/api/history/1').json()
    assert len(result['observations']) == 2
    assert result['metrics']['goals_home'] == 3
    assert not result['conflicts']
    assert any(o['metrics']['goals_home'] == 2 for o in result['observations'])


def test_same_source_tied_timestamp_conflict_is_not_silently_overwritten(client):
    client.post('/api/history/import', json=payload())
    client.post('/api/history/import', json=payload(BASE.replace(',2,1,', ',3,1,')))
    result = client.get('/api/history/1').json()
    assert result['conflicts'] == ['goals_home']
    assert result['metrics']['goals_home'] is None


def test_historical_as_of_uses_receipt_time(client):
    client.post('/api/history/import', json=payload())
    summary = client.get('/api/teams/summary', params={'team': 'Synthetic Home', 'as_of': '2025-01-01T00:00:00Z'}).json()
    assert summary['windows']['20']['sample'] == 0
    summary = client.get('/api/teams/summary', params={'team': ' synthetic   HOME '}).json()
    assert summary['windows']['5']['sample'] == 1
    assert summary['windows']['5']['form'] == ['W']
    assert summary['windows']['5']['goals_for_per_match'] == 2
    assert not summary['windows']['5']['complete_window']
    assert summary['venues']['away']['goals_for_per_match'] is None


@pytest.mark.parametrize('replacement', [
    ('2,1,', '-1,1,'), ('2,1,', '2.5,1,'),
    ('2,1,', ',1,'), ('2,1,', '9999,1,'),
    ('2024-01-01T19:00:00+00:00', '2024-01-01T15:00:00+00:00'),
    ('2024-01-01T19:00:00+00:00', '2024-01-01T19:00:00'),
    ('2024-01-01T19:00:00+00:00', '2099-01-01T19:00:00Z'),
    ('2024-01-01T18:00:00+02:00', '2099-01-01T18:00:00+02:00'),
    ('Synthetic Away', 'Synthetic Home'),
])
def test_bad_history_rejected_atomically(client, replacement):
    data = payload(BASE + '\n' + BASE.replace(*replacement))
    assert client.post('/api/history/import', json=data).status_code == 422
    assert client.get('/api/history').json()['total'] == 0


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-0.1'])
def test_invalid_xg_rejected(client, value):
    data = payload(BASE + ',' + value, HEADER + ',xg_home')
    assert client.post('/api/history/import', json=data).status_code == 422


def test_halftime_cannot_exceed_fulltime(client):
    data = payload(BASE + ',3', HEADER + ',ht_goals_home')
    assert client.post('/api/history/import', json=data).status_code == 422


def test_valid_optional_stats_and_actual_zero_preserved(client):
    data = payload(BASE + ',1.8,0,0,0', HEADER + ',xg_home,corners_home,yellow_home,red_home')
    assert client.post('/api/history/import', json=data).status_code == 200
    result = client.get('/api/history/1').json()
    assert result['metrics']['xg_home'] == 1.8
    assert result['metrics']['corners_home'] == 0
    assert 'corners_home' not in result['missing']
    assert result['metrics']['corners_away'] is None


@pytest.mark.parametrize('url', ['http://example.org', 'https://user:secret@example.org', 'https://example.org?key=secret', 'https://example.org/#secret'])
def test_source_urls_cannot_contain_credentials(client, url):
    result = client.post('/api/history/import', json=payload(url=url))
    assert result.status_code == 422
    assert 'secret' not in result.text


def test_pagination_filter_and_unknown_id(client):
    client.post('/api/history/import', json=payload())
    assert client.get('/api/history', params={'offset': 1}).json()['items'] == []
    assert client.get('/api/history', params={'team': 'synthetic home'}).json()['total'] == 1
    assert client.get('/api/history', params={'team': 'Other Team'}).json()['total'] == 0
    assert client.get('/api/history/999').status_code == 404
    assert client.get('/api/history?limit=10000').status_code == 422


def test_status_reports_actual_data_and_live_blockers(client):
    client.post('/api/history/import', json=payload())
    status = client.get('/api/data/status').json()
    assert status['historical_matches'] == status['observations'] == 1
    assert not status['live_integration']
    assert len(status['integration_blockers']) == 2


def test_repeat_initialization_preserves_history_and_phase1(client):
    client.post('/api/history/import', json=payload())
    settings = client.get('/api/settings').json()
    main.initialize()
    assert client.get('/api/history').json()['total'] == 1
    assert client.get('/api/settings').json() == settings


def test_as_of_rejects_naive_and_future(client):
    for value in ('2025-01-01T00:00:00', '2099-01-01T00:00:00Z'):
        assert client.get('/api/teams/summary', params={'team': 'Synthetic Home', 'as_of': value}).status_code == 422
