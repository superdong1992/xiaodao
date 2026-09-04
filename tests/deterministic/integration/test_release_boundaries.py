from __future__ import annotations

import ast
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[3]
BUSINESS_ROOTS = (
    ROOT / "src/problem_locator/domain",
    ROOT / "src/problem_locator/application",
)
EXPECTED_ENV_KEYS = {
    "BIND_HOST",
    "CLAUDE_COMMAND",
    "DATA_ROOT",
    "DFX_LOG_LEVEL",
    "GENERIC_SKILL_NAME",
    "DFX_LOG_DIR",
    "SPECIALIZED_REVIEWER_ENABLED",
    "LOGPARSE_CONFIG_PATH",
    "LOGPARSE_PYTHON",
    "LOGPARSE_REPO",
    "PORT",
    "PUBLIC_BASE_URL",
    "SKILL_DIR",
}
EXPECTED_TEST_ROOTS = [
    "tests/deterministic",
]


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def test_business_layers_do_not_depend_on_json_file_adapters() -> None:
    for root in BUSINESS_ROOTS:
        for path in sorted(root.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            assert "state.json" not in source, path
            assert not any(
                module == "problem_locator.storage"
                or module.startswith("problem_locator.storage.")
                for module in _imported_modules(path)
            ), path


def test_release_metadata_keeps_the_offline_database_boundary() -> None:
    configuration = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    dependencies = configuration["project"]["dependencies"]
    assert not any(
        marker in dependency.lower()
        for dependency in dependencies
        for marker in ("postgres", "psycopg", "sqlalchemy", "asyncpg")
    )
    assert configuration["tool"]["pytest"]["ini_options"]["testpaths"] == (
        EXPECTED_TEST_ROOTS
    )

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    required_release_boundaries = (
        "State、Job 和权威 Outcome 已硬切到 V9",
        "Replay every durable, finalized but unconfirmed Job Outcome",
        "`state.json` approaches 16 MiB",
        "retained history approaches 500 Cases",
        "second service instance or high availability",
        "keep the original JSON root read-only",
    )
    for statement in required_release_boundaries:
        assert statement in readme


def test_env_example_contains_only_the_public_settings() -> None:
    assignments = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        assert separator == "=" and key and value
        assignments[key] = value

    assert set(assignments) == EXPECTED_ENV_KEYS
    forbidden_fragments = (
        "JOB_CONCURRENCY",
        "_LIMIT_",
        "_MAX_",
        "_RETENTION_",
        "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT",
        "PROBLEM_LOCATOR_LOGPARSE_TOKEN",
    )
    assert all(
        fragment not in key
        for key in assignments
        for fragment in forbidden_fragments
    )
    assert all(
        value.startswith("/")
        for key, value in assignments.items()
        if key
        in {
            "DATA_ROOT",
            "SKILL_DIR",
            "LOGPARSE_REPO",
            "LOGPARSE_CONFIG_PATH",
            "LOGPARSE_PYTHON",
            "DFX_LOG_DIR",
        }
    )
