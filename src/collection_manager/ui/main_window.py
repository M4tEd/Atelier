from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from PySide6.QtCore import QItemSelection, QSettings, QSize, Qt
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QTabBar,
    QTableView,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from collection_manager.constants import TIER_DESCENDING, CollectionKind, Tier
from collection_manager.database import Database
from collection_manager.rating_service import RatingService
from collection_manager.repository import ArtistRepository
from collection_manager.ui.data import ArtistStore, ArtistView
from collection_manager.ui.dialogs import (
    AddArtistDialog,
    LogUpdateDialog,
    ManualTierDialog,
    PointAdjustmentDialog,
    RuleSuggestionsDialog,
)
from collection_manager.ui.import_wizard import ConflictResolutionDialog, ImportWizard
from collection_manager.ui.table_model import (
    ARTIST_ROLE,
    ArtistFilterProxyModel,
    ArtistTableModel,
)
from collection_manager.ui.widgets import ArtistDetailPanel


class MainWindow(QMainWindow):
    def __init__(
        self,
        database: Database,
        library_dir: Path,
        settings: QSettings,
        parent=None,  # noqa: ANN001
    ):
        super().__init__(parent)
        self.database = database
        self.library_dir = Path(library_dir)
        self.settings = settings
        self.store = ArtistStore(database)
        self._rating_session = database.session_factory()
        self._rating_service = RatingService(self._rating_session)
        self._last_rating_action_depth = 0
        self._collection_kind = CollectionKind.VIDEOS
        self._navigation = "all"
        self._selected_artist_id: int | None = None
        self._nav_items: dict[str, QListWidgetItem] = {}

        self.setWindowTitle(f"Collection Manager — {self.library_dir.name}")
        self.setMinimumSize(1050, 680)
        self.resize(1450, 850)
        self._build_actions()
        self._build_toolbar()
        self._build_central_widget()
        self.navigation.setCurrentRow(0)
        self._update_collection_context()
        self._build_shortcuts()
        self.statusBar().showMessage(f"Library: {self.library_dir}")

        geometry = self.settings.value("window/geometry")
        if geometry:
            self.restoreGeometry(geometry)
        state = self.settings.value("window/state")
        if state:
            self.restoreState(state)
        self.reload()

    def _build_actions(self) -> None:
        self.add_action = QAction("Add", self)
        self.add_action.setToolTip("Add an artist (Ctrl+N)")
        self.add_action.setShortcut(QKeySequence.StandardKey.New)
        self.add_action.triggered.connect(self.add_artist)

        self.import_action = QAction("Import", self)
        self.import_action.setToolTip("Import a tiered text file")
        self.import_action.triggered.connect(self.import_artists)

        self.export_action = QAction("Export", self)
        self.export_action.setToolTip("Export canonical text (Ctrl+E)")
        self.export_action.setShortcut(QKeySequence("Ctrl+E"))
        self.export_action.triggered.connect(self.export_artists)

        self.recalculate_action = QAction("Recalculate", self)
        self.recalculate_action.setToolTip("Review rating suggestions (Ctrl+R)")
        self.recalculate_action.setShortcut(QKeySequence("Ctrl+R"))
        self.recalculate_action.triggered.connect(self.recalculate)

        self.undo_action = QAction("Undo", self)
        self.undo_action.setToolTip("Reverse the latest auditable rating action (Ctrl+Z)")
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.triggered.connect(self.undo_last)

        self.refresh_action = QAction("Refresh", self)
        self.refresh_action.setShortcut(QKeySequence.StandardKey.Refresh)
        self.refresh_action.triggered.connect(self.reload)

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main actions", self)
        toolbar.setObjectName("main-toolbar")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(18, 18))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        toolbar.addAction(self.add_action)
        toolbar.addSeparator()
        toolbar.addAction(self.import_action)
        toolbar.addAction(self.export_action)
        toolbar.addSeparator()
        toolbar.addAction(self.recalculate_action)
        toolbar.addAction(self.undo_action)
        toolbar.addSeparator()
        toolbar.addAction(self.refresh_action)
        self.addToolBar(toolbar)

    def _build_central_widget(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(10, 8, 10, 0)
        layout.setSpacing(8)

        self.collection_tabs = QTabBar()
        self.collection_tabs.setObjectName("collection-tabs")
        self.collection_tabs.setExpanding(False)
        self.collection_tabs.setDrawBase(False)
        for collection_kind in (CollectionKind.VIDEOS, CollectionKind.IMAGES):
            label = self._collection_label(collection_kind)
            index = self.collection_tabs.addTab(label)
            self.collection_tabs.setTabData(index, collection_kind)
            self.collection_tabs.setTabToolTip(index, f"View the {label} collection")
        self.collection_tabs.setCurrentIndex(0)
        self.collection_tabs.currentChanged.connect(self._collection_changed)
        layout.addWidget(self.collection_tabs)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_navigation())
        splitter.addWidget(self._build_browser())
        self.detail = ArtistDetailPanel(self.library_dir)
        self.detail.save_requested.connect(self.save_artist)
        self.detail.log_update_requested.connect(self.log_update)
        self.detail.point_adjustment_requested.connect(self.adjust_points)
        self.detail.resolve_shift_requested.connect(self.resolve_tier_shift)
        self.detail.trash_requested.connect(self.trash_artist)
        self.detail.restore_requested.connect(self.restore_artist)
        self.detail.permanent_delete_requested.connect(self.permanent_delete_artist)
        splitter.addWidget(self.detail)
        splitter.setSizes([205, 755, 490])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        layout.addWidget(splitter, 1)
        self.setCentralWidget(central)

    def _build_navigation(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(180)
        panel.setMaximumWidth(260)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 10, 0)
        title = QLabel("VIEWS")
        title.setProperty("muted", True)
        layout.addWidget(title)
        self.navigation = QListWidget()
        self.navigation.setObjectName("navigation")
        self._add_nav_item("All Artists", "all")
        for tier in TIER_DESCENDING:
            self._add_nav_item(tier.value, tier.value)
        self._add_nav_item("Attention Needed", "attention")
        self._add_nav_item("Trash", "trash")
        self.navigation.currentItemChanged.connect(self._navigation_changed)
        layout.addWidget(self.navigation, 1)
        library_label = QLabel(str(self.library_dir))
        library_label.setProperty("muted", True)
        library_label.setWordWrap(True)
        library_label.setToolTip(str(self.library_dir))
        layout.addWidget(library_label)
        return panel

    def _add_nav_item(self, title: str, key: str) -> None:
        item = QListWidgetItem(title)
        item.setData(Qt.ItemDataRole.UserRole, key)
        item.setData(Qt.ItemDataRole.UserRole + 1, title)
        self.navigation.addItem(item)
        self._nav_items[key] = item

    def _build_browser(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 10, 0)
        filters = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("artist-search")
        self.search_edit.setPlaceholderText("Search artists, notes, and tags…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._search_changed)
        filters.addWidget(self.search_edit, 1)
        self.heavy_chip = QToolButton()
        self.heavy_chip.setText("Heavy")
        self.heavy_chip.setCheckable(True)
        self.heavy_chip.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.heavy_chip.toggled.connect(self._heavy_filter_changed)
        filters.addWidget(self.heavy_chip)
        self.compressed_chip = QToolButton()
        self.compressed_chip.setText("Compressed")
        self.compressed_chip.setCheckable(True)
        self.compressed_chip.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.compressed_chip.toggled.connect(self._compressed_filter_changed)
        filters.addWidget(self.compressed_chip)
        layout.addLayout(filters)

        self.table_model = ArtistTableModel(self)
        self.proxy_model = ArtistFilterProxyModel(self)
        self.proxy_model.setSourceModel(self.table_model)
        self.table = QTableView()
        self.table.setObjectName("artist-table")
        self.table.setModel(self.proxy_model)
        self.table.setSortingEnabled(True)
        # The default catalog view follows the rating ladder, with names alphabetical per tier.
        self.table.sortByColumn(1, Qt.SortOrder.AscendingOrder)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setMinimumSectionSize(55)
        self.table.setColumnWidth(0, 220)
        self.table.setColumnWidth(1, 155)
        self.table.setColumnWidth(2, 65)
        self.table.setColumnWidth(3, 105)
        self.table.setColumnWidth(4, 90)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table, 1)
        self.empty_label = QLabel("No artists match this view.")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setProperty("muted", True)
        self.empty_label.hide()
        layout.addWidget(self.empty_label)
        return panel

    def _build_shortcuts(self) -> None:
        search_shortcut = QShortcut(QKeySequence.StandardKey.Find, self)
        search_shortcut.activated.connect(self._focus_search)

    def reload(self, select_artist_id: int | None = None) -> None:
        wanted = select_artist_id if select_artist_id is not None else self._selected_artist_id
        trash = self._navigation == "trash"
        try:
            artists = self.store.list_artists(
                self._collection_kind,
                include_deleted=trash,
            )
        except Exception as exc:
            self._error("Could not load the library", exc)
            return
        self.table_model.set_artists(artists)
        self.proxy_model.set_navigation(self._navigation)
        self._update_counts()
        self._update_empty_state()
        if wanted is not None and self._select_artist(wanted):
            return
        self._selected_artist_id = None
        self.detail.set_artist(None)
        if self.proxy_model.rowCount() > 0:
            self.table.selectRow(0)

    def _collection_changed(self, index: int) -> None:
        collection_kind = self.collection_tabs.tabData(index)
        if collection_kind is None:
            return
        collection_kind = CollectionKind(collection_kind)
        if collection_kind is self._collection_kind:
            return
        self._collection_kind = collection_kind
        self._selected_artist_id = None
        self.table.clearSelection()
        self.detail.set_artist(None)
        self._update_collection_context()
        self.reload()

    def _update_collection_context(self) -> None:
        label = self._collection_label(self._collection_kind)
        self.add_action.setToolTip(f"Add an artist to {label} (Ctrl+N)")
        self.import_action.setToolTip(f"Import a tiered text file into {label}")
        self.export_action.setToolTip(f"Export {label} as canonical text (Ctrl+E)")
        self.recalculate_action.setToolTip(f"Review rating suggestions for {label} (Ctrl+R)")
        self.empty_label.setText(f"No artists match this {label} view.")
        self.statusBar().showMessage(f"{label} collection — Library: {self.library_dir}")

    def _select_artist(self, artist_id: int) -> bool:
        for source_row in range(self.table_model.rowCount()):
            artist = self.table_model.artist_at(source_row)
            if artist is None or artist.id != artist_id:
                continue
            source_index = self.table_model.index(source_row, 0)
            proxy_index = self.proxy_model.mapFromSource(source_index)
            if proxy_index.isValid():
                self.table.selectRow(proxy_index.row())
                self.table.scrollTo(proxy_index)
                return True
        return False

    def _navigation_changed(
        self,
        current: QListWidgetItem | None,
        _previous: QListWidgetItem | None,
    ) -> None:
        if current is None:
            return
        self._navigation = str(current.data(Qt.ItemDataRole.UserRole))
        self.proxy_model.set_navigation(self._navigation)
        self.reload()

    def _selection_changed(self, _selected: QItemSelection, _deselected: QItemSelection) -> None:
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            self._selected_artist_id = None
            self.detail.set_artist(None)
            return
        artist = indexes[0].data(ARTIST_ROLE)
        if isinstance(artist, ArtistView):
            self._selected_artist_id = artist.id
            self.detail.set_artist(artist)

    def _search_changed(self, text: str) -> None:
        self.proxy_model.set_search(text)
        self._update_empty_state()
        self._show_visible_count()

    def _heavy_filter_changed(self, checked: bool) -> None:
        self.proxy_model.set_heavy_only(checked)
        self._update_empty_state()
        self._show_visible_count()

    def _compressed_filter_changed(self, checked: bool) -> None:
        self.proxy_model.set_compressed_only(checked)
        self._update_empty_state()
        self._show_visible_count()

    def _update_empty_state(self) -> None:
        empty = self.proxy_model.rowCount() == 0
        self.table.setVisible(not empty)
        self.empty_label.setVisible(empty)

    def _show_visible_count(self) -> None:
        count = self.proxy_model.rowCount()
        label = self._collection_label(self._collection_kind)
        self.statusBar().showMessage(
            f"{count} artist{'s' if count != 1 else ''} shown in {label}", 3000
        )

    def _update_counts(self) -> None:
        active = self.store.list_artists(self._collection_kind, include_deleted=False)
        trash = self.store.list_artists(self._collection_kind, include_deleted=True)
        counts: dict[str, int] = {
            "all": len(active),
            "attention": sum(artist.needs_attention for artist in active),
            "trash": len(trash),
        }
        for tier in TIER_DESCENDING:
            counts[tier.value] = sum(artist.tier is tier for artist in active)
        for key, item in self._nav_items.items():
            base = str(item.data(Qt.ItemDataRole.UserRole + 1))
            item.setText(f"{base}  {counts.get(key, 0)}")

    def _focus_search(self) -> None:
        self.search_edit.setFocus()
        self.search_edit.selectAll()

    def add_artist(self) -> None:
        dialog = AddArtistDialog(self)
        label = self._collection_label(self._collection_kind)
        dialog.setWindowTitle(f"Add artist to {label}")
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        try:
            artist_id = self.store.create(dialog.values(), self._collection_kind)
        except Exception as exc:
            self._error("Artist could not be added", exc)
            return
        self._switch_navigation("all")
        self.reload(artist_id)
        self.statusBar().showMessage(f"Artist added to {label}", 3000)

    def save_artist(self, values: dict[str, object]) -> None:
        artist = self.detail.artist
        if artist is None:
            return
        desired_tier = values.pop("tier", artist.tier)
        tier_reason: str | None = None
        if desired_tier != artist.tier:
            tier_dialog = ManualTierDialog(
                artist.name,
                artist.tier,
                desired_tier if isinstance(desired_tier, Tier) else Tier(str(desired_tier)),
                self,
            )
            if tier_dialog.exec() != tier_dialog.DialogCode.Accepted:
                self.detail.set_artist(artist)
                return
            tier_reason = tier_dialog.reason
        try:

            def save(service: RatingService) -> None:
                repository = ArtistRepository(self._rating_session)
                repository.update(
                    artist.id,
                    name=str(values.pop("name", artist.name)),
                    tags=values.pop("tags", ()),
                    **values,
                )
                if tier_reason is not None:
                    service.manual_tier_override(artist.id, desired_tier, tier_reason)

            self._run_rating_transaction(save)
            if tier_reason is not None:
                self._last_rating_action_depth = 1
        except Exception as exc:
            self._error("Changes could not be saved", exc)
            return
        self.reload(artist.id)
        self.statusBar().showMessage("Changes saved", 3000)

    def log_update(self) -> None:
        artist = self.detail.artist
        if artist is None:
            return
        dialog = LogUpdateDialog(artist.name, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        update_date, sentiment, reason = dialog.values()
        delta = 1 if sentiment == "good" else -1
        decision = self._threshold_decision(artist, artist.points + delta)
        if decision == "cancel":
            return
        try:

            def apply_update(service: RatingService) -> None:
                service.log_update(artist.id, update_date, sentiment, reason)
                if decision == "proceed":
                    service.approve_tier_shift(artist.id)
                elif decision == "later":
                    service.defer_tier_shift(artist.id)

            self._run_rating_transaction(apply_update)
            self._last_rating_action_depth = 2 if decision == "proceed" else 1
        except Exception as exc:
            self._error("Update could not be logged", exc)
            return
        self.reload(artist.id)
        self.statusBar().showMessage("Update logged", 3000)

    def adjust_points(self) -> None:
        artist = self.detail.artist
        if artist is None:
            return
        dialog = PointAdjustmentDialog(artist.name, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        delta, reason = dialog.values()
        decision = self._threshold_decision(artist, artist.points + delta)
        if decision == "cancel":
            return
        try:

            def apply_adjustment(service: RatingService) -> None:
                service.adjust_points(artist.id, delta, reason)
                if decision == "proceed":
                    service.approve_tier_shift(artist.id)
                elif decision == "later":
                    service.defer_tier_shift(artist.id)

            self._run_rating_transaction(apply_adjustment)
            self._last_rating_action_depth = 2 if decision == "proceed" else 1
        except Exception as exc:
            self._error("Points could not be adjusted", exc)
            return
        self.reload(artist.id)

    def _threshold_decision(self, artist: ArtistView, projected_points: int) -> str:
        try:
            with self.database.session() as session:
                proposal = RatingService(session).propose_tier_shift(
                    artist.id, points=projected_points
                )
        except Exception as exc:
            self._error("The tier threshold could not be evaluated", exc)
            return "cancel"
        if proposal is None:
            return "none"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Tier threshold reached")
        box.setText(
            f"This change gives {artist.name} {projected_points:+d} points. "
            f"Move from {proposal.old_tier.value} to {proposal.new_tier.value}?"
        )
        box.setInformativeText(
            "Proceed moves one tier and resets points. Later keeps the balance in Attention Needed."
        )
        proceed = box.addButton("Proceed", QMessageBox.ButtonRole.AcceptRole)
        later = box.addButton("Later", QMessageBox.ButtonRole.ActionRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(proceed)
        box.exec()
        clicked = box.clickedButton()
        if clicked is proceed:
            return "proceed"
        if clicked is later:
            return "later"
        if clicked is cancel:
            return "cancel"
        return "cancel"

    def resolve_tier_shift(self) -> None:
        artist = self.detail.artist
        if artist is None:
            return
        try:
            with self.database.session() as session:
                proposal = RatingService(session).propose_tier_shift(artist.id)
        except Exception as exc:
            self._error("Tier shift could not be evaluated", exc)
            return
        if proposal is None:
            QMessageBox.information(self, "No tier shift", "There is no legal tier shift now.")
            self.reload(artist.id)
            return
        answer = QMessageBox.question(
            self,
            "Resolve tier shift",
            f"Move {artist.name} from {proposal.old_tier.value} to "
            f"{proposal.new_tier.value} and reset {proposal.points:+d} points to zero?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._run_rating_transaction(lambda service: service.approve_tier_shift(artist.id))
            self._last_rating_action_depth = 1
        except Exception as exc:
            self._error("Tier shift could not be applied", exc)
            return
        self.reload(artist.id)

    def recalculate(self) -> None:
        try:
            with self.database.session() as session:
                suggestions = RatingService(session).evaluate_rules(
                    date.today(), collection_kind=self._collection_kind
                )
        except Exception as exc:
            self._error("Rating rules could not be evaluated", exc)
            return
        if not suggestions:
            QMessageBox.information(
                self,
                "No suggestions",
                "Every current rating rule is already up to date.",
            )
            return
        dialog = RuleSuggestionsDialog(suggestions, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        selected = dialog.selected_suggestions()
        if not selected:
            return
        try:
            self._run_rating_transaction(
                lambda service: service.apply_rule_suggestions(selected, date.today())
            )
            self._last_rating_action_depth = 1
        except Exception as exc:
            self._error("Selected suggestions could not be applied", exc)
            return
        self.reload()
        QMessageBox.information(
            self,
            "Recalculation complete",
            f"Applied {len(selected)} reviewed adjustment{'s' if len(selected) != 1 else ''}. "
            "Any legal tier shifts are now in Attention Needed.",
        )

    def undo_last(self) -> None:
        if self._last_rating_action_depth <= 0:
            QMessageBox.information(
                self,
                "Nothing to undo",
                "There is no reversible action from this application session.",
            )
            return
        try:
            depth = self._last_rating_action_depth

            def reverse_last(service: RatingService):  # noqa: ANN202
                reversed_event = None
                for _index in range(depth):
                    reversed_event = service.undo_last_session_action()
                return reversed_event

            reversed_event = self._run_rating_transaction(reverse_last)
        except Exception as exc:
            self._error("The latest action could not be reversed", exc)
            return
        if reversed_event is None:
            QMessageBox.information(self, "Nothing to undo", "There is no reversible action.")
            return
        self._last_rating_action_depth = 0
        self.reload()
        self.statusBar().showMessage("Latest rating action reversed", 4000)

    def trash_artist(self) -> None:
        artist = self.detail.artist
        if artist is None:
            return
        answer = QMessageBox.question(
            self,
            "Move artist to Trash",
            f"Move {artist.name} to Trash? You can restore it later.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            with self.database.session() as session:
                ArtistRepository(session).trash(artist.id)
        except Exception as exc:
            self._error("Artist could not be moved to Trash", exc)
            return
        self._selected_artist_id = None
        self.reload()

    def restore_artist(self) -> None:
        artist = self.detail.artist
        if artist is None:
            return
        try:
            with self.database.session() as session:
                ArtistRepository(session).restore(artist.id)
        except Exception as exc:
            self._error("Artist could not be restored", exc)
            return
        self._switch_navigation("all")
        self.reload(artist.id)

    def permanent_delete_artist(self) -> None:
        artist = self.detail.artist
        if artist is None:
            return
        answer = QMessageBox.warning(
            self,
            "Delete artist permanently",
            f"Permanently delete {artist.name} and its entire audit history?\n\n"
            "This cannot be undone.",
            QMessageBox.StandardButton.Delete | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Delete:
            return
        try:
            with self.database.session() as session:
                ArtistRepository(session).permanent_delete(artist.id)
        except Exception as exc:
            self._error("Artist could not be deleted", exc)
            return
        self._selected_artist_id = None
        self.reload()

    def import_artists(self) -> None:
        collection_kind = self._collection_kind
        collection_label = self._collection_label(collection_kind)
        source, _filter = QFileDialog.getOpenFileName(
            self,
            f"Import into {collection_label}",
            str(self.library_dir),
            "Collection text (*.txt);;All files (*)",
        )
        if not source:
            return
        try:
            ImportService = _import_service_class()  # noqa: N806 - runtime class import
            service = ImportService(self.database, collection_kind)
            preview = service.preview_import(Path(source))
        except Exception as exc:
            self._error("The source file could not be previewed", exc)
            return
        wizard = ImportWizard(preview, self, collection_kind=collection_kind)
        if wizard.exec() != wizard.DialogCode.Accepted:
            return
        resolutions = wizard.resolutions()
        try:
            conflicts = service.find_conflicts(preview, resolutions)
        except Exception as exc:
            self._error("Existing records could not be compared", exc)
            return
        conflict_decisions: dict[str, str] = {}
        if conflicts:
            current_records = {
                artist.id: artist
                for artist in (
                    self.store.list_artists(collection_kind, include_deleted=False)
                    + self.store.list_artists(collection_kind, include_deleted=True)
                )
            }
            conflict_dialog = ConflictResolutionDialog(
                conflicts,
                self,
                current_records=current_records,
            )
            if conflict_dialog.exec() != conflict_dialog.DialogCode.Accepted:
                return
            conflict_decisions = conflict_dialog.decisions()
        try:
            result = service.apply_import(
                preview,
                resolutions=resolutions,
                conflict_resolutions=conflict_decisions,
                create_backup=True,
            )
        except Exception as exc:
            self._error("Import could not be committed", exc)
            return
        self._switch_navigation("all")
        self.reload()
        QMessageBox.information(
            self,
            f"{collection_label} import complete",
            _import_result_text(result),
        )

    def export_artists(self) -> None:
        collection_kind = self._collection_kind
        collection_label = self._collection_label(collection_kind)
        filename_kind = collection_kind.value.replace("_", "-").casefold()
        suggested = self.library_dir / (f"{filename_kind}-export-{date.today().isoformat()}.txt")
        destination, _filter = QFileDialog.getSaveFileName(
            self,
            f"Export {collection_label}",
            str(suggested),
            "Collection text (*.txt)",
        )
        if not destination:
            return
        if not destination.casefold().endswith(".txt"):
            destination += ".txt"
        try:
            from collection_manager.exporting import export_text

            exported = export_text(
                self.database,
                Path(destination),
                collection_kind=collection_kind,
            )
        except Exception as exc:
            self._error("The library could not be exported", exc)
            return
        self.statusBar().showMessage(f"Exported to {exported}", 6000)
        QMessageBox.information(
            self,
            f"{collection_label} export complete",
            f"Saved the {collection_label} collection as canonical text to:\n{exported}",
        )

    @staticmethod
    def _collection_label(collection_kind: CollectionKind) -> str:
        return collection_kind.value.replace("_", " ").title()

    def _switch_navigation(self, key: str) -> None:
        item = self._nav_items.get(key)
        if item is not None:
            self.navigation.setCurrentItem(item)

    def _error(self, title: str, error: Exception) -> None:
        QMessageBox.critical(self, title, f"{title}.\n\n{error}")

    def _run_rating_transaction(self, operation):  # noqa: ANN001, ANN202
        """Run against the window-lifetime service while keeping transactions short."""

        self._rating_session.expire_all()
        try:
            result = operation(self._rating_service)
            self._rating_session.commit()
            return result
        except Exception:
            self._rating_session.rollback()
            # An operation can append to RatingService's action stack before a later flush or
            # commit fails. Rebuild it rather than retaining an action that never persisted.
            self._rating_service = RatingService(self._rating_session)
            self._last_rating_action_depth = 0
            raise

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.setValue("window/state", self.saveState())
        self.settings.sync()
        self.detail.shutdown_folder_scans()
        self._rating_session.close()
        super().closeEvent(event)


def _import_service_class():  # noqa: ANN202
    try:
        from collection_manager.importing import ImportService
    except ImportError:
        from collection_manager.import_service import ImportService
    return ImportService


def _import_result_text(result: object) -> str:
    labels = (
        ("added", "added"),
        ("updated", "updated"),
        ("skipped", "unchanged"),
        ("artist_count", "artists in import"),
    )
    parts = []
    for attribute, label in labels:
        value: Any = getattr(result, attribute, None)
        if value is not None:
            parts.append(f"{value} {label}")
    if not parts:
        return "The reviewed import was committed successfully."
    return "Import complete: " + ", ".join(parts) + "."
