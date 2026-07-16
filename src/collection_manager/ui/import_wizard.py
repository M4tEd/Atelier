from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation

from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from collection_manager.constants import CollectionKind, HeavyStatus, SizeQualifier, Tier
from collection_manager.domain import ArtistResolution, DuplicateGroup, ImportPreview, ParsedArtist
from collection_manager.ui.tag_text import format_tag_text, parse_tag_text
from collection_manager.ui.widgets import OptionalDateField


class DuplicateResolutionDialog(QDialog):
    """Guided, explicit resolution for one possible duplicate group."""

    def __init__(
        self,
        group: DuplicateGroup,
        artists: list[ParsedArtist],
        parent=None,  # noqa: ANN001
    ):
        super().__init__(parent)
        self.group = group
        self.members = [artists[index] for index in group.member_indexes]
        self.setWindowTitle("Resolve possible duplicate")
        self.resize(920, 680)

        layout = QVBoxLayout(self)
        certainty = "Definite duplicate" if group.definite else "Possible duplicate"
        heading = QLabel(f"<b>{certainty}:</b> {'; '.join(group.reasons)}")
        heading.setWordWrap(True)
        layout.addWidget(heading)

        self.source_table = QTableWidget(len(self.members), 5)
        self.source_table.setHorizontalHeaderLabels(
            ("Line", "Source name", "Tier", "Points", "Raw input")
        )
        self.source_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.source_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        for row, artist in enumerate(self.members):
            values = (
                str(artist.source_line),
                artist.name,
                artist.tier.value,
                f"{artist.points:+d}",
                artist.raw_line,
            )
            for column, value in enumerate(values):
                self.source_table.setItem(row, column, QTableWidgetItem(value))
        header = self.source_table.horizontalHeader()
        for column in range(4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.source_table.setMaximumHeight(210)
        layout.addWidget(self.source_table)

        self.different_artists = QCheckBox("These are different artists; keep every source line")
        self.different_artists.toggled.connect(self._different_toggled)
        layout.addWidget(self.different_artists)

        form = QFormLayout()
        self.authority_combo = QComboBox()
        for row, artist in enumerate(self.members):
            self.authority_combo.addItem(f"Line {artist.source_line}: {artist.name}", row)
        self.authority_combo.currentIndexChanged.connect(self._load_authority)
        self.name_edit = QLineEdit()
        self.tier_combo = QComboBox()
        for tier in reversed(tuple(Tier)):
            self.tier_combo.addItem(tier.value, tier)
        self.points_spin = QSpinBox()
        self.points_spin.setRange(-99, 99)
        self.updated_edit = OptionalDateField("Known")
        self.size_edit = QLineEdit()
        self.size_edit.setPlaceholderText("Numeric value, or blank")
        self.size_unit = QComboBox()
        self.size_unit.addItems(("B", "KB", "MB", "GB", "TB"))
        self.size_unit.setCurrentText("GB")
        self.size_qualifier = QComboBox()
        self.size_qualifier.addItem("Not specified", None)
        for qualifier in SizeQualifier:
            self.size_qualifier.addItem(qualifier.value.replace("_", " ").title(), qualifier)
        self.heavy_combo = QComboBox()
        self.heavy_combo.addItem("No", HeavyStatus.NO)
        self.heavy_combo.addItem("Yes", HeavyStatus.YES)
        self.heavy_combo.addItem("Needs review", HeavyStatus.UNKNOWN)
        self.compressed = QCheckBox("Compressed")
        self.tags_edit = QLineEdit()
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setMaximumHeight(100)
        self.url_edit = QLineEdit()
        form.addRow("Authoritative source", self.authority_combo)
        form.addRow("Canonical name", self.name_edit)
        form.addRow("Tier", self.tier_combo)
        form.addRow("Points", self.points_spin)
        form.addRow("Newest update", self.updated_edit)
        form.addRow("Size", self.size_edit)
        form.addRow("Size unit", self.size_unit)
        form.addRow("Size qualifier", self.size_qualifier)
        form.addRow("Heavy", self.heavy_combo)
        form.addRow("Compression", self.compressed)
        form.addRow("Combined tags", self.tags_edit)
        form.addRow("Combined notes", self.notes_edit)
        form.addRow("Reference URL", self.url_edit)
        layout.addLayout(form)

        note = QLabel(
            "Review every field. Variant source lines will be retained only in the import report."
        )
        note.setWordWrap(True)
        note.setProperty("muted", True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._load_authority(0)

    def _load_authority(self, row: int) -> None:
        if not 0 <= row < len(self.members):
            return
        source = self.members[row]
        self.name_edit.setText(source.name)
        self._set_combo(self.tier_combo, source.tier)
        self.points_spin.setValue(source.points)
        newest = max(
            (member.last_updated for member in self.members if member.last_updated), default=None
        )
        self.updated_edit.set_value(newest)
        self.size_edit.setText(str(source.size_value) if source.size_value is not None else "")
        self.size_unit.setCurrentText(source.size_unit or "GB")
        self._set_combo(self.size_qualifier, source.size_qualifier)
        self._set_combo(self.heavy_combo, source.heavy_status)
        self.compressed.setChecked(source.is_compressed)
        self.url_edit.setText(source.reference_url or "")

        tags: list[str] = []
        tag_keys: set[str] = set()
        for member in self.members:
            for tag in member.tags:
                key = tag.casefold()
                if key not in tag_keys:
                    tag_keys.add(key)
                    tags.append(tag)
        self.tags_edit.setText(format_tag_text(tags))
        notes: list[str] = []
        for member in self.members:
            clean = member.notes.strip()
            if clean and clean.casefold() not in {value.casefold() for value in notes}:
                notes.append(clean)
        self.notes_edit.setPlainText("\n".join(notes))

    @staticmethod
    def _set_combo(combo: QComboBox, data: object) -> None:
        index = combo.findData(data)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _different_toggled(self, checked: bool) -> None:
        for widget in (
            self.authority_combo,
            self.name_edit,
            self.tier_combo,
            self.points_spin,
            self.updated_edit,
            self.size_edit,
            self.size_unit,
            self.size_qualifier,
            self.heavy_combo,
            self.compressed,
            self.tags_edit,
            self.notes_edit,
            self.url_edit,
        ):
            widget.setEnabled(not checked)

    def _accept_if_valid(self) -> None:
        if not self.different_artists.isChecked() and not self.name_edit.text().strip():
            QMessageBox.warning(self, "Name required", "Choose a canonical artist name.")
            return
        try:
            if self.size_edit.text().strip():
                value = Decimal(self.size_edit.text().strip())
                if value <= 0:
                    raise InvalidOperation
        except InvalidOperation:
            QMessageBox.warning(self, "Invalid size", "Size must be a positive numeric value.")
            return
        try:
            parse_tag_text(self.tags_edit.text())
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid tags", str(exc))
            return
        if (
            not self.different_artists.isChecked()
            and HeavyStatus(self.heavy_combo.currentData()) is HeavyStatus.UNKNOWN
            and any(_requires_heavy_confirmation(member.warnings) for member in self.members)
        ):
            QMessageBox.warning(
                self,
                "Heavy status required",
                "Choose Yes or No for the ambiguous heavy-file warning before saving.",
            )
            return
        self.accept()

    def resolution(self) -> ArtistResolution:
        source = deepcopy(self.members[self.authority_combo.currentData() or 0])
        if self.different_artists.isChecked():
            return ArtistResolution(
                member_indexes=self.group.member_indexes,
                canonical=source,
                different_artists=True,
            )
        source.name = " ".join(self.name_edit.text().split())
        source.tier = Tier(self.tier_combo.currentData())
        source.points = self.points_spin.value()
        source.last_updated = self.updated_edit.value()
        source.size_value = (
            Decimal(self.size_edit.text().strip()) if self.size_edit.text().strip() else None
        )
        source.size_unit = self.size_unit.currentText() if source.size_value is not None else None
        qualifier = self.size_qualifier.currentData()
        source.size_qualifier = SizeQualifier(qualifier) if qualifier is not None else None
        source.heavy_status = HeavyStatus(self.heavy_combo.currentData())
        source.is_compressed = self.compressed.isChecked()
        source.tags = parse_tag_text(self.tags_edit.text())
        source.notes = self.notes_edit.toPlainText().strip()
        source.reference_url = self.url_edit.text().strip() or None
        return ArtistResolution(member_indexes=self.group.member_indexes, canonical=source)


class ArtistWarningReviewDialog(QDialog):
    """Edit and explicitly confirm a parsed artist that carries parser warnings."""

    def __init__(self, artist: ParsedArtist, parent=None):  # noqa: ANN001
        super().__init__(parent)
        self._source = deepcopy(artist)
        self.setWindowTitle(f"Review import warning — {artist.name}")
        self.resize(660, 610)
        layout = QVBoxLayout(self)
        warning_label = QLabel(
            "<b>Review required:</b><br>"
            + "<br>".join(f"• {warning}" for warning in artist.warnings)
        )
        warning_label.setWordWrap(True)
        layout.addWidget(warning_label)
        raw = QPlainTextEdit(artist.raw_line)
        raw.setReadOnly(True)
        raw.setMaximumHeight(70)
        layout.addWidget(raw)

        form = QFormLayout()
        self.name_edit = QLineEdit(artist.name)
        self.tier_combo = QComboBox()
        for tier in reversed(tuple(Tier)):
            self.tier_combo.addItem(tier.value, tier)
        _set_combo_data(self.tier_combo, artist.tier)
        self.points_spin = QSpinBox()
        self.points_spin.setRange(-99, 99)
        self.points_spin.setValue(artist.points)
        self.updated_edit = OptionalDateField("Known")
        self.updated_edit.set_value(artist.last_updated)
        self.size_edit = QLineEdit(str(artist.size_value) if artist.size_value is not None else "")
        self.size_unit = QComboBox()
        self.size_unit.addItems(("B", "KB", "MB", "GB", "TB"))
        self.size_unit.setCurrentText(artist.size_unit or "GB")
        self.size_qualifier = QComboBox()
        self.size_qualifier.addItem("Not specified", None)
        for qualifier in SizeQualifier:
            self.size_qualifier.addItem(qualifier.value.replace("_", " ").title(), qualifier)
        _set_combo_data(self.size_qualifier, artist.size_qualifier)
        self.heavy_combo = QComboBox()
        self.heavy_combo.addItem("No", HeavyStatus.NO)
        self.heavy_combo.addItem("Yes", HeavyStatus.YES)
        self.heavy_combo.addItem("Needs review", HeavyStatus.UNKNOWN)
        _set_combo_data(self.heavy_combo, artist.heavy_status)
        self.compressed = QCheckBox("Compressed")
        self.compressed.setChecked(artist.is_compressed)
        self.tags_edit = QLineEdit(format_tag_text(artist.tags))
        self.notes_edit = QPlainTextEdit(artist.notes)
        self.notes_edit.setMaximumHeight(90)
        self.url_edit = QLineEdit(artist.reference_url or "")
        form.addRow("Artist", self.name_edit)
        form.addRow("Tier", self.tier_combo)
        form.addRow("Points", self.points_spin)
        form.addRow("Last updated", self.updated_edit)
        form.addRow("Size", self.size_edit)
        form.addRow("Size unit", self.size_unit)
        form.addRow("Size qualifier", self.size_qualifier)
        form.addRow("Heavy", self.heavy_combo)
        form.addRow("Compression", self.compressed)
        form.addRow("Tags", self.tags_edit)
        form.addRow("Notes", self.notes_edit)
        form.addRow("Reference URL", self.url_edit)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Confirm reviewed values")
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept_if_valid(self) -> None:
        if not self.name_edit.text().strip():
            QMessageBox.warning(self, "Name required", "Enter a canonical artist name.")
            return
        try:
            if self.size_edit.text().strip():
                value = Decimal(self.size_edit.text().strip())
                if value <= 0:
                    raise InvalidOperation
        except InvalidOperation:
            QMessageBox.warning(self, "Invalid size", "Size must be a positive numeric value.")
            return
        try:
            parse_tag_text(self.tags_edit.text())
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid tags", str(exc))
            return
        if HeavyStatus(self.heavy_combo.currentData()) is HeavyStatus.UNKNOWN and (
            _requires_heavy_confirmation(self._source.warnings)
        ):
            QMessageBox.warning(
                self,
                "Heavy status required",
                "Choose Yes or No to explicitly resolve the ambiguous size/heavy warning.",
            )
            return
        self.accept()

    def artist(self) -> ParsedArtist:
        reviewed = deepcopy(self._source)
        reviewed.name = " ".join(self.name_edit.text().split())
        reviewed.tier = Tier(self.tier_combo.currentData())
        reviewed.points = self.points_spin.value()
        reviewed.last_updated = self.updated_edit.value()
        reviewed.size_value = (
            Decimal(self.size_edit.text().strip()) if self.size_edit.text().strip() else None
        )
        reviewed.size_unit = (
            self.size_unit.currentText() if reviewed.size_value is not None else None
        )
        qualifier = self.size_qualifier.currentData()
        reviewed.size_qualifier = SizeQualifier(qualifier) if qualifier is not None else None
        reviewed.heavy_status = HeavyStatus(self.heavy_combo.currentData())
        reviewed.is_compressed = self.compressed.isChecked()
        reviewed.tags = parse_tag_text(self.tags_edit.text())
        reviewed.notes = self.notes_edit.toPlainText().strip()
        reviewed.reference_url = self.url_edit.text().strip() or None
        return reviewed


class ImportWizard(QDialog):
    """Preview parsed input and require a decision for each duplicate candidate."""

    def __init__(
        self,
        preview: ImportPreview,
        parent=None,  # noqa: ANN001
        *,
        collection_kind: CollectionKind = CollectionKind.VIDEOS,
    ):
        super().__init__(parent)
        self.preview = preview
        self.collection_kind = CollectionKind(collection_kind)
        collection_label = self.collection_kind.value.replace("_", " ").title()
        self._resolutions: dict[tuple[int, ...], ArtistResolution] = {}
        grouped_indexes = {
            index for group in preview.duplicate_groups for index in group.member_indexes
        }
        self._warning_indexes = [
            index
            for index, artist in enumerate(preview.artists)
            if artist.warnings and index not in grouped_indexes
        ]
        self._confirmed_warning_indexes: set[int] = set()
        self.setWindowTitle(f"Import into {collection_label}")
        self.resize(980, 670)

        layout = QVBoxLayout(self)
        summary = QLabel(
            f"Destination: <b>{collection_label}</b> collection.<br>"
            f"Parsed <b>{len(preview.artists)}</b> artist lines from "
            f"<b>{preview.source_path.name}</b>. "
            f"Found <b>{len(preview.duplicate_groups)}</b> duplicate candidate groups."
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)
        tabs = QTabWidget()
        tabs.addTab(self._preview_tab(), "Parsed artists")
        tabs.addTab(self._duplicates_tab(), f"Duplicates ({len(preview.duplicate_groups)})")
        tabs.addTab(
            self._warning_review_tab(),
            f"Review warnings ({len(self._warning_indexes)})",
        )
        tabs.addTab(self._messages_tab(), "Messages")
        if preview.duplicate_groups:
            tabs.setCurrentIndex(1)
        elif self._warning_indexes:
            tabs.setCurrentIndex(2)
        layout.addWidget(tabs, 1)
        self.unresolved_label = QLabel()
        self.unresolved_label.setProperty("muted", True)
        layout.addWidget(self.unresolved_label)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.import_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.import_button.setText(f"Continue import into {collection_label}")
        buttons.accepted.connect(self._accept_if_resolved)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update_status()

    def _preview_tab(self) -> QWidget:
        table = QTableWidget(len(self.preview.artists), 6)
        table.setHorizontalHeaderLabels(("Line", "Artist", "Tier", "Points", "Updated", "Warnings"))
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.verticalHeader().hide()
        for row, artist in enumerate(self.preview.artists):
            values = (
                str(artist.source_line),
                artist.name,
                artist.tier.value,
                f"{artist.points:+d}",
                artist.last_updated.isoformat() if artist.last_updated else "—",
                "; ".join(artist.warnings),
            )
            for column, value in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(value))
        header = table.horizontalHeader()
        for column in range(5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        return table

    def _duplicates_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        guidance = QLabel(
            "Every group must be explicitly merged or marked as different artists. "
            "No source line is merged automatically."
        )
        guidance.setWordWrap(True)
        layout.addWidget(guidance)
        self.duplicate_table = QTableWidget(len(self.preview.duplicate_groups), 4)
        self.duplicate_table.setHorizontalHeaderLabels(
            ("Status", "Candidates", "Confidence", "Why")
        )
        self.duplicate_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.duplicate_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.duplicate_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.duplicate_table.verticalHeader().hide()
        for row, group in enumerate(self.preview.duplicate_groups):
            names = [self.preview.artists[index].name for index in group.member_indexes]
            self.duplicate_table.setItem(row, 0, QTableWidgetItem("Needs review"))
            self.duplicate_table.setItem(row, 1, QTableWidgetItem(" / ".join(names)))
            self.duplicate_table.setItem(
                row, 2, QTableWidgetItem("Definite" if group.definite else "Possible")
            )
            self.duplicate_table.setItem(row, 3, QTableWidgetItem("; ".join(group.reasons)))
        header = self.duplicate_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.duplicate_table.doubleClicked.connect(lambda _index: self._resolve_selected())
        layout.addWidget(self.duplicate_table, 1)
        resolve_button = QPushButton("Resolve selected group…")
        resolve_button.clicked.connect(self._resolve_selected)
        layout.addWidget(resolve_button)
        if self.preview.duplicate_groups:
            self.duplicate_table.selectRow(0)
        return page

    def _warning_review_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        guidance = QLabel(
            "Unique records with parser warnings must be opened, corrected if needed, and "
            "explicitly confirmed before import."
        )
        guidance.setWordWrap(True)
        layout.addWidget(guidance)
        self.warning_table = QTableWidget(len(self._warning_indexes), 4)
        self.warning_table.setHorizontalHeaderLabels(("Status", "Line", "Artist", "Warnings"))
        self.warning_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.warning_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.warning_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.warning_table.verticalHeader().hide()
        for row, artist_index in enumerate(self._warning_indexes):
            artist = self.preview.artists[artist_index]
            self.warning_table.setItem(row, 0, QTableWidgetItem("Needs confirmation"))
            self.warning_table.setItem(row, 1, QTableWidgetItem(str(artist.source_line)))
            self.warning_table.setItem(row, 2, QTableWidgetItem(artist.name))
            self.warning_table.setItem(row, 3, QTableWidgetItem("; ".join(artist.warnings)))
        header = self.warning_table.horizontalHeader()
        for column in range(3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.warning_table.doubleClicked.connect(lambda _index: self._review_warning())
        layout.addWidget(self.warning_table, 1)
        review_button = QPushButton("Review selected record…")
        review_button.clicked.connect(self._review_warning)
        layout.addWidget(review_button)
        if self._warning_indexes:
            self.warning_table.selectRow(0)
        return page

    def _messages_tab(self) -> QWidget:
        widget = QListWidget()
        for warning in self.preview.global_warnings:
            widget.addItem(warning)
        for line, raw, error in self.preview.unparseable:
            widget.addItem(f"Line {line}: {error} — {raw}")
        for artist in self.preview.artists:
            for warning in artist.warnings:
                widget.addItem(f"Line {artist.source_line} ({artist.name}): {warning}")
        if widget.count() == 0:
            widget.addItem("No parser warnings.")
        return widget

    def _review_warning(self) -> None:
        row = self.warning_table.currentRow()
        if row < 0:
            return
        artist_index = self._warning_indexes[row]
        dialog = ArtistWarningReviewDialog(self.preview.artists[artist_index], self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        reviewed = dialog.artist()
        self.preview.artists[artist_index] = reviewed
        self._confirmed_warning_indexes.add(artist_index)
        self.warning_table.item(row, 0).setText("Confirmed")
        self.warning_table.item(row, 2).setText(reviewed.name)
        self._update_status()

    def _resolve_selected(self) -> None:
        row = self.duplicate_table.currentRow()
        if row < 0:
            return
        group = self.preview.duplicate_groups[row]
        dialog = DuplicateResolutionDialog(group, self.preview.artists, self)
        existing = self._resolutions.get(group.member_indexes)
        if existing and existing.different_artists:
            dialog.different_artists.setChecked(True)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        resolution = dialog.resolution()
        self._resolutions[group.member_indexes] = resolution
        decision = (
            "Keep separate"
            if resolution.different_artists
            else f"Merge as {resolution.canonical.name}"
        )
        self.duplicate_table.item(row, 0).setText(decision)
        self._update_status()

    def _update_status(self) -> None:
        unresolved = len(self.preview.duplicate_groups) - len(self._resolutions)
        unconfirmed = len(self._warning_indexes) - len(self._confirmed_warning_indexes)
        messages: list[str] = []
        if unresolved:
            messages.append(
                f"{unresolved} duplicate group{'s' if unresolved != 1 else ''} need review"
            )
        if unconfirmed:
            messages.append(
                f"{unconfirmed} warning record{'s' if unconfirmed != 1 else ''} need confirmation"
            )
        self.unresolved_label.setText(
            "; ".join(messages) + "." if messages else "All required import decisions are complete."
        )
        self.import_button.setEnabled(
            unresolved == 0 and unconfirmed == 0 and not self.preview.unparseable
        )

    def _accept_if_resolved(self) -> None:
        if self.preview.unparseable:
            QMessageBox.warning(
                self,
                "Unparseable lines",
                "Import cannot continue until all source lines can be parsed.",
            )
            return
        if len(self._resolutions) != len(self.preview.duplicate_groups):
            QMessageBox.warning(
                self,
                "Duplicate review required",
                "Resolve every duplicate candidate group before importing.",
            )
            return
        if len(self._confirmed_warning_indexes) != len(self._warning_indexes):
            QMessageBox.warning(
                self,
                "Warning review required",
                "Open and confirm every unique record with parser warnings before importing.",
            )
            return
        self.accept()

    def resolutions(self) -> list[ArtistResolution]:
        return list(self._resolutions.values())


class ConflictResolutionDialog(QDialog):
    def __init__(
        self,
        conflicts: list[object],
        parent=None,  # noqa: ANN001
        *,
        current_records: dict[int, object] | None = None,
    ):
        super().__init__(parent)
        self.conflicts = conflicts
        self.current_records = current_records or {}
        self.setWindowTitle("Resolve re-import changes")
        self.resize(1050, 500)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "These artists already exist with different data. Choose the complete record to keep; "
            "applying the imported record replaces its exportable metadata while preserving "
            "folder paths and app-only dates."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.table = QTableWidget(len(conflicts), 5)
        self.table.setHorizontalHeaderLabels(
            ("Artist", "Changed fields", "Database value", "Imported value", "Decision")
        )
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        for row, conflict in enumerate(conflicts):
            name = _attr(
                conflict,
                "artist_name",
                "name",
                "canonical_name",
                default=f"Record {row + 1}",
            )
            fields = _attr(conflict, "changed_fields", "differences", default=())
            field_names = list(fields) if isinstance(fields, (dict, tuple, list, set)) else []
            fields_text = "\n".join(map(str, field_names)) or str(fields or "Metadata")
            current = self.current_records.get(int(_attr(conflict, "artist_id", default=-1)))
            imported = _attr(conflict, "imported")
            database_values = "\n".join(
                _display_field_value(current, field) for field in field_names
            )
            imported_values = "\n".join(
                _display_field_value(imported, field) for field in field_names
            )
            self.table.setItem(row, 0, QTableWidgetItem(str(name)))
            self.table.setItem(row, 1, QTableWidgetItem(fields_text))
            self.table.setItem(row, 2, QTableWidgetItem(database_values or "—"))
            self.table.setItem(row, 3, QTableWidgetItem(imported_values or "—"))
            decision = QComboBox()
            decision.addItem("Keep database record", "keep_database")
            decision.addItem("Apply imported record", "apply_import")
            self.table.setCellWidget(row, 4, decision)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.resizeRowsToContents()
        layout.addWidget(self.table, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def decisions(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for row, conflict in enumerate(self.conflicts):
            key = _attr(conflict, "name_key", "key", "artist_name", "name", default=str(row))
            combo = self.table.cellWidget(row, 4)
            if isinstance(combo, QComboBox):
                result[str(key)] = str(combo.currentData())
        return result


def _attr(value: object, *names: str, default=None):  # noqa: ANN001, ANN202
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _display_field_value(record: object | None, field: object) -> str:
    if record is None:
        return "—"
    value = getattr(record, str(field), None)
    if str(field) == "tags":
        return format_tag_text(value or ()) or "—"
    if hasattr(value, "value"):
        value = value.value
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _set_combo_data(combo: QComboBox, value: object) -> None:
    index = combo.findData(value)
    combo.setCurrentIndex(index if index >= 0 else 0)


def _requires_heavy_confirmation(warnings: list[str]) -> bool:
    return any(
        "heavy" in warning.casefold() or "size" in warning.casefold() for warning in warnings
    )
