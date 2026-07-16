from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from collection_manager.constants import Tier
from collection_manager.domain import RuleSuggestion
from collection_manager.ui.tag_text import parse_tag_text


class AddArtistDialog(QDialog):
    def __init__(self, parent=None):  # noqa: ANN001
        super().__init__(parent)
        self.setWindowTitle("Add artist")
        self.setMinimumWidth(390)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Canonical artist name")
        self.tier_combo = QComboBox()
        for tier in reversed(tuple(Tier)):
            self.tier_combo.addItem(tier.value, tier)
        default_index = self.tier_combo.findData(Tier.WORTH_REVISITING)
        self.tier_combo.setCurrentIndex(default_index)
        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText('Optional; quote commas: "portrait, advanced"')
        form.addRow("Name", self.name_edit)
        form.addRow("Starting tier", self.tier_combo)
        form.addRow("Tags", self.tags_edit)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.name_edit.setFocus()

    def _accept_if_valid(self) -> None:
        if not self.name_edit.text().strip():
            QMessageBox.warning(self, "Name required", "Enter the artist's canonical name.")
            return
        try:
            parse_tag_text(self.tags_edit.text())
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid tags", str(exc))
            return
        self.accept()

    def values(self) -> dict[str, object]:
        return {
            "name": self.name_edit.text(),
            "tier": self.tier_combo.currentData(),
            "tags": parse_tag_text(self.tags_edit.text()),
            "date_added": date.today(),
        }


class LogUpdateDialog(QDialog):
    def __init__(self, artist_name: str, parent=None):  # noqa: ANN001
        super().__init__(parent)
        self.setWindowTitle(f"Log update — {artist_name}")
        self.setMinimumWidth(430)
        layout = QVBoxLayout(self)
        explanation = QLabel(
            "Record the update date and your judgment. A good update is +1; a bad update is -1."
        )
        explanation.setWordWrap(True)
        explanation.setProperty("muted", True)
        layout.addWidget(explanation)
        form = QFormLayout()
        self.date_edit = QDateEdit(QDate.currentDate())
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.sentiment_combo = QComboBox()
        self.sentiment_combo.addItem("Good update (+1)", "good")
        self.sentiment_combo.addItem("Bad update (−1)", "bad")
        self.reason_edit = QPlainTextEdit()
        self.reason_edit.setPlaceholderText("What changed? (optional)")
        self.reason_edit.setMaximumHeight(100)
        form.addRow("Update date", self.date_edit)
        form.addRow("Judgment", self.sentiment_combo)
        form.addRow("Reason", self.reason_edit)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple[date, str, str]:
        return (
            self.date_edit.date().toPython(),
            str(self.sentiment_combo.currentData()),
            self.reason_edit.toPlainText().strip(),
        )


class PointAdjustmentDialog(QDialog):
    def __init__(self, artist_name: str, parent=None):  # noqa: ANN001
        super().__init__(parent)
        self.setWindowTitle(f"Adjust points — {artist_name}")
        self.setMinimumWidth(390)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.delta_spin = QSpinBox()
        self.delta_spin.setRange(-99, 99)
        self.delta_spin.setValue(1)
        self.delta_spin.setPrefix("+")
        self.delta_spin.valueChanged.connect(
            lambda value: self.delta_spin.setPrefix("+" if value > 0 else "")
        )
        self.reason_edit = QLineEdit()
        self.reason_edit.setPlaceholderText("Required audit reason")
        form.addRow("Adjustment", self.delta_spin)
        form.addRow("Reason", self.reason_edit)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept_if_valid(self) -> None:
        if self.delta_spin.value() == 0:
            QMessageBox.warning(self, "Adjustment required", "Choose a non-zero adjustment.")
        elif not self.reason_edit.text().strip():
            QMessageBox.warning(self, "Reason required", "Enter an audit reason.")
        else:
            self.accept()

    def values(self) -> tuple[int, str]:
        return self.delta_spin.value(), self.reason_edit.text().strip()


class ManualTierDialog(QDialog):
    def __init__(self, artist_name: str, old_tier: Tier, new_tier: Tier, parent=None):  # noqa: ANN001
        super().__init__(parent)
        self.setWindowTitle("Confirm manual tier override")
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        notice = QLabel(
            f"Move <b>{artist_name}</b> from <b>{old_tier.value}</b> to "
            f"<b>{new_tier.value}</b>? Points will reset to zero and both changes will be logged."
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        form = QFormLayout()
        self.reason_edit = QLineEdit()
        self.reason_edit.setPlaceholderText("Required audit reason")
        form.addRow("Reason", self.reason_edit)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept_if_valid(self) -> None:
        if not self.reason_edit.text().strip():
            QMessageBox.warning(self, "Reason required", "Enter a reason for changing tier.")
            return
        self.accept()

    @property
    def reason(self) -> str:
        return self.reason_edit.text().strip()


class RuleSuggestionsDialog(QDialog):
    def __init__(self, suggestions: Iterable[RuleSuggestion], parent=None):  # noqa: ANN001
        super().__init__(parent)
        self.setWindowTitle("Review rating suggestions")
        self.resize(850, 460)
        self._suggestions = list(suggestions)
        layout = QVBoxLayout(self)
        explanation = QLabel(
            "Nothing changes until you approve it. Select the adjustments to apply as one batch."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        selection_row = QHBoxLayout()
        select_all = QCheckBox("Select all")
        select_all.setChecked(True)
        select_all.toggled.connect(self._set_all_checked)
        selection_row.addWidget(select_all)
        selection_row.addStretch()
        layout.addLayout(selection_row)

        self.table = QTableWidget(len(self._suggestions), 5)
        self.table.setHorizontalHeaderLabels(("Apply", "Artist", "Rule", "Change", "Reason"))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        for row, suggestion in enumerate(self._suggestions):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(row, 0, check)
            self.table.setItem(row, 1, QTableWidgetItem(suggestion.artist_name))
            rule_text = suggestion.rule_kind.value.replace("_", " ").title()
            if suggestion.is_reversal:
                rule_text += " reversal"
            self.table.setItem(row, 2, QTableWidgetItem(rule_text))
            self.table.setItem(row, 3, QTableWidgetItem(f"{suggestion.delta:+d}"))
            self.table.setItem(row, 4, QTableWidgetItem(suggestion.reason))
        header = self.table.horizontalHeader()
        for column in range(4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _set_all_checked(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(self.table.rowCount()):
            self.table.item(row, 0).setCheckState(state)

    def selected_suggestions(self) -> list[RuleSuggestion]:
        return [
            suggestion
            for row, suggestion in enumerate(self._suggestions)
            if self.table.item(row, 0).checkState() is Qt.CheckState.Checked
        ]
