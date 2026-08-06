from __future__ import annotations

import json
import os
from pathlib import Path
import select
import signal
import stat
import sys
import time


RUNTIME = Path("/tmp/attempt52-service-supervisor")
PID_FILE = RUNTIME / "service.pid"
STARTTIME_FILE = RUNTIME / "service.starttime"
PROCESS_EVIDENCE_FILE = Path("/evidence/service-process-isolation.json")
LAUNCH_EVIDENCE_FILE = Path("/evidence/service-supervisor-launch.txt")
EXIT_EVIDENCE_FILE = Path("/evidence/service-exit-status.txt")
STOP_EVIDENCE_FILE = Path("/evidence/service-stop-verification.txt")
SERVICE_LOG_FILE = RUNTIME / "service.log"
ARCHIVED_SERVICE_LOG_FILE = Path("/evidence/service.log")
SERVICE_UID = 10001
SERVICE_GID = 10001
MAX_PIPE_BYTES = 8192
MAX_RECEIPT_BYTES = 4096
MAX_SERVICE_LOG_BYTES = 8 * 1024 * 1024
VERIFY_TIMEOUT_SECONDS = 10.0
TERMINATE_TIMEOUT_SECONDS = 30.0
EXPECTED_COMMAND = [
    "/opt/venvs/xiaodao/bin/python",
    "-m",
    "problem_locator",
    "serve",
]
EXPECTED_ENV = {
    "BIND_HOST": "0.0.0.0",
    "CLAUDE_COMMAND": "/usr/bin/timeout --foreground --signal=TERM --kill-after=5s 240s /usr/local/bin/claude -p --no-chrome --no-session-persistence --dangerously-skip-permissions --tools Bash,Read,Write,Skill --allowedTools Skill(logparse-diagnose) --setting-sources user --settings /run/plagent-claude/settings.json --model haiku --effort low --max-budget-usd 3.00",
    "DATA_ROOT": "/var/lib/problem-locator",
    "HOME": "/run/plagent-claude",
    "LANG": "C.UTF-8",
    "LOGNAME": "plagent",
    "LOGPARSE_CONFIG_PATH": "/opt/src/logparse/config.yaml",
    "LOGPARSE_PYTHON": "/opt/venvs/logparse/bin/python",
    "LOGPARSE_REPO": "/opt/src/logparse",
    "PATH": "/opt/venvs/xiaodao/bin:/opt/venvs/logparse/bin:/usr/local/bin:/usr/bin:/bin",
    "PORT": "8000",
    "PUBLIC_BASE_URL": "http://127.0.0.1:18000",
    "PYTHONNOUSERSITE": "1",
    "PYTHONPYCACHEPREFIX": "/tmp/attempt52-service-pycache",
    "SHELL": "/bin/sh",
    "SKILL_DIR": "/opt/e2e-skills",
    "USER": "plagent",
}


def require(condition: bool, code: str) -> None:
    if not condition:
        raise RuntimeError(code)


def require_root() -> None:
    require(os.getresuid() == (0, 0, 0), "SERVICE_VERIFIER_ROOT_UID")
    require(os.getresgid() == (0, 0, 0), "SERVICE_VERIFIER_ROOT_GID")


def receipt_open_flags(read_only: bool) -> int:
    flags = os.O_RDONLY if read_only else os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def validate_root_receipt_info(info: os.stat_result) -> None:
    require(stat.S_ISREG(info.st_mode), "SERVICE_RECEIPT_TYPE")
    require(info.st_uid == 0 and info.st_gid == 0, "SERVICE_RECEIPT_OWNER")
    require(stat.S_IMODE(info.st_mode) == 0o600, "SERVICE_RECEIPT_MODE")
    require(info.st_nlink == 1, "SERVICE_RECEIPT_LINKS")


def root_receipt_text(path: Path) -> str:
    fd = os.open(path, receipt_open_flags(read_only=True))
    try:
        validate_root_receipt_info(os.fstat(fd))
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(1024, MAX_RECEIPT_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            require(total <= MAX_RECEIPT_BYTES, "SERVICE_RECEIPT_SIZE")
        validate_root_receipt_info(os.fstat(fd))
    finally:
        os.close(fd)
    return b"".join(chunks).decode("ascii")


def write_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        require(written > 0, "SERVICE_WRITE")
        offset += written


def create_root_evidence(path: Path, data: bytes) -> None:
    require_root()
    require(0 < len(data) <= MAX_RECEIPT_BYTES, "SERVICE_EVIDENCE_SIZE")
    require(data.endswith(b"\n") and b"\r" not in data, "SERVICE_EVIDENCE_FRAMING")
    data.decode("ascii")
    fd = os.open(path, receipt_open_flags(read_only=False), 0o600)
    try:
        validate_root_receipt_info(os.fstat(fd))
        write_all(fd, data)
        os.fsync(fd)
        validate_root_receipt_info(os.fstat(fd))
    finally:
        os.close(fd)


def archive_service_log() -> None:
    require_root()
    source_fd = os.open(SERVICE_LOG_FILE, receipt_open_flags(read_only=True))
    destination_fd: int | None = None
    try:
        validate_root_receipt_info(os.fstat(source_fd))
        destination_fd = os.open(
            ARCHIVED_SERVICE_LOG_FILE,
            receipt_open_flags(read_only=False),
            0o600,
        )
        validate_root_receipt_info(os.fstat(destination_fd))
        total = 0
        while True:
            chunk = os.read(source_fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            require(total <= MAX_SERVICE_LOG_BYTES, "SERVICE_LOG_SIZE")
            write_all(destination_fd, chunk)
        validate_root_receipt_info(os.fstat(source_fd))
        os.fsync(destination_fd)
        validate_root_receipt_info(os.fstat(destination_fd))
    finally:
        os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)


def parse_status(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="ascii").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            result[key] = value.strip()
    return result


def read_process_receipts() -> tuple[str, str]:
    require_root()
    pid_raw = root_receipt_text(PID_FILE)
    require(pid_raw.endswith("\n") and pid_raw.count("\n") == 1, "SERVICE_PID_FRAMING")
    pid_text = pid_raw[:-1]
    require(pid_text.isascii() and pid_text.isdecimal(), "SERVICE_PID_FORMAT")
    require(int(pid_text) > 1, "SERVICE_PID_RANGE")
    starttime_raw = root_receipt_text(STARTTIME_FILE)
    require(
        starttime_raw.endswith("\n") and starttime_raw.count("\n") == 1,
        "SERVICE_STARTTIME_FRAMING",
    )
    expected_starttime = starttime_raw[:-1]
    require(
        expected_starttime.isascii() and expected_starttime.isdecimal(),
        "SERVICE_STARTTIME_FORMAT",
    )
    return pid_text, expected_starttime


def inspect_as_service_user(pid_text: str, expected_starttime: str) -> dict[str, object]:
    require(os.getresuid() == (SERVICE_UID,) * 3, "INSPECTOR_RESUID")
    require(os.getresgid() == (SERVICE_GID,) * 3, "INSPECTOR_RESGID")
    require(os.getgroups() == [], "INSPECTOR_GROUPS")
    proc = Path("/proc") / pid_text
    require(proc.is_dir(), "SERVICE_PROCESS_ABSENT")
    status = parse_status(proc / "status")
    require(status.get("Uid", "").split() == [str(SERVICE_UID)] * 4, "SERVICE_UID")
    require(status.get("Gid", "").split() == [str(SERVICE_GID)] * 4, "SERVICE_GID")
    require(status.get("NoNewPrivs") == "1", "SERVICE_NO_NEW_PRIVS")
    require(status.get("CapInh") == "0000000000000000", "SERVICE_CAP_INH")
    require(status.get("CapEff") == "0000000000000000", "SERVICE_CAP_EFF")
    require(status.get("CapPrm") == "0000000000000000", "SERVICE_CAP_PRM")
    require(status.get("CapAmb") == "0000000000000000", "SERVICE_CAP_AMB")
    command_bytes = (proc / "cmdline").read_bytes()
    require(len(command_bytes) <= 4096, "SERVICE_COMMAND_SIZE")
    command = [item.decode("utf-8") for item in command_bytes.split(b"\0") if item]
    require(command == EXPECTED_COMMAND, "SERVICE_COMMAND")
    environ_bytes = (proc / "environ").read_bytes()
    require(len(environ_bytes) <= 16384, "SERVICE_ENV_SIZE")
    environ_items = [item.decode("utf-8") for item in environ_bytes.split(b"\0") if item]
    environ: dict[str, str] = {}
    for item in environ_items:
        require("=" in item, "SERVICE_ENV_FORMAT")
        key, value = item.split("=", 1)
        require(key not in environ, "SERVICE_ENV_DUPLICATE")
        environ[key] = value
    require(environ == EXPECTED_ENV, "SERVICE_ENV_ALLOWLIST")
    require(os.readlink(proc / "cwd") == "/opt/src/xiaodao", "SERVICE_CWD")
    executable = os.readlink(proc / "exe")
    require(executable.startswith("/opt/uv-python/"), "SERVICE_MANAGED_PYTHON")
    stat_fields = (proc / "stat").read_text(encoding="ascii").split()
    require(len(stat_fields) > 21, "SERVICE_STARTTIME_PROC")
    require(stat_fields[21] == expected_starttime, "SERVICE_STARTTIME")
    return {
        "capabilities_ambient": "none",
        "capabilities_effective": "none",
        "capabilities_inheritable": "none",
        "capabilities_permitted": "none",
        "command_exact": True,
        "cwd": "/opt/src/xiaodao",
        "environment_key_count": len(environ),
        "environment_matches_exact_allowlist": True,
        "gid": SERVICE_GID,
        "inspection_groups_empty": True,
        "inspection_identity_matches_service": True,
        "managed_python": True,
        "no_new_privileges": True,
        "pid": int(pid_text),
        "schema_version": 2,
        "starttime_matches_root_receipt": True,
        "uid": SERVICE_UID,
    }


def canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("ascii")


def child_main(write_fd: int, pid_text: str, expected_starttime: str) -> None:
    try:
        os.setgroups([])
        os.setgid(SERVICE_GID)
        os.setuid(SERVICE_UID)
        summary = inspect_as_service_user(pid_text, expected_starttime)
        message = canonical_bytes({"ok": True, "summary": summary})
        require(len(message) <= MAX_PIPE_BYTES, "SERVICE_PIPE_MESSAGE_SIZE")
        write_all(write_fd, message)
        os.close(write_fd)
        os._exit(0)
    except BaseException:
        error = canonical_bytes({"error": "SERVICE_CHILD_VERIFICATION_FAILED", "ok": False})
        try:
            write_all(write_fd, error)
            os.close(write_fd)
        except BaseException:
            pass
        os._exit(70)


def terminate_and_reap(child_pid: int) -> None:
    try:
        os.kill(child_pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 1.0
    while True:
        try:
            waited_pid, _ = os.waitpid(child_pid, os.WNOHANG)
        except ChildProcessError:
            return
        if waited_pid == child_pid:
            return
        require(time.monotonic() < deadline, "SERVICE_CHILD_REAP_TIMEOUT")
        time.sleep(0.01)


def inspect_via_same_uid_child(
    pid_text: str,
    expected_starttime: str,
    close_in_child: int | None = None,
) -> dict[str, object]:
    read_fd, write_fd = os.pipe()
    try:
        child_pid = os.fork()
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        raise
    if child_pid == 0:
        os.close(read_fd)
        if close_in_child is not None:
            os.close(close_in_child)
        child_main(write_fd, pid_text, expected_starttime)
        raise AssertionError("unreachable")
    os.close(write_fd)
    deadline = time.monotonic() + VERIFY_TIMEOUT_SECONDS
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            remaining = deadline - time.monotonic()
            require(remaining > 0, "SERVICE_CHILD_READ_TIMEOUT")
            readable, _, _ = select.select([read_fd], [], [], remaining)
            require(readable == [read_fd], "SERVICE_CHILD_READ_TIMEOUT")
            chunk = os.read(read_fd, min(4096, MAX_PIPE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            require(total <= MAX_PIPE_BYTES, "SERVICE_CHILD_MESSAGE_SIZE")
    except BaseException:
        os.close(read_fd)
        terminate_and_reap(child_pid)
        raise
    os.close(read_fd)
    while True:
        waited_pid, wait_status = os.waitpid(child_pid, os.WNOHANG)
        if waited_pid == child_pid:
            break
        if time.monotonic() >= deadline:
            terminate_and_reap(child_pid)
            raise RuntimeError("SERVICE_CHILD_WAIT_TIMEOUT")
        time.sleep(0.01)
    require(os.WIFEXITED(wait_status), "SERVICE_CHILD_NOT_EXITED")
    require(os.WEXITSTATUS(wait_status) == 0, "SERVICE_CHILD_EXIT")
    raw = b"".join(chunks)
    require(raw.endswith(b"\n") and raw.count(b"\n") == 1, "SERVICE_CHILD_FRAMING")
    decoded = json.loads(raw.decode("ascii"))
    require(canonical_bytes(decoded) == raw, "SERVICE_CHILD_NOT_CANONICAL")
    require(isinstance(decoded, dict), "SERVICE_CHILD_ENVELOPE")
    require(set(decoded) == {"ok", "summary"}, "SERVICE_CHILD_ENVELOPE")
    require(decoded.get("ok") is True, "SERVICE_CHILD_RESULT")
    summary = decoded.get("summary")
    require(isinstance(summary, dict), "SERVICE_CHILD_SUMMARY")
    require(summary.get("environment_key_count") == len(EXPECTED_ENV), "SERVICE_CHILD_ENV_COUNT")
    require(summary.get("pid") == int(pid_text), "SERVICE_CHILD_PID")
    require(summary.get("uid") == SERVICE_UID, "SERVICE_CHILD_UID")
    require(summary.get("gid") == SERVICE_GID, "SERVICE_CHILD_GID")
    return summary


def inspect() -> dict[str, object]:
    pid_text, expected_starttime = read_process_receipts()
    return inspect_via_same_uid_child(pid_text, expected_starttime)


def terminate_service() -> None:
    pid_text, expected_starttime = read_process_receipts()
    require(hasattr(os, "pidfd_open"), "SERVICE_PIDFD_UNAVAILABLE")
    require(hasattr(signal, "pidfd_send_signal"), "SERVICE_PIDFD_SIGNAL_UNAVAILABLE")
    pidfd = os.pidfd_open(int(pid_text), 0)
    try:
        inspect_via_same_uid_child(pid_text, expected_starttime, close_in_child=pidfd)
        signal.pidfd_send_signal(pidfd, signal.SIGTERM, None, 0)
        readable, _, _ = select.select([pidfd], [], [], TERMINATE_TIMEOUT_SECONDS)
        require(readable == [pidfd], "SERVICE_PIDFD_EXIT_TIMEOUT")
    finally:
        os.close(pidfd)


def verify_structured_lifecycle() -> None:
    pid_text, _ = read_process_receipts()
    expected_pid = int(pid_text)
    require(SERVICE_LOG_FILE.is_file(), "SERVICE_LOG_ABSENT")
    raw = SERVICE_LOG_FILE.read_bytes()
    require(0 < len(raw) <= MAX_SERVICE_LOG_BYTES, "SERVICE_LOG_SIZE")
    expected_messages = [
        f"Started server process [{expected_pid}]",
        "Application startup complete.",
        "Shutting down",
        "Application shutdown complete.",
        f"Finished server process [{expected_pid}]",
    ]
    observed: list[tuple[int, str]] = []
    for line_number, line in enumerate(raw.splitlines(), start=1):
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict) or record.get("logger") != "uvicorn.error":
            continue
        message = record.get("message")
        if message not in expected_messages:
            continue
        require(record.get("event") == "uvicorn.error", "SERVICE_LIFECYCLE_EVENT")
        require(record.get("level") == "INFO", "SERVICE_LIFECYCLE_LEVEL")
        require(record.get("process_id") == expected_pid, "SERVICE_LIFECYCLE_PID")
        observed.append((line_number, message))
    require(
        [message for _, message in observed] == expected_messages,
        "SERVICE_LIFECYCLE_EXACT_ORDER",
    )
    require(
        all(observed[index][0] < observed[index + 1][0] for index in range(4)),
        "SERVICE_LIFECYCLE_LINE_ORDER",
    )


def record_process_evidence(payload: dict[str, object]) -> None:
    create_root_evidence(PROCESS_EVIDENCE_FILE, canonical_bytes(payload))


def record_launch_evidence() -> None:
    create_root_evidence(
        LAUNCH_EVIDENCE_FILE,
        (
            "supervisor_user=root\n"
            "service_user=plagent\n"
            "service_uid=10001\n"
            "service_gid=10001\n"
            "service_environment=env-i-exact-allowlist\n"
            "service_no_new_privileges=true\n"
            "service_log_capture=root-mode-0600\n"
            "service_log_evidence_install=only-after-exact-secret-scan\n"
        ).encode("ascii"),
    )


def expected_exit_evidence(pid_text: str) -> bytes:
    return (
        f"service_pid={pid_text}\n"
        "service_exit_code=143\n"
        "graceful_sigterm=true\n"
        "lifecycle_exact_unique_ordered=true\n"
        "service_log_secret_scan=pass\n"
        "service_log_archived=true\n"
    ).encode("ascii")


def record_exit_evidence(status_text: str) -> None:
    require(status_text == "143", "SERVICE_EXIT_STATUS")
    pid_raw = root_receipt_text(PID_FILE)
    require(pid_raw.endswith("\n") and pid_raw.count("\n") == 1, "SERVICE_PID_FRAMING")
    pid_text = pid_raw[:-1]
    require(pid_text.isdecimal() and int(pid_text) > 1, "SERVICE_PID_FORMAT")
    create_root_evidence(EXIT_EVIDENCE_FILE, expected_exit_evidence(pid_text))


def record_stop_evidence() -> None:
    pid_raw = root_receipt_text(PID_FILE)
    require(pid_raw.endswith("\n") and pid_raw.count("\n") == 1, "SERVICE_PID_FRAMING")
    pid_text = pid_raw[:-1]
    require(pid_text.isdecimal() and int(pid_text) > 1, "SERVICE_PID_FORMAT")
    require(root_receipt_text(EXIT_EVIDENCE_FILE).encode("ascii") == expected_exit_evidence(pid_text), "SERVICE_EXIT_RECEIPT")
    create_root_evidence(
        STOP_EVIDENCE_FILE,
        (
            "signal=TERM\n"
            "target=verified-pidfd-for-exact-service-starttime\n"
            "forced_kill=false\n"
            "service_exit_code=143\n"
            "graceful_sigterm=true\n"
            "lifecycle_exact_unique_ordered=true\n"
            "service_log_secret_scan=pass\n"
        ).encode("ascii"),
    )


def verify_with_retry() -> dict[str, object]:
    deadline = time.monotonic() + VERIFY_TIMEOUT_SECONDS
    last_error = "SERVICE_PROCESS_NOT_READY"
    while time.monotonic() < deadline:
        try:
            return inspect()
        except (OSError, RuntimeError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            time.sleep(0.1)
    raise RuntimeError(last_error)


def main() -> None:
    args = sys.argv[1:]
    require(bool(args), "SERVICE_VERIFY_MODE")
    mode = args[0]
    if mode == "launch" and len(args) == 1:
        record_launch_evidence()
    elif mode == "record" and len(args) == 1:
        record_process_evidence(verify_with_retry())
    elif mode == "check" and len(args) == 1:
        verify_with_retry()
    elif mode == "terminate" and len(args) == 1:
        terminate_service()
    elif mode == "lifecycle" and len(args) == 1:
        verify_structured_lifecycle()
    elif mode == "exit" and len(args) == 2:
        record_exit_evidence(args[1])
    elif mode == "stop" and len(args) == 1:
        record_stop_evidence()
    elif mode == "archive-log" and len(args) == 1:
        archive_service_log()
    else:
        raise RuntimeError("SERVICE_VERIFY_MODE")


try:
    main()
except Exception:
    raise SystemExit("SERVICE_PROCESS_VERIFICATION_FAILED") from None
