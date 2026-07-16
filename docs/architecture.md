# Architecture

The application is organized as testable domain services with a thin PySide6 UI.

- `models.py`: SQLAlchemy persistence schema and relationships, including the fixed Videos and
  Images catalog discriminator.
- `database.py`: SQLite lifecycle, transactions, WAL configuration, and backups.
- `parsing.py`, `duplicates.py`, `import_service.py`, `exporting.py`: text interchange pipeline.
- `folder_scanner.py`, `ui/folder_scan.py`: read-only folder measurement and asynchronous Qt
  coordination.
- `repository.py`: artist CRUD, filtering, tags, and Trash.
- `rating_service.py`: point ledger, suggestions, tier state machine, and reversals.
- `ui/`: Qt models, dialogs, and the main window.
- `app.py`: first-launch configuration and application composition.

All product behavior is callable without Qt. The UI opens short-lived SQLAlchemy sessions for
each command and refreshes view models after successful commits. Migrations are managed through
Alembic; new installations may create the same schema from metadata before stamping the current
revision.

Artists are independent per media catalog. Name uniqueness, imports, exports, browsing, Trash,
Attention Needed, and recalculation are scoped to one `CollectionKind`; ID-based histories and
rating operations remain attached to their individual artist record.

Folder measurement is an explicit, read-only action. Files are enumerated on a worker thread and
only a user-approved size metadata update is persisted; the application does not watch or alter
the selected directory.
