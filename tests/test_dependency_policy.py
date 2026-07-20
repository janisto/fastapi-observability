import ast
import re
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
FULL_ACTION_VERSION = re.compile(r"^[^@\s]+@v\d+\.\d+\.\d+$")
USES_CLAUSE = re.compile(r"^\s*(?:-\s+)?uses:\s*([^\s#]+)")
WORKFLOW_PATTERNS = ("*.yml", "*.yaml")


def _workflow_paths(directory: Path) -> list[Path]:
    return sorted(path for pattern in WORKFLOW_PATTERNS for path in directory.glob(pattern))


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
