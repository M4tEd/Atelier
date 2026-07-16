from __future__ import annotations

from collection_manager.constants import TIER_COLORS

DARK_STYLESHEET = f"""
QWidget {{
    background: #15181d;
    color: #e7e9ed;
    font-size: 13px;
}}
QMainWindow, QDialog {{ background: #111419; }}
QToolBar {{
    background: #1b1f26;
    border: none;
    border-bottom: 1px solid #303640;
    spacing: 6px;
    padding: 7px;
}}
QToolBar QToolButton, QPushButton {{
    background: #262c35;
    border: 1px solid #3a424e;
    border-radius: 6px;
    padding: 7px 11px;
}}
QToolBar QToolButton:hover, QPushButton:hover {{ background: #333b47; }}
QToolBar QToolButton:pressed, QPushButton:pressed {{ background: #20252d; }}
QPushButton:disabled, QToolButton:disabled {{ color: #6f7680; background: #20242a; }}
QPushButton[accent="true"] {{ background: #3e72d9; border-color: #5b8aeb; color: white; }}
QPushButton[danger="true"] {{ background: #6e3035; border-color: #9a474e; }}
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox,
QDateEdit, QListWidget, QTableView, QTreeWidget {{
    background: #1d2229;
    border: 1px solid #353c47;
    border-radius: 5px;
    padding: 6px;
    selection-background-color: #315fba;
    selection-color: white;
}}
QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QDateEdit:focus {{
    border-color: #5b8aeb;
}}
QListWidget {{ padding: 5px; outline: none; }}
QListWidget::item {{ border-radius: 5px; padding: 8px 10px; margin: 1px 0; }}
QListWidget::item:hover {{ background: #242a33; }}
QListWidget::item:selected {{ background: #30466e; }}
QHeaderView::section {{
    background: #20252c;
    border: none;
    border-right: 1px solid #343b45;
    border-bottom: 1px solid #343b45;
    padding: 7px;
    font-weight: 600;
}}
QTableView {{ gridline-color: #2b313a; alternate-background-color: #191d23; }}
QTableView::item {{ padding: 5px; }}
QGroupBox {{
    border: 1px solid #303741;
    border-radius: 7px;
    margin-top: 12px;
    padding-top: 8px;
    font-weight: 600;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 5px; }}
QSplitter::handle {{ background: #303640; width: 1px; height: 1px; }}
QStatusBar {{ background: #1b1f26; border-top: 1px solid #303640; }}
QToolTip {{ background: #262c35; color: white; border: 1px solid #4d5664; }}
QScrollBar:vertical {{ background: #171a1f; width: 12px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #3a424e; min-height: 24px; border-radius: 5px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QTabWidget::pane {{ border: 1px solid #303741; border-radius: 5px; }}
QTabBar::tab {{ background: #20252c; padding: 7px 12px; border: 1px solid #303741; }}
QTabBar::tab:selected {{ background: #30466e; }}
QTabBar#collection-tabs::tab {{
    min-width: 120px;
    padding: 10px 24px;
    border-radius: 6px;
    margin-right: 5px;
    font-size: 14px;
    font-weight: 600;
}}
QTabBar#collection-tabs::tab:selected {{
    background: #315fba;
    border-color: #5b8aeb;
}}
QLabel[muted="true"] {{ color: #929aa6; }}
QLabel[tier="{next(iter(TIER_COLORS.values()))}"] {{ font-weight: 600; }}
"""


def tier_badge_style(color: str) -> str:
    return (
        f"background: {color}; color: #ffffff; border-radius: 7px; "
        "font-weight: 600; padding: 2px 7px;"
    )
