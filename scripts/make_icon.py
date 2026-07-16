from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QRectF, QSize
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


def render_icon(source: Path, destination: Path) -> None:
    renderer = QSvgRenderer(str(source))
    if not renderer.isValid():
        raise ValueError(f"Invalid SVG: {source}")
    image = QImage(QSize(256, 256), QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    renderer.render(painter, QRectF(0, 0, 256, 256))
    painter.end()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(destination), "ICO"):
        raise RuntimeError(f"Could not write icon: {destination}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: make_icon.py SOURCE.svg DESTINATION.ico")
    app = QGuiApplication.instance() or QGuiApplication([])
    render_icon(Path(sys.argv[1]), Path(sys.argv[2]))
