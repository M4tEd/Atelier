# Collection Manager

Collection Manager is a Windows-first, local-only PySide6 desktop application for keeping
independent video and image catalogs with a complete rating record per artist in each catalog.
Data lives in a user-selected SQLite library and can be imported from or exported to a
human-readable text format. Selecting a local artist folder can also calculate its logical file
size in the background; the application never modifies the folder's contents.

## Double-click launch

- **Windows:** double-click `Launch Collection Manager.bat`.
- **macOS:** double-click `Launch Collection Manager.command`.

The launcher performs setup automatically the first time. Later launches open the application
directly without entering PowerShell or terminal commands. Python 3.12 or newer must be installed
if the included development environment is not present.

## Development

Requires Python 3.12 or newer.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest
python -m collection_manager
```

On macOS or Linux, activate `.venv/bin/activate` instead.
For a pinned development and packaging environment:

```powershell
python -m pip install -r requirements-lock.txt
python -m pip install --no-build-isolation --no-deps -e .
```

## Windows portable build

Run `scripts/build_windows.ps1` from an activated Python 3.12 virtual environment. The script
uses Qt for Python's deployment tool in standalone mode and creates a ZIP in `dist/`.

See [the v2 specification](docs/collection_manager_spec_v2.md) for the product contract and
[the architecture notes](docs/architecture.md) for implementation details.
