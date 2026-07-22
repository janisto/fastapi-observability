"""Verify built distribution contents and public metadata."""

from __future__ import annotations

import email
import posixpath
import re
import tarfile
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).parent.parent
EXPECTED_NAME = "fastapi-request-observability"
EXPECTED_VERSION = "2.0.0"
EXPECTED_PYTHON = ">=3.13"
EXPECTED_DEPENDENCIES = ["fastapi>=0.130.0"]
EXPECTED_STATUS = "Development Status :: 5 - Production/Stable"
EXPECTED_URLS = {
    "Repository, https://github.com/janisto/fastapi-observability",
    "Issues, https://github.com/janisto/fastapi-observability/issues",
    "Changelog, https://github.com/janisto/fastapi-observability/blob/main/CHANGELOG.md",
}
MARKDOWN_INLINE_LINK = re.compile(r"!?\[[^\]\n]*\]\(\s*(?P<destination><[^>\n]+>|[^)\s]+)")
URI_SCHEME = re.compile(r"^[a-z][a-z\d+.-]*:", re.IGNORECASE)


def assert_packaged_markdown_links_resolve(
    document: str,
    available_paths: set[str],
    *,
    artifact_name: str,
) -> None:
    """Require every artifact-relative inline Markdown target to be packaged."""
    missing_targets: set[str] = set()
    for match in MARKDOWN_INLINE_LINK.finditer(document):
        destination = match.group("destination")
        if destination.startswith("<"):
            destination = destination[1:-1]
        if destination.startswith(("#", "//")) or URI_SCHEME.match(destination):
            continue

        target = posixpath.normpath(unquote(urlsplit(destination).path))
        if target not in available_paths:
            missing_targets.add(target)

    assert not missing_targets, (
        f"{artifact_name} README relative link targets are absent from the artifact: "
        f"{', '.join(sorted(missing_targets))}"
    )


def main() -> None:
    wheel_paths = list((ROOT / "dist").glob("*.whl"))
    sdist_paths = list((ROOT / "dist").glob("*.tar.gz"))
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
        description = metadata.get_payload()
        assert isinstance(description, str), "wheel METADATA description must be text"
        assert_packaged_markdown_links_resolve(description, wheel_names, artifact_name="wheel METADATA")
        assert metadata["Name"] == EXPECTED_NAME
        assert metadata["Version"] == EXPECTED_VERSION
        assert metadata["Requires-Python"] == EXPECTED_PYTHON
        assert metadata["License-Expression"] == "MIT"
        assert requirements == EXPECTED_DEPENDENCIES
        assert EXPECTED_STATUS in (metadata.get_all("Classifier") or [])
        assert set(project_urls) == EXPECTED_URLS
        assert all(
            forbidden not in requirement.lower()
            for requirement in requirements
            for forbidden in ("opentelemetry", "prometheus", "structlog")
        )

    with tarfile.open(sdist_paths[0]) as archive:
        sdist_names = archive.getnames()
        readme_name = next(name for name in sdist_names if name.endswith("/README.md"))
        package_root = readme_name.removesuffix("README.md")
        packaged_paths = {name.removeprefix(package_root) for name in sdist_names if name.startswith(package_root)}
        packaged_readme = archive.extractfile(readme_name)
        assert packaged_readme is not None
        assert_packaged_markdown_links_resolve(
            packaged_readme.read().decode(),
            packaged_paths,
            artifact_name="source distribution README",
        )
        assert any(name.endswith("/LICENSE") for name in sdist_names)
        assert any(name.endswith("/src/fastapi_request_observability/py.typed") for name in sdist_names)
        assert not any("/plans/" in name for name in sdist_names)


if __name__ == "__main__":
    main()
