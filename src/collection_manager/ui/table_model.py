from __future__ import annotations

from datetime import date
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor, QFont

from collection_manager.constants import TIER_COLORS, TIER_DESCENDING
from collection_manager.ui.data import ArtistView

ARTIST_ROLE = Qt.ItemDataRole.UserRole
SORT_ROLE = Qt.ItemDataRole.UserRole + 1
TIER_SORT_ORDER = {tier: index for index, tier in enumerate(TIER_DESCENDING)}


class ArtistTableModel(QAbstractTableModel):
    HEADERS = ("Artist", "Tier", "Points", "Updated", "Size", "Tags")

    def __init__(self, parent=None):  # noqa: ANN001
        super().__init__(parent)
        self._artists: list[ArtistView] = []

    def set_artists(self, artists: list[ArtistView]) -> None:
        self.beginResetModel()
        self._artists = artists
        self.endResetModel()

    def artist_at(self, row: int) -> ArtistView | None:
        return self._artists[row] if 0 <= row < len(self._artists) else None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802, B008
        return 0 if parent.isValid() else len(self._artists)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802, B008
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = 0):  # noqa: N802
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = 0) -> Any:
        if not index.isValid():
            return None
        artist = self._artists[index.row()]
        column = index.column()

        if role == ARTIST_ROLE:
            return artist
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display_value(artist, column)
        if role == SORT_ROLE:
            return self._sort_value(artist, column)
        if role == Qt.ItemDataRole.ForegroundRole and column == 1:
            return QColor(TIER_COLORS[artist.tier])
        if role == Qt.ItemDataRole.FontRole and column in (0, 1):
            font = QFont()
            font.setWeight(QFont.Weight.DemiBold if column == 0 else QFont.Weight.Medium)
            return font
        if role == Qt.ItemDataRole.TextAlignmentRole and column == 2:
            return Qt.AlignmentFlag.AlignCenter
        if role == Qt.ItemDataRole.ToolTipRole:
            if column == 0 and artist.needs_attention:
                return "A tier decision is waiting in Attention Needed."
            if column == 5:
                return ", ".join(artist.tags)
        return None

    @staticmethod
    def _display_value(artist: ArtistView, column: int) -> str:
        values = (
            ("⚠ " if artist.needs_attention else "") + artist.name,
            artist.tier.value,
            f"{artist.points:+d}" if artist.points else "0",
            artist.last_updated.isoformat() if artist.last_updated else "—",
            artist.size_text or "—",
            ", ".join(artist.tags) or "—",
        )
        return values[column]

    @staticmethod
    def _sort_value(artist: ArtistView, column: int):  # noqa: ANN205
        values = (
            artist.name.casefold(),
            artist.tier.value.casefold(),
            artist.points,
            artist.last_updated or date.min,
            artist.size_value or -1,
            " ".join(artist.tags).casefold(),
        )
        return values[column]


class ArtistFilterProxyModel(QSortFilterProxyModel):
    def __init__(self, parent=None):  # noqa: ANN001
        super().__init__(parent)
        self._search = ""
        self._navigation = "all"
        self._heavy_only = False
        self._compressed_only = False
        self.setSortRole(SORT_ROLE)
        self.setDynamicSortFilter(True)
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def set_search(self, value: str) -> None:
        self._search = " ".join(value.casefold().split())
        self._invalidate_rows()

    def set_navigation(self, value: str) -> None:
        self._navigation = value
        self._invalidate_rows()

    def set_heavy_only(self, enabled: bool) -> None:
        self._heavy_only = enabled
        self._invalidate_rows()

    def set_compressed_only(self, enabled: bool) -> None:
        self._compressed_only = enabled
        self._invalidate_rows()

    def _invalidate_rows(self) -> None:
        """Invalidate row filters across the supported Qt 6 minor versions."""

        if hasattr(self, "beginFilterChange") and hasattr(self, "endFilterChange"):
            self.beginFilterChange()
            direction = getattr(QSortFilterProxyModel, "Direction", None)
            if direction is not None:
                self.endFilterChange(direction.Rows)
            else:  # pragma: no cover - transitional Qt build
                self.endFilterChange()
        else:  # pragma: no cover - PySide 6.8 compatibility
            self.invalidateFilter()

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:  # noqa: N802
        """Sort the Tier column by ladder rank, then artist name.

        Ascending tier order is intentionally best-to-worst because the visible tier labels are
        not alphabetically meaningful. Other columns retain Qt's normal sortable behavior.
        """

        if left.column() == 1 and right.column() == 1:
            left_artist = left.data(ARTIST_ROLE)
            right_artist = right.data(ARTIST_ROLE)
            if isinstance(left_artist, ArtistView) and isinstance(right_artist, ArtistView):
                left_key = (TIER_SORT_ORDER[left_artist.tier], left_artist.name.casefold())
                right_key = (TIER_SORT_ORDER[right_artist.tier], right_artist.name.casefold())
                return left_key < right_key
        return super().lessThan(left, right)

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:  # noqa: N802
        source = self.sourceModel()
        if not isinstance(source, ArtistTableModel):
            return True
        artist = source.artist_at(source_row)
        if artist is None:
            return False

        if self._navigation == "attention" and not artist.needs_attention:
            return False
        if (
            self._navigation not in {"all", "attention", "trash"}
            and artist.tier.value != self._navigation
        ):
            return False
        if self._heavy_only and artist.heavy_status.value != "yes":
            return False
        if self._compressed_only and not artist.is_compressed:
            return False

        if not self._search:
            return True
        haystack = " ".join(
            (
                artist.name,
                artist.tier.value,
                " ".join(artist.tags),
                artist.notes,
                artist.reference_url or "",
            )
        ).casefold()
        return all(word in haystack for word in self._search.split())
