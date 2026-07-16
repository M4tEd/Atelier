from __future__ import annotations

import threading
import time
from datetime import date
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
from collection_manager.ui.data import ArtistView
from collection_manager.ui.folder_scan import FolderScanController
from collection_manager.ui.main_window import MainWindow
from collection_manager.ui.widgets import ArtistDetailPanel


class FakeFolderScanController(QObject):
    started = Signal(int, str, object)
    progress = Signal(int, object)
    completed = Signal(int, object)
    failed = Signal(int, str)
    cancelled = Signal(int)
    job_finished = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self.next_id = 0
        self.current_request_id: int | None = None
        self.starts: list[tuple[int, str, object]] = []
        self.shutdown_calls: list[int] = []

    def start(self, path: Path | str, context: object = None) -> int:
        self.cancel_current()
        self.next_id += 1
        self.current_request_id = self.next_id
        record = (self.next_id, str(path), context)
        self.starts.append(record)
        self.started.emit(*record)
        return self.next_id

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


def _artist(
    artist_id: int,
    *,
    size_value: Decimal | None = None,
    size_unit: str | None = None,
    size_qualifier: SizeQualifier | None = None,
    heavy_status: HeavyStatus = HeavyStatus.NO,
    is_compressed: bool = False,
) -> ArtistView:
    return ArtistView(
        id=artist_id,
        collection_kind=CollectionKind.VIDEOS,
        name=f"Artist {artist_id}",
        tier=Tier.WORTH_REVISITING,
        points=0,
        last_updated=None,
        date_evaluated=None,
        date_added=date(2026, 1, 1),
        size_value=size_value,
        size_unit=size_unit,
        size_qualifier=size_qualifier,
        heavy_status=heavy_status,
        is_compressed=is_compressed,
        tags=(),
        notes="",
        reference_url=None,
        folder_path=None,
        deleted_at=None,
        needs_attention=False,
        history=(),
    )


def test_controller_runs_off_ui_thread_and_suppresses_stale_result(qtbot, tmp_path: Path) -> None:  # noqa: ANN001
    first_started = threading.Event()
    completed_ids: list[int] = []

    def scanner(path, *, is_cancelled, progress):  # noqa: ANN001, ANN202
        if Path(path).name == "first":
            first_started.set()
            while not is_cancelled():
                time.sleep(0.002)
            return SimpleNamespace(status=ScanStatus.CANCELLED)
        progress(SimpleNamespace(file_count=1, total_bytes=12))
        return SimpleNamespace(status=ScanStatus.COMPLETE)

    controller = FolderScanController(scanner=scanner)
    controller.completed.connect(lambda request_id, _scan: completed_ids.append(request_id))
    first = controller.start(tmp_path / "first", context=1)

    qtbot.waitUntil(first_started.is_set, timeout=2000)
    with qtbot.waitSignal(controller.completed, timeout=2000) as completion:
        second = controller.start(tmp_path / "second", context=2)

    assert first != second
    assert completion.args[0] == second
    assert completed_ids == [second]
    qtbot.waitUntil(lambda: controller.active_job_count == 0, timeout=2000)
    assert controller.shutdown()


def test_browse_scans_and_applies_zero_size_without_auto_save(
    qtbot, tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    controller = FakeFolderScanController()
    panel = ArtistDetailPanel(tmp_path, folder_scan_controller=controller)
    qtbot.addWidget(panel)
    panel.set_artist(_artist(7, is_compressed=True))
    selected = tmp_path / "empty"
    selected.mkdir()
    monkeypatch.setattr(
        "collection_manager.ui.widgets.QFileDialog.getExistingDirectory",
        lambda *_args, **_kwargs: str(selected),
    )
    candidate = SizeMetadataCandidate(
        size_value=Decimal(0),
        size_unit="B",
        size_qualifier=SizeQualifier.EXACT,
        heavy_status=HeavyStatus.NO,
        source_bytes=0,
    )
    monkeypatch.setattr(
        "collection_manager.ui.widgets.size_candidate_from_scan", lambda _scan: candidate
    )
    saved: list[dict[str, object]] = []
    panel.save_requested.connect(saved.append)

    panel.browse_folder_button.click()

    request_id = panel.active_folder_scan_id
    assert request_id is not None
    assert controller.starts[-1][1] == str(selected.resolve())
    assert controller.starts[-1][2] == 7
    assert panel.is_folder_scan_active
    assert not panel.save_button.isEnabled()
    assert panel.cancel_folder_scan_button.isVisibleTo(panel)
    assert saved == []

    controller.completed.emit(request_id, SimpleNamespace(status=ScanStatus.COMPLETE))

    assert not panel.is_folder_scan_active
    assert panel.save_button.isEnabled()
    assert panel.size_known.isChecked()
    assert panel.size_value.value() == 0.0
    assert panel.size_unit.currentText() == "B"
    assert SizeQualifier(panel.size_qualifier.currentData()) is SizeQualifier.EXACT
    assert HeavyStatus(panel.heavy_status.currentData()) is HeavyStatus.NO
    assert panel.compressed.isChecked()  # A size scan never changes compression state.
    assert "Click Save changes" in panel.folder_scan_status_text
    assert saved == []

    panel.save_button.click()
    assert len(saved) == 1
    assert saved[0]["size_value"] == 0.0
    assert saved[0]["folder_path"] == str(selected)


def test_real_controller_and_panel_scan_nested_folder(qtbot, tmp_path: Path) -> None:  # noqa: ANN001
    selected = tmp_path / "nested"
    child = selected / "child"
    child.mkdir(parents=True)
    (selected / "one.bin").write_bytes(b"a" * 750)
    (child / "two.bin").write_bytes(b"b" * 500)
    panel = ArtistDetailPanel(tmp_path)
    qtbot.addWidget(panel)
    panel.set_artist(_artist(8))
    panel.folder_edit.setText(str(selected))

    with qtbot.waitSignal(panel.folder_scan_completed, timeout=3000):
        panel.calculate_folder_size_button.click()

    assert panel.size_known.isChecked()
    assert panel.size_value.value() == 1.25
    assert panel.size_unit.currentText() == "KB"
    assert SizeQualifier(panel.size_qualifier.currentData()) is SizeQualifier.EXACT
    assert HeavyStatus(panel.heavy_status.currentData()) is HeavyStatus.NO
    assert panel.shutdown_folder_scans()


def test_partial_result_populates_lower_bound_and_preserves_compression(
    qtbot, tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    controller = FakeFolderScanController()
    panel = ArtistDetailPanel(tmp_path, folder_scan_controller=controller)
    qtbot.addWidget(panel)
    panel.set_artist(_artist(3, is_compressed=False))
    panel.folder_edit.setText(str(tmp_path))
    candidate = SizeMetadataCandidate(
        size_value=Decimal("4.250"),
        size_unit="GB",
        size_qualifier=SizeQualifier.AT_LEAST,
        heavy_status=HeavyStatus.UNKNOWN,
        source_bytes=4_250_000_000,
    )
    monkeypatch.setattr(
        "collection_manager.ui.widgets.size_candidate_from_scan", lambda _scan: candidate
    )

    panel.calculate_folder_size_button.click()
    request_id = panel.active_folder_scan_id
    assert request_id is not None
    controller.completed.emit(request_id, SimpleNamespace(status=ScanStatus.PARTIAL))

    assert panel.size_value.value() == 4.25
    assert SizeQualifier(panel.size_qualifier.currentData()) is SizeQualifier.AT_LEAST
    assert HeavyStatus(panel.heavy_status.currentData()) is HeavyStatus.UNKNOWN
    assert not panel.compressed.isChecked()
    assert panel.folder_scan_status_text.startswith("Partial scan")


def test_failed_and_cancelled_scans_preserve_previous_size(qtbot, tmp_path: Path) -> None:  # noqa: ANN001
    controller = FakeFolderScanController()
    panel = ArtistDetailPanel(tmp_path, folder_scan_controller=controller)
    qtbot.addWidget(panel)
    panel.set_artist(
        _artist(
            4,
            size_value=Decimal("2.5"),
            size_unit="GB",
            size_qualifier=SizeQualifier.EXACT,
        )
    )
    panel.folder_edit.setText(str(tmp_path))

    panel.calculate_folder_size_button.click()
    failed_request = panel.active_folder_scan_id
    assert failed_request is not None
    controller.failed.emit(failed_request, "Access denied")

    assert panel.size_value.value() == 2.5
    assert SizeQualifier(panel.size_qualifier.currentData()) is SizeQualifier.EXACT
    assert panel.save_button.isEnabled()
    assert "Access denied" in panel.folder_scan_status_text

    panel.calculate_folder_size_button.click()
    cancelled_request = panel.active_folder_scan_id
    assert cancelled_request is not None
    panel.cancel_folder_scan_button.click()

    assert not panel.is_folder_scan_active
    assert panel.size_value.value() == 2.5
    assert panel.save_button.isEnabled()
    assert "cancelled" in panel.folder_scan_status_text.casefold()


def test_artist_and_typed_path_changes_invalidate_stale_results(
    qtbot, tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    controller = FakeFolderScanController()
    panel = ArtistDetailPanel(tmp_path, folder_scan_controller=controller)
    qtbot.addWidget(panel)
    stale_candidate = SizeMetadataCandidate(
        size_value=Decimal("99"),
        size_unit="GB",
        size_qualifier=SizeQualifier.EXACT,
        heavy_status=HeavyStatus.YES,
        source_bytes=99_000_000_000,
    )
    monkeypatch.setattr(
        "collection_manager.ui.widgets.size_candidate_from_scan", lambda _scan: stale_candidate
    )

    panel.set_artist(_artist(1))
    panel.folder_edit.setText(str(tmp_path / "one"))
    panel.calculate_folder_size_button.click()
    artist_one_request = panel.active_folder_scan_id
    assert artist_one_request is not None

    panel.set_artist(
        _artist(
            2,
            size_value=Decimal("1.5"),
            size_unit="GB",
            size_qualifier=SizeQualifier.EXACT,
        )
    )
    controller.completed.emit(artist_one_request, SimpleNamespace(status=ScanStatus.COMPLETE))
    assert panel.size_value.value() == 1.5

    panel.folder_edit.setText(str(tmp_path / "two"))
    panel.calculate_folder_size_button.click()
    path_request = panel.active_folder_scan_id
    assert path_request is not None
    panel.folder_edit.setText(str(tmp_path / "changed"))
    panel.folder_edit.textEdited.emit(panel.folder_edit.text())
    controller.completed.emit(path_request, SimpleNamespace(status=ScanStatus.COMPLETE))

    assert not panel.is_folder_scan_active
    assert panel.size_value.value() == 1.5
    assert "Calculate size" in panel.folder_scan_status_text
    assert panel.shutdown_folder_scans(123)
    assert controller.shutdown_calls == [123]


def test_main_window_close_shuts_down_folder_scans(qtbot, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    database = Database(tmp_path / "collection-manager.sqlite3")
    database.initialize()
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = MainWindow(database, tmp_path, settings)
    qtbot.addWidget(window)
    shutdown_calls: list[bool] = []
    monkeypatch.setattr(
        window.detail,
        "shutdown_folder_scans",
        lambda: shutdown_calls.append(True) or True,
    )

    window.close()

    assert shutdown_calls == [True]


def test_calculated_size_persists_only_after_normal_save(qtbot, tmp_path: Path) -> None:  # noqa: ANN001
    database = Database(tmp_path / "collection-manager.sqlite3")
    database.initialize()
    with database.session() as session:
        artist_id = ArtistRepository(session).create("Scanned Artist").id
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = MainWindow(database, tmp_path, settings)
    qtbot.addWidget(window)
    assert window._select_artist(artist_id)
    selected = tmp_path / "artist-files"
    selected.mkdir()
    window.detail.folder_edit.setText(str(selected))

    with qtbot.waitSignal(window.detail.folder_scan_completed, timeout=3000):
        window.detail.calculate_folder_size_button.click()

    with database.session() as session:
        before_save = ArtistRepository(session).get(artist_id)
        assert before_save.size_value is None
        assert before_save.folder_path is None

    window.detail.save_button.click()

    with database.session() as session:
        saved = ArtistRepository(session).get(artist_id)
        assert saved.size_value == Decimal(0)
        assert saved.size_unit == "B"
        assert saved.size_qualifier == SizeQualifier.EXACT.value
        assert saved.folder_path == str(selected)
