from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / "src" / "problem_locator"
SKILL_ROOT = REPO_ROOT / ".claude" / "skills"
SKILL_DIRS = (
    SKILL_ROOT / "wiki-to-diagnosis-skill",
    SKILL_ROOT / "logparse-diagnose",
    SKILL_ROOT / "diagnose-service-takeover",
)
LOGPARSE_ROOT = SRC_ROOT / "integrations" / "logparse"

# Archive APIs remain forbidden for uploaded-input handling. These are the
# exact user-result builder/validator seams; neither extracts an input archive.
CONTROLLED_ARCHIVE_ALLOWLIST = {
    (
        "src/problem_locator/integrations/result_archive.py",
        "<module>",
    ): frozenset({"import:zipfile"}),
    (
        "src/problem_locator/integrations/result_archive.py",
        "_zip_info",
    ): frozenset({"call:zipfile.ZipInfo"}),
    (
        "src/problem_locator/integrations/result_archive.py",
        "build_result_archive",
    ): frozenset(
        {
            "call:zipfile.ZipFile",
            "call:archive.writestr",
        }
    ),
    (
        "src/problem_locator/integrations/result_archive.py",
        "validate_result_archive_bytes",
    ): frozenset(
        {
            "call:zipfile.ZipFile",
            "call:archive.infolist",
            "call:archive.read",
        }
    ),
    (
        ".claude/skills/wiki-to-diagnosis-skill/scripts/validate_generated_skill.py",
        "<module>",
    ): frozenset({"import:zipfile"}),
    (
        ".claude/skills/wiki-to-diagnosis-skill/scripts/validate_generated_skill.py",
        "validate_result_zip",
    ): frozenset(
        {
            "call:zipfile.ZipFile",
            "call:archive.infolist",
            "call:archive.read",
        }
    ),
}

ARCHIVE_MODULES = frozenset({"zipfile", "tarfile", "gzip"})
ARCHIVE_CALLS = frozenset(
    {
        "zipfile.ZipFile",
        "zipfile.ZipInfo",
        "tarfile.open",
        "gzip.open",
        "shutil.unpack_archive",
        "archive.infolist",
        "archive.writestr",
        "archive.extract",
        "archive.extractall",
        "archive.open",
        "archive.read",
    }
)


def _relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _python_files() -> list[Path]:
    roots = (SRC_ROOT, *SKILL_DIRS)
    return sorted(
        path
        for root in roots
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _markdown_files() -> list[Path]:
    return sorted(path for root in SKILL_DIRS for path in root.rglob("*.md"))


def _qualified_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


@dataclass(frozen=True)
class _Use:
    path: str
    function: str
    operation: str
    line: int


class _ArchiveVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = _relative(path)
        self.function = "<module>"
        self.uses: list[_Use] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        previous = self.function
        self.function = node.name
        self.generic_visit(node)
        self.function = previous

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".", 1)[0]
            if root in ARCHIVE_MODULES:
                self.uses.append(
                    _Use(self.path, self.function, f"import:{root}", node.lineno)
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        root = (node.module or "").split(".", 1)[0]
        if root in ARCHIVE_MODULES:
            self.uses.append(
                _Use(self.path, self.function, f"import:{root}", node.lineno)
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _qualified_name(node.func)
        if name in ARCHIVE_CALLS:
            self.uses.append(
                _Use(self.path, self.function, f"call:{name}", node.lineno)
            )
        self.generic_visit(node)


@dataclass(frozen=True)
class _ProcessCall:
    path: str
    function: str
    name: str
    line: int
    node: ast.Call


class _ProcessVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = _relative(path)
        self.function = "<module>"
        self.calls: list[_ProcessCall] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        previous = self.function
        self.function = node.name
        self.generic_visit(node)
        self.function = previous

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        name = _qualified_name(node.func)
        if name in {
            "subprocess.Popen",
            "subprocess.run",
            "os.system",
            "os.popen",
        }:
            self.calls.append(
                _ProcessCall(self.path, self.function, name, node.lineno, node)
            )
        self.generic_visit(node)


def _keyword(call: ast.Call, name: str) -> ast.AST | None:
    return next((item.value for item in call.keywords if item.arg == name), None)


def _string_constants(node: ast.AST) -> set[str]:
    return {
        item.value
        for item in ast.walk(node)
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    }


def _paragraphs(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]


def test_archive_apis_are_absent_except_controlled_user_result_seams() -> None:
    actual: dict[tuple[str, str], set[str]] = {}
    uses: list[_Use] = []
    for path in _python_files():
        visitor = _ArchiveVisitor(path)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        uses.extend(visitor.uses)

    unexpected = []
    for use in uses:
        key = (use.path, use.function)
        allowed = CONTROLLED_ARCHIVE_ALLOWLIST.get(key, frozenset())
        if use.operation not in allowed:
            unexpected.append(use)
        actual.setdefault(key, set()).add(use.operation)

    assert unexpected == []
    assert actual == {
        key: set(operations) for key, operations in CONTROLLED_ARCHIVE_ALLOWLIST.items()
    }


def test_no_shell_or_search_command_can_scan_workspace_inputs() -> None:
    process_calls: list[_ProcessCall] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        visitor = _ProcessVisitor(path)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        process_calls.extend(visitor.calls)

    assert all(call.name not in {"os.system", "os.popen"} for call in process_calls)
    for call in process_calls:
        constants = {value.casefold() for value in _string_constants(call.node)}
        assert not constants.intersection({"grep", "rg", "findstr"}), call
        shell = _keyword(call.node, "shell")
        assert not (
            isinstance(shell, ast.Constant) and shell.value is True
        ), call

    for path in _markdown_files():
        for paragraph in _paragraphs(path.read_text(encoding="utf-8")):
            if not re.search(r"\bgrep\b|\brg\b|findstr|扫描.*(?:inputs|归档)", paragraph):
                continue
            assert re.search(
                r"不得|禁止|严禁|不能|不使用|不读|不直接|不打开|不用|不接受|不把|绝不|"
                r"do not|never|forbid",
                paragraph,
                flags=re.IGNORECASE,
            ), f"archive/input search is not negated in {_relative(path)}: {paragraph}"


def test_only_the_controlled_executor_can_spawn_the_fixed_logparse_process() -> None:
    calls: list[_ProcessCall] = []
    # Other slices may legitimately own unrelated subprocess adapters (for
    # example the Agent Backend).  This invariant is intentionally scoped to
    # S07's integration package; the raw-logparse argv scan below remains
    # repository-wide and catches any broker bypass.
    for path in sorted(LOGPARSE_ROOT.rglob("*.py")):
        visitor = _ProcessVisitor(path)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        calls.extend(visitor.calls)

    subprocess_calls = {
        (call.path, call.function, call.name) for call in calls if "subprocess." in call.name
    }
    assert subprocess_calls == {
        (
            "src/problem_locator/integrations/logparse/fingerprint.py",
            "_git_paths",
            "subprocess.run",
        ),
        (
            "src/problem_locator/integrations/logparse/fingerprint.py",
            "_python_version",
            "subprocess.run",
        ),
        (
            "src/problem_locator/integrations/logparse/process.py",
            "terminate_process_tree",
            "subprocess.run",
        ),
        (
            "src/problem_locator/integrations/logparse/process.py",
            "run",
            "subprocess.Popen",
        ),
    }

    popen = next(call for call in calls if call.name == "subprocess.Popen")
    shell = _keyword(popen.node, "shell")
    assert isinstance(shell, ast.Constant) and shell.value is False
    assert _qualified_name(_keyword(popen.node, "env")) is None
    assert "sanitized_logparse_environment()" in ast.unparse(
        _keyword(popen.node, "env") or ast.Constant(value=None)
    )
    assert "list(argv)" in ast.unparse(popen.node)

    git_call = next(call for call in calls if call.function == "_git_paths")
    assert {
        "git",
        "-C",
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
    } <= _string_constants(git_call.node)
    version_call = next(call for call in calls if call.function == "_python_version")
    assert "--version" in _string_constants(version_call.node)
    taskkill_call = next(call for call in calls if call.function == "terminate_process_tree")
    assert "taskkill" in _string_constants(taskkill_call.node)


def test_raw_logparse_argv_is_owned_only_by_the_broker_adapter() -> None:
    owners: list[tuple[str, int, set[str]]] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.List, ast.Tuple)):
                continue
            constants = _string_constants(node)
            if "mech-target-logs" in constants or (
                "parse" in constants and "cli.py" in constants
            ):
                owners.append((_relative(path), node.lineno, constants))

    assert all(
        path == "src/problem_locator/integrations/logparse/broker.py"
        for path, _line, _constants in owners
    ), owners
    broker_path = LOGPARSE_ROOT / "broker.py"
    assert broker_path.is_file()
    combined = set().union(*(constants for _path, _line, constants in owners))
    assert "parse" in combined
    assert "mech-target-logs" in combined
    assert {"-c", "-o"} <= combined
    assert '"--product"' in broker_path.read_text(encoding="utf-8")
    assert {
        "--output",
        "--problem-time",
        "--module",
        "--slot",
        "--process-name",
    } <= combined
    assert "--debug-expand-gz" not in combined
    assert "--profile" not in combined
    assert "--keep-workspace" not in combined

    agent_stub = (LOGPARSE_ROOT / "cli.py").read_text(encoding="utf-8")
    for raw_name in ("LOGPARSE_REPO", "LOGPARSE_CONFIG_PATH", "LOGPARSE_PYTHON"):
        assert raw_name not in agent_stub
    assert "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT" in agent_stub
    assert "PROBLEM_LOCATOR_LOGPARSE_TOKEN" in agent_stub
