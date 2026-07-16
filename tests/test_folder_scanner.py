from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

import collection_manager.folder_scanner as scanner_module
from collection_manager.constants import HeavyStatus, SizeQualifier
from collection_manager.folder_scanner import (
    HEAVY_THRESHOLD_BYTES,
    ScanIssueCode,
    ScanStatus,
    bytes_to_display_size,
    classify_heavy_bytes,
    scan_folder_size,
    size_candidate_from_scan,
)


def test_complete_scan_sums_nested_regular_files_and_reports_progress(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (tmp_path / "one.bin").write_bytes(b"a" * 1_500_000)
    (nested / "two.bin").write_bytes(b"b" * 500_000)
    progress = []

    result = scan_folder_size(tmp_path, progress=progress.append)
    candidate = size_candidate_from_scan(result)

    assert result.status is ScanStatus.COMPLETE
    assert result.total_bytes == 2_000_000
    assert result.file_count == 2
    assert result.directory_count == 2
    assert progress
    assert progress[-1].total_bytes == 2_000_000
    assert candidate.size_value.as_tuple().exponent == -3
    assert candidate.size_value == 2
    assert candidate.size_unit == "MB"
    assert candidate.size_qualifier is SizeQualifier.EXACT
    assert candidate.heavy_status is HeavyStatus.NO


def test_display_conversion_uses_si_units_and_rounds_down() -> None:
    value, unit, lost_precision = bytes_to_display_size(1_234_567)

    assert value.as_tuple().exponent == -3
    assert value == Decimal("1.234")
    assert unit == "MB"
    assert lost_precision is True

    value, unit, lost_precision = bytes_to_display_size(999, unit="B")
    assert value == 999
    assert unit == "B"
    assert lost_precision is False


def test_complete_quantized_scan_is_approximate(tmp_path: Path) -> None:
    (tmp_path / "uneven.bin").write_bytes(b"x" * 1_234_567)

    candidate = size_candidate_from_scan(scan_folder_size(tmp_path))

    assert candidate.size_value == Decimal("1.234")
    assert candidate.size_unit == "MB"
    assert candidate.size_qualifier is SizeQualifier.APPROXIMATE
    assert candidate.source_bytes == 1_234_567


def test_descendant_permission_error_produces_usable_lower_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "visible.bin").write_bytes(b"x" * 1_000)
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "hidden.bin").write_bytes(b"y" * 5_000)
    real_scandir = os.scandir

    def guarded_scandir(path):  # noqa: ANN001, ANN202 - test double for os.scandir
        if Path(path) == blocked:
            raise PermissionError(13, "Permission denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(scanner_module.os, "scandir", guarded_scandir)

    result = scan_folder_size(tmp_path)
    candidate = size_candidate_from_scan(result)

    assert result.status is ScanStatus.PARTIAL
    assert result.total_bytes == 1_000
    assert result.usable is True
    assert result.issue_count == 1
    assert result.issues[0].relative_path == "blocked"
    assert result.issues[0].code is ScanIssueCode.PERMISSION_DENIED
    assert candidate.size_qualifier is SizeQualifier.AT_LEAST
    assert candidate.heavy_status is HeavyStatus.UNKNOWN


def test_error_while_advancing_directory_iterator_becomes_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unstable = tmp_path / "unstable"
    unstable.mkdir()
    real_scandir = os.scandir

    class BrokenIterator:
        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *_args) -> None:  # noqa: ANN002
            return None

        def __iter__(self):  # noqa: ANN204
            return self

        def __next__(self):  # noqa: ANN204
            raise OSError(5, "Enumeration failed")

    def guarded_scandir(path):  # noqa: ANN001, ANN202 - test double for os.scandir
        if Path(path) == unstable:
            return BrokenIterator()
        return real_scandir(path)

    monkeypatch.setattr(scanner_module.os, "scandir", guarded_scandir)

    result = scan_folder_size(tmp_path)

    assert result.status is ScanStatus.PARTIAL
    assert result.total_bytes == 0
    assert result.issue_count == 1
    assert result.issues[0].relative_path == "unstable"
    assert result.issues[0].code is ScanIssueCode.IO_ERROR


def test_root_failures_are_not_usable(tmp_path: Path) -> None:
    missing = scan_folder_size(tmp_path / "missing")
    file_path = tmp_path / "file.txt"
    file_path.write_text("not a folder", encoding="utf-8")
    not_directory = scan_folder_size(file_path)

    assert missing.status is ScanStatus.FAILED
    assert missing.total_bytes is None
    assert missing.issues[0].code is ScanIssueCode.NOT_FOUND
    assert not_directory.status is ScanStatus.FAILED
    assert not_directory.total_bytes is None
    assert not_directory.issues[0].code is ScanIssueCode.NOT_A_DIRECTORY
    with pytest.raises(ValueError, match="complete or partial"):
        size_candidate_from_scan(missing)


def test_unreadable_root_is_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def denied_scandir(path):  # noqa: ANN001, ANN202 - test double for os.scandir
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(scanner_module.os, "scandir", denied_scandir)

    result = scan_folder_size(tmp_path)

    assert result.status is ScanStatus.FAILED
    assert result.total_bytes is None
    assert result.usable is False
    assert result.issues[0].code is ScanIssueCode.PERMISSION_DENIED


def test_cancellation_returns_unusable_result_after_progress(tmp_path: Path) -> None:
    for index in range(3):
        (tmp_path / f"{index}.bin").write_bytes(b"x" * 100)
    should_cancel = False

    def receive_progress(progress) -> None:  # noqa: ANN001
        nonlocal should_cancel
        if progress.file_count:
            should_cancel = True

    result = scan_folder_size(
        tmp_path,
        is_cancelled=lambda: should_cancel,
        progress=receive_progress,
    )

    assert result.status is ScanStatus.CANCELLED
    assert result.total_bytes == 100
    assert result.usable is False
    with pytest.raises(ValueError, match="complete or partial"):
        size_candidate_from_scan(result)


def test_descendant_symlink_is_reported_but_not_followed(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "large.bin").write_bytes(b"x" * 5_000)
    link = tmp_path / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlink creation is unavailable: {exc}")
    (tmp_path / "local.bin").write_bytes(b"x" * 100)

    result = scan_folder_size(tmp_path)

    assert result.status is ScanStatus.COMPLETE
    assert result.total_bytes == 100
    assert result.skipped_link_count == 1
    assert any(issue.code is ScanIssueCode.SKIPPED_LINK for issue in result.issues)


def test_raw_byte_heavy_classification_preserves_partial_certainty() -> None:
    assert classify_heavy_bytes(HEAVY_THRESHOLD_BYTES, complete=False) is HeavyStatus.YES
    assert classify_heavy_bytes(HEAVY_THRESHOLD_BYTES - 1, complete=False) is HeavyStatus.UNKNOWN
    assert classify_heavy_bytes(HEAVY_THRESHOLD_BYTES - 1, complete=True) is HeavyStatus.NO


def test_issue_detail_limit_does_not_hide_total_count(tmp_path: Path) -> None:
    for index in range(3):
        target = tmp_path / f"target-{index}"
        target.write_text("x", encoding="utf-8")
        link = tmp_path / f"link-{index}"
        try:
            link.symlink_to(target)
        except OSError as exc:
            pytest.skip(f"Symlink creation is unavailable: {exc}")

    result = scan_folder_size(tmp_path, issue_limit=1)

    assert result.issue_count == 3
    assert len(result.issues) == 1
