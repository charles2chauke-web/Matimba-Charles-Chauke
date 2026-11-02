"""Private football research workspace. No unvalidated prediction claims."""
import csv
import io
import json
import os
import sqlite3
import threading
import unicodedata
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware
from backend import history, models, odds, backtesting, public_data, shortlist

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get('CHARLES_DATA_DIR', ROOT / 'data'))
SAST = timezone(timedelta(hours=2), 'SAST')
PROVIDERS = {'api-football': 'API-Football / API-Sports', 'sportmonks': 'Sportmonks', 'football-data': 'Football-Data.org'}
STATUSES = {'scheduled', 'postponed', 'cancelled', 'live', 'completed'}
WORKFLOW_LOCK=threading.Lock()


def now():
    return datetime.now(timezone.utc)


@contextmanager
def db():
    connection = sqlite3.connect(DATA / 'charles.sqlite3', timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def windows_protect(value: bytes, decrypt=False) -> bytes:
    """Seal the encryption key to the current Windows user's DPAPI identity."""
    import ctypes
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.windll.crypt32
    if decrypt:
        ok = crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        ok = crypt.CryptProtectData(ctypes.byref(source), 'Charles credentials', None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise RuntimeError('Windows credential protection failed')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)


def credential_cipher():
    supplied = os.environ.get('CHARLES_CREDENTIAL_KEY')
    if supplied:
        return Fernet(supplied.encode())
    path = DATA / ('credentials.dpapi' if os.name == 'nt' else 'credentials.key')
    if not path.exists():
        key = Fernet.generate_key()
        sealed = windows_protect(key) if os.name == 'nt' else key
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as file:
            file.write(sealed)
    sealed = path.read_bytes()
    return Fernet(windows_protect(sealed, True) if os.name == 'nt' else sealed)


def initialize():
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    with db() as connection:
        connection.executescript('''
        CREATE TABLE IF NOT EXISTS fixtures (
          id INTEGER PRIMARY KEY, home TEXT NOT NULL, away TEXT NOT NULL,
          competition TEXT NOT NULL, kickoff_utc TEXT NOT NULL, status TEXT NOT NULL,
          identity TEXT NOT NULL UNIQUE, source TEXT NOT NULL, imported_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS providers (
          id TEXT PRIMARY KEY, encrypted_key TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS model_runs (
          id INTEGER PRIMARY KEY, fixture_id INTEGER NOT NULL, generated_at TEXT NOT NULL,
          status TEXT NOT NULL, result TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS runs_fixture_time ON model_runs(fixture_id,generated_at);
        CREATE TABLE IF NOT EXISTS fixture_observations (
          id INTEGER PRIMARY KEY, fixture_id INTEGER NOT NULL, identity TEXT NOT NULL,
          status TEXT NOT NULL, recorded_at TEXT NOT NULL, source TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS fixture_observation_identity ON fixture_observations(identity,recorded_at);
        CREATE TABLE IF NOT EXISTS workflow_runs (
          id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, result TEXT NOT NULL);
        ''')
        connection.execute('INSERT OR IGNORE INTO settings VALUES (1, ?)', (json.dumps(Settings().model_dump()),))
        history.initialize(connection)
        odds.initialize(connection)
        backtesting.initialize(connection)
        # Preserve only the existing row's latest asserted state/time. Do not
        # manufacture earlier schedule/status history during migration.
        connection.execute('''INSERT INTO fixture_observations(fixture_id,identity,status,recorded_at,source)
            SELECT f.id,f.identity,f.status,f.imported_at,f.source FROM fixtures f
            WHERE NOT EXISTS (SELECT 1 FROM fixture_observations o WHERE o.fixture_id=f.id)''')
    credential_cipher()


@asynccontextmanager
async def lifespan(app):
    initialize()
    yield


app = FastAPI(title='Charles Football Betting Intelligence System', lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1', 'testserver'])


@app.exception_handler(RequestValidationError)
async def validation_error(request, error):
    # Pydantic's default response echoes input, including submitted credentials.
    return JSONResponse({'detail': 'Invalid request. Check field types, lengths and allowed ranges.'}, status_code=422)


@app.middleware('http')
async def local_security(request: Request, call_next):
    origin = request.headers.get('origin')
    if request.url.path.startswith('/api') and origin:
        parsed = urlsplit(origin)
        # No cross-origin access. Vite uses a same-origin /api proxy.
        if parsed.netloc != request.headers.get('host') or parsed.scheme not in ('http', 'https'):
            return JSONResponse({'detail': 'Cross-origin access is disabled'}, status_code=403)
    if request.method in ('POST', 'PUT', 'PATCH') and request.headers.get('content-type', '').split(';')[0] != 'application/json':
        return JSONResponse({'detail': 'Use application/json'}, status_code=415)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    if request.url.path.startswith('/api'):
        response.headers['Cache-Control'] = 'no-store'
    return response


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    min_probability: float = Field(default=.75, ge=0, le=1)
    min_ev: float = Field(default=.03, ge=0, le=10)
    min_sample: int = Field(default=30, ge=1, le=10000)
    max_per_league: int = Field(default=2, ge=1, le=5)
    window: str = Field(default='today', pattern='^(today|24h)$')
    schedule: str = Field(default='07:00', pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    primary_provider: str = Field(default='api-football', pattern='^(api-football|sportmonks|football-data)$')


class CsvImport(BaseModel):
    csv: str = Field(max_length=1_000_000, min_length=1)


class ProviderKey(BaseModel):
    key: str = Field(min_length=1, max_length=512, repr=False)


class HistoricalImport(CsvImport):
    model_config = ConfigDict(extra='forbid')
    source: str = Field(min_length=1, max_length=120)
    source_url: str = Field(default='', max_length=500)


class ModelOptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    training_days: int = Field(default=730, ge=30, le=3650)
    half_life_days: float = Field(default=180, ge=7, le=1000)
    ridge: float = Field(default=1, gt=0, le=100)
    min_team_matches: int = Field(default=5, ge=3, le=100)
    elo_k: float = Field(default=20, ge=1, le=100)


class ValueOptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model: str = Field(default='poisson', pattern='^(poisson|dixon_coles|xg_poisson)$')
    pricing_time: str = Field(default='current', pattern='^(current|at_prediction)$')
    max_odds_age_minutes: float = Field(default=60, ge=1, le=1440)
    max_model_age_hours: float = Field(default=24, ge=.25, le=168)


class ValueCalculation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    probability: float = Field(ge=0,le=1)
    decimal_odds: float = Field(gt=1,le=10000)
    push: float = Field(default=0,ge=0,le=1)


class BacktestOptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    competition: str = Field(min_length=1,max_length=150)
    method: str = Field(default='walk_forward',pattern='^(walk_forward|saved_predictions)$')
    calibration_start: date
    test_start: date
    test_end: date
    calibration: str = Field(default='temperature',pattern='^(temperature|none)$')
    model_options: ModelOptions = Field(default_factory=ModelOptions)
    minimum_sample: int = Field(default=30,ge=30,le=10000)
    decision_lead_minutes: int = Field(default=60,ge=0,le=1440)
    strategy_model: str = Field(default='poisson',pattern='^(poisson|dixon_coles|xg_poisson)$')
    strategy_probabilities: str = Field(default='raw',pattern='^(raw|calibrated_1x2)$')
    market: str = Field(default='1x2',max_length=30)
    selection: str = Field(default='home',max_length=30)
    line: str = Field(default='',max_length=10)
    min_probability: float = Field(default=.75,ge=0,le=1)
    min_ev: float = Field(default=.03,ge=-1,le=1000)
    max_odds_age_minutes: float = Field(default=60,ge=1,le=1440)
    max_model_age_hours: float = Field(default=24,ge=.25,le=168)
    flat_stake: float = Field(default=1,gt=0,le=10000)


class WorkflowOptions(BaseModel):
    model_config=ConfigDict(extra='forbid')
    seasons: list[str] | None = Field(default=None,max_length=3)
    horizon_days: int = Field(default=7,ge=1,le=14)
    max_fixtures: int = Field(default=12,ge=1,le=30)


def normalized(text):
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().split())


def parse_csv(content):
    reader = csv.DictReader(io.StringIO(content.lstrip('\ufeff')))
    if reader.fieldnames != ['home', 'away', 'competition', 'kickoff_sast', 'status']:
        raise ValueError('Expected header: home,away,competition,kickoff_sast,status')
    rows = []
    for index, row in enumerate(reader, 2):
        if index > 5001:
            raise ValueError('Maximum 5,000 rows per import')
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f'Row {index}: expected exactly five columns')
        row = {k: v.strip() for k, v in row.items()}
        if any(not row[k] or len(row[k]) > 150 for k in ('home', 'away', 'competition')):
            raise ValueError(f'Row {index}: team and competition names must contain 1–150 characters')
        if normalized(row['home']) == normalized(row['away']):
            raise ValueError(f'Row {index}: home and away must differ')
        if row['status'] not in STATUSES:
            raise ValueError(f'Row {index}: invalid status')
        try:
            kickoff = datetime.fromisoformat(row['kickoff_sast'])
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=SAST)
            if kickoff.utcoffset() != timedelta(hours=2):
                raise ValueError()
        except ValueError:
            raise ValueError(f'Row {index}: kickoff_sast must be an ISO datetime in SAST (+02:00)') from None
        kickoff = kickoff.astimezone(timezone.utc).isoformat()
        identity = json.dumps([normalized(row[k]) for k in ('home', 'away', 'competition')] + [kickoff])
        rows.append((row['home'], row['away'], row['competition'], kickoff, row['status'], identity, 'Manual CSV · unverified', now().isoformat()))
    if not rows:
        raise ValueError('CSV contains no fixtures')
    return rows


@app.get('/api/health')
def health():
    with db() as connection:
        connection.execute('SELECT 1').fetchone()
    return {'status': 'ok', 'phase': 5, 'timezone': 'Africa/Johannesburg'}


@app.get('/api/settings')
def get_settings():
    with db() as connection:
        return json.loads(connection.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])


@app.put('/api/settings')
def save_settings(settings: Settings):
    with db() as connection:
        connection.execute('UPDATE settings SET value=? WHERE id=1', (settings.model_dump_json(),))
    return settings


@app.post('/api/fixtures/import')
def import_fixtures(payload: CsvImport):
    try:
        rows = parse_csv(payload.csv)
    except (ValueError, csv.Error) as error:
        raise HTTPException(422, str(error)) from None
    inserted = updated = unchanged = 0
    with db() as connection:
        for row in rows:
            existing = connection.execute('SELECT id,status FROM fixtures WHERE identity=?', (row[5],)).fetchone()
            if existing is None:
                cursor=connection.execute('INSERT INTO fixtures(home,away,competition,kickoff_utc,status,identity,source,imported_at) VALUES (?,?,?,?,?,?,?,?)', row)
                connection.execute('INSERT INTO fixture_observations(fixture_id,identity,status,recorded_at,source) VALUES (?,?,?,?,?)',
                                   (cursor.lastrowid,row[5],row[4],row[7],row[6]))
                inserted += 1
            elif existing['status'] != row[4]:
                connection.execute('UPDATE fixtures SET status=?, imported_at=?,source=? WHERE identity=?', (row[4], row[7], row[6], row[5]))
                connection.execute('INSERT INTO fixture_observations(fixture_id,identity,status,recorded_at,source) VALUES (?,?,?,?,?)',
                                   (existing['id'],row[5],row[4],row[7],row[6]))
                updated += 1
            else:
                unchanged += 1
    return {'inserted': inserted, 'updated': updated, 'unchanged': unchanged}


@app.get('/api/fixtures')
def fixtures():
    with db() as connection:
        rows = connection.execute('SELECT * FROM fixtures ORDER BY kickoff_utc').fetchall()
    result = []
    for row in rows:
        item = dict(row)
        del item['identity']
        item['kickoff_sast'] = datetime.fromisoformat(item['kickoff_utc']).astimezone(SAST).isoformat()
        item['eligible'] = item['status'] == 'scheduled' and datetime.fromisoformat(item['kickoff_utc']) > now()
        item['research_eligible'] = item['status'] in ('scheduled','listed') and datetime.fromisoformat(item['kickoff_utc']) > now()
        item['reliability'] = 'Community schedule · status/timezone unconfirmed' if item['source'].startswith('OpenFootball') else 'Unverified manual import'
        result.append(item)
    return result


@app.get('/api/dashboard')
def dashboard():
    current = now().astimezone(SAST)
    settings = get_settings()
    upcoming = [f for f in fixtures() if f['research_eligible'] and (
        datetime.fromisoformat(f['kickoff_sast']) < current + timedelta(hours=24) if settings['window'] == '24h'
        else datetime.fromisoformat(f['kickoff_sast']).date() == current.date())]
    with db() as connection:
        last = connection.execute('SELECT MAX(imported_at) FROM fixtures').fetchone()[0]
        research_count=connection.execute("SELECT COUNT(DISTINCT fixture_id) FROM model_runs WHERE status='experimental'").fetchone()[0]
        latest_workflow=connection.execute('SELECT id,result FROM workflow_runs ORDER BY id DESC LIMIT 1').fetchone()
    return {'upcoming': len(upcoming), 'analysed': 0, 'research_analysed':research_count,'qualified': 0, 'average_probability': None,
            'historical_roi': None, 'last_import': last, 'last_successful_analysis': None,
            'selections': [], 'fixtures': upcoming, 'phase': 5,
            'workflow':{'id':latest_workflow['id'],**json.loads(latest_workflow['result'])} if latest_workflow else None,
            'message': 'Experimental model and value research are available. No calibrated models or value validation qualify recommendations.'}


@app.post('/api/history/import')
def import_history(payload: HistoricalImport):
    imported_at = now()
    try:
        records = history.parse(payload.csv, payload.source, payload.source_url, imported_at)
    except (ValueError, csv.Error) as error:
        raise HTTPException(422, str(error)) from None
    with db() as connection:
        return history.ingest(connection, records, imported_at.isoformat())


@app.get('/api/shortlist')
def daily_shortlist(model: str=Query('poisson',pattern='^(poisson|dixon_coles|xg_poisson)$')):
    with db() as connection:
        try:
            return shortlist.build(connection,get_settings(),now().astimezone(SAST),model)
        except ValueError as error:
            raise HTTPException(422,str(error)) from None


@app.get('/api/history')
def historical_matches(limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0),
                       team: str | None = Query(None, min_length=1, max_length=150)):
    with db() as connection:
        return history.match_list(connection, limit, offset, team)


@app.get('/api/history/{match_id}')
def historical_match(match_id: int):
    with db() as connection:
        match = connection.execute('SELECT id,home,away,competition,kickoff_utc FROM historical_matches WHERE id=?', (match_id,)).fetchone()
        if match is None:
            raise HTTPException(404, 'Historical match not found')
        observations = history.observations(connection, match_id)
    return {**dict(match), **history.resolve(observations), 'observations': observations}


@app.get('/api/teams/summary')
def team_summary(team: str = Query(min_length=1, max_length=150), as_of: datetime | None = None):
    current = now()
    if as_of is not None:
        if as_of.tzinfo is None or as_of > current:
            raise HTTPException(422, 'as_of must be an offset-aware datetime no later than now')
        current = as_of.astimezone(timezone.utc)
    with db() as connection:
        return history.team_summary(connection, team, current.isoformat())


@app.get('/api/data/status')
def data_status():
    with db() as connection:
        matches = connection.execute('SELECT COUNT(*) FROM historical_matches').fetchone()[0]
        observations = connection.execute('SELECT COUNT(*),MAX(imported_at) FROM historical_observations').fetchone()
        sources = connection.execute('SELECT source,source_url,COUNT(*) AS observations,MAX(imported_at) AS last_import FROM historical_observations GROUP BY source,source_url ORDER BY source').fetchall()
    return {'historical_matches': matches, 'observations': observations[0], 'last_history_import': observations[1],
            'sources': [dict(row) for row in sources], 'live_integration': False,
            'integration_blockers': ['Official provider documentation access must be enabled and reviewed before implementing an adapter.',
                                     'A usable provider credential is required for live validation.'],
            'available_workflow': 'Import historical exports you are permitted to use. Source authenticity is not independently verified.',
            'missing_features': ['Live fixtures and statistics sync', 'Provider coverage and usage monitoring', 'Validated prediction models']}


@app.post('/api/fixtures/{fixture_id}/analysis')
def analyse_fixture(fixture_id: int, options: ModelOptions):
    cutoff = now()
    minimum = max(30, get_settings()['min_sample'])
    with db() as connection:
        record = connection.execute('SELECT * FROM fixtures WHERE id=?', (fixture_id,)).fetchone()
        if record is None:
            raise HTTPException(404, 'Fixture not found')
        if record['status'] not in ('scheduled','listed') or datetime.fromisoformat(record['kickoff_utc']) <= cutoff:
            raise HTTPException(409, 'Research analysis requires a future scheduled fixture or an explicitly unconfirmed community listing')
        fixture = {key: record[key] for key in ('id','home','away','competition','status','source','imported_at')}
        fixture['kickoff_sast'] = datetime.fromisoformat(record['kickoff_utc']).astimezone(SAST).isoformat()
        try:
            rows, fingerprint, evidence = models.load_dataset(connection, record['competition'], cutoff, options.training_days)
        except models.ModelUnavailable as error:
            raise HTTPException(422, str(error)) from None
    result = models.analyse(rows, fixture, cutoff, options.model_dump(), minimum)
    if record['status']=='listed':
        result['warnings'].insert(0,'Community fixture listing is unconfirmed; its timezone is an explicit import assumption. This is research only, not a verified pre-match betting opportunity.')
    snapshot = {**result, 'fixture': fixture, 'generated_at': cutoff.isoformat(), 'training_cutoff': cutoff.isoformat(),
                'training_start': (cutoff-timedelta(days=options.training_days)).isoformat(),
                'options': options.model_dump(), 'minimum_sample': minimum,
                'dataset_fingerprint': fingerprint, 'observation_ids': evidence, 'model_version': models.VERSION}
    with db() as connection:
        run = connection.execute('INSERT INTO model_runs(fixture_id,generated_at,status,result) VALUES (?,?,?,?)',
                                 (fixture_id, cutoff.isoformat(), snapshot['status'], json.dumps(snapshot, allow_nan=False)))
        run_id = run.lastrowid
    return {'id': run_id, **snapshot}


@app.get('/api/fixtures/{fixture_id}/analyses')
def saved_analyses(fixture_id: int):
    with db() as connection:
        rows = connection.execute('SELECT id,generated_at,status FROM model_runs WHERE fixture_id=? ORDER BY id DESC LIMIT 50', (fixture_id,)).fetchall()
    return [dict(row) for row in rows]


@app.get('/api/analysis/{run_id}')
def saved_analysis(run_id: int):
    with db() as connection:
        row = connection.execute('SELECT result FROM model_runs WHERE id=?', (run_id,)).fetchone()
    if row is None:
        raise HTTPException(404, 'Analysis snapshot not found')
    return {'id': run_id, **json.loads(row['result'])}


@app.post('/api/odds/import')
def import_odds(payload: HistoricalImport):
    with db() as connection:
        fixtures = {row['id']: dict(row) for row in connection.execute('SELECT * FROM fixtures')}
        try:
            rows = odds.parse(payload.csv,payload.source,payload.source_url,now(),fixtures)
        except (ValueError,csv.Error) as error:
            raise HTTPException(422,str(error)) from None
        return odds.ingest(connection,rows)


@app.get('/api/odds/status')
def odds_status():
    with db() as connection:
        row = connection.execute('SELECT COUNT(*),COUNT(DISTINCT bookmaker_key),MAX(imported_at) FROM odds_observations').fetchone()
    return {'observations':row[0],'bookmakers':row[1],'last_import':row[2],
            'live_feed':False,'reliability':'Manual exports · source, licence and price availability are not independently verified'}


@app.get('/api/fixtures/{fixture_id}/odds')
def fixture_odds(fixture_id: int,limit: int=Query(100,ge=1,le=500),offset: int=Query(0,ge=0)):
    with db() as connection:
        if connection.execute('SELECT 1 FROM fixtures WHERE id=?',(fixture_id,)).fetchone() is None:
            raise HTTPException(404,'Fixture not found')
        return odds.observation_list(connection,fixture_id,limit,offset)


@app.post('/api/analysis/{run_id}/value')
def evaluate_value(run_id: int,options: ValueOptions):
    snapshot=saved_analysis(run_id)
    if options.model not in snapshot['models']:
        raise HTTPException(409,'Selected model is unavailable in this research snapshot')
    created_at=now()
    cutoff=datetime.fromisoformat(snapshot['generated_at']) if options.pricing_time=='at_prediction' else created_at
    if cutoff>created_at:
        raise HTTPException(409,'Model timestamp is in the future; check the system clock before comparison')
    if cutoff<datetime.fromisoformat(snapshot['generated_at']):
        raise HTTPException(409,'Pricing cutoff cannot precede the model generation time')
    with db() as connection:
        if options.pricing_time=='current':
            fixture=connection.execute('SELECT * FROM fixtures WHERE id=?',(snapshot['fixture']['id'],)).fetchone()
            if fixture is None or fixture['status']!='scheduled' or datetime.fromisoformat(fixture['kickoff_utc'])<=cutoff:
                raise HTTPException(409,'Current value research requires a scheduled fixture before kickoff; use at-prediction mode for archived research')
            if odds.fixture_identity(dict(fixture))!=odds.fixture_identity(snapshot['fixture']):
                raise HTTPException(409,'Fixture details changed; generate a new model analysis before current price comparison')
        try:
            result=odds.assess(connection,snapshot,options.model,cutoff,created_at,options.model_dump())
        except ValueError as error:
            raise HTTPException(422,str(error)) from None
        cursor=connection.execute('INSERT INTO value_assessments(run_id,fixture_id,created_at,pricing_cutoff,result) VALUES (?,?,?,?,?)',
            (run_id,snapshot['fixture']['id'],created_at.isoformat(),cutoff.isoformat(),json.dumps(result,allow_nan=False)))
        result_id=cursor.lastrowid
    return {'id':result_id,**result}


@app.get('/api/analysis/{run_id}/values')
def saved_values(run_id: int):
    with db() as connection:
        rows=connection.execute('SELECT id,created_at,pricing_cutoff FROM value_assessments WHERE run_id=? ORDER BY id DESC LIMIT 50',(run_id,)).fetchall()
    return [dict(row) for row in rows]


@app.get('/api/value/{assessment_id}')
def value_assessment(assessment_id: int):
    with db() as connection:
        row=connection.execute('SELECT result FROM value_assessments WHERE id=?',(assessment_id,)).fetchone()
    if row is None:
        raise HTTPException(404,'Value assessment not found')
    return {'id':assessment_id,**json.loads(row['result'])}


@app.post('/api/value/calculator')
def value_calculator(payload: ValueCalculation):
    try:
        result=odds.calculate(payload.probability,payload.decimal_odds,payload.push)
    except ValueError as error:
        raise HTTPException(422,str(error)) from None
    return {**result,'source':'User-entered probabilities · unverified','recommendation':None}


@app.post('/api/backtests')
def run_backtest(options: BacktestOptions):
    created_at=now()
    with db() as connection:
        try:
            report=backtesting.run(connection,options.model_dump(mode='json'),created_at)
        except ValueError as error:
            raise HTTPException(422,str(error)) from None
        cursor=connection.execute('INSERT INTO backtest_runs(created_at,status,result) VALUES (?,?,?)',
            (created_at.isoformat(),report['status'],json.dumps(report,allow_nan=False)))
        report_id=cursor.lastrowid
    return {'id':report_id,**report}


@app.get('/api/backtest-context')
def backtest_context():
    with db() as connection:
        competitions=connection.execute('''SELECT MIN(competition) AS competition,COUNT(*) AS matches,
            MIN(kickoff_utc) AS first_kickoff,MAX(kickoff_utc) AS last_kickoff
            FROM historical_matches GROUP BY competition_key ORDER BY competition''').fetchall()
        receipt=connection.execute('SELECT MIN(imported_at) FROM historical_observations').fetchone()[0]
        runs=connection.execute('SELECT COUNT(*) FROM model_runs').fetchone()[0]
    return {'competitions':[dict(row) for row in competitions],'earliest_history_receipt':receipt,'stored_model_runs':runs,
            'target_limit':100,'automatic_deployment':False}


@app.get('/api/backtests')
def list_backtests(limit: int=Query(50,ge=1,le=100),offset: int=Query(0,ge=0)):
    with db() as connection:
        rows=connection.execute('SELECT id,created_at,status FROM backtest_runs ORDER BY id DESC LIMIT ? OFFSET ?',(limit,offset)).fetchall()
        total=connection.execute('SELECT COUNT(*) FROM backtest_runs').fetchone()[0]
    return {'items':[dict(row) for row in rows],'total':total,'limit':limit,'offset':offset}


@app.get('/api/backtests/{report_id}')
def saved_backtest(report_id: int):
    with db() as connection:
        row=connection.execute('SELECT result FROM backtest_runs WHERE id=?',(report_id,)).fetchone()
    if row is None:
        raise HTTPException(404,'Backtest report not found')
    return {'id':report_id,**json.loads(row['result'])}


@app.get('/api/workflow/status')
def workflow_status():
    with db() as connection:
        counts={table:connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
                for table in ('fixtures','historical_matches','model_runs')}
        row=connection.execute('SELECT id,result FROM workflow_runs ORDER BY id DESC LIMIT 1').fetchone()
        key=connection.execute("SELECT 1 FROM providers WHERE id='api-football'").fetchone()
    present=bool(os.environ.get('API_FOOTBALL_KEY')) or key is not None
    return {'counts':counts,'api_football_key_present':present,'api_football_adapter_available':False,
            'api_football_blocker':'Current official documentation access/review and live connection validation are still required.',
            'public_feed':{'name':'OpenFootball English Premier League','license':'CC0-1.0','license_url':public_data.LICENSE_URL,
                'timezone':'Europe/London','timezone_basis':'Explicit assumption: the JSON feed omits offsets',
                'independently_verified':False},
            'running':WORKFLOW_LOCK.locked(),'last_run':{'id':row['id'],**json.loads(row['result'])} if row else None}


@app.post('/api/workflow/run')
def run_workflow(options: WorkflowOptions):
    if not WORKFLOW_LOCK.acquire(blocking=False):
        raise HTTPException(409,'A data-and-model workflow is already running')
    started=now()
    try:
        seasons=options.seasons if options.seasons is not None else public_data.default_seasons(started)
        try:
            public_data.validate_seasons(seasons,started)
        except public_data.FeedError as error:
            raise HTTPException(422,str(error)) from None
        try:
            datasets=public_data.fetch(seasons,DATA/'source-cache',started,options.horizon_days)
        except public_data.FeedError as error:
            raise HTTPException(502,str(error)) from None
        history_inserted=history_unchanged=fixture_inserted=fixture_updated=0
        received=now()
        with db() as connection:
            for dataset in datasets:
                imported=public_data.ingest_history(connection,dataset['records'],received)
                stored=public_data.ingest_fixtures(connection,dataset['fixtures'],received)
                history_inserted+=imported['inserted'];history_unchanged+=imported['unchanged']
                fixture_inserted+=stored['inserted'];fixture_updated+=stored['updated']
        end=received+timedelta(days=options.horizon_days)
        with db() as connection:
            targets=connection.execute('''SELECT id,home,away FROM fixtures WHERE competition=?
                AND status IN ('scheduled','listed') AND kickoff_utc>? AND kickoff_utc<=? ORDER BY kickoff_utc,id''',
                (public_data.COMPETITION,received.isoformat(),end.isoformat())).fetchall()
        analyses=[]
        for target in targets[:options.max_fixtures]:
            result=analyse_fixture(target['id'],ModelOptions())
            analyses.append({'fixture_id':target['id'],'home':target['home'],'away':target['away'],
                'run_id':result['id'],'status':result['status'],'models':list(result['models']),
                'sample':result['eligible_matches'],'unavailable':result['unavailable']})
        dates=[d['latest_result_date'] for d in datasets if d['latest_result_date']]
        latest=max(dates) if dates else None
        age=(received.astimezone(SAST).date()-date.fromisoformat(latest)).days if latest else None
        successful=sum(a['status']=='experimental' for a in analyses)
        report={'status':'research_working' if successful else 'insufficient_data','created_at':received.isoformat(),
            'completed_at':now().isoformat(),'source':'OpenFootball CC0 community feed','competition':public_data.COMPETITION,
            'seasons':seasons,'history_inserted':history_inserted,'history_unchanged':history_unchanged,
            'fixtures_inserted':fixture_inserted,'fixtures_updated':fixture_updated,'models_generated':successful,
            'pending_analysis':max(0,len(targets)-len(analyses)),'analyses':analyses,
            'latest_reported_result_date':latest,'result_age_days':age,
            'sources':[{**d['data'],'matches':d['matches'],'past_missing_results':d['past_missing_results'],
                        'missing_times':d['missing_times']} for d in datasets],
            'warnings':['Community-reported data is CC0 but not independently verified; this is a working research model, not a validated betting system.',
                'The feed omits timezone offsets and authoritative match statuses. Europe/London is an explicit assumption; future fixtures remain listed/unconfirmed.',
                'Data retrieval time is not source freshness. Latest reported result: '+str(latest)+'; '+str(age)+' days before retrieval.',
                'No bookmaker odds, xG, corners, cards, injuries or line-ups are supplied by this feed. Missing fields remain missing.',
                'API-Football remains the requested primary provider; its key/network/documentation validation must be completed before using that integration.']}
        with db() as connection:
            cursor=connection.execute('INSERT INTO workflow_runs(created_at,result) VALUES (?,?)',(received.isoformat(),json.dumps(report,allow_nan=False)))
            run_id=cursor.lastrowid
        return {'id':run_id,**report}
    finally:
        WORKFLOW_LOCK.release()


@app.get('/api/providers')
def providers():
    with db() as connection:
        saved = {row['id']: row['updated_at'] for row in connection.execute('SELECT id,updated_at FROM providers')}
    env_present=bool(os.environ.get('API_FOOTBALL_KEY'))
    return [{'id': key, 'name': name, 'configured': key in saved or (key=='api-football' and env_present), 'updated_at': saved.get(key),
             'status': 'Environment key present · untested' if key=='api-football' and env_present else 'Stored · connection untested' if key in saved else 'Not configured',
             'integration': 'Pending official documentation review', 'usage': None,
             'connection_test_available': False} for key, name in PROVIDERS.items()]


def check_provider(provider):
    if provider not in PROVIDERS:
        raise HTTPException(404, 'Unknown provider')


@app.put('/api/providers/{provider}')
def save_key(provider: str, payload: ProviderKey):
    check_provider(provider)
    key = payload.key.strip()
    if not key or any(ord(c) < 32 for c in key):
        raise HTTPException(422, 'Invalid key format')
    encrypted = credential_cipher().encrypt(key.encode()).decode()
    with db() as connection:
        connection.execute('INSERT INTO providers VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET encrypted_key=excluded.encrypted_key,updated_at=excluded.updated_at', (provider, encrypted, now().isoformat()))
    return {'saved': True}


@app.delete('/api/providers/{provider}')
def delete_key(provider: str):
    check_provider(provider)
    with db() as connection:
        connection.execute('DELETE FROM providers WHERE id=?', (provider,))
    return {'deleted': True}


@app.exception_handler(sqlite3.Error)
async def database_error(request, error):
    return JSONResponse({'detail': 'Local database operation failed'}, status_code=500)


DIST = ROOT / 'frontend' / 'dist'
if DIST.exists():
    app.mount('/assets', StaticFiles(directory=DIST / 'assets'), name='assets')

    @app.get('/')
    def index():
        return FileResponse(DIST / 'index.html')
