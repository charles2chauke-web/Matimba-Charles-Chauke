"""Private football research workspace. No unvalidated prediction claims."""
import csv
import io
import json
import logging
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
from backend import history, models, odds, backtesting, public_data, shortlist, api_football, provider_odds, daily_policy, count_models, provider_statistics, strict_shortlist

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
    log=logging.getLogger('uvicorn.error')
    log.info('Charles startup: preparing local database tables and indexes')
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
        CREATE INDEX IF NOT EXISTS fixture_observation_fixture ON fixture_observations(fixture_id);
        CREATE TABLE IF NOT EXISTS workflow_runs (
          id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, result TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS top_five_runs (
          id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, result TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS top_five_jobs (
          id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, status TEXT NOT NULL, result TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS top_five_daily_fixtures (
          provider_id INTEGER NOT NULL, day TEXT NOT NULL, checked_at TEXT NOT NULL,
          PRIMARY KEY(provider_id,day));
        ''')
        connection.execute('INSERT OR IGNORE INTO settings VALUES (1, ?)', (json.dumps(Settings().model_dump()),))
        stored_settings=json.loads(connection.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])
        if stored_settings.get('window')!='today':
            connection.execute('UPDATE settings SET value=? WHERE id=1',(json.dumps({**stored_settings,'window':'today'}),))
        log.info('Charles startup: checking history and provider tables')
        history.initialize(connection)
        odds.initialize(connection)
        backtesting.initialize(connection)
        api_football.initialize(connection)
        connection.execute("UPDATE top_five_jobs SET status='interrupted' WHERE status='running'")
        # Preserve only the existing row's latest asserted state/time. Do not
        # manufacture earlier schedule/status history during migration.
        log.info('Charles startup: checking existing fixture observations')
        connection.execute('''INSERT INTO fixture_observations(fixture_id,identity,status,recorded_at,source)
            SELECT f.id,f.identity,f.status,f.imported_at,f.source FROM fixtures f
            WHERE NOT EXISTS (SELECT 1 FROM fixture_observations o WHERE o.fixture_id=f.id)''')
    log.info('Charles startup: opening local credential protection')
    credential_cipher()
    log.info('Charles startup: ready')


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
    window: str = Field(default='today', pattern='^today$')
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
    source: str = Field(default='auto',pattern='^(auto|api-football|public)$')
    league_id: int = Field(default=39,ge=1,le=100000)
    api_seasons: list[int] | None = Field(default=None,min_length=1,max_length=3)
    seasons: list[str] | None = Field(default=None,max_length=3)
    horizon_days: int = Field(default=1,ge=1,le=1)
    max_fixtures: int = Field(default=12,ge=1,le=30)
    statistics_budget: int = Field(default=0,ge=0,le=120)


class TopFiveOptions(BaseModel):
    model_config=ConfigDict(extra='forbid')
    leagues: list[int] | None = Field(default=None,min_length=1,max_length=1000)
    window: str = Field(default='today',pattern='^today$')
    mode: str = Field(default='value',pattern='^(value|research)$')
    model: str = Field(default='poisson',pattern='^(poisson|dixon_coles)$')
    max_odds_age_minutes: int = Field(default=180,ge=1,le=240)
    min_probability: float = Field(default=.75,ge=.75,le=1)
    min_ev: float = Field(default=.03,ge=0,le=10)
    max_per_league: int = Field(default=2,ge=1,le=5)
    statistics_budget: int = Field(default=500,ge=0,le=750)


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
        item['eligible'] = item['status'] == 'scheduled' and daily_policy.is_today(item['kickoff_utc'],now())
        item['research_eligible'] = item['status'] in ('scheduled','listed') and daily_policy.is_today(item['kickoff_utc'],now())
        item['reliability'] = ('API-Football reported · not independently verified' if item['source'].startswith('API-Football')
                               else 'Community schedule · status/timezone unconfirmed' if item['source'].startswith('OpenFootball') else 'Unverified manual import')
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
        latest_top=connection.execute('SELECT result FROM top_five_runs ORDER BY id DESC LIMIT 1').fetchone()
        top=None
        if latest_top:
            saved=json.loads(latest_top['result'])
            top=strict_shortlist.build(connection,{**settings,**saved['settings']},current,saved['model'],saved['mode'],saved['max_odds_age_minutes'],saved.get('league_ids'))
    return {'upcoming': len(upcoming), 'analysed': 0, 'research_analysed':research_count,'qualified': 0, 'average_probability': None,
            'historical_roi': None, 'last_import': last, 'last_successful_analysis': None,
            'selections': [], 'fixtures': upcoming, 'phase': 5,
            'workflow':{'id':latest_workflow['id'],**json.loads(latest_workflow['result'])} if latest_workflow else None,
            'top_five':top,
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


@app.get('/api/research/shortlist')
def daily_shortlist(model: str=Query('poisson',pattern='^(poisson|dixon_coles|xg_poisson)$'),
                    mode: str=Query('research',pattern='^(research|value)$'),
                    window: str | None=Query(None,pattern='^(today|24h|7d)$'),
                    max_odds_age_minutes: int=Query(60,ge=1,le=240),
                    min_probability: float | None=Query(None,ge=0,le=1),
                    min_ev: float | None=Query(None,ge=0,le=10),
                    max_per_league: int | None=Query(None,ge=1,le=5),
                    leagues: list[int] | None=Query(None,min_length=1,max_length=5)):
    settings=get_settings()
    settings.update({k:v for k,v in {'min_probability':min_probability,'min_ev':min_ev,'max_per_league':max_per_league}.items() if v is not None})
    with db() as connection:
        try:
            return shortlist.build(connection,settings,now().astimezone(SAST),model,mode,window,max_odds_age_minutes,leagues)
        except ValueError as error:
            raise HTTPException(422,str(error)) from None


@app.get('/api/daily-predictions')
@app.get('/api/shortlist')
def strict_daily_predictions(mode: str=Query('research',pattern='^(research|value)$'),model: str=Query('poisson',pattern='^(poisson|dixon_coles)$'),
    min_probability:float=Query(.75,ge=.75,le=1),min_ev:float=Query(.03,ge=0,le=10),max_per_league:int=Query(2,ge=1,le=5),max_odds_age_minutes:int=Query(180,ge=1,le=240)):
    settings={**get_settings(),'min_probability':min_probability,'min_ev':min_ev,'max_per_league':max_per_league}
    with db() as connection:
        return strict_shortlist.build(connection,settings,now(),model,mode,max_odds_age_minutes)


def top_five_progress(job_id,report,status='running'):
    with db() as connection:
        connection.execute('UPDATE top_five_jobs SET status=?,result=? WHERE id=?',
                           (status,json.dumps(report,allow_nan=False),job_id))


def build_top_five(job_id,options):
    """Background local job. Each league is atomic; failed coverage is visible."""
    progress={'message':'Discovering today’s fixtures across all available API leagues','completed_leagues':0,'total_leagues':0,'leagues':[]}
    try:
        generation_day=now().astimezone(SAST).date()
        key=api_football_key()
        with api_football.LOCK:
            with api_football.client() as client:
                try:
                    account=api_football.status(client,key)
                except api_football.ProviderError as error:
                    save_provider_check(key,error.report())
                    raise
                save_provider_check(key,account)
                if not account['ok']:raise HTTPException(409,account['message'])
                discovered=api_football.daily_leagues(client,key,now())
                if options.mode=='value':
                    try:progress['count_odds_coverage']=provider_odds.load_count_markets(client,key)
                    except api_football.ProviderError as error:progress['count_odds_error']=str(error)
        if options.leagues is not None:discovered=[l for l in discovered if l['league_id'] in options.leagues]
        discovered.sort(key=lambda l:(l['league_id'] not in daily_policy.MAJOR_LEAGUES,l['first_kickoff'] if 'first_kickoff' in l else '',l['league_id']))
        checked=now().isoformat()
        with db() as connection:
            connection.execute('DELETE FROM top_five_daily_fixtures WHERE day=?',(generation_day.isoformat(),))
            connection.executemany('INSERT INTO top_five_daily_fixtures VALUES (?,?,?)',
                [(pid,generation_day.isoformat(),checked) for league in discovered for pid in league['fixture_ids']])
        progress['total_leagues']=len(discovered)
        progress['fixtures_discovered']=sum(len(l['fixture_ids']) for l in discovered)
        top_five_progress(job_id,progress)
        successful=[]
        remaining_statistics=options.statistics_budget
        for daily in discovered:
            if now().astimezone(SAST).date()!=generation_day:
                progress['message']='The SAST date changed during the build. Build again for the new day; yesterday’s matches were not promoted.'
                top_five_progress(job_id,progress,'interrupted')
                return
            league=daily['league_id']
            progress['message']=f'Loading results and fitting models for league {league}'
            top_five_progress(job_id,progress)
            item={'league_id':league,'models_generated':0,'odds_inserted':0,'error':None}
            try:
                with api_football.LOCK:
                    with api_football.client() as client:
                        seasons=api_football.available_seasons(client,key,league,daily['season'])
                def statistics_progress(message):
                    progress['message']=message;top_five_progress(job_id,progress)
                run=run_api_football_workflow(WorkflowOptions(source='api-football',league_id=league,api_seasons=seasons,max_fixtures=30,statistics_budget=min(120,remaining_statistics)),now(),daily['fixture_ids'],progress=statistics_progress)
                item['statistics']=run.get('statistics',{})
                remaining_statistics=max(0,remaining_statistics-item['statistics'].get('calls',0))
                item['models_generated']=run['models_generated']
                item['competition']=run['competition']
                if run.get('pending_analysis'):
                    item['error']=f"{run['pending_analysis']} daily fixtures exceeded the 30-match analysis limit for this league; they remain unanalysed."
                successful.append(league)
                if options.mode=='value':
                    progress['message']=f'Loading bookmaker odds for league {league}'
                    top_five_progress(job_id,progress)
                    key=api_football_key()
                    if not api_football.LOCK.acquire(blocking=False):
                        raise HTTPException(409,'Provider busy. Try building the list again after the current request.')
                    try:
                        with api_football.client() as client:
                            season=daily['season']
                            raw=provider_odds.pages(client,key,league,season,day=generation_day.isoformat())
                        received=now()
                        with db() as connection:
                            fixtures={r['provider_id']:dict(r) for r in connection.execute('''SELECT f.*,p.provider_id FROM fixtures f
                                JOIN api_football_fixtures p ON p.fixture_id=f.id WHERE p.league_id=? AND p.season=?''',(league,season))}
                            records,diagnostics=provider_odds.parse_with_diagnostics(raw,fixtures,league,season,received)
                            result=odds.ingest(connection,records)
                            item['odds_inserted']=result['inserted']
                            item['odds_unchanged']=result['duplicates']
                            item['odds_fixtures_skipped']=diagnostics['fixtures_skipped']
                            item['odds_diagnostics']=diagnostics
                            if not records:
                                reasons=', '.join(diagnostics['rejection_counts']) or 'No bookmaker prices returned for this league/date'
                                item['error']='No usable odds: '+reasons+'.'
                    finally:
                        api_football.LOCK.release()
            except (HTTPException,api_football.ProviderError,ValueError) as error:
                item['error']=error.detail if isinstance(error,HTTPException) else str(error) if isinstance(error,api_football.ProviderError) else 'Provider odds validation failed. No odds from this league were imported.'
            progress['leagues'].append(item)
            progress['completed_leagues']+=1
            top_five_progress(job_id,progress)
        settings={**get_settings(),'min_probability':options.min_probability,'min_ev':options.min_ev,'max_per_league':options.max_per_league}
        current=now().astimezone(SAST)
        if current.date()!=generation_day:
            progress['message']='The SAST date changed. Build again for today.'
            top_five_progress(job_id,progress,'interrupted')
            return
        with db() as connection:
            report=strict_shortlist.build(connection,settings,current,options.model,options.mode,options.max_odds_age_minutes,successful)
            report['league_ids']=successful
            cursor=connection.execute('INSERT INTO top_five_runs(created_at,result) VALUES (?,?)',(current.isoformat(),json.dumps(report,allow_nan=False)))
            report_id=cursor.lastrowid
        progress.update({'message':'Top Five screening complete','report_id':report_id,'report':report})
        top_five_progress(job_id,progress,'completed')
    except (HTTPException,api_football.ProviderError) as error:
        progress['message']=error.detail if isinstance(error,HTTPException) else str(error)
        top_five_progress(job_id,progress,'failed')
    except Exception:
        # Background exceptions must not expose credentials or upstream objects.
        progress['message']='Build interrupted by a local error. Your imported data remains stored. Try again after checking the app terminal.'
        top_five_progress(job_id,progress,'failed')
    finally:
        WORKFLOW_LOCK.release()


@app.post('/api/top-five/build')
def start_top_five(options: TopFiveOptions):
    api_football_key()
    if options.leagues is not None and (len(set(options.leagues))!=len(options.leagues) or any(not 1<=i<=100000 for i in options.leagues)):
        raise HTTPException(422,'Choose distinct valid league IDs, or omit leagues to use all available daily leagues.')
    if not WORKFLOW_LOCK.acquire(blocking=False):
        raise HTTPException(409,'A data/model build is already running. Wait for it to finish.')
    try:
        with db() as connection:
            cursor=connection.execute('INSERT INTO top_five_jobs(created_at,status,result) VALUES (?,?,?)',
                (now().isoformat(),'running',json.dumps({'message':'Discovering today’s fixtures','completed_leagues':0,'total_leagues':0,'leagues':[]})))
            job_id=cursor.lastrowid
        threading.Thread(target=build_top_five,args=(job_id,options),daemon=True).start()
        return {'id':job_id,'status':'running'}
    except Exception:
        WORKFLOW_LOCK.release()
        raise


@app.get('/api/top-five/jobs/latest')
def latest_top_five_job():
    with db() as connection:
        row=connection.execute('SELECT * FROM top_five_jobs ORDER BY id DESC LIMIT 1').fetchone()
    return {'id':row['id'],'status':row['status'],**json.loads(row['result'])} if row else None


@app.get('/api/top-five/runs/{report_id}')
def top_five_archive(report_id: int):
    with db() as connection:
        row=connection.execute('SELECT * FROM top_five_runs WHERE id=?',(report_id,)).fetchone()
    if not row:raise HTTPException(404,'Top Five report not found')
    return {'id':row['id'],**json.loads(row['result'])}


@app.get('/api/top-five/performance')
def top_five_performance():
    """First archived priced selection per fixture. No retrospective invented bets."""
    current=now();entries=[];seen=set()
    with db() as connection:
        archives=connection.execute('SELECT id,created_at,result FROM top_five_runs ORDER BY id LIMIT 1001').fetchall()
        if len(archives)>1000:raise HTTPException(422,'Paper audit currently supports 1,000 archived Top Five builds.')
        for archive in archives:
            report=json.loads(archive['result'])
            if report.get('mode')!='value':continue
            for candidate in report['selections']:
                fid=candidate['fixture_id']
                if datetime.fromisoformat(archive['created_at'])>=datetime.fromisoformat(candidate['kickoff_sast']):continue
                if fid in seen:continue
                seen.add(fid)
                identity=odds.fixture_identity(candidate)
                item={'fixture_id':fid,'home':candidate['home'],'away':candidate['away'],'report_id':archive['id'],
                      'decision_at':archive['created_at'],'decimal_odds':candidate['decimal_odds'],'probability':candidate['probability'],
                      'status':'pending','profit':None}
                live=connection.execute('SELECT identity FROM fixtures WHERE id=?',(fid,)).fetchone()
                match=connection.execute('SELECT id FROM historical_matches WHERE identity=?',(identity,)).fetchone()
                if not live or live['identity']!=identity:
                    item['status']='schedule_changed'
                elif match and datetime.fromisoformat(candidate['kickoff_sast'])<current:
                    observations=history.observations(connection,match['id'],current.isoformat())
                    state=history.resolve(observations)['metrics']
                    home,away=state['goals_home'],state['goals_away']
                    # The decision must precede every result observation used to settle.
                    if home is not None and away is not None and observations and all(datetime.fromisoformat(o['available_at'])>datetime.fromisoformat(archive['created_at']) for o in observations):
                        if candidate['market'] in ('corners','yellow_cards'):
                            metric='corners' if candidate['market']=='corners' else 'yellow'
                            counts=[state[metric+'_'+s] for s in ('home','away')]
                            if any(v is None for v in counts):
                                entries.append(item);continue
                            outcome='win' if sum(counts)>float(candidate['line']) else 'loss'
                        else:
                            outcome=backtesting.settlement(candidate['market'],candidate['selection'],candidate['line'],home,away)
                        item.update({'status':outcome,'profit':candidate['decimal_odds']-1 if outcome=='win' else 0 if outcome=='push' else -1})
                entries.append(item)
    settled=[e for e in entries if e['profit'] is not None]
    profit=sum(e['profit'] for e in settled) if settled else None
    return {'status':'paper_tracking','selections':len(entries),'settled':len(settled),
        'pending':sum(e['status']=='pending' for e in entries),'profit_units':profit,
        'roi':profit/len(settled) if settled else None,'entries':entries,
        'message':'Prospective paper tracking of the first saved value-screened selection per match. Provider quotes may not be obtainable; costs are excluded. This is not independent validation or real betting performance.'}


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
    check=providers()[0]['last_check']
    return {'historical_matches': matches, 'observations': observations[0], 'last_history_import': observations[1],
            'sources': [dict(row) for row in sources], 'live_integration': bool(check and check['ok']),
            'api_football_adapter_available':True,
            'integration_blockers': ['Test your local API-Football key and run the league import to verify live account and season coverage.'],
            'available_workflow': 'API-Football fixture/results import, public fallback, or historical CSV. Source accuracy is not independently verified.',
            'missing_features': ['Live odds and detailed statistics import', 'Validated production prediction models']}


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
        if not daily_policy.is_today(record['kickoff_utc'],cutoff):
            raise HTTPException(409,'Strict daily policy: generate predictions only for upcoming fixtures playing today in SAST.')
        fixture = {key: record[key] for key in ('id','home','away','competition','status','source','imported_at')}
        fixture['kickoff_sast'] = datetime.fromisoformat(record['kickoff_utc']).astimezone(SAST).isoformat()
        try:
            rows, fingerprint, evidence = models.load_dataset(connection, record['competition'], cutoff, options.training_days)
        except models.ModelUnavailable as error:
            raise HTTPException(422, str(error)) from None
    result = models.analyse(rows, fixture, cutoff, options.model_dump(), minimum)
    result['count_models'],result['count_unavailable']=count_models.analyse(rows,fixture,cutoff,options.model_dump(),minimum)
    if result['count_models']:result['status']='experimental'
    if record['status']=='listed':
        result['warnings'].insert(0,('Provider marks the fixture time as to be defined. Its listed kickoff is unconfirmed; this is research only.'
            if record['source'].startswith('API-Football') else
            'Community fixture listing is unconfirmed; its timezone is an explicit import assumption. This is research only, not a verified pre-match betting opportunity.'))
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
    return {'counts':counts,'api_football_key_present':present,'api_football_adapter_available':True,
            'api_football_blocker':'Test your saved key in API Settings. Coverage is checked during import; models remain experimental.',
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
        if options.source=='api-football' or (options.source=='auto' and api_football_key(optional=True)):
            return run_api_football_workflow(options,started)
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
            targets=connection.execute('''SELECT id,home,away,kickoff_utc FROM fixtures WHERE competition=?
                AND status IN ('scheduled','listed') AND kickoff_utc>? AND kickoff_utc<=? ORDER BY kickoff_utc,id''',
                (public_data.COMPETITION,received.isoformat(),end.isoformat())).fetchall()
        targets=[t for t in targets if daily_policy.is_today(t['kickoff_utc'],now())]
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
                'This run explicitly uses the public fallback. Select API-Football to use your saved provider key.']}
        with db() as connection:
            cursor=connection.execute('INSERT INTO workflow_runs(created_at,result) VALUES (?,?)',(received.isoformat(),json.dumps(report,allow_nan=False)))
            run_id=cursor.lastrowid
        return {'id':run_id,**report}
    finally:
        WORKFLOW_LOCK.release()


def api_football_key(optional=False):
    with db() as connection:
        row=connection.execute("SELECT encrypted_key FROM providers WHERE id='api-football'").fetchone()
    try:
        key=credential_cipher().decrypt(row['encrypted_key'].encode()).decode() if row else os.environ.get('API_FOOTBALL_KEY','').strip()
    except Exception:
        # Never echo a ciphertext, plaintext or cryptographic exception.
        raise HTTPException(409,'The saved API-Football key cannot be decrypted under this user profile. Save the key again in API Settings.') from None
    if not key and not optional:
        raise HTTPException(409,'Save your direct API-Sports key in API Settings before testing or importing.')
    return key


def save_provider_check(key,result):
    checked=now().isoformat()
    # A replacement key must never inherit a result for the previous key.
    if api_football_key(optional=True)==key:
        with db() as connection:
            connection.execute('INSERT OR REPLACE INTO provider_checks VALUES (?,?,?,?)',
                ('api-football',api_football.revision(key),checked,json.dumps(result,allow_nan=False)))
    return {**result,'checked_at':checked}


@app.post('/api/providers/{provider}/test')
def test_provider(provider: str):
    check_provider(provider)
    if provider!='api-football':
        raise HTTPException(409,'Connection testing is currently implemented only for API-Football.')
    key=api_football_key()
    if not api_football.LOCK.acquire(blocking=False):
        raise HTTPException(409,'An API-Football request is already running. Wait for it to finish.')
    try:
        try:
            with api_football.client() as client:
                result=api_football.status(client,key)
        except api_football.ProviderError as error:
            result=error.report()
        return save_provider_check(key,result)
    finally:
        api_football.LOCK.release()


def run_api_football_workflow(options,started,target_provider_ids=None,progress=lambda message:None):
    key=api_football_key()
    year=started.year if started.month>=7 else started.year-1
    seasons=options.api_seasons if options.api_seasons is not None else [year-2,year-1,year]
    if len(set(seasons))!=len(seasons) or any(type(s) is not int or not 2010<=s<=started.year for s in seasons):
        raise HTTPException(422,'Select one to three distinct API-Football season start years between 2010 and the current year.')
    if not api_football.LOCK.acquire(blocking=False):
        raise HTTPException(409,'An API-Football request is already running. Wait for it to finish.')
    try:
        try:
            with api_football.client() as client:
                try:
                    status=api_football.status(client,key)
                except api_football.ProviderError as error:
                    save_provider_check(key,error.report())
                    raise
                save_provider_check(key,status)
                if not status['ok']:
                    raise HTTPException(409,status['message'])
                datasets=api_football.fetch(client,key,options.league_id,seasons,now())
            received=now()
            with db() as connection:
                imported=api_football.ingest(connection,datasets,received)
            statistics={'calls':0,'cached':0,'available':0,'errors':[]}
            if options.statistics_budget:
                with api_football.client() as client:
                    statistics=provider_statistics.enrich(db,client,key,options.league_id,now(),options.statistics_budget,progress,clock=now)
        except api_football.ProviderError as error:
            # Authentication status is separate from subsequent data-import errors.
            raise HTTPException(502,str(error)) from None
        end=received+timedelta(days=options.horizon_days)
        with db() as connection:
            targets=connection.execute('''SELECT f.id,f.home,f.away,f.kickoff_utc,p.provider_id FROM fixtures f JOIN api_football_fixtures p ON p.fixture_id=f.id
                WHERE p.league_id=? AND p.last_received_at=? AND f.status='scheduled'
                AND f.kickoff_utc>? AND f.kickoff_utc<=? ORDER BY f.kickoff_utc,f.id''',
                (options.league_id,received.isoformat(),received.isoformat(),end.isoformat())).fetchall()
        if target_provider_ids is not None:targets=[t for t in targets if t['provider_id'] in target_provider_ids]
        targets=[t for t in targets if daily_policy.is_today(t['kickoff_utc'],now())]
        analyses=[]
        for target in targets[:options.max_fixtures]:
            result=analyse_fixture(target['id'],ModelOptions())
            analyses.append({'fixture_id':target['id'],'home':target['home'],'away':target['away'],'run_id':result['id'],
                'status':result['status'],'models':list(result['models']),'sample':result['eligible_matches'],'unavailable':result['unavailable']})
        latest=imported.pop('latest_result_utc')
        latest_date=datetime.fromisoformat(latest).astimezone(SAST).date() if latest else None
        successful=sum(a['status']=='experimental' for a in analyses)
        report={'status':'research_working' if successful else 'no_upcoming_fixtures' if not targets else 'insufficient_data',
            'created_at':received.isoformat(),'completed_at':now().isoformat(),'source':'API-Football · provider-reported',
            'competition':', '.join(imported['competitions']),'league_id':options.league_id,'seasons':seasons,**imported,
            'statistics':statistics,
            'history_unchanged':max(0,sum(1 for d in datasets for f in d['fixtures'] if f['metrics'] is not None)-imported['history_inserted']),
            'models_generated':successful,'pending_analysis':max(0,len(targets)-len(analyses)),'analyses':analyses,
            'latest_reported_result_date':latest_date.isoformat() if latest_date else None,
            'result_age_days':(received.astimezone(SAST).date()-latest_date).days if latest_date else None,
            'sources':[{'url':api_football.BASE+'/fixtures','retrieved_at':received.isoformat(),'cache_status':'provider_response',
                        'season':d['season'],'coverage':d['coverage']} for d in datasets],
            'warnings':['Provider account and league-season access were tested. This does not independently validate source accuracy or betting profitability.',
                'Only regulation-time FT results train this adapter. Extra-time/penalty, awarded and walkover results are excluded.',
                'Reported FT corners/cards are imported within the statistics budget when available. Missing counts stay missing. Top Five loads compatible bookmaker quotes separately; xG, injuries and line-ups remain unavailable.',
                'These forecasts remain uncalibrated research. Production betting qualification is unavailable.']}
        with db() as connection:
            cursor=connection.execute('INSERT INTO workflow_runs(created_at,result) VALUES (?,?)',(received.isoformat(),json.dumps(report,allow_nan=False)))
            run_id=cursor.lastrowid
        return {'id':run_id,**report}
    finally:
        api_football.LOCK.release()


@app.get('/api/providers')
def providers():
    with db() as connection:
        saved = {row['id']: row['updated_at'] for row in connection.execute('SELECT id,updated_at FROM providers')}
        row=connection.execute("SELECT * FROM provider_checks WHERE provider='api-football'").fetchone()
    env_present=bool(os.environ.get('API_FOOTBALL_KEY'))
    try:
        current_key=api_football_key(optional=True)
        check={**json.loads(row['result']),'checked_at':row['checked_at']} if row and current_key and row['credential_revision']==api_football.revision(current_key) else None
    except HTTPException:
        check=None
    result=[]
    for key,name in PROVIDERS.items():
        configured=key in saved or (key=='api-football' and env_present)
        last=check if key=='api-football' else None
        status=('Connected · last test successful' if last['ok'] else 'Last connection test failed') if last else 'Stored · connection untested' if configured else 'Not configured'
        result.append({'id':key,'name':name,'configured':configured,'updated_at':saved.get(key),'status':status,
            'integration':'Account test and league/results import available' if key=='api-football' else 'Adapter not implemented',
            'usage':last.get('usage') if last else None,'connection_test_available':key=='api-football','last_check':last})
    return result


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
        connection.execute('DELETE FROM provider_checks WHERE provider=?',(provider,))
    return {'saved': True}


@app.delete('/api/providers/{provider}')
def delete_key(provider: str):
    check_provider(provider)
    with db() as connection:
        connection.execute('DELETE FROM providers WHERE id=?', (provider,))
        connection.execute('DELETE FROM provider_checks WHERE provider=?',(provider,))
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
