from __future__ import annotations

import shutil
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, event
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.orm import Session, sessionmaker

from collection_manager.models import Base


class Database:
    """SQLite lifecycle, transactions, and safe backups for one user library."""

    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = self._build_engine(self.path)
        self.session_factory = sessionmaker(
            bind=self.engine, expire_on_commit=False, autoflush=False, class_=Session
        )

    @staticmethod
    def _build_engine(path: Path) -> Engine:
        engine = create_engine(f"sqlite:///{path}", future=True)

        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

        return engine

    def initialize(self) -> None:
        """Upgrade through Alembic when source migrations are available.

        Source checkouts keep migration assets at the repository root. Standalone builds bundle
        the same assets beside the executable so existing portable libraries can be upgraded.
        Fresh libraries still fall back to model metadata if those assets are unavailable.
        """

        migration_assets = _find_migration_assets()
        if migration_assets is None:
            Base.metadata.create_all(self.engine)
            return
        config_path, migration_dir = migration_assets

        config = Config(str(config_path))
        config.set_main_option("script_location", str(migration_dir))
        config.set_main_option("sqlalchemy.url", f"sqlite:///{self.path.as_posix()}")
        script = ScriptDirectory.from_config(config)
        head = script.get_current_head()
        with self.engine.connect() as connection:
            tables = set(sqlalchemy_inspect(connection).get_table_names())
            current = MigrationContext.configure(connection).get_current_revision()
        if tables and current != head:
            self.backup(reason="pre-migration")
        command.upgrade(config, "head")

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def backup(self, backup_dir: Path | None = None, reason: str = "manual") -> Path | None:
        if not self.path.exists():
            return None
        backup_dir = backup_dir or self.path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        safe_reason = "".join(c if c.isalnum() or c in "-_" else "-" for c in reason)
        destination = backup_dir / f"collection-manager-{stamp}-{safe_reason}.sqlite3"
        with self.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA wal_checkpoint(FULL)")
        shutil.copy2(self.path, destination)
        return destination


def database_path_for_library(library_dir: Path) -> Path:
    return Path(library_dir).expanduser().resolve() / "collection-manager.sqlite3"


def _find_migration_assets() -> tuple[Path, Path] | None:
    """Locate Alembic assets in a source checkout or a standalone deployment folder."""

    module_path = Path(__file__).resolve()
    source_root = module_path
    for _level in range(3):
        source_root = source_root.parent
    candidates = [source_root, Path(sys.executable).resolve().parent]
    seen: set[Path] = set()
    for root in candidates:
        if root in seen:
            continue
        seen.add(root)
        config_path = root / "alembic.ini"
        migration_dir = root / "migrations"
        if config_path.is_file() and migration_dir.is_dir():
            return config_path, migration_dir
    return None
