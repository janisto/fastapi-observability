import re

import pytest

from tests.check_distribution import assert_packaged_markdown_links_resolve


@pytest.mark.parametrize(
    "destination",
    [
        "e2e/README.md",
        "examples",
        "CHANGELOG.md#migration-from-1x",
        "LICENSE",
    ],
)
def test_packaged_readme_rejects_missing_relative_link_targets(destination):
    expected_target = destination.partition("#")[0]

    with pytest.raises(AssertionError, match=re.escape(expected_target)):
        assert_packaged_markdown_links_resolve(
            f"[missing]({destination})",
            {"README.md"},
            artifact_name="test artifact",
        )


def test_packaged_readme_accepts_anchors_absolute_urls_and_present_relative_targets():
    document = """
[section](#configuration)
[repository](https://github.com/janisto/fastapi-observability)
[contact](mailto:maintainer@example.com)
[protocol-relative](//github.com/janisto/fastapi-observability)
[migration](CHANGELOG.md#migration-from-1x)
"""

    assert_packaged_markdown_links_resolve(
        document,
        {"CHANGELOG.md"},
        artifact_name="test artifact",
    )
