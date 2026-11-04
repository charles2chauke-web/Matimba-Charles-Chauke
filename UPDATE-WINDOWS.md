# Update an existing Windows installation

The latest odds recovery update preserves usable quotes when other records are
invalid. After updating, open **Top Five**, select **Value screened**, and click
**Build Top Five**. League progress shows specific skipped-item reasons. If prices
are still missing, share `/api/top-five/jobs/latest`; it contains no API key.

1. Close the app's terminal with **Ctrl+C** before copying its database.
2. In your old project folder, copy the entire **data** folder to a safe backup
   location. It contains your database and local credential-protection files.
3. Download the latest GitHub ZIP and extract it into a **new folder**.
4. Copy your old **data** folder into the new project folder, beside `backend` and
   `start-windows.cmd`. Keep the backup. Do not copy `.venv`, `.venv-win312` or `node_modules`.
5. Run **start-windows.cmd** in the new folder and wait for the local server.
6. Refresh the browser with **Ctrl+F5**, then open **API Settings → Test connection**.
7. Open **Top Five → Build Top Five**. It discovers today's future matches
   across all leagues returned by your API, fits available models and imports
   bookmaker quotes. Keep the terminal open while the progress updates.
8. Review the ranked cards and any coverage/exclusion messages. The daily list
   never substitutes tomorrow's matches, and may contain fewer than five.

This update adds operational **experimental Top Five selections**, source odds and
forward paper tracking. They are not independently validated betting tips.
Update the whole application, not just `start-windows.cmd`. A short destination
such as **C:\Charles\app** avoids Windows path-length problems.
Keep API keys out of chat. Windows credential backups require the same Windows
user profile; see README.md before migrating to a different computer.

## SciPy / Fortran installation error

Install **Python 3.12, Windows installer 64-bit x86-64**, including its launcher:
https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe

Download the updated project and run its `start-windows.cmd`. It selects Python
3.12 explicitly, uses a new `.venv-win312` environment, and only installs prebuilt
packages. An older incompatible `.venv` is ignored. There is no need to install
Fortran or remove your application data. Actual execution of the Windows script
still needs confirmation on Windows; wheel availability is checked separately.

## Stuck at “Waiting for application startup” with a large database

The startup observation migration previously lacked an index on
`fixture_observations.fixture_id`, causing repeated scans as fixture/history
coverage grew. The updated backend creates this index before the migration and
logs each startup stage. A synthetic 30,000-fixture / 89,997-observation legacy
store migrated in 0.027 seconds on the Linux development machine; Windows timing
will vary. The user's Windows hang has not yet been confirmed by a local rerun.

You can repair an existing installation without reinstalling or changing results:

1. Close all Charles server terminal windows.
2. In File Explorer open the project folder containing `start-windows.cmd`.
3. Type `cmd` in File Explorer's address bar and press Enter.
4. Paste this single line and press Enter:

```bat
.venv-win312\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('file:data/charles.sqlite3?mode=rw', uri=True, timeout=10); c.execute('CREATE INDEX IF NOT EXISTS fixture_observation_fixture ON fixture_observations(fixture_id)'); c.commit(); c.close(); print('Startup index repaired.')"
```

5. After `Startup index repaired.`, run `start-windows.cmd` normally.

The command opens an existing database only (`mode=rw`) and creates a lookup index.
It does not delete results, model snapshots or encrypted credentials. If the file
cannot be opened, check that you opened Command Prompt in the actual project
folder. `database is locked` means another process may still be using the store;
close the old server rather than launching more copies. Installations using a
custom `CHARLES_DATA_DIR` must use their configured database path instead.
