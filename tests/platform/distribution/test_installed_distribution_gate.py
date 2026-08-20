from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import time
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import pytest

from problem_locator.contracts import (
    ReadinessReport,
    StateExport,
    ValidationReport,
    canonical_json_bytes,
)


ROOT = Path(__file__).resolve().parents[3]
TAKEOVER_PRODUCT_HASH = (
    "7f0447460e4a56f882a1f46493ceb645930c0a527bccb303c7929a1d7b3cbe9e"
)
TAKEOVER_SKILL = (
    ROOT
    / "tests"
    / "fixtures"
    / "components"
    / "diagnosis-generator"
    / "diagnose-service-takeover"
)
OFFICIAL_KEYS = {
    "BIND_HOST",
    "CLAUDE_COMMAND",
    "DATA_ROOT",
    "GENERIC_SKILL_NAME",
    "LOGPARSE_CONFIG_PATH",
    "LOGPARSE_PYTHON",
    "LOGPARSE_REPO",
    "PORT",
    "PUBLIC_BASE_URL",
    "SKILL_DIR",
}
EXPECTED_RUNTIME_VERSIONS = {
    "fastapi": "0.139.2",
    "httpx": "0.28.1",
    "mcp": "1.29.0",
    "problem-locator": "4.0.0",
    "pydantic": "2.13.4",
    "python-dotenv": "1.2.2",
    "starlette": "1.3.1",
    "uvicorn": "0.49.0",
}
EXPECTED_MCP_TOOL_NAMES = [
    "problem_locator_create_case",
    "problem_locator_prepare_attachment",
    "problem_locator_submit_supplement",
    "problem_locator_get_case",
    "problem_locator_resume_case",
    "problem_locator_cancel_case",
    "problem_locator_list_artifacts",
]


pytestmark = pytest.mark.skipif(
    os.environ.get("TEST_FLOW_INSTALLED_DISTRIBUTION_GATE") != "1",
    reason="requires explicit TEST_FLOW_INSTALLED_DISTRIBUTION_GATE=1 opt-in",
)


def _outside_environment() -> dict[str, str]:
    environ = os.environ.copy()
    for key in (
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "UV_PROJECT_ENVIRONMENT",
        "VIRTUAL_ENV",
    ):
        environ.pop(key, None)
    environ["PYTHONNOUSERSITE"] = "1"
    return environ


def _diagnostic_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _run_checked(
    command: list[str],
    *,
    cwd: Path,
    environ: dict[str, str],
    label: str,
    timeout_seconds: float = 120.0,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=environ,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(
            f"{label} timed out after {timeout_seconds:g}s; "
            f"stdout={_diagnostic_text(error.stdout)[-4000:]!r}; "
            f"stderr={_diagnostic_text(error.stderr)[-4000:]!r}"
        )
    if completed.returncode != 0:
        pytest.fail(
            f"{label} failed with exit {completed.returncode}; "
            f"command={command!r}; stdout={completed.stdout[-6000:]!r}; "
            f"stderr={completed.stderr[-6000:]!r}"
        )
    return completed


def _run_installed_cli(
    python: Path,
    arguments: list[str],
    *,
    cwd: Path,
    environ: dict[str, str],
    label: str,
) -> subprocess.CompletedProcess[bytes]:
    command = [os.fspath(python), "-I", "-m", "problem_locator", *arguments]
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=environ,
            check=False,
            capture_output=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(
            f"{label} timed out; stdout="
            f"{_diagnostic_text(error.stdout)[-4000:]!r}; stderr="
            f"{_diagnostic_text(error.stderr)[-4000:]!r}"
        )
    if completed.returncode != 0:
        pytest.fail(
            f"{label} failed with exit {completed.returncode}; "
            f"command={command!r}; stdout="
            f"{_diagnostic_text(completed.stdout)[-6000:]!r}; stderr="
            f"{_diagnostic_text(completed.stderr)[-6000:]!r}"
        )
    return completed


def _required_executable(value: str, label: str) -> Path:
    path = Path(value)
    assert path.is_absolute(), f"{label} must be an absolute path: {path}"
    assert path.is_file(), f"{label} is not an ordinary file: {path}"
    assert os.access(path, os.X_OK), f"{label} is not executable: {path}"
    return path


def _discover_uv() -> Path:
    configured = os.environ.get("TEST_FLOW_UV")
    if configured:
        return _required_executable(configured, "TEST_FLOW_UV")
    discovered = shutil.which("uv")
    assert discovered is not None, "set TEST_FLOW_UV or put uv on PATH"
    return _required_executable(os.path.abspath(discovered), "uv")


def _discover_cpython312(
    uv: Path,
    outside_cwd: Path,
    environ: dict[str, str],
) -> Path:
    configured = os.environ.get("TEST_FLOW_PYTHON_312")
    if configured:
        candidate = _required_executable(configured, "TEST_FLOW_PYTHON_312")
    elif sys.version_info[:2] == (3, 12) and sys.implementation.name == "cpython":
        candidate = _required_executable(sys.executable, "current CPython")
    else:
        located = _run_checked(
            [
                os.fspath(uv),
                "python",
                "find",
                "--no-python-downloads",
                "3.12",
            ],
            cwd=outside_cwd,
            environ=environ,
            label="CPython 3.12 discovery",
        ).stdout.strip()
        candidate = _required_executable(located, "discovered CPython 3.12")

    version = _run_checked(
        [
            os.fspath(candidate),
            "-I",
            "-c",
            (
                "import json,platform,sys;"
                "print(json.dumps([platform.python_implementation(),"
                "sys.version_info.major,sys.version_info.minor]))"
            ),
        ],
        cwd=outside_cwd,
        environ=environ,
        label="CPython 3.12 verification",
    )
    assert json.loads(version.stdout) == ["CPython", 3, 12]
    return candidate


def _uv_environment() -> dict[str, str]:
    environ = _outside_environment()
    offline = os.environ.get("TEST_FLOW_UV_OFFLINE", "0")
    assert offline in {"0", "1"}, "TEST_FLOW_UV_OFFLINE must be 0 or 1"
    if offline == "1":
        environ["UV_OFFLINE"] = "1"
    else:
        environ.pop("UV_OFFLINE", None)
    environ["UV_NO_PROGRESS"] = "1"
    environ["UV_PYTHON_DOWNLOADS"] = "never"
    environ["UV_LINK_MODE"] = "copy"
    cache = os.environ.get("TEST_FLOW_UV_CACHE_DIR")
    if cache:
        cache_path = Path(cache)
        assert cache_path.is_absolute(), "TEST_FLOW_UV_CACHE_DIR must be absolute"
        environ["UV_CACHE_DIR"] = os.fspath(cache_path)
    return environ


def _venv_python(venv: Path) -> Path:
    relative = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    return venv / relative


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _dotenv_value(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _write_env_file(path: Path, values: dict[str, str]) -> None:
    assert set(values) == OFFICIAL_KEYS
    path.write_text(
        "".join(
            f"{key}={_dotenv_value(values[key])}\n" for key in sorted(values)
        ),
        encoding="utf-8",
    )


def _wait_for_json(
    process: subprocess.Popen[str],
    url: str,
    *,
    timeout_seconds: float = 30.0,
) -> tuple[int, dict[str, object]]:
    deadline = time.monotonic() + timeout_seconds
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            stdout, stderr = process.communicate(timeout=1)
            raise AssertionError(
                f"installed service exited before readiness (exit={exit_code}); "
                f"stdout={stdout[-4000:]!r}; stderr={stderr[-6000:]!r}"
            )
        try:
            with urlopen(url, timeout=0.5) as response:  # noqa: S310 - loopback gate
                return response.status, json.loads(response.read())
        except HTTPError as error:
            if url.endswith("/ready") and error.code == 503:
                last_error = error
                time.sleep(0.05)
                continue
            try:
                payload = json.loads(error.read())
            except (UnicodeDecodeError, ValueError):
                payload = {}
            return error.code, payload
        except (OSError, URLError, ValueError) as error:
            last_error = error
            time.sleep(0.05)
    raise AssertionError(
        "installed loopback endpoint did not become ready; "
        f"last_error={type(last_error).__name__}: {last_error}"
    )


def _stop_service(process: subprocess.Popen[str]) -> tuple[str, str]:
    if process.poll() is None:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.send_signal(signal.SIGINT)
    try:
        stdout, stderr = process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
        pytest.fail(
            "installed service did not complete bounded SIGINT shutdown; "
            f"stdout={stdout[-4000:]!r}; stderr={stderr[-6000:]!r}"
        )
    assert process.returncode == 0, (
        f"installed service exited with {process.returncode}; "
        f"stdout={stdout[-4000:]!r}; stderr={stderr[-6000:]!r}"
    )
    return stdout, stderr


def _real_asset_paths(
    outside_cwd: Path,
    environ: dict[str, str],
) -> tuple[Path, Path, Path, Path]:
    configured: dict[str, Path] = {}
    for name in (
        "SKILL_DIR",
        "LOGPARSE_REPO",
        "LOGPARSE_CONFIG_PATH",
        "LOGPARSE_PYTHON",
    ):
        value = os.environ.get(name)
        assert value, f"{name} is required for the installed-distribution gate"
        configured[name] = Path(value)
    skill_dir = configured["SKILL_DIR"]
    logparse_repo = configured["LOGPARSE_REPO"]
    logparse_config = configured["LOGPARSE_CONFIG_PATH"]
    logparse_python = configured["LOGPARSE_PYTHON"]

    for label, path in (
        ("SKILL_DIR", skill_dir),
        ("LOGPARSE_REPO", logparse_repo),
        ("LOGPARSE_CONFIG_PATH", logparse_config),
        ("LOGPARSE_PYTHON", logparse_python),
    ):
        assert path.is_absolute(), f"{label} must be absolute: {path}"
    assert skill_dir.is_dir() and not skill_dir.is_symlink()
    assert logparse_repo.is_dir() and not logparse_repo.is_symlink()
    assert logparse_config.is_file() and not logparse_config.is_symlink()
    assert logparse_python.is_file() and os.access(logparse_python, os.X_OK)

    return skill_dir, logparse_repo, logparse_config, logparse_python


def test_clean_installed_distribution_import_cli_and_server_gate(
    tmp_path: Path,
) -> None:
    outside_cwd = tmp_path / "outside-cwd"
    outside_cwd.mkdir()
    assert ROOT.resolve() not in (
        outside_cwd.resolve(),
        *outside_cwd.resolve().parents,
    )
    uv = _discover_uv()
    uv_environ = _uv_environment()
    python312 = _discover_cpython312(uv, outside_cwd, uv_environ)
    _run_checked(
        [os.fspath(uv), "--version"],
        cwd=outside_cwd,
        environ=uv_environ,
        label="uv verification",
    )
    skill_dir, logparse_repo, logparse_config, logparse_python = (
        _real_asset_paths(outside_cwd, _outside_environment())
    )
    assert TAKEOVER_SKILL.is_dir() and not TAKEOVER_SKILL.is_symlink()

    wheelhouse = tmp_path / "wheelhouse"
    _run_checked(
        [
            os.fspath(uv),
            "build",
            "--wheel",
            "--no-build-isolation",
            "--python",
            os.fspath(python312),
            "--out-dir",
            os.fspath(wheelhouse),
            os.fspath(ROOT),
        ],
        cwd=outside_cwd,
        environ=uv_environ,
        label="release-candidate wheel build",
    )
    wheels = sorted(wheelhouse.glob("problem_locator-*.whl"))
    assert len(wheels) == 1, f"expected one Problem Locator wheel, got {wheels!r}"
    wheel = wheels[0]
    with zipfile.ZipFile(wheel) as distribution:
        archive_names = distribution.namelist()
        assert all(not name.endswith(".ps1") for name in archive_names)
        assert all("client-hooks-settings" not in name for name in archive_names)
        assert all("client-dfx" not in name for name in archive_names)
        assert all(
            b"PROBLEM_LOCATOR_CLIENT_DFX_LOG_FILE" not in distribution.read(name)
            for name in archive_names
            if not name.endswith("/")
        )

    requirements = tmp_path / "runtime-requirements.lock"
    _run_checked(
        [
            os.fspath(uv),
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--no-header",
            "--no-annotate",
            "--format",
            "requirements.txt",
            "--output-file",
            os.fspath(requirements),
        ],
        cwd=ROOT,
        environ=uv_environ,
        label="locked runtime dependency export",
    )
    requirement_bytes = requirements.read_bytes()
    assert b"--hash=sha256:" in requirement_bytes
    assert b"problem-locator" not in requirement_bytes
    assert b"pytest==" not in requirement_bytes
    assert b"hatchling==" not in requirement_bytes

    # Formal Release installs the product into the sealed image runtime; it
    # does not reconstruct all third-party dependencies from an incidental uv
    # cache. Clone that runtime, then exact-sync the exported production lock
    # so this probe is isolated and contains no build or test dependencies.
    sealed_runtime = python312.parent.parent
    assert (sealed_runtime / "pyvenv.cfg").is_file()
    venv = tmp_path / "installed-venv"
    shutil.copytree(sealed_runtime, venv, symlinks=True)
    installed_python = _venv_python(venv)
    assert installed_python.is_file()
    _run_checked(
        [
            os.fspath(uv),
            "pip",
            "sync",
            "--python",
            os.fspath(installed_python),
            "--require-hashes",
            "--strict",
            os.fspath(requirements),
        ],
        cwd=outside_cwd,
        environ=uv_environ,
        label="strict sealed-runtime production dependency sync",
    )
    _run_checked(
        [
            os.fspath(uv),
            "pip",
            "install",
            "--python",
            os.fspath(installed_python),
            "--no-deps",
            "--strict",
            os.fspath(wheel),
        ],
        cwd=outside_cwd,
        environ=uv_environ,
        label="release-candidate wheel installation",
    )
    _run_checked(
        [
            os.fspath(uv),
            "pip",
            "check",
            "--python",
            os.fspath(installed_python),
        ],
        cwd=outside_cwd,
        environ=uv_environ,
        label="installed runtime dependency verification",
    )
    proxy_entrypoint = installed_python.parent / (
        "problem-locator-client-proxy.exe"
        if os.name == "nt"
        else "problem-locator-client-proxy"
    )
    assert not proxy_entrypoint.exists()
    finalizer_entrypoint = installed_python.parent / (
        "problem-locator-seal-outcome-draft.exe"
        if os.name == "nt"
        else "problem-locator-seal-outcome-draft"
    )
    assert finalizer_entrypoint.is_file()

    probe_code = textwrap.dedent(
        """
        import importlib.metadata
        import json
        import os
        import pathlib
        import sys
        import sysconfig

        import problem_locator
        from problem_locator.runtime.catalog import hash_product_directory

        names = (
            "fastapi",
            "httpx",
            "mcp",
            "problem-locator",
            "pydantic",
            "python-dotenv",
            "starlette",
            "uvicorn",
        )
        installed_names = {
            distribution.metadata["Name"].lower()
            for distribution in importlib.metadata.distributions()
        }
        print(json.dumps({
            "cwd": os.getcwd(),
            "module_file": str(pathlib.Path(problem_locator.__file__).resolve()),
            "purelib": str(pathlib.Path(sysconfig.get_paths()["purelib"]).resolve()),
            "skill_hash": hash_product_directory(pathlib.Path(sys.argv[1])),
            "sys_path": sys.path,
            "versions": {
                name: importlib.metadata.version(name) for name in names
            },
            "has_hatchling": "hatchling" in installed_names,
            "has_pytest": "pytest" in installed_names,
        }, sort_keys=True))
        """
    )
    installed_environ = _outside_environment()
    import_probe = _run_checked(
        [
            os.fspath(installed_python),
            "-I",
            "-c",
            probe_code,
            os.fspath(TAKEOVER_SKILL),
        ],
        cwd=outside_cwd,
        environ=installed_environ,
        label="isolated installed-package import",
    )
    probe = json.loads(import_probe.stdout)
    module_file = Path(probe["module_file"])
    purelib = Path(probe["purelib"])
    assert module_file.is_relative_to(purelib)
    assert purelib.is_relative_to(venv.resolve())
    assert module_file.is_relative_to(ROOT.resolve()) is False
    assert probe["cwd"] == os.fspath(outside_cwd)
    assert all(os.fspath(ROOT.resolve()) not in entry for entry in probe["sys_path"])
    assert probe["versions"] == EXPECTED_RUNTIME_VERSIONS
    assert probe["has_hatchling"] is False
    assert probe["has_pytest"] is False
    assert probe["skill_hash"] == TAKEOVER_PRODUCT_HASH

    data_root = tmp_path / "installed-data"
    export_path = tmp_path / "installed-state-export.json"
    port = _free_loopback_port()
    env_file = tmp_path / "installed-service.env"
    _write_env_file(
        env_file,
        {
            "BIND_HOST": "127.0.0.1",
            "CLAUDE_COMMAND": os.environ.get("CLAUDE_COMMAND", "claude"),
            "DATA_ROOT": os.fspath(data_root),
            "GENERIC_SKILL_NAME": "generic-problem-locator-smoke",
            "LOGPARSE_CONFIG_PATH": os.fspath(logparse_config),
            "LOGPARSE_PYTHON": os.fspath(logparse_python),
            "LOGPARSE_REPO": os.fspath(logparse_repo),
            "PORT": str(port),
            "PUBLIC_BASE_URL": f"http://127.0.0.1:{port}",
            "SKILL_DIR": os.fspath(skill_dir),
        },
    )
    service_environ = _outside_environment()
    for key in OFFICIAL_KEYS:
        service_environ.pop(key, None)
    service_environ.pop("PROBLEM_LOCATOR_LOGPARSE_ENDPOINT", None)
    service_environ.pop("PROBLEM_LOCATOR_LOGPARSE_TOKEN", None)
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    launcher = Path("/evidence/test_service_launcher.py")
    if not launcher.is_file():
        launcher = ROOT / "tools/test-flow/runtime-support/test_service_launcher.py"
    assert launcher.is_file()
    process = subprocess.Popen(
        [
            os.fspath(installed_python),
            "-I",
            os.fspath(launcher),
            "serve",
            "--env-file",
            os.fspath(env_file),
        ],
        cwd=outside_cwd,
        env=service_environ,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=creationflags,
    )
    try:
        live_status, live = _wait_for_json(
            process,
            f"http://127.0.0.1:{port}/live",
        )
        assert live_status == 200
        assert live == {"ok": True, "data": {"status": "live"}, "error": None}

        ready_status, ready = _wait_for_json(
            process,
            f"http://127.0.0.1:{port}/ready",
        )
        assert ready_status == 200, ready
        assert ready["ok"] is True and ready["error"] is None
        report = ReadinessReport.model_validate(ready["data"])
        assert report.ready is True and report.error is None
        assert [check.name for check in report.checks] == [
            "CONFIG",
            "INSTANCE_LOCK",
            "STATE",
            "DATA_DIRECTORIES",
            "RECOVERY",
        ]
        assert all(check.passed and check.message is None for check in report.checks)

        mcp_probe_code = textwrap.dedent(
            """
            import asyncio
            import json
            import sys

            import httpx
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client

            async def probe():
                async with httpx.AsyncClient(trust_env=False) as http_client:
                    async with streamable_http_client(
                        sys.argv[1],
                        http_client=http_client,
                    ) as (read_stream, write_stream, _get_session_id):
                        async with ClientSession(read_stream, write_stream) as session:
                            await session.initialize()
                            listed = await session.list_tools()
                            return {
                                "tool_names": [tool.name for tool in listed.tools],
                                "output_schema_types": [
                                    None
                                    if tool.outputSchema is None
                                    else tool.outputSchema.get("type")
                                    for tool in listed.tools
                                ],
                            }

            print(json.dumps(asyncio.run(probe()), sort_keys=True))
            """
        )
        mcp_probe = _run_checked(
            [
                os.fspath(installed_python),
                "-I",
                "-c",
                mcp_probe_code,
                f"http://127.0.0.1:{port}/mcp",
            ],
            cwd=outside_cwd,
            environ=service_environ,
            label="installed official MCP initialize/list-tools probe",
        )
        mcp_inventory = json.loads(mcp_probe.stdout)
        assert mcp_inventory["tool_names"] == EXPECTED_MCP_TOOL_NAMES
        assert mcp_inventory["output_schema_types"] == ["object"] * len(
            EXPECTED_MCP_TOOL_NAMES
        )
    finally:
        _stop_service(process)

    validated = _run_installed_cli(
        installed_python,
        ["validate-state", "--data-root", os.fspath(data_root)],
        cwd=outside_cwd,
        environ=service_environ,
        label="installed validate-state",
    )
    assert validated.stderr == b""
    validation_report = ValidationReport.model_validate_json(validated.stdout)
    assert validated.stdout == canonical_json_bytes(validation_report)
    assert validation_report.valid is True
    assert validation_report.errors == []

    exported = _run_installed_cli(
        installed_python,
        [
            "export-state",
            "--data-root",
            os.fspath(data_root),
            "--output",
            os.fspath(export_path),
        ],
        cwd=outside_cwd,
        environ=service_environ,
        label="installed export-state",
    )
    assert exported.stdout == b"" and exported.stderr == b""
    export_bytes = export_path.read_bytes()
    state_export = StateExport.model_validate_json(export_bytes)
    assert export_bytes == canonical_json_bytes(state_export)
    assert state_export.object_counts.cases == 0
    assert state_export.object_counts.runtime_epochs == 1
    assert state_export.object_counts.recovery_processing_records == 1
