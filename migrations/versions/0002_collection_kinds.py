"""Separate video and image collections.

Revision ID: 0002_collection_kinds
Revises: 0001_initial
Create Date: 2026-07-10

The initial revision intentionally used ``Base.metadata.create_all``. That means a fresh
installation can arrive here with the collection columns already present, while an existing
library stamped at 0001 still has the original schema. Portable releases could also create the
original schema without an Alembic version table. Keep this migration schema-aware so all three
states converge on the same result without losing existing records.
"""

from __future__ import annotations

from collections.abc import Iterable

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

revision = "0002_collection_kinds"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

DEFAULT_COLLECTION = "videos"
COMPOSITE_NAME_INDEX = "ux_artists_collection_kind_name_key"
COLLECTION_INDEXES = {
    "artists": "ix_artists_collection_kind",
    "import_runs": "ix_import_runs_collection_kind",
}


def _inspector() -> Inspector:
    return sa.inspect(op.get_bind())


def _column_names(inspector: Inspector, table_name: str) -> set[str]:
    return {str(column["name"]) for column in inspector.get_columns(table_name)}


def _column_tuple(item: dict[str, object]) -> tuple[str, ...]:
    values = item.get("column_names") or ()
    return tuple(str(value) for value in values)


def _has_unique_columns(
    indexes: Iterable[dict[str, object]],
    constraints: Iterable[dict[str, object]],
    columns: tuple[str, ...],
) -> bool:
    return any(
        bool(item.get("unique")) and _column_tuple(item) == columns for item in indexes
    ) or any(_column_tuple(item) == columns for item in constraints)


def _ensure_collection_column(table_name: str) -> None:
    inspector = _inspector()
    if table_name not in inspector.get_table_names():
        return
    if "collection_kind" not in _column_names(inspector, table_name):
        op.add_column(
            table_name,
            sa.Column(
                "collection_kind",
                sa.String(length=20),
                nullable=False,
                server_default=DEFAULT_COLLECTION,
            ),
        )
    # Defensive for databases produced by prerelease builds with a nullable column.
    op.execute(
        sa.text(
            f"UPDATE {table_name} "  # noqa: S608 - table names are fixed migration constants
            "SET collection_kind = :default_kind "
            "WHERE collection_kind IS NULL OR trim(collection_kind) = ''"
        ).bindparams(default_kind=DEFAULT_COLLECTION)
    )

    inspector = _inspector()
    indexes = inspector.get_indexes(table_name)
    if not any(_column_tuple(index) == ("collection_kind",) for index in indexes):
        op.create_index(
            COLLECTION_INDEXES[table_name],
            table_name,
            ["collection_kind"],
            unique=False,
        )


def _replace_artist_name_uniqueness() -> None:
    inspector = _inspector()
    if "artists" not in inspector.get_table_names():
        return

    indexes = inspector.get_indexes("artists")
    for index in indexes:
        if bool(index.get("unique")) and _column_tuple(index) == ("name_key",):
            index_name = index.get("name")
            if index_name:
                op.drop_index(str(index_name), table_name="artists")

    # Refresh after dropping the v1 global-name index. A fresh dynamic 0001 may already have
    # either the explicit v2 index or an equivalent composite unique constraint.
    inspector = _inspector()
    if not _has_unique_columns(
        inspector.get_indexes("artists"),
        inspector.get_unique_constraints("artists"),
        ("collection_kind", "name_key"),
    ):
        op.create_index(
            COMPOSITE_NAME_INDEX,
            "artists",
            ["collection_kind", "name_key"],
            unique=True,
        )


def upgrade() -> None:
    _ensure_collection_column("artists")
    _ensure_collection_column("import_runs")
    _replace_artist_name_uniqueness()


def downgrade() -> None:
    inspector = _inspector()
    if "artists" in inspector.get_table_names():
        for index in inspector.get_indexes("artists"):
            if _column_tuple(index) == ("collection_kind", "name_key"):
                index_name = index.get("name")
                if index_name:
                    op.drop_index(str(index_name), table_name="artists")
        inspector = _inspector()
        if not _has_unique_columns(
            inspector.get_indexes("artists"),
            inspector.get_unique_constraints("artists"),
            ("name_key",),
        ):
            # This deliberately fails if callers created the same name in both collections;
            # collapsing those independent records during downgrade would be data loss.
            op.create_index("ix_artists_name_key", "artists", ["name_key"], unique=True)

    inspector = _inspector()
    for table_name, expected_name in COLLECTION_INDEXES.items():
        if table_name not in inspector.get_table_names():
            continue
        for index in inspector.get_indexes(table_name):
            if _column_tuple(index) != ("collection_kind",):
                continue
            index_name = index.get("name") or expected_name
            op.drop_index(str(index_name), table_name=table_name)

    inspector = _inspector()
    if "import_runs" in inspector.get_table_names() and "collection_kind" in _column_names(
        inspector, "import_runs"
    ):
        op.drop_column("import_runs", "collection_kind")
    inspector = _inspector()
    if "artists" in inspector.get_table_names() and "collection_kind" in _column_names(
        inspector, "artists"
    ):
        op.drop_column("artists", "collection_kind")
