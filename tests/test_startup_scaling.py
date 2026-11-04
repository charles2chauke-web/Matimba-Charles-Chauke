"""Synthetic large-store startup regression; never real football data."""
import time
from backend import main
from tests.test_foundation import client


def test_legacy_large_database_startup_preserves_evidence_and_uses_index(client):
    count=30000
    stamp='2026-11-01T12:00:00+00:00'
    with main.db() as c:
        c.execute('DROP INDEX fixture_observation_fixture')
        c.executemany('INSERT INTO fixtures VALUES (?,?,?,?,?,?,?,?,?)',
            ((i,'Synthetic home','Synthetic away','Synthetic league',stamp,'completed',f'synthetic-{i}','Synthetic evidence',stamp) for i in range(1,count+1)))
        c.executemany('INSERT INTO fixture_observations(fixture_id,identity,status,recorded_at,source) VALUES (?,?,?,?,?)',
            ((i,f'synthetic-{i}','completed',stamp,'Synthetic evidence') for repeat in range(3) for i in range(1,count)))
        query='SELECT f.id FROM fixtures f WHERE NOT EXISTS (SELECT 1 FROM fixture_observations o WHERE o.fixture_id=f.id)'
        before=[r['detail'] for r in c.execute('EXPLAIN QUERY PLAN '+query)]
        assert any('SCAN o' in detail for detail in before)
    started=time.monotonic()
    main.initialize()
    duration=time.monotonic()-started
    with main.db() as c:
        plan=[r['detail'] for r in c.execute('EXPLAIN QUERY PLAN '+query)]
        assert any('SEARCH o' in detail for detail in plan)
        assert c.execute('SELECT COUNT(*) FROM fixture_observations').fetchone()[0]==3*(count-1)+1
        row=c.execute('SELECT * FROM fixture_observations WHERE fixture_id=?',(count,)).fetchone()
        assert row['recorded_at']==stamp and row['source']=='Synthetic evidence'
    main.initialize()
    with main.db() as c:
        assert c.execute('SELECT COUNT(*) FROM fixture_observations').fetchone()[0]==3*(count-1)+1
    print(f'Large-store startup: {count} fixtures / {3*(count-1)} prior observations; {duration:.3f}s with indexed lookup')
