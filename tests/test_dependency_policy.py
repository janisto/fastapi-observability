import ast
import re
import tomllib
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
FULL_ACTION_VERSION = re.compile(r"^[^@\s]+@v\d+\.\d+\.\d+$")
USES_CLAUSE = re.compile(r"^\s*(?:-\s+)?uses:\s*([^\s#]+)")
WORKFLOW_PATTERNS = ("*.yml", "*.yaml")
GITHUB_REST_CALLER = re.compile(
    r"\bgh\s+api\b|\b(?:github|octokit)\.(?:rest\b|request\s*\(|paginate\s*\()|"
    r"https?://api\.github\.com\b"
)
LOCKED_GITHUB_HEADER = re.compile(r"X-GitHub-Api-Version[\"']?\s*(?::|=|\s)\s*[\"']?2026-03-10\b")
GITHUB_API_HEADER_NAME = re.compile(r"X-GitHub-Api-Version", re.IGNORECASE)
GITHUB_CLIENT_ALIAS = re.compile(
    r"\b(?:const\s+|let\s+|var\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(?::=|=)\s*"
    r"(?:new\s+)?[^\n]*(?:Octokit|GitHub|Github|octokit|github)\b"
)
AUTOMATED_SUFFIXES = {
    ".bash",
    ".cjs",
    ".go",
    ".js",
    ".json",
    ".mjs",
    ".py",
    ".rs",
    ".sh",
    ".toml",
    ".ts",
    ".yaml",
    ".yml",
    ".zsh",
}
SKIPPED_POLICY_DIRECTORIES = {
    ".git",
    ".venv",
    "artifacts",
    "coverage",
    "dist",
    "mutants",
    "node_modules",
    "target",
}


def _workflow_paths(directory: Path) -> list[Path]:
    return sorted(path for pattern in WORKFLOW_PATTERNS for path in directory.glob(pattern))


def _is_automated_policy_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return (
        normalized != "tests/test_dependency_policy.py"
        and not normalized.endswith(".md")
        and (Path(normalized).suffix in AUTOMATED_SUFFIXES or Path(normalized).name == "Justfile")
    )


def _is_github_api_caller(line: str, aliases: set[str]) -> bool:
    return GITHUB_REST_CALLER.search(line) is not None or any(
        re.search(rf"\b{re.escape(alias)}\.(?:rest\b|request\s*\(|paginate\s*\()", line) is not None
        for alias in aliases
    )


def _github_api_policy_violations(files: Mapping[str, str]) -> list[str]:
    violations = []
    for path, content in files.items():
        if not _is_automated_policy_path(path):
            continue
        lines = content.splitlines()
        aliases = set(GITHUB_CLIENT_ALIAS.findall(content))

        for index, line in enumerate(lines):
            if not _is_github_api_caller(line, aliases):
                continue
            limit = min(len(lines), index + 12)
            end = index + 1
            while end < limit and not _is_github_api_caller(lines[end], aliases):
                end += 1
            block = "\n".join(lines[index:end])
            if len(GITHUB_API_HEADER_NAME.findall(block)) != 1 or LOCKED_GITHUB_HEADER.search(block) is None:
                violations.append(f"{path}:{index + 1}")
    return violations


def _repository_policy_files() -> dict[str, str]:
    files = {}
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if any(part in SKIPPED_POLICY_DIRECTORIES for part in relative.parts):
            continue
        relative_text = relative.as_posix()
        if path.is_file() and _is_automated_policy_path(relative_text):
            files[relative_text] = path.read_text()
    return files


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def _distribution_name(requirement: str) -> str:
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
    assert match is not None, f"could not parse dependency name from {requirement!r}"
    return re.sub(r"[-_.]+", "-", match.group().lower())


def test_legacy_httpx_cannot_reenter_dependency_manifests():
    legacy_name = "http" + "x"
    legacy_pytest_name = "pytest-" + legacy_name
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project = pyproject["project"]
    configured_requirements = _strings(
        [
            project.get("dependencies", []),
            project.get("optional-dependencies", {}),
            pyproject.get("dependency-groups", {}),
        ]
    )
    configured_names = {_distribution_name(requirement) for requirement in configured_requirements}

    lockfile = tomllib.loads((ROOT / "uv.lock").read_text())
    locked_names = {package["name"] for package in lockfile["package"]}

    assert legacy_name not in configured_names
    assert legacy_pytest_name not in configured_names
    assert legacy_name not in locked_names
    assert legacy_pytest_name not in locked_names
    assert "httpx2" in configured_names
    assert "httpx2" in locked_names


def test_legacy_httpx_and_framework_test_clients_cannot_be_imported():
    legacy_name = "http" + "x"

    project_paths = [ROOT / "src", ROOT / "tests", ROOT / "examples"]
    framework_test_clients = {"fastapi.test" + "client", "starlette.test" + "client"}
    for directory in project_paths:
        for path in directory.rglob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    assert all(alias.name.partition(".")[0] != legacy_name for alias in node.names), path
                elif isinstance(node, ast.ImportFrom):
                    imported_root = (node.module or "").partition(".")[0]
                    assert imported_root != legacy_name, path
                    assert node.module not in framework_test_clients, path


def test_external_github_actions_use_full_release_version_tags():
    for workflow in _workflow_paths(ROOT / ".github" / "workflows"):
        for line_number, line in enumerate(workflow.read_text().splitlines(), start=1):
            match = USES_CLAUSE.match(line)
            if match is None:
                continue
            action = match.group(1)
            if action.startswith("./"):
                continue
            assert FULL_ACTION_VERSION.fullmatch(action), (
                f"{workflow.relative_to(ROOT)}:{line_number}: external actions must use @vMAJOR.MINOR.PATCH"
            )


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("      uses: actions/checkout@v7.0.0", "actions/checkout@v7.0.0"),
        ("      - uses: actions/checkout@v7.0.0 # inline step", "actions/checkout@v7.0.0"),
    ],
)
def test_uses_clause_recognizes_named_and_inline_step_forms(line, expected):
    match = USES_CLAUSE.match(line)
    assert match is not None
    assert match.group(1) == expected


def test_workflow_discovery_includes_yml_and_yaml(tmp_path):
    (tmp_path / "one.yml").touch()
    (tmp_path / "two.yaml").touch()
    (tmp_path / "ignored.txt").touch()
    assert [path.name for path in _workflow_paths(tmp_path)] == ["one.yml", "two.yaml"]


def test_github_api_policy_passes_without_automated_callers_and_ignores_docs():
    assert _github_api_policy_violations({"README.md": "Use `gh api` with the locally installed CLI."}) == []


def test_github_api_policy_accepts_exact_locked_header():
    content = """github.request("GET /repos/{owner}/{repo}", {
  headers: {"X-GitHub-Api-Version": "2026-03-10"},
})"""
    assert _github_api_policy_violations({"workflow.py": content}) == []


@pytest.mark.parametrize(
    "content",
    [
        'github.request("GET /repos/{owner}/{repo}")',
        'github.request("GET /repos/{owner}/{repo}", headers={"X-GitHub-Api-Version": VERSION})',
        'github.request("GET /repos/{owner}/{repo}", headers={"X-GitHub-Api-Version": "2022-11-28"})',
    ],
    ids=["missing", "dynamic", "different"],
)
def test_github_api_policy_rejects_unpinned_automated_callers(content):
    assert _github_api_policy_violations({"client.py": content}) == ["client.py:1"]


def test_github_api_policy_checks_each_automated_caller():
    content = 'github.request("GET /one", headers={"X-GitHub-Api-Version": "2026-03-10"})\ngithub.request("GET /two")'
    assert _github_api_policy_violations({"client.py": content}) == ["client.py:2"]


def test_github_api_policy_rejects_conflicting_versions_in_one_call():
    content = (
        'github.request("GET /one", headers={"X-GitHub-Api-Version": "2026-03-10"})\n'
        'headers["X-GitHub-Api-Version"] = "2022-11-28"'
    )
    assert _github_api_policy_violations({"client.py": content}) == ["client.py:1"]


def test_github_api_policy_detects_aliased_octokit_caller():
    content = 'const client = new Octokit()\nclient.request("GET /repos/{owner}/{repo}")'
    assert _github_api_policy_violations({"client.py": content}) == ["client.py:2"]


def test_repository_has_no_unpinned_automated_github_rest_caller():
    assert _github_api_policy_violations(_repository_policy_files()) == []
