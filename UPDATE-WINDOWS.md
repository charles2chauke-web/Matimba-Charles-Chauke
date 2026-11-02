# Update an existing Windows installation

1. Close the app's terminal with **Ctrl+C** before copying its database.
2. In your old project folder, copy the entire **data** folder to a safe backup
   location. It contains your database and local credential-protection files.
3. Download the latest GitHub ZIP and extract it into a **new folder**.
4. Copy your old **data** folder into the new project folder, beside `backend` and
   `start-windows.cmd`. Keep the backup. Do not copy `.venv`, `.venv-win312` or `node_modules`.
5. Run **start-windows.cmd** in the new folder and wait for the local server.
6. Refresh the browser with **Ctrl+F5**, then open **API Settings → Test connection**.
7. After a successful connection test, open **Run Model → Load data & run model**.
   The default API-Football league is 39 (English Premier League); select another
   league ID or season start years if needed. Open **View predictions** for results.

The new page displays research candidates and qualification blockers instead of a
development placeholder. It does not fabricate five qualified betting tips.

This update adds an API-Football **Test connection** button and league
fixture/results import. You must update the whole application, not just
`start-windows.cmd`, to get these features. Your saved key remains untested until
you run the test locally. Live odds and independent production model approval
remain unfinished; qualified betting picks are not enabled by this update.
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
