"""Verify built distribution contents and public metadata."""

from __future__ import annotations

import email
import glob
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
EXPECTED_NAME = "fastapi-request-observability"
EXPECTED_VERSION = "0.1.0"
EXPECTED_PYTHON = ">=3.13"
EXPECTED_DEPENDENCIES = ["fastapi>=0.139.0"]
EXPECTED_URLS = {
    "Repository, https://github.com/janisto/fastapi-observability",
    "Issues, https://github.com/janisto/fastapi-observability/issues",
    "Changelog, https://github.com/janisto/fastapi-observability/blob/main/CHANGELOG.md",
}

wheel_paths = glob.glob(str(ROOT / "dist" / "*.whl"))
sdist_paths = glob.glob(str(ROOT / "dist" / "*.tar.gz"))
assert len(wheel_paths) == 1, f"expected one wheel, found {wheel_paths}"
assert len(sdist_paths) == 1, f"expected one sdist, found {sdist_paths}"

with zipfile.ZipFile(wheel_paths[0]) as archive:
    wheel_names = set(archive.namelist())
    assert "fastapi_request_observability/__init__.py" in wheel_names
    assert "fastapi_request_observability/py.typed" in wheel_names
    assert any(name.endswith(".dist-info/licenses/LICENSE") for name in wheel_names)
    assert not any(name.startswith(("tests/", "examples/", "plans/")) for name in wheel_names)

    metadata_name = next(name for name in wheel_names if name.endswith(".dist-info/METADATA"))
    metadata = email.message_from_bytes(archive.read(metadata_name))
    requirements = metadata.get_all("Requires-Dist") or []
    project_urls = metadata.get_all("Project-URL") or []
    assert metadata["Name"] == EXPECTED_NAME
    assert metadata["Version"] == EXPECTED_VERSION
    assert metadata["Requires-Python"] == EXPECTED_PYTHON
    assert metadata["License-Expression"] == "MIT"
    assert requirements == EXPECTED_DEPENDENCIES
    assert set(project_urls) == EXPECTED_URLS
    assert all(
        forbidden not in requirement.lower()
        for requirement in requirements
        for forbidden in ("opentelemetry", "prometheus", "structlog")
    )

with tarfile.open(sdist_paths[0]) as archive:
    sdist_names = archive.getnames()
    assert any(name.endswith("/LICENSE") for name in sdist_names)
    assert any(name.endswith("/README.md") for name in sdist_names)
    assert any(name.endswith("/src/fastapi_request_observability/py.typed") for name in sdist_names)
    assert not any("/plans/" in name for name in sdist_names)
