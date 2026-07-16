from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QObject, QSettings, Signal

from collection_manager.constants import (
    CollectionKind,
    HeavyStatus,
    SizeQualifier,
    Tier,
)
from collection_manager.database import Database
from collection_manager.folder_scanner import ScanStatus, SizeMetadataCandidate
from collection_manager.repository import ArtistRepository
from collection_manager.ui import main_window as main_window_module
from collection_manager.ui import widgets as widgets_module
from collection_manager.ui.main_window import MainWindow
from collection_manager.ui.widgets import ArtistDetailPanel


class _DeterministicScanController(QObject):
    started = Signal(int, str, object)
    progress = Signal(int, object)
    completed = Signal(int, object)
    failed = Signal(int, str)
    cancelled = Signal(int)
    job_finished = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self.current_request_id: int | None = None
        self.started_paths: list[tuple[str, object]] = []
        self.shutdown_calls: list[int] = []

    def start(self, path: Path | str, context: object = None) -> int:
        self.cancel_current()
        self.current_request_id = 1
        normalized = str(Path(path).resolve(strict=False))
        self.started_paths.append((normalized, context))
        self.started.emit(1, normalized, context)
        return 1

    def finish(self) -> None:
        request_id = self.current_request_id
        assert request_id is not None
        self.current_request_id = None
        self.completed.emit(request_id, SimpleNamespace(status=ScanStatus.COMPLETE))
        self.job_finished.emit(request_id)

    def cancel_current(self) -> int | None:
        request_id = self.current_request_id
        if request_id is not None:
            self.current_request_id = None
            self.cancelled.emit(request_id)
        return request_id

    def shutdown(self, timeout_ms: int = 5000) -> bool:
        self.shutdown_calls.append(timeout_ms)
        self.cancel_current()
        return True


def test_calculated_folder_metadata_persists_through_main_window_save(
    qtbot, tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    database = Database(tmp_path / "collection-manager.sqlite3")
    database.initialize()
    with database.session() as session:
        repository = ArtistRepository(session)
        target = repository.create(
            "Shared Artist",
            collection_kind=CollectionKind.VIDEOS,
            tier=Tier.BANGERS,
            points=2,
            size_value=Decimal("1.000"),
            size_unit="GB",
            size_qualifier=SizeQualifier.EXACT,
            heavy_status=HeavyStatus.NO,
            is_compressed=True,
        )
        target_id = target.id
        other = repository.create(
            "Shared Artist",
            collection_kind=CollectionKind.IMAGES,
            tier=Tier.BORING,
            points=-1,
            size_value=Decimal("0.500"),
            size_unit="GB",
            size_qualifier=SizeQualifier.EXACT,
            heavy_status=HeavyStatus.NO,
            is_compressed=False,
        )
        other_id = other.id

    selected_folder = (tmp_path / "calculated folder").resolve()
    selected_folder.mkdir()
    candidate = SizeMetadataCandidate(
        size_value=Decimal("6.250"),
        size_unit="GB",
        size_qualifier=SizeQualifier.EXACT,
        heavy_status=HeavyStatus.YES,
        source_bytes=6_250_000_000,
    )
    controller = _DeterministicScanController()

    def make_detail_panel(library_dir: Path) -> ArtistDetailPanel:
        return ArtistDetailPanel(library_dir, folder_scan_controller=controller)

    monkeypatch.setattr(main_window_module, "ArtistDetailPanel", make_detail_panel)
    monkeypatch.setattr(widgets_module, "size_candidate_from_scan", lambda _scan: candidate)
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = MainWindow(database=database, library_dir=tmp_path, settings=settings)
    qtbot.addWidget(window)

    try:
        assert window._select_artist(target_id)
        window.detail.folder_edit.setText(str(selected_folder))
        window.detail.calculate_folder_size_button.click()
        assert controller.started_paths == [(str(selected_folder), target_id)]

        controller.finish()
        window.detail.save_button.click()

        with database.session() as session:
            repository = ArtistRepository(session)
            persisted = repository.get(target_id)
            untouched = repository.get(other_id)

            assert persisted.folder_path == str(selected_folder)
            assert persisted.size_value == Decimal("6.250")
            assert persisted.size_unit == "GB"
            assert persisted.size_qualifier_value is SizeQualifier.EXACT
            assert persisted.heavy_status_value is HeavyStatus.YES
            assert persisted.is_compressed is True
            assert persisted.tier_value is Tier.BANGERS
            assert persisted.points == 2

            assert untouched.folder_path is None
            assert untouched.size_value == Decimal("0.500")
            assert untouched.size_qualifier_value is SizeQualifier.EXACT
            assert untouched.heavy_status_value is HeavyStatus.NO
            assert untouched.is_compressed is False
            assert untouched.tier_value is Tier.BORING
            assert untouched.points == -1
    finally:
        window.close()

    assert controller.shutdown_calls
