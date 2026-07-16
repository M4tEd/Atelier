from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, text

from collection_manager.database import Database, _find_migration_assets

HEAD_REVISION = "0002_collection_kinds"


def _create_v1_library(path: Path, *, stamped: bool) -> Database:
    """Create the original portable schema subset with representative audit data."""

    database = Database(path)
    statements = (
        """
        CREATE TABLE artists (
            id INTEGER NOT NULL PRIMARY KEY,
            name_key VARCHAR(300) NOT NULL,
            name VARCHAR(300) NOT NULL,
            tier VARCHAR(40) NOT NULL,
            points INTEGER NOT NULL,
            last_updated DATE,
            date_evaluated DATE,
            date_added DATE NOT NULL,
            size_value NUMERIC(12, 3),
            size_unit VARCHAR(8),
            size_qualifier VARCHAR(20),
            heavy_status VARCHAR(12) NOT NULL,
            is_compressed BOOLEAN NOT NULL,
            notes TEXT NOT NULL,
            reference_url TEXT,
            folder_path TEXT,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            deleted_at DATETIME
        )
        """,
        "CREATE UNIQUE INDEX ix_artists_name_key ON artists (name_key)",
        """
        CREATE TABLE import_runs (
            id INTEGER NOT NULL PRIMARY KEY,
            source_path TEXT NOT NULL,
            source_sha256 VARCHAR(64) NOT NULL,
            source_line_count INTEGER NOT NULL,
            artist_count INTEGER NOT NULL,
            warnings JSON NOT NULL,
            created_at DATETIME NOT NULL
        )
        """,
        """
        CREATE TABLE point_events (
            id INTEGER NOT NULL PRIMARY KEY,
            artist_id INTEGER NOT NULL,
            kind VARCHAR(30) NOT NULL,
            delta INTEGER NOT NULL,
            reason TEXT NOT NULL,
            old_points INTEGER NOT NULL,
            new_points INTEGER NOT NULL,
            rule_key VARCHAR(120),
            pending_tier_shift BOOLEAN NOT NULL,
            created_at DATETIME NOT NULL,
            reversed_event_id INTEGER,
            FOREIGN KEY(artist_id) REFERENCES artists (id) ON DELETE CASCADE,
            FOREIGN KEY(reversed_event_id) REFERENCES point_events (id)
        )
        """,
    )
    with database.engine.begin() as connection:
        for statement in statements:
            connection.exec_driver_sql(statement)
        connection.execute(
            text(
                """
                INSERT INTO artists (
                    id, name_key, name, tier, points, last_updated, date_added,
                    heavy_status, is_compressed, notes, created_at, updated_at
                ) VALUES (
                    1, 'legacy artist', 'Legacy Artist', 'Bangers', 2, '2025-02-03',
                    '2024-01-02', 'no', 0, 'Preserve this note',
                    '2024-01-02 03:04:05', '2025-02-03 04:05:06'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO import_runs (
                    id, source_path, source_sha256, source_line_count,
                    artist_count, warnings, created_at
                ) VALUES (
                    7, 'legacy.txt', :fingerprint, 1, 1, '[]', '2025-02-03 04:05:06'
                )
                """
            ),
            {"fingerprint": "a" * 64},
        )
        connection.execute(
            text(
                """
                INSERT INTO point_events (
                    id, artist_id, kind, delta, reason, old_points, new_points,
                    pending_tier_shift, created_at
                ) VALUES (
                    11, 1, 'legacy_opening', 2, 'Legacy opening balance',
                    0, 2, 0, '2024-01-02 03:04:05'
                )
                """
            )
        )
        if stamped:
            connection.exec_driver_sql(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
            )
            connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                {"revision": "0001_initial"},
            )
    return database


def _revision(database: Database) -> str:
    with database.engine.connect() as connection:
        return str(connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one())


def test_initialize_runs_all_migrations_from_an_empty_database(tmp_path: Path) -> None:
    database = Database(tmp_path / "library.sqlite3")

    database.initialize()

    inspector = inspect(database.engine)
    tables = set(inspector.get_table_names())
    artist_columns = {column["name"] for column in inspector.get_columns("artists")}
    import_columns = {column["name"] for column in inspector.get_columns("import_runs")}
    artist_indexes = {index["name"]: index for index in inspector.get_indexes("artists")}
    import_indexes = {index["name"]: index for index in inspector.get_indexes("import_runs")}

    assert "artists" in tables
    assert "point_events" in tables
    assert _revision(database) == HEAD_REVISION
    assert "collection_kind" in artist_columns
    assert "collection_kind" in import_columns
    assert artist_indexes["ux_artists_collection_kind_name_key"]["column_names"] == [
        "collection_kind",
        "name_key",
    ]
    assert artist_indexes["ux_artists_collection_kind_name_key"]["unique"] == 1
    assert artist_indexes["ix_artists_collection_kind"]["column_names"] == ["collection_kind"]
    assert import_indexes["ix_import_runs_collection_kind"]["column_names"] == ["collection_kind"]


def test_stamped_v1_library_migrates_to_videos_with_backup_and_preserves_data(
    tmp_path: Path,
) -> None:
    database = _create_v1_library(tmp_path / "library.sqlite3", stamped=True)

    database.initialize()

    inspector = inspect(database.engine)
    artist_indexes = {index["name"]: index for index in inspector.get_indexes("artists")}
    backups = list((tmp_path / "backups").glob("*-pre-migration.sqlite3"))
    with database.engine.connect() as connection:
        artist = connection.execute(
            text(
                """
                SELECT collection_kind, name, tier, points, last_updated, notes
                FROM artists WHERE id = 1
                """
            )
        ).one()
        import_run = connection.execute(
            text("SELECT collection_kind, source_path, artist_count FROM import_runs WHERE id = 7")
        ).one()
        point_event = connection.execute(
            text("SELECT artist_id, delta, reason FROM point_events WHERE id = 11")
        ).one()

    assert _revision(database) == HEAD_REVISION
    assert artist == (
        "videos",
        "Legacy Artist",
        "Bangers",
        2,
        "2025-02-03",
        "Preserve this note",
    )
    assert import_run == ("videos", "legacy.txt", 1)
    assert point_event == (1, 2, "Legacy opening balance")
    assert "ix_artists_name_key" not in artist_indexes
    assert artist_indexes["ux_artists_collection_kind_name_key"]["unique"] == 1
    assert len(backups) == 1

    backup_inspector = inspect(Database(backups[0]).engine)
    assert "collection_kind" not in {
        column["name"] for column in backup_inspector.get_columns("artists")
    }


def test_versionless_portable_v1_library_is_backed_up_stamped_and_preserved(
    tmp_path: Path,
) -> None:
    database = _create_v1_library(tmp_path / "library.sqlite3", stamped=False)

    database.initialize()

    with database.engine.connect() as connection:
        artist = connection.execute(
            text("SELECT collection_kind, name, points FROM artists WHERE id = 1")
        ).one()
        import_run = connection.execute(
            text("SELECT collection_kind, source_path FROM import_runs WHERE id = 7")
        ).one()

    assert _revision(database) == HEAD_REVISION
    assert artist == ("videos", "Legacy Artist", 2)
    assert import_run == ("videos", "legacy.txt")
    assert len(list((tmp_path / "backups").glob("*-pre-migration.sqlite3"))) == 1


def test_source_checkout_exposes_migration_assets() -> None:
    assets = _find_migration_assets()

    assert assets is not None
    config_path, migration_dir = assets
    assert config_path.name == "alembic.ini"
    assert (migration_dir / "versions" / "0002_collection_kinds.py").is_file()


def test_backup_copies_existing_library(tmp_path: Path) -> None:
    database = Database(tmp_path / "library.sqlite3")
    database.initialize()

    backup = database.backup(reason="test")

    assert backup is not None
    assert backup.exists()
    assert backup.parent == tmp_path / "backups"
    assert "-test.sqlite3" in backup.name
