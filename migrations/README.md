# Database migrations

Run migrations against a selected library by overriding the URL:

```powershell
alembic -x db_path="C:\path\to\collection-manager.sqlite3" upgrade head
```

Application startup creates a safety backup before upgrading an existing database.

Revision `0002_collection_kinds` upgrades both Alembic-stamped and older versionless portable
libraries. Existing artists and import reports are assigned to the Videos collection, while
artist-name uniqueness becomes scoped to each collection. The Windows portable build keeps this
directory and `alembic.ini` beside the executable because Alembic loads revision files at runtime.
