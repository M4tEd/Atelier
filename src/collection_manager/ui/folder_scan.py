from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from collection_manager.folder_scanner import FolderSizeScan, ScanStatus, scan_folder_size

FolderScanner = Callable[..., FolderSizeScan]


class _WorkerSignals(QObject):
    progress = Signal(int, object)
    completed = Signal(int, object)
    failed = Signal(int, str)
    cancelled = Signal(int)
    finished = Signal(int)


class _FolderScanRunnable(QRunnable):
    """One cancellable traversal executed by a ``QThreadPool`` worker thread."""

    def __init__(
        self,
        request_id: int,
        path: Path,
        scanner: FolderScanner,
        cancel_event: threading.Event,
        signals: _WorkerSignals,
    ):
        super().__init__()
        self.setAutoDelete(True)
        self.request_id = request_id
        self.path = path
        self.scanner = scanner
        self.cancel_event = cancel_event
        self.signals = signals
        self._last_progress_at = 0.0

    @Slot()
    def run(self) -> None:
        try:
            scan = self.scanner(
                self.path,
                is_cancelled=self.cancel_event.is_set,
                progress=self._report_progress,
            )
            if self.cancel_event.is_set() or _is_cancelled_scan(scan):
                self.signals.cancelled.emit(self.request_id)
            elif getattr(scan, "status", None) is ScanStatus.FAILED:
                self.signals.failed.emit(self.request_id, _failed_scan_message(scan))
            else:
                self.signals.completed.emit(self.request_id, scan)
        except Exception as exc:  # pragma: no cover - exact errors belong to scanner tests
            if self.cancel_event.is_set():
                self.signals.cancelled.emit(self.request_id)
            else:
                self.signals.failed.emit(self.request_id, str(exc) or type(exc).__name__)
        finally:
            self.signals.finished.emit(self.request_id)

    def _report_progress(self, *values: object) -> None:
        """Coalesce filesystem progress so large folders do not flood Qt's event queue."""

        if self.cancel_event.is_set():
            return
        now = time.monotonic()
        if now - self._last_progress_at < 0.08:
            return
        self._last_progress_at = now
        payload: object = values[0] if len(values) == 1 else values
        self.signals.progress.emit(self.request_id, payload)


@dataclass(slots=True)
class _ScanJob:
    path: Path
    context: object
    cancel_event: threading.Event
    signals: _WorkerSignals


class FolderScanController(QObject):
    """Own cancellable background folder scans and expose only the latest request.

    Older jobs can take a moment to observe cancellation. Their results are deliberately not
    forwarded, which protects widgets from stale updates after a folder or artist change.
    """

    started = Signal(int, str, object)
    progress = Signal(int, object)
    completed = Signal(int, object)
    failed = Signal(int, str)
    cancelled = Signal(int)
    job_finished = Signal(int)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        scanner: FolderScanner = scan_folder_size,
        max_thread_count: int = 1,
    ):
        super().__init__(parent)
        self._scanner = scanner
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max(1, int(max_thread_count)))
        self._next_request_id = 0
        self._current_request_id: int | None = None
        self._jobs: dict[int, _ScanJob] = {}
        self._shutdown_requested = False

    @property
    def current_request_id(self) -> int | None:
        return self._current_request_id

    @property
    def is_scanning(self) -> bool:
        return self._current_request_id is not None

    @property
    def active_job_count(self) -> int:
        return len(self._jobs)

    @property
    def is_shutdown(self) -> bool:
        return self._shutdown_requested

    def start(self, path: Path | str, context: object = None) -> int:
        if self._shutdown_requested:
            raise RuntimeError("FolderScanController has already been shut down")
        self.cancel_current()
        self._next_request_id += 1
        request_id = self._next_request_id
        normalized_path = Path(path).expanduser().resolve(strict=False)
        cancel_event = threading.Event()
        signals = _WorkerSignals(self)
        signals.progress.connect(self._worker_progress)
        signals.completed.connect(self._worker_completed)
        signals.failed.connect(self._worker_failed)
        signals.cancelled.connect(self._worker_cancelled)
        signals.finished.connect(self._worker_finished)
        job = _ScanJob(normalized_path, context, cancel_event, signals)
        self._jobs[request_id] = job
        self._current_request_id = request_id
        runnable = _FolderScanRunnable(
            request_id,
            normalized_path,
            self._scanner,
            cancel_event,
            signals,
        )
        self.started.emit(request_id, str(normalized_path), context)
        self._pool.start(runnable)
        return request_id

    def cancel_current(self) -> int | None:
        request_id = self._current_request_id
        if request_id is None:
            return None
        self._current_request_id = None
        job = self._jobs.get(request_id)
        if job is not None:
            job.cancel_event.set()
        self.cancelled.emit(request_id)
        return request_id

    def shutdown(self, timeout_ms: int = 5000) -> bool:
        """Request cancellation and wait briefly so no pool outlives its owning Qt object."""

        self._shutdown_requested = True
        self._current_request_id = None
        for job in self._jobs.values():
            job.cancel_event.set()
        return bool(self._pool.waitForDone(max(0, int(timeout_ms))))

    @Slot(int, object)
    def _worker_progress(self, request_id: int, payload: object) -> None:
        if request_id == self._current_request_id:
            self.progress.emit(request_id, payload)

    @Slot(int, object)
    def _worker_completed(self, request_id: int, scan: FolderSizeScan) -> None:
        if request_id != self._current_request_id:
            return
        self._current_request_id = None
        self.completed.emit(request_id, scan)

    @Slot(int, str)
    def _worker_failed(self, request_id: int, message: str) -> None:
        if request_id != self._current_request_id:
            return
        self._current_request_id = None
        self.failed.emit(request_id, message)

    @Slot(int)
    def _worker_cancelled(self, request_id: int) -> None:
        if request_id != self._current_request_id:
            return
        self._current_request_id = None
        self.cancelled.emit(request_id)

    @Slot(int)
    def _worker_finished(self, request_id: int) -> None:
        job = self._jobs.pop(request_id, None)
        if job is not None:
            job.signals.deleteLater()
        self.job_finished.emit(request_id)


def _is_cancelled_scan(scan: object) -> bool:
    status: Any = getattr(scan, "status", None)
    value = getattr(status, "value", status)
    return str(value).casefold() in {"cancelled", "canceled"}


def _failed_scan_message(scan: FolderSizeScan) -> str:
    issues = getattr(scan, "issues", ())
    if issues:
        return str(getattr(issues[0], "message", issues[0]))
    return "The selected folder could not be scanned"
