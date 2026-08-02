from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import tempfile


class ScanBoundaryError(AssertionError):
    pass


class SensitiveValueError(AssertionError):
    pass


def _contains_sensitive_value(path: Path, needles: tuple[bytes, ...]) -> bool:
    overlap_size = max(len(needle) for needle in needles) - 1
    overlap = b""
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                return False
            candidate = overlap + chunk
            if any(needle in candidate for needle in needles):
                return True
            overlap = candidate[-overlap_size:] if overlap_size else b""


def _resolve_allowed_symlink(path: Path, root: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ScanBoundaryError(f"unresolvable symlink: {path}") from None
    if not resolved.is_relative_to(root):
        raise ScanBoundaryError(f"symlink escapes scan root: {path}")
    mode = resolved.stat(follow_symlinks=False).st_mode
    if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        raise ScanBoundaryError(f"symlink target is not regular file/directory: {path}")
    return resolved


def scan_root(
    lexical_root: Path,
    needles: tuple[bytes, ...],
    *,
    allow_internal_symlinks: bool,
) -> tuple[int, int]:
    if lexical_root.is_symlink():
        raise ScanBoundaryError(f"scan root must not be symlink: {lexical_root}")
    root = lexical_root.resolve(strict=True)
    if not stat.S_ISDIR(root.stat(follow_symlinks=False).st_mode):
        raise ScanBoundaryError(f"scan root must be directory: {lexical_root}")

    pending = [root]
    resolved_directories: set[Path] = set()
    resolved_files: set[Path] = set()
    files_scanned = 0
    bytes_scanned = 0
    while pending:
        candidate = pending.pop()
        if candidate.is_symlink():
            if not allow_internal_symlinks:
                raise ScanBoundaryError(f"symlink forbidden in fail-closed root: {candidate}")
            candidate = _resolve_allowed_symlink(candidate, root)
        else:
            candidate = candidate.resolve(strict=True)
            if not candidate.is_relative_to(root):
                raise ScanBoundaryError(f"path escapes scan root: {candidate}")

        mode = candidate.stat(follow_symlinks=False).st_mode
        if stat.S_ISDIR(mode):
            if candidate in resolved_directories:
                continue
            resolved_directories.add(candidate)
            pending.extend(sorted(candidate.iterdir(), reverse=True))
            continue
        if not stat.S_ISREG(mode):
            raise ScanBoundaryError(f"non-regular scan entry: {candidate}")
        if candidate in resolved_files:
            continue
        resolved_files.add(candidate)
        size = candidate.stat(follow_symlinks=False).st_size
        files_scanned += 1
        bytes_scanned += size
        if _contains_sensitive_value(candidate, needles):
            raise SensitiveValueError(f"sensitive value found in {candidate}")
    return files_scanned, bytes_scanned


def _expect_boundary(callable_object) -> None:
    try:
        callable_object()
    except ScanBoundaryError:
        return
    raise AssertionError("expected ScanBoundaryError")


def _expect_sensitive(callable_object) -> None:
    try:
        callable_object()
    except SensitiveValueError:
        return
    raise AssertionError("expected SensitiveValueError")


def run_harness() -> None:
    harness = Path(tempfile.mkdtemp(prefix="attempt41-secret-scanner-v2-", dir="/tmp"))
    needles = (
        b"scanner-harness-sensitive-token",
        b"https://scanner-harness.invalid/anthropic",
    )

    positive = harness / "positive"
    real = positive / "test-case-0"
    real.mkdir(parents=True)
    (real / "result.txt").write_bytes(b"safe\n")
    (positive / "test-case-current").symlink_to(real, target_is_directory=True)
    assert scan_root(positive, needles, allow_internal_symlinks=True) == (1, 5)

    detected = harness / "detected"
    detected.mkdir()
    (detected / "result.txt").write_bytes(b"prefix " + needles[1] + b" suffix\n")
    _expect_sensitive(
        lambda: scan_root(detected, needles, allow_internal_symlinks=True)
    )

    simulated_evidence = harness / "simulated-evidence"
    simulated_evidence.mkdir()
    (simulated_evidence / "real.txt").write_bytes(b"safe\n")
    (simulated_evidence / "alias.txt").symlink_to(
        simulated_evidence / "real.txt"
    )
    _expect_boundary(
        lambda: scan_root(
            simulated_evidence,
            needles,
            allow_internal_symlinks=False,
        )
    )

    outside = harness / "outside"
    outside.mkdir()
    (outside / "outside.txt").write_bytes(b"safe\n")
    cross = harness / "cross-boundary"
    cross.mkdir()
    (cross / "escape").symlink_to(outside, target_is_directory=True)
    _expect_boundary(lambda: scan_root(cross, needles, allow_internal_symlinks=True))

    dangling = harness / "dangling"
    dangling.mkdir()
    (dangling / "missing").symlink_to(dangling / "does-not-exist")
    _expect_boundary(
        lambda: scan_root(dangling, needles, allow_internal_symlinks=True)
    )

    loop = harness / "loop"
    loop.mkdir()
    (loop / "a").symlink_to(loop / "b")
    (loop / "b").symlink_to(loop / "a")
    _expect_boundary(lambda: scan_root(loop, needles, allow_internal_symlinks=True))

    to_evidence = harness / "to-evidence"
    to_evidence.mkdir()
    (to_evidence / "evidence").symlink_to(Path("/evidence"), target_is_directory=True)
    _expect_boundary(
        lambda: scan_root(to_evidence, needles, allow_internal_symlinks=True)
    )

    print(
        json.dumps(
            {
                "cases": {
                    "basetemp_internal_current_alias": "pass",
                    "exact_sensitive_value_detection": "pass",
                    "basetemp_cross_boundary": "rejected",
                    "basetemp_dangling": "rejected",
                    "basetemp_loop": "rejected",
                    "basetemp_to_evidence": "rejected",
                    "evidence_internal_symlink": "rejected",
                },
                "evidence_written": False,
                "schema_version": 1,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def run_scan(phase: str) -> None:
    assert phase in {"pre", "post"}
    settings_path = Path(
        os.environ.get("SECRET_SCAN_SETTINGS_PATH", "/root/.claude/settings.json")
    )
    assert settings_path.is_absolute() and not settings_path.is_symlink()
    settings_stat = settings_path.stat(follow_symlinks=False)
    assert stat.S_ISREG(settings_stat.st_mode)
    assert stat.S_IMODE(settings_stat.st_mode) == 0o600
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    token = settings["env"]["ANTHROPIC_AUTH_TOKEN"]
    base_url = settings["env"]["ANTHROPIC_BASE_URL"]
    assert isinstance(token, str) and len(token) >= 16
    assert isinstance(base_url, str) and base_url
    needles = (token.encode("utf-8"), base_url.encode("utf-8"))
    for needle in needles:
        assert needle
        assert b"\x00" not in needle and b"\n" not in needle and b"\r" not in needle

    evidence = Path(os.environ.get("SECRET_SCAN_EVIDENCE", "/evidence"))
    basetemp = Path(
        os.environ.get(
            "SECRET_SCAN_BASETEMP",
            "/tmp/pytest-attempt41-real-agent",
        )
    )
    files_scanned, bytes_scanned = scan_root(
        evidence,
        needles,
        allow_internal_symlinks=False,
    )
    roots_scanned = 1
    if basetemp.exists():
        more_files, more_bytes = scan_root(
            basetemp,
            needles,
            allow_internal_symlinks=True,
        )
        files_scanned += more_files
        bytes_scanned += more_bytes
        roots_scanned += 1

    payload = {
        "basetemp_internal_symlinks": "strict-resolve-inside-root-and-deduplicate",
        "bytes_scanned": bytes_scanned,
        "evidence_symlinks": "fail-closed",
        "files_scanned": files_scanned,
        "phase": phase,
        "roots_scanned": roots_scanned,
        "schema_version": 2,
        "sensitive_values_checked": [
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
        ],
        "sensitive_value_occurrences": 0,
        "settings_mode": "0600",
    }
    output_prefix = os.environ.get(
        "SECRET_SCAN_OUTPUT_PREFIX",
        "secret-scan-real-agent",
    )
    assert output_prefix and all(
        character.isascii() and (character.isalnum() or character == "-")
        for character in output_prefix
    )
    output = evidence / f"{output_prefix}-{phase}-v2.json"
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        stream.write("\n")


mode = sys.argv[1] if len(sys.argv) == 2 else ""
if mode == "harness":
    run_harness()
else:
    run_scan(mode)
