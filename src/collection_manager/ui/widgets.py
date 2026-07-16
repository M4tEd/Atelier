from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from PySide6.QtCore import QDate, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from collection_manager.constants import TIER_COLORS, HeavyStatus, SizeQualifier, Tier
from collection_manager.folder_scanner import ScanStatus, size_candidate_from_scan
from collection_manager.ui.data import ArtistView
from collection_manager.ui.folder_scan import FolderScanController
from collection_manager.ui.styles import tier_badge_style
from collection_manager.ui.tag_text import format_tag_text, parse_tag_text


def _to_qdate(value: date) -> QDate:
    return QDate(value.year, value.month, value.day)


class OptionalDateField(QWidget):
    changed = Signal()

    def __init__(self, label: str, parent=None):  # noqa: ANN001
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.known = QCheckBox(label)
        self.editor = QDateEdit()
        self.editor.setCalendarPopup(True)
        self.editor.setDisplayFormat("yyyy-MM-dd")
        self.editor.setDate(QDate.currentDate())
        self.editor.setEnabled(False)
        self.known.toggled.connect(self.editor.setEnabled)
        self.known.toggled.connect(self.changed)
        self.editor.dateChanged.connect(self.changed)
        layout.addWidget(self.known)
        layout.addWidget(self.editor, 1)

    def set_value(self, value: date | None) -> None:
        self.known.blockSignals(True)
        self.editor.blockSignals(True)
        self.known.setChecked(value is not None)
        self.editor.setEnabled(value is not None)
        self.editor.setDate(_to_qdate(value) if value else QDate.currentDate())
        self.known.blockSignals(False)
        self.editor.blockSignals(False)

    def value(self) -> date | None:
        if not self.known.isChecked():
            return None
        return self.editor.date().toPython()


class ArtistDetailPanel(QFrame):
    save_requested = Signal(object)
    log_update_requested = Signal()
    point_adjustment_requested = Signal()
    trash_requested = Signal()
    restore_requested = Signal()
    permanent_delete_requested = Signal()
    resolve_shift_requested = Signal()
    folder_scan_started = Signal(int, str)
    folder_scan_progress = Signal(int, object)
    folder_scan_completed = Signal(int, object)
    folder_scan_failed = Signal(int, str)
    folder_scan_cancelled = Signal(int)

    def __init__(
        self,
        library_dir: Path,
        parent=None,  # noqa: ANN001
        *,
        folder_scan_controller: FolderScanController | None = None,
    ):
        super().__init__(parent)
        self.library_dir = library_dir
        self._artist: ArtistView | None = None
        self._loading = False
        self.folder_scan_controller = folder_scan_controller or FolderScanController(self)
        self._active_folder_scan: tuple[int, int, str] | None = None
        self.folder_scan_controller.progress.connect(self._folder_scan_progressed)
        self.folder_scan_controller.completed.connect(self._folder_scan_succeeded)
        self.folder_scan_controller.failed.connect(self._folder_scan_failed)
        self.folder_scan_controller.cancelled.connect(self._folder_scan_was_cancelled)
        self.setMinimumWidth(355)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 0, 0, 0)

        header = QHBoxLayout()
        self.name_heading = QLabel("Select an artist")
        heading_font = self.name_heading.font()
        heading_font.setPointSize(15)
        heading_font.setBold(True)
        self.name_heading.setFont(heading_font)
        self.tier_badge = QLabel("")
        self.tier_badge.hide()
        header.addWidget(self.name_heading, 1)
        header.addWidget(self.tier_badge)
        root.addLayout(header)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self.tabs.addTab(self._build_details_tab(), "Details")
        self.tabs.addTab(self._build_history_tab(), "History")
        self.set_artist(None)

    def _build_details_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)

        identity = QGroupBox("Artist")
        identity_form = QFormLayout(identity)
        self.name_edit = QLineEdit()
        self.tier_combo = QComboBox()
        for tier in reversed(tuple(Tier)):
            self.tier_combo.addItem(tier.value, tier)
        self.points_label = QLabel("0")
        identity_form.addRow("Name", self.name_edit)
        identity_form.addRow("Tier", self.tier_combo)
        identity_form.addRow("Points", self.points_label)
        layout.addWidget(identity)

        dates = QGroupBox("Dates")
        dates_form = QFormLayout(dates)
        self.last_updated_edit = OptionalDateField("Known")
        self.evaluated_edit = OptionalDateField("Known")
        self.added_edit = OptionalDateField("Known")
        dates_form.addRow("Last updated", self.last_updated_edit)
        dates_form.addRow("Evaluated", self.evaluated_edit)
        dates_form.addRow("Added", self.added_edit)
        layout.addWidget(dates)

        storage = QGroupBox("Storage")
        storage_grid = QGridLayout(storage)
        self.size_known = QCheckBox("Size known")
        self.size_value = QDoubleSpinBox()
        self.size_value.setRange(0.0, 999999.0)
        self.size_value.setDecimals(3)
        self.size_value.setEnabled(False)
        self.size_known.toggled.connect(self.size_value.setEnabled)
        self.size_unit = QComboBox()
        self.size_unit.addItems(("B", "KB", "MB", "GB", "TB"))
        self.size_unit.setCurrentText("GB")
        self.size_known.toggled.connect(self.size_unit.setEnabled)
        self.size_qualifier = QComboBox()
        for qualifier in SizeQualifier:
            self.size_qualifier.addItem(qualifier.value.replace("_", " ").title(), qualifier)
        self.heavy_status = QComboBox()
        self.heavy_status.addItem("No", HeavyStatus.NO)
        self.heavy_status.addItem("Yes", HeavyStatus.YES)
        self.heavy_status.addItem("Needs review", HeavyStatus.UNKNOWN)
        self.compressed = QCheckBox("Compressed")
        storage_grid.addWidget(self.size_known, 0, 0)
        storage_grid.addWidget(self.size_value, 0, 1)
        storage_grid.addWidget(self.size_unit, 0, 2)
        storage_grid.addWidget(QLabel("Qualifier"), 1, 0)
        storage_grid.addWidget(self.size_qualifier, 1, 1)
        storage_grid.addWidget(QLabel("Heavy"), 2, 0)
        storage_grid.addWidget(self.heavy_status, 2, 1)
        storage_grid.addWidget(self.compressed, 3, 0, 1, 2)
        layout.addWidget(storage)

        metadata = QGroupBox("Metadata")
        metadata_form = QFormLayout(metadata)
        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText('portrait, "anatomy, advanced", sculpting')
        self.tags_edit.setToolTip("Use JSON quotes around a tag that contains a comma.")
        self.notes_edit = QTextEdit()
        self.notes_edit.setAcceptRichText(False)
        self.notes_edit.setMinimumHeight(90)
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://…")
        url_row = QWidget()
        url_layout = QHBoxLayout(url_row)
        url_layout.setContentsMargins(0, 0, 0, 0)
        url_layout.addWidget(self.url_edit, 1)
        self.open_url_button = QPushButton("Open")
        self.open_url_button.clicked.connect(self._open_url)
        url_layout.addWidget(self.open_url_button)
        metadata_form.addRow("Tags", self.tags_edit)
        metadata_form.addRow("Notes", self.notes_edit)
        metadata_form.addRow("Reference", url_row)
        layout.addWidget(metadata)

        folder = QGroupBox("Local folder")
        folder_layout = QVBoxLayout(folder)
        folder_path_row = QHBoxLayout()
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("Optional folder path")
        self.folder_edit.textEdited.connect(self._folder_path_edited)
        self.browse_folder_button = QPushButton("Browse")
        self.browse_folder_button.clicked.connect(self._browse_folder)
        self.open_folder_button = QPushButton("Open")
        self.open_folder_button.clicked.connect(self._open_folder)
        folder_path_row.addWidget(self.folder_edit, 1)
        folder_path_row.addWidget(self.browse_folder_button)
        folder_path_row.addWidget(self.open_folder_button)
        folder_layout.addLayout(folder_path_row)

        folder_scan_row = QHBoxLayout()
        self.calculate_folder_size_button = QPushButton("Calculate size")
        self.calculate_folder_size_button.clicked.connect(self._calculate_folder_size)
        self.cancel_folder_scan_button = QPushButton("Cancel")
        self.cancel_folder_scan_button.clicked.connect(self._cancel_folder_scan)
        self.cancel_folder_scan_button.hide()
        self.folder_scan_progress_bar = QProgressBar()
        self.folder_scan_progress_bar.setRange(0, 0)
        self.folder_scan_progress_bar.setTextVisible(False)
        self.folder_scan_progress_bar.setMaximumWidth(90)
        self.folder_scan_progress_bar.hide()
        folder_scan_row.addWidget(self.calculate_folder_size_button)
        folder_scan_row.addWidget(self.cancel_folder_scan_button)
        folder_scan_row.addWidget(self.folder_scan_progress_bar)
        folder_scan_row.addStretch()
        folder_layout.addLayout(folder_scan_row)
        self.folder_scan_status_label = QLabel("")
        self.folder_scan_status_label.setObjectName("folder-scan-status")
        self.folder_scan_status_label.setProperty("muted", True)
        self.folder_scan_status_label.setWordWrap(True)
        folder_layout.addWidget(self.folder_scan_status_label)
        layout.addWidget(folder)

        actions = QGridLayout()
        self.save_button = QPushButton("Save changes")
        self.save_button.setProperty("accent", True)
        self.save_button.clicked.connect(self._emit_save)
        self.log_update_button = QPushButton("Log update…")
        self.log_update_button.clicked.connect(self.log_update_requested)
        self.adjust_points_button = QPushButton("Adjust points…")
        self.adjust_points_button.clicked.connect(self.point_adjustment_requested)
        self.resolve_shift_button = QPushButton("Resolve tier shift…")
        self.resolve_shift_button.clicked.connect(self.resolve_shift_requested)
        self.trash_button = QPushButton("Move to Trash")
        self.trash_button.setProperty("danger", True)
        self.trash_button.clicked.connect(self.trash_requested)
        self.restore_button = QPushButton("Restore")
        self.restore_button.clicked.connect(self.restore_requested)
        self.delete_button = QPushButton("Delete permanently")
        self.delete_button.setProperty("danger", True)
        self.delete_button.clicked.connect(self.permanent_delete_requested)
        actions.addWidget(self.save_button, 0, 0, 1, 2)
        actions.addWidget(self.log_update_button, 1, 0)
        actions.addWidget(self.adjust_points_button, 1, 1)
        actions.addWidget(self.resolve_shift_button, 2, 0, 1, 2)
        actions.addWidget(self.trash_button, 3, 0, 1, 2)
        actions.addWidget(self.restore_button, 4, 0)
        actions.addWidget(self.delete_button, 4, 1)
        layout.addLayout(actions)
        layout.addStretch()

        self._editable_widgets = [
            self.name_edit,
            self.tier_combo,
            self.last_updated_edit,
            self.evaluated_edit,
            self.added_edit,
            self.size_known,
            self.size_value,
            self.size_unit,
            self.size_qualifier,
            self.heavy_status,
            self.compressed,
            self.tags_edit,
            self.notes_edit,
            self.url_edit,
            self.folder_edit,
            self.browse_folder_button,
            self.calculate_folder_size_button,
            self.cancel_folder_scan_button,
            self.open_folder_button,
            self.open_url_button,
            self.save_button,
            self.log_update_button,
            self.adjust_points_button,
            self.resolve_shift_button,
            self.trash_button,
            self.restore_button,
            self.delete_button,
        ]
        return scroll

    def _build_history_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        helper = QLabel("Every point and tier mutation is recorded here.")
        helper.setProperty("muted", True)
        helper.setWordWrap(True)
        self.history_tree = QTreeWidget()
        self.history_tree.setHeaderLabels(("When", "Type", "Change", "Reason"))
        self.history_tree.setRootIsDecorated(False)
        self.history_tree.setAlternatingRowColors(True)
        self.history_tree.header().setStretchLastSection(True)
        layout.addWidget(helper)
        layout.addWidget(self.history_tree, 1)
        return page

    @property
    def artist(self) -> ArtistView | None:
        return self._artist

    @property
    def active_folder_scan_id(self) -> int | None:
        return self._active_folder_scan[0] if self._active_folder_scan else None

    @property
    def is_folder_scan_active(self) -> bool:
        return self._active_folder_scan is not None

    @property
    def folder_scan_status_text(self) -> str:
        return self.folder_scan_status_label.text()

    def set_artist(self, artist: ArtistView | None) -> None:
        self._invalidate_folder_scan(clear_status=True)
        self._loading = True
        self._artist = artist
        enabled = artist is not None
        for widget in self._editable_widgets:
            widget.setEnabled(enabled)
        self.history_tree.clear()
        self.tier_badge.setVisible(enabled)
        if artist is None:
            self.name_heading.setText("Select an artist")
            self.name_edit.clear()
            self.points_label.setText("—")
            self.restore_button.hide()
            self.delete_button.hide()
            self.trash_button.show()
            self._set_folder_scan_busy(False)
            self._loading = False
            return

        self.name_heading.setText(artist.name)
        self.name_edit.setText(artist.name)
        self._set_combo_data(self.tier_combo, artist.tier)
        self.points_label.setText(f"{artist.points:+d}" if artist.points else "0")
        self.last_updated_edit.set_value(artist.last_updated)
        self.evaluated_edit.set_value(artist.date_evaluated)
        self.added_edit.set_value(artist.date_added)
        self.size_known.setChecked(artist.size_value is not None)
        self.size_value.setEnabled(artist.size_value is not None)
        self.size_value.setValue(float(artist.size_value) if artist.size_value is not None else 0.0)
        self.size_unit.setEnabled(artist.size_value is not None)
        self.size_unit.setCurrentText(artist.size_unit or "GB")
        self._set_combo_data(self.size_qualifier, artist.size_qualifier or SizeQualifier.EXACT)
        self._set_combo_data(self.heavy_status, artist.heavy_status)
        self.compressed.setChecked(artist.is_compressed)
        self.tags_edit.setText(format_tag_text(artist.tags))
        self.notes_edit.setPlainText(artist.notes)
        self.url_edit.setText(artist.reference_url or "")
        self.folder_edit.setText(artist.folder_path or "")
        is_trashed = artist.deleted_at is not None
        for widget in (
            self.save_button,
            self.log_update_button,
            self.adjust_points_button,
            self.resolve_shift_button,
            self.trash_button,
        ):
            widget.setVisible(not is_trashed)
        self.resolve_shift_button.setVisible(not is_trashed and artist.needs_attention)
        self.restore_button.setVisible(is_trashed)
        self.delete_button.setVisible(is_trashed)
        self.calculate_folder_size_button.setEnabled(not is_trashed)
        self.browse_folder_button.setEnabled(not is_trashed)
        self._set_folder_scan_busy(False)
        self.tier_badge.setText(artist.tier.value)
        self.tier_badge.setStyleSheet(tier_badge_style(TIER_COLORS[artist.tier]))
        for item in artist.history:
            row = QTreeWidgetItem(
                (
                    item.created_at.strftime("%Y-%m-%d %H:%M"),
                    item.category,
                    item.summary,
                    item.reason,
                )
            )
            row.setToolTip(3, item.reason)
            self.history_tree.addTopLevelItem(row)
        for column in range(3):
            self.history_tree.resizeColumnToContents(column)
        self._loading = False

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: object) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def values(self) -> dict[str, object]:
        return {
            "name": self.name_edit.text(),
            "tier": self.tier_combo.currentData(),
            "last_updated": self.last_updated_edit.value(),
            "date_evaluated": self.evaluated_edit.value(),
            "date_added": self.added_edit.value(),
            "size_value": self.size_value.value() if self.size_known.isChecked() else None,
            "size_unit": self.size_unit.currentText() if self.size_known.isChecked() else None,
            "size_qualifier": (
                self.size_qualifier.currentData() if self.size_known.isChecked() else None
            ),
            "heavy_status": self.heavy_status.currentData(),
            "is_compressed": self.compressed.isChecked(),
            "tags": parse_tag_text(self.tags_edit.text()),
            "notes": self.notes_edit.toPlainText(),
            "reference_url": self.url_edit.text(),
            "folder_path": self.folder_edit.text(),
        }

    def _emit_save(self) -> None:
        if self._artist is None:
            return
        try:
            values = self.values()
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid tags", str(exc))
            return
        self.save_requested.emit(values)

    def _browse_folder(self) -> None:
        initial = self.folder_edit.text().strip() or str(self.library_dir)
        selected = QFileDialog.getExistingDirectory(self, "Choose artist folder", initial)
        if selected:
            self.folder_edit.setText(selected)
            self._start_folder_scan(selected)

    def _calculate_folder_size(self) -> None:
        self._start_folder_scan(self.folder_edit.text())

    def _start_folder_scan(self, raw_path: str | Path) -> None:
        artist = self._artist
        path_text = str(raw_path).strip()
        if artist is None or artist.deleted_at is not None:
            return
        if not path_text:
            self.folder_scan_status_label.setText("Choose a folder before calculating its size.")
            return

        self._invalidate_folder_scan(clear_status=False)
        normalized_path = self._normalized_folder_path(path_text)
        try:
            request_id = self.folder_scan_controller.start(normalized_path, artist.id)
        except Exception as exc:
            self.folder_scan_status_label.setText(
                f"Folder size could not be calculated: {str(exc) or type(exc).__name__}"
            )
            self._set_folder_scan_busy(False)
            return
        self._active_folder_scan = (request_id, artist.id, normalized_path)
        self._set_folder_scan_busy(True)
        self.folder_scan_status_label.setText("Calculating folder size…")
        self.folder_scan_started.emit(request_id, normalized_path)

    def _folder_path_edited(self, text: str) -> None:
        if self._loading:
            return
        self._invalidate_folder_scan(clear_status=False)
        self.folder_scan_status_label.setText(
            "Click Calculate size to scan this folder." if text.strip() else ""
        )

    def _cancel_folder_scan(self) -> None:
        active = self._active_folder_scan
        if active is None:
            return
        self.folder_scan_controller.cancel_current()
        # A test double or a shutting-down controller may not emit cancellation itself.
        if self._active_folder_scan is not None:
            self._finish_folder_scan_cancelled(active[0])

    def _invalidate_folder_scan(self, *, clear_status: bool) -> None:
        active = self._active_folder_scan
        self._active_folder_scan = None
        if active is not None:
            self.folder_scan_controller.cancel_current()
        self._set_folder_scan_busy(False)
        if clear_status and hasattr(self, "folder_scan_status_label"):
            self.folder_scan_status_label.clear()

    def _folder_scan_progressed(self, request_id: int, payload: object) -> None:
        if not self._matches_folder_scan(request_id):
            return
        self.folder_scan_status_label.setText(_progress_text(payload))
        self.folder_scan_progress.emit(request_id, payload)

    def _folder_scan_succeeded(self, request_id: int, scan: object) -> None:
        if not self._matches_folder_scan(request_id):
            return
        try:
            if not _is_usable_scan(scan):
                raise ValueError("The folder scan did not produce a usable size")
            candidate = size_candidate_from_scan(scan)
        except Exception as exc:
            self._finish_folder_scan_failed(
                request_id,
                str(exc) or "The folder scan did not produce a usable size",
            )
            return

        self._active_folder_scan = None
        self._set_folder_scan_busy(False)
        self.size_known.setChecked(True)
        self.size_value.setValue(float(candidate.size_value))
        self.size_unit.setCurrentText(candidate.size_unit)
        self._set_combo_data(self.size_qualifier, candidate.size_qualifier)
        self._set_combo_data(self.heavy_status, candidate.heavy_status)
        qualifier = getattr(candidate.size_qualifier, "value", candidate.size_qualifier)
        size_text = f"{candidate.size_value} {candidate.size_unit}"
        if str(qualifier).casefold() == SizeQualifier.AT_LEAST.value:
            self.folder_scan_status_label.setText(
                f"Partial scan: at least {size_text}. Click Save changes to keep it."
            )
        else:
            self.folder_scan_status_label.setText(
                f"Calculated {size_text}. Click Save changes to keep it."
            )
        self.folder_scan_completed.emit(request_id, scan)

    def _folder_scan_failed(self, request_id: int, message: str) -> None:
        if self._matches_folder_scan(request_id):
            self._finish_folder_scan_failed(request_id, message)

    def _finish_folder_scan_failed(self, request_id: int, message: str) -> None:
        self._active_folder_scan = None
        self._set_folder_scan_busy(False)
        self.folder_scan_status_label.setText(f"Folder size could not be calculated: {message}")
        self.folder_scan_failed.emit(request_id, message)

    def _folder_scan_was_cancelled(self, request_id: int) -> None:
        if self._matches_folder_scan(request_id):
            self._finish_folder_scan_cancelled(request_id)

    def _finish_folder_scan_cancelled(self, request_id: int) -> None:
        self._active_folder_scan = None
        self._set_folder_scan_busy(False)
        self.folder_scan_status_label.setText("Folder size calculation cancelled.")
        self.folder_scan_cancelled.emit(request_id)

    def _matches_folder_scan(self, request_id: int) -> bool:
        active = self._active_folder_scan
        artist = self._artist
        if active is None or artist is None:
            return False
        expected_id, artist_id, expected_path = active
        return (
            request_id == expected_id
            and artist.id == artist_id
            and self._normalized_folder_path(self.folder_edit.text()) == expected_path
        )

    def _set_folder_scan_busy(self, busy: bool) -> None:
        self.folder_scan_progress_bar.setVisible(busy)
        self.cancel_folder_scan_button.setVisible(busy)
        artist_is_editable = self._artist is not None and self._artist.deleted_at is None
        self.calculate_folder_size_button.setEnabled(not busy and artist_is_editable)
        self.save_button.setEnabled(not busy and artist_is_editable)

    @staticmethod
    def _normalized_folder_path(value: str | Path) -> str:
        resolved = Path(str(value).strip()).expanduser().resolve(strict=False)
        return os.path.normcase(str(resolved))

    def shutdown_folder_scans(self, timeout_ms: int = 5000) -> bool:
        self._active_folder_scan = None
        self.folder_scan_controller.cancel_current()
        self._set_folder_scan_busy(False)
        return self.folder_scan_controller.shutdown(timeout_ms)

    def _open_folder(self) -> None:
        folder = self.folder_edit.text().strip()
        if folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _open_url(self) -> None:
        value = self.url_edit.text().strip()
        if value:
            QDesktopServices.openUrl(QUrl.fromUserInput(value))


def _scan_status_value(scan: object) -> str:
    status: ScanStatus | object = getattr(scan, "status", "")
    return str(getattr(status, "value", status)).casefold()


def _is_usable_scan(scan: object) -> bool:
    return _scan_status_value(scan) in {"complete", "completed", "partial", "success"}


def _progress_text(payload: object) -> str:
    files = None
    for attribute in ("file_count", "files_scanned", "files"):
        value = getattr(payload, attribute, None)
        if isinstance(value, int):
            files = value
            break
    if files is None and isinstance(payload, tuple) and payload:
        files = payload[0] if isinstance(payload[0], int) else None
    if files is None:
        return "Calculating folder size…"
    return f"Calculating folder size… {files:,} file{'s' if files != 1 else ''} scanned"
