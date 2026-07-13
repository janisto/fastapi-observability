import ast
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_legacy_httpx_cannot_reenter_repository_dependencies_or_tests():
    legacy_name = "http" + "x"
    legacy_pytest_name = "pytest-" + legacy_name
    pyproject = (ROOT / "pyproject.toml").read_text()
    lockfile = (ROOT / "uv.lock").read_text()

    direct_dependency = rf'^\s*"(?:{re.escape(legacy_name)}|{re.escape(legacy_pytest_name)})(?:\[|["<>=!~ ])'
    assert re.search(direct_dependency, pyproject, re.MULTILINE) is None
    assert f'\nname = "{legacy_name}"\n' not in lockfile
    assert f'\nname = "{legacy_pytest_name}"\n' not in lockfile
    assert '\nname = "httpx2"\n' in lockfile

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
