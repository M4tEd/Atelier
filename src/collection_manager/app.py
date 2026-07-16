from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from platformdirs import user_config_dir
from PySide6.QtCore import QCoreApplication, QSettings, Qt
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from collection_manager.database import Database, database_path_for_library
from collection_manager.ui.main_window import MainWindow
from collection_manager.ui.styles import DARK_STYLESHEET

APP_NAME = "Collection Manager"
ORGANIZATION_NAME = "Collection Manager"


def application_settings() -> QSettings:
    """Return settings stored in the platform's per-user application-data folder."""

    override = os.environ.get("COLLECTION_MANAGER_CONFIG_DIR")
    config_dir = (
        Path(override).expanduser()
        if override
        else Path(user_config_dir(APP_NAME, ORGANIZATION_NAME))
    )
    config_dir.mkdir(parents=True, exist_ok=True)
    return QSettings(str(config_dir / "settings.ini"), QSettings.Format.IniFormat)


def _default_library_directory() -> Path:
    documents = Path.home() / "Documents"
    return documents if documents.is_dir() else Path.home()


def choose_library_directory(settings: QSettings, requested: Path | None = None) -> Path | None:
    """Resolve the library directory, prompting only on the first interactive launch."""

    if requested is not None:
        candidate = requested.expanduser().resolve()
    else:
        configured = str(settings.value("library/path", "") or "").strip()
        candidate = Path(configured).expanduser() if configured else None
        if candidate is None or not candidate.is_dir():
            selected = QFileDialog.getExistingDirectory(
                None,
                "Choose a Collection Manager library folder",
                str(_default_library_directory()),
                QFileDialog.Option.ShowDirsOnly,
            )
            if not selected:
                return None
            candidate = Path(selected)

    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".collection-manager-write-test"
        probe.touch(exist_ok=True)
        probe.unlink(missing_ok=True)
    except OSError as exc:
        QMessageBox.critical(
            None,
            "Library unavailable",
            f"Collection Manager cannot write to this folder:\n{candidate}\n\n{exc}",
        )
        return None

    resolved = candidate.resolve()
    settings.setValue("library/path", str(resolved))
    settings.sync()
    return resolved


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local artist collection manager")
    parser.add_argument(
        "--library",
        type=Path,
        help="Use this library folder instead of the saved per-user setting.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    options = _parser().parse_args(arguments)

    QCoreApplication.setApplicationName(APP_NAME)
    QCoreApplication.setOrganizationName(ORGANIZATION_NAME)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontShowIconsInMenus, False)
    app = QApplication.instance() or QApplication([sys.argv[0], *arguments])
    app.setApplicationDisplayName(APP_NAME)
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_STYLESHEET)

    settings = application_settings()
    library_dir = choose_library_directory(settings, options.library)
    if library_dir is None:
        return 0

    try:
        database = Database(database_path_for_library(library_dir))
        database.initialize()
    except Exception as exc:  # pragma: no cover - requires an OS/database failure
        QMessageBox.critical(
            None,
            "Database unavailable",
            f"The library database could not be opened:\n{library_dir}\n\n{exc}",
        )
        return 1

    window = MainWindow(database=database, library_dir=library_dir, settings=settings)
    window.show()
    if os.environ.get("COLLECTION_MANAGER_TEST_MODE") == "1":
        return 0
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
