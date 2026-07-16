from __future__ import annotations

import os

# Qt's native platform plugins can abort in headless shells before pytest-qt creates its app.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
