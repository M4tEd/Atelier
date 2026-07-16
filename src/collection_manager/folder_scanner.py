from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from pathlib import Path

from collection_manager.constants import HeavyStatus, SizeQualifier

__all__ = [
    "HEAVY_THRESHOLD_BYTES",
    "SI_UNIT_BYTES",
    "CancellationCallback",
    "FolderScanIssue",
    "FolderSizeScan",
    "ProgressCallback",
    "ScanIssueCode",
    "ScanProgress",
    "ScanStatus",
    "SizeMetadataCandidate",
    "bytes_to_display_size",
    "classify_heavy_bytes",
    "scan_folder_size",
    "size_candidate_from_scan",
]

HEAVY_THRESHOLD_BYTES = 5_000_000_000
SI_UNIT_BYTES: dict[str, int] = {
    "B": 1,
    "KB": 1_000,
    "MB": 1_000_000,
    "GB": 1_000_000_000,
    "TB": 1_000_000_000_000,
}


class ScanStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ScanIssueCode(StrEnum):
    NOT_FOUND = "not_found"
    NOT_A_DIRECTORY = "not_a_directory"
    PERMISSION_DENIED = "permission_denied"
    IO_ERROR = "io_error"
    SKIPPED_LINK = "skipped_link"
    SKIPPED_SPECIAL = "skipped_special"


@dataclass(slots=True, frozen=True)
class FolderScanIssue:
    relative_path: str
    code: ScanIssueCode
    message: str


@dataclass(slots=True, frozen=True)
class ScanProgress:
    current_path: Path
    total_bytes: int
    file_count: int
    directory_count: int


@dataclass(slots=True, frozen=True)
class FolderSizeScan:
    requested_path: Path
    resolved_root: Path | None
    status: ScanStatus
    total_bytes: int | None
    file_count: int
    directory_count: int
    skipped_link_count: int
    issue_count: int
    issues: tuple[FolderScanIssue, ...]
    started_at: datetime
    finished_at: datetime

    @property
    def usable(self) -> bool:
        return self.status in {ScanStatus.COMPLETE, ScanStatus.PARTIAL}


@dataclass(slots=True, frozen=True)
class SizeMetadataCandidate:
    size_value: Decimal
    size_unit: str
    size_qualifier: SizeQualifier
    heavy_status: HeavyStatus
    source_bytes: int


CancellationCallback = Callable[[], bool]
ProgressCallback = Callable[[ScanProgress], None]


def scan_folder_size(
    path: Path | str,
    *,
    is_cancelled: CancellationCallback | None = None,
    progress: ProgressCallback | None = None,
    issue_limit: int = 100,
) -> FolderSizeScan:
    """Measure a folder's logical file size without reading or modifying file contents.

    The explicitly selected root may itself be a symlink or junction, but descendant links and
    junctions are excluded. Errors below the root produce a usable lower-bound result; errors
    resolving or opening the root produce a failed result with no byte total.
    """

    if issue_limit < 0:
        raise ValueError("issue_limit cannot be negative")

    started_at = datetime.now(UTC)
    requested_path = Path(path).expanduser().absolute()
    issues: list[FolderScanIssue] = []
    issue_count = 0
    total_bytes = 0
    file_count = 0
    directory_count = 0
    skipped_link_count = 0
    incomplete = False

    def add_issue(relative_path: str, code: ScanIssueCode, message: str) -> None:
        nonlocal issue_count
        issue_count += 1
        if len(issues) < issue_limit:
            issues.append(FolderScanIssue(relative_path, code, message))

    def result(
        status: ScanStatus,
        *,
        resolved_root: Path | None,
        measured_bytes: int | None,
    ) -> FolderSizeScan:
        return FolderSizeScan(
            requested_path=requested_path,
            resolved_root=resolved_root,
            status=status,
            total_bytes=measured_bytes,
            file_count=file_count,
            directory_count=directory_count,
            skipped_link_count=skipped_link_count,
            issue_count=issue_count,
            issues=tuple(issues),
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )

    if is_cancelled is not None and is_cancelled():
        return result(ScanStatus.CANCELLED, resolved_root=None, measured_bytes=0)

    try:
        resolved_root = requested_path.resolve(strict=True)
        root_stat = resolved_root.stat()
    except FileNotFoundError as exc:
        add_issue(".", ScanIssueCode.NOT_FOUND, _error_message(exc, "Folder was not found"))
        return result(ScanStatus.FAILED, resolved_root=None, measured_bytes=None)
    except PermissionError as exc:
        add_issue(
            ".",
            ScanIssueCode.PERMISSION_DENIED,
            _error_message(exc, "Folder cannot be accessed"),
        )
        return result(ScanStatus.FAILED, resolved_root=None, measured_bytes=None)
    except OSError as exc:
        add_issue(".", ScanIssueCode.IO_ERROR, _error_message(exc, "Folder cannot be accessed"))
        return result(ScanStatus.FAILED, resolved_root=None, measured_bytes=None)

    if not stat.S_ISDIR(root_stat.st_mode):
        add_issue(".", ScanIssueCode.NOT_A_DIRECTORY, "Selected path is not a directory")
        return result(ScanStatus.FAILED, resolved_root=resolved_root, measured_bytes=None)

    stack = [resolved_root]
    while stack:
        if is_cancelled is not None and is_cancelled():
            return result(
                ScanStatus.CANCELLED,
                resolved_root=resolved_root,
                measured_bytes=total_bytes,
            )

        directory = stack.pop()
        try:
            iterator = os.scandir(directory)
        except FileNotFoundError as exc:
            relative = _relative_path(directory, resolved_root)
            if directory == resolved_root and directory_count == 0:
                add_issue(
                    relative,
                    ScanIssueCode.NOT_FOUND,
                    _error_message(exc, "Folder vanished before it could be scanned"),
                )
                return result(ScanStatus.FAILED, resolved_root=resolved_root, measured_bytes=None)
            add_issue(
                relative,
                ScanIssueCode.NOT_FOUND,
                _error_message(exc, "Folder vanished while scanning"),
            )
            incomplete = True
            continue
        except PermissionError as exc:
            relative = _relative_path(directory, resolved_root)
            if directory == resolved_root and directory_count == 0:
                add_issue(
                    relative,
                    ScanIssueCode.PERMISSION_DENIED,
                    _error_message(exc, "Folder cannot be read"),
                )
                return result(ScanStatus.FAILED, resolved_root=resolved_root, measured_bytes=None)
            add_issue(
                relative,
                ScanIssueCode.PERMISSION_DENIED,
                _error_message(exc, "Folder cannot be read"),
            )
            incomplete = True
            continue
        except OSError as exc:
            relative = _relative_path(directory, resolved_root)
            if directory == resolved_root and directory_count == 0:
                add_issue(
                    relative,
                    ScanIssueCode.IO_ERROR,
                    _error_message(exc, "Folder cannot be read"),
                )
                return result(ScanStatus.FAILED, resolved_root=resolved_root, measured_bytes=None)
            add_issue(
                relative,
                ScanIssueCode.IO_ERROR,
                _error_message(exc, "Folder cannot be read"),
            )
            incomplete = True
            continue

        directory_count += 1
        _report_progress(
            progress,
            directory,
            total_bytes,
            file_count,
            directory_count,
        )
        enumerated_entry = False
        with iterator:
            while True:
                try:
                    entry = next(iterator)
                except StopIteration:
                    break
                except FileNotFoundError as exc:
                    add_issue(
                        _relative_path(directory, resolved_root),
                        ScanIssueCode.NOT_FOUND,
                        _error_message(exc, "Folder changed while scanning"),
                    )
                    if directory == resolved_root and not enumerated_entry:
                        return result(
                            ScanStatus.FAILED,
                            resolved_root=resolved_root,
                            measured_bytes=None,
                        )
                    incomplete = True
                    break
                except PermissionError as exc:
                    add_issue(
                        _relative_path(directory, resolved_root),
                        ScanIssueCode.PERMISSION_DENIED,
                        _error_message(exc, "Folder became unreadable while scanning"),
                    )
                    if directory == resolved_root and not enumerated_entry:
                        return result(
                            ScanStatus.FAILED,
                            resolved_root=resolved_root,
                            measured_bytes=None,
                        )
                    incomplete = True
                    break
                except OSError as exc:
                    add_issue(
                        _relative_path(directory, resolved_root),
                        ScanIssueCode.IO_ERROR,
                        _error_message(exc, "Folder could not be fully enumerated"),
                    )
                    if directory == resolved_root and not enumerated_entry:
                        return result(
                            ScanStatus.FAILED,
                            resolved_root=resolved_root,
                            measured_bytes=None,
                        )
                    incomplete = True
                    break

                enumerated_entry = True
                if is_cancelled is not None and is_cancelled():
                    return result(
                        ScanStatus.CANCELLED,
                        resolved_root=resolved_root,
                        measured_bytes=total_bytes,
                    )

                entry_path = Path(entry.path)
                relative = _relative_path(entry_path, resolved_root)
                try:
                    is_link = entry.is_symlink() or entry.is_junction()
                    if is_link:
                        skipped_link_count += 1
                        add_issue(
                            relative,
                            ScanIssueCode.SKIPPED_LINK,
                            "Descendant symlink or junction was not followed",
                        )
                    elif entry.is_dir(follow_symlinks=False):
                        stack.append(entry_path)
                    elif entry.is_file(follow_symlinks=False):
                        file_size = entry.stat(follow_symlinks=False).st_size
                        total_bytes += max(0, int(file_size))
                        file_count += 1
                    else:
                        add_issue(
                            relative,
                            ScanIssueCode.SKIPPED_SPECIAL,
                            "Non-regular filesystem entry was not counted",
                        )
                except FileNotFoundError as exc:
                    add_issue(
                        relative,
                        ScanIssueCode.NOT_FOUND,
                        _error_message(exc, "Entry vanished while scanning"),
                    )
                    incomplete = True
                except PermissionError as exc:
                    add_issue(
                        relative,
                        ScanIssueCode.PERMISSION_DENIED,
                        _error_message(exc, "Entry cannot be read"),
                    )
                    incomplete = True
                except OSError as exc:
                    add_issue(
                        relative,
                        ScanIssueCode.IO_ERROR,
                        _error_message(exc, "Entry cannot be read"),
                    )
                    incomplete = True

                _report_progress(
                    progress,
                    entry_path,
                    total_bytes,
                    file_count,
                    directory_count,
                )

    status = ScanStatus.PARTIAL if incomplete else ScanStatus.COMPLETE
    return result(status, resolved_root=resolved_root, measured_bytes=total_bytes)


def bytes_to_display_size(
    total_bytes: int,
    *,
    unit: str | None = None,
    decimal_places: int = 3,
) -> tuple[Decimal, str, bool]:
    """Convert bytes to a decimal SI value, rounded down for safe threshold comparisons.

    Returns ``(value, unit, lost_precision)``. Automatic conversion selects the largest unit
    whose unrounded value is at least one, while zero remains ``0 B``.
    """

    if isinstance(total_bytes, bool) or not isinstance(total_bytes, int) or total_bytes < 0:
        raise ValueError("total_bytes must be a non-negative integer")
    if not 0 <= decimal_places <= 9:
        raise ValueError("decimal_places must be between 0 and 9")

    normalized_unit = unit.upper() if unit is not None else _automatic_unit(total_bytes)
    try:
        factor = SI_UNIT_BYTES[normalized_unit]
    except KeyError as exc:
        choices = ", ".join(SI_UNIT_BYTES)
        raise ValueError(f"Unsupported size unit {unit!r}; expected one of {choices}") from exc

    unrounded = Decimal(total_bytes) / Decimal(factor)
    quantum = Decimal(1).scaleb(-decimal_places)
    value = unrounded.quantize(quantum, rounding=ROUND_DOWN)
    return value, normalized_unit, value != unrounded


def classify_heavy_bytes(total_bytes: int, *, complete: bool) -> HeavyStatus:
    """Classify the five-GB decimal SI threshold from unrounded logical bytes."""

    if isinstance(total_bytes, bool) or not isinstance(total_bytes, int) or total_bytes < 0:
        raise ValueError("total_bytes must be a non-negative integer")
    if total_bytes >= HEAVY_THRESHOLD_BYTES:
        return HeavyStatus.YES
    return HeavyStatus.NO if complete else HeavyStatus.UNKNOWN


def size_candidate_from_scan(
    scan: FolderSizeScan,
    *,
    unit: str | None = None,
) -> SizeMetadataCandidate:
    """Convert a complete or partial scan into reviewable persisted size metadata."""

    if not scan.usable or scan.total_bytes is None:
        raise ValueError("Only complete or partial folder scans can produce size metadata")

    value, normalized_unit, lost_precision = bytes_to_display_size(
        scan.total_bytes,
        unit=unit,
    )
    if scan.status is ScanStatus.PARTIAL:
        qualifier = SizeQualifier.AT_LEAST
    elif lost_precision:
        qualifier = SizeQualifier.APPROXIMATE
    else:
        qualifier = SizeQualifier.EXACT
    return SizeMetadataCandidate(
        size_value=value,
        size_unit=normalized_unit,
        size_qualifier=qualifier,
        heavy_status=classify_heavy_bytes(
            scan.total_bytes,
            complete=scan.status is ScanStatus.COMPLETE,
        ),
        source_bytes=scan.total_bytes,
    )


def _automatic_unit(total_bytes: int) -> str:
    for unit in reversed(tuple(SI_UNIT_BYTES)):
        if total_bytes >= SI_UNIT_BYTES[unit]:
            return unit
    return "B"


def _relative_path(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return str(path)
    text = str(relative)
    return text if text else "."


def _error_message(error: OSError, fallback: str) -> str:
    return error.strerror or str(error) or fallback


def _report_progress(
    callback: ProgressCallback | None,
    current_path: Path,
    total_bytes: int,
    file_count: int,
    directory_count: int,
) -> None:
    if callback is not None:
        callback(
            ScanProgress(
                current_path=current_path,
                total_bytes=total_bytes,
                file_count=file_count,
                directory_count=directory_count,
            )
        )
