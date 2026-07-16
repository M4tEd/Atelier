from __future__ import annotations

from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("sqlalchemy")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QDialog, QMessageBox

from collection_manager.constants import CollectionKind, HeavyStatus, SizeQualifier, Tier
from collection_manager.database import Database
from collection_manager.import_service import ImportService
from collection_manager.parsing import parse_text
from collection_manager.rating_service import RatingService
from collection_manager.repository import ArtistRepository
from collection_manager.ui.import_wizard import (
    ArtistWarningReviewDialog,
    DuplicateResolutionDialog,
    ImportWizard,
)
from collection_manager.ui.main_window import MainWindow
from collection_manager.ui.table_model import ARTIST_ROLE
from collection_manager.ui.tag_text import format_tag_text, parse_tag_text


def test_main_window_loads_and_selects_artist(qtbot, tmp_path: Path) -> None:  # noqa: ANN001
    database = Database(tmp_path / "collection-manager.sqlite3")
    database.initialize()
    with database.session() as session:
        artist = ArtistRepository(session).create("Example Artist", tags=("sculpting",))
        artist_id = artist.id

    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = MainWindow(database=database, library_dir=tmp_path, settings=settings)
    qtbot.addWidget(window)
    window.show()

    assert window.table_model.rowCount() == 1
    assert window.proxy_model.rowCount() == 1
    assert window._select_artist(artist_id)
    assert window.detail.artist is not None
    assert window.detail.artist.name == "Example Artist"


def test_search_and_tier_navigation_filter_rows(qtbot, tmp_path: Path) -> None:  # noqa: ANN001
    database = Database(tmp_path / "collection-manager.sqlite3")
    database.initialize()
    with database.session() as session:
        repository = ArtistRepository(session)
        repository.create("Alpha Artist", tier="Bangers")
        repository.create("Beta Artist", tier="Boring")

    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = MainWindow(database=database, library_dir=tmp_path, settings=settings)
    qtbot.addWidget(window)

    window.search_edit.setText("Alpha")
    assert window.proxy_model.rowCount() == 1
    window.search_edit.clear()
    window._switch_navigation("Boring")
    assert window.proxy_model.rowCount() == 1


def test_collection_tabs_show_independent_catalogs_and_preserve_filters(
    qtbot, tmp_path: Path
) -> None:  # noqa: ANN001
    database = Database(tmp_path / "collection-manager.sqlite3")
    database.initialize()
    with database.session() as session:
        repository = ArtistRepository(session)
        video_shared = repository.create(
            "Shared Artist",
            collection_kind=CollectionKind.VIDEOS,
            tier=Tier.BANGERS,
        )
        repository.create("Video Only", collection_kind=CollectionKind.VIDEOS)
        image_shared = repository.create(
            "Shared Artist",
            collection_kind=CollectionKind.IMAGES,
            tier=Tier.CREAM,
        )
        repository.create("Image Only", collection_kind=CollectionKind.IMAGES)

    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = MainWindow(database=database, library_dir=tmp_path, settings=settings)
    qtbot.addWidget(window)

    assert window._collection_kind is CollectionKind.VIDEOS
    assert {
        window.table_model.artist_at(row).name for row in range(window.table_model.rowCount())
    } == {"Shared Artist", "Video Only"}
    assert window._select_artist(video_shared.id)
    window.search_edit.setText("Shared")
    assert window.proxy_model.rowCount() == 1

    window.collection_tabs.setCurrentIndex(1)

    assert window._collection_kind is CollectionKind.IMAGES
    assert window.search_edit.text() == "Shared"
    assert window.proxy_model.rowCount() == 1
    assert window.detail.artist is not None
    assert window.detail.artist.id == image_shared.id
    assert window.detail.artist.collection_kind is CollectionKind.IMAGES
    assert window.detail.artist.id != video_shared.id
    window.search_edit.clear()
    assert {
        window.table_model.artist_at(row).name for row in range(window.table_model.rowCount())
    } == {"Shared Artist", "Image Only"}


def test_collection_counts_and_trash_are_scoped_to_active_tab(qtbot, tmp_path: Path) -> None:  # noqa: ANN001
    database = Database(tmp_path / "collection-manager.sqlite3")
    database.initialize()
    with database.session() as session:
        repository = ArtistRepository(session)
        trashed_video = repository.create("Trashed Video", collection_kind=CollectionKind.VIDEOS)
        repository.trash(trashed_video.id)
        repository.create("Active Image", collection_kind=CollectionKind.IMAGES)

    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = MainWindow(database=database, library_dir=tmp_path, settings=settings)
    qtbot.addWidget(window)

    assert window._nav_items["all"].text().endswith("0")
    assert window._nav_items["trash"].text().endswith("1")
    window.collection_tabs.setCurrentIndex(1)
    assert window._nav_items["all"].text().endswith("1")
    assert window._nav_items["trash"].text().endswith("0")


def test_recalculate_uses_the_active_collection(qtbot, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    database = Database(tmp_path / "collection-manager.sqlite3")
    database.initialize()
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = MainWindow(database=database, library_dir=tmp_path, settings=settings)
    qtbot.addWidget(window)
    window.collection_tabs.setCurrentIndex(1)
    seen: list[CollectionKind | None] = []

    def evaluate_rules(_service, _as_of_date=None, _artist_ids=None, collection_kind=None):  # noqa: ANN001, ANN202
        seen.append(collection_kind)
        return []

    monkeypatch.setattr(RatingService, "evaluate_rules", evaluate_rules)
    monkeypatch.setattr(QMessageBox, "information", lambda *_args, **_kwargs: None)

    window.recalculate()

    assert seen == [CollectionKind.IMAGES]


def test_default_sort_is_best_tier_then_alphabetical(qtbot, tmp_path: Path) -> None:  # noqa: ANN001
    database = Database(tmp_path / "collection-manager.sqlite3")
    database.initialize()
    with database.session() as session:
        repository = ArtistRepository(session)
        for name, tier in (
            ("Zeta Cream", Tier.CREAM),
            ("Alpha Cream", Tier.CREAM),
            ("Zulu Banger", Tier.BANGERS),
            ("Beta Banger", Tier.BANGERS),
            ("Unsavable", Tier.UNSAVABLE_BANGERS),
            ("Revisit", Tier.WORTH_REVISITING),
            ("Fallen", Tier.FELL_OFF),
            ("Boring", Tier.BORING),
        ):
            repository.create(name, tier=tier)

    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = MainWindow(database=database, library_dir=tmp_path, settings=settings)
    qtbot.addWidget(window)

    visible_names = [
        window.proxy_model.index(row, 0).data(ARTIST_ROLE).name
        for row in range(window.proxy_model.rowCount())
    ]
    assert visible_names == [
        "Alpha Cream",
        "Zeta Cream",
        "Beta Banger",
        "Zulu Banger",
        "Unsavable",
        "Revisit",
        "Fallen",
        "Boring",
    ]


def test_tag_text_round_trips_commas_and_json_escapes() -> None:
    tags = ["portrait", "anatomy, advanced", 'say "hello"', "path\\reference"]

    rendered = format_tag_text(tags)

    assert '"anatomy, advanced"' in rendered
    assert parse_tag_text(rendered) == tags


def test_detail_editor_preserves_comma_containing_tag(qtbot, tmp_path: Path) -> None:  # noqa: ANN001
    database = Database(tmp_path / "collection-manager.sqlite3")
    database.initialize()
    with database.session() as session:
        artist = ArtistRepository(session).create(
            "Comma Artist", tags=("portrait", "anatomy, advanced")
        )
        artist_id = artist.id

    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = MainWindow(database=database, library_dir=tmp_path, settings=settings)
    qtbot.addWidget(window)
    assert window._select_artist(artist_id)

    assert window.detail.values()["tags"] == ["portrait", "anatomy, advanced"]


def test_session_undo_restores_previous_update_date(qtbot, tmp_path: Path) -> None:  # noqa: ANN001
    database = Database(tmp_path / "collection-manager.sqlite3")
    database.initialize()
    previous = date(2024, 5, 1)
    with database.session() as session:
        artist = ArtistRepository(session).create("Undo Artist", last_updated=previous)
        artist_id = artist.id

    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = MainWindow(database=database, library_dir=tmp_path, settings=settings)
    qtbot.addWidget(window)
    window._run_rating_transaction(
        lambda service: service.log_update(artist_id, date(2026, 7, 10), "good")
    )
    window._last_rating_action_depth = 1

    window.undo_last()

    with database.session() as session:
        restored = ArtistRepository(session).get(artist_id)
        assert restored.last_updated == previous
        assert restored.points == 0


def test_fresh_window_does_not_offer_persisted_history_undo(
    qtbot, tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    database = Database(tmp_path / "collection-manager.sqlite3")
    database.initialize()
    with database.session() as session:
        artist = ArtistRepository(session).create("Old History")
        artist_id = artist.id
        RatingService(session).adjust_points(artist_id, 1, "Persisted before window")

    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = MainWindow(database=database, library_dir=tmp_path, settings=settings)
    qtbot.addWidget(window)
    messages: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda _parent, _title, text, *_args: messages.append(text),
    )

    window.undo_last()

    assert messages == ["There is no reversible action from this application session."]
    with database.session() as session:
        assert ArtistRepository(session).get(artist_id).points == 1


def test_unique_warning_blocks_import_until_edited_confirmation(qtbot, monkeypatch) -> None:  # noqa: ANN001
    preview = parse_text("Bangers\nJayanam (2, 4+ GB)\n")
    assert preview.artists[0].heavy_status is HeavyStatus.UNKNOWN
    wizard = ImportWizard(preview)
    qtbot.addWidget(wizard)

    assert wizard._warning_indexes == [0]
    assert not wizard.import_button.isEnabled()

    reviewed = deepcopy(preview.artists[0])
    reviewed.heavy_status = HeavyStatus.YES

    class ConfirmedReview:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def exec(self) -> QDialog.DialogCode:
            return QDialog.DialogCode.Accepted

        def artist(self):  # noqa: ANN202
            return reviewed

    monkeypatch.setattr(
        "collection_manager.ui.import_wizard.ArtistWarningReviewDialog",
        ConfirmedReview,
    )
    wizard.warning_table.selectRow(0)
    wizard._review_warning()

    assert wizard.preview.artists[0].heavy_status is HeavyStatus.YES
    assert wizard.import_button.isEnabled()


def test_real_warning_review_returns_domain_enums_and_commits(qtbot, tmp_path: Path) -> None:  # noqa: ANN001
    preview = parse_text("Bangers\nJayanam (2, 4+ GB)\n")
    dialog = ArtistWarningReviewDialog(preview.artists[0])
    qtbot.addWidget(dialog)
    dialog.heavy_combo.setCurrentIndex(dialog.heavy_combo.findData(HeavyStatus.YES))

    reviewed = dialog.artist()

    assert reviewed.tier is Tier.BANGERS
    assert reviewed.size_qualifier is SizeQualifier.AT_LEAST
    assert reviewed.heavy_status is HeavyStatus.YES
    preview.artists[0] = reviewed
    database = Database(tmp_path / "collection-manager.sqlite3")
    database.initialize()
    result = ImportService(database, CollectionKind.VIDEOS).apply_import(preview)
    assert result.added == 1


def test_real_duplicate_warning_resolution_returns_domain_enums_and_commits(
    qtbot, tmp_path: Path
) -> None:  # noqa: ANN001
    preview = parse_text("Bangers\nJayanam (2, 4+ GB)\n\nWorth Revisiting\nJayanam Early\n")
    group = preview.duplicate_groups[0]
    dialog = DuplicateResolutionDialog(group, preview.artists)
    qtbot.addWidget(dialog)
    dialog.heavy_combo.setCurrentIndex(dialog.heavy_combo.findData(HeavyStatus.YES))

    resolution = dialog.resolution()

    assert resolution.canonical.tier is Tier.BANGERS
    assert resolution.canonical.size_qualifier is SizeQualifier.AT_LEAST
    assert resolution.canonical.heavy_status is HeavyStatus.YES
    database = Database(tmp_path / "collection-manager.sqlite3")
    database.initialize()
    result = ImportService(database, CollectionKind.VIDEOS).apply_import(preview, [resolution])
    assert result.added == 1
