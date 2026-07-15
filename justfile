# Justfile for fastapi-request-observability
# https://github.com/casey/just
# Development and release checks for fastapi-request-observability.

@_:
    just --list

[group('qa')]
lint:
    uvx ruff check
    uvx ruff format --check

[group('qa')]
typing:
    uvx ty check

[group('test')]
test *args:
    uv run -m pytest -v --cov=fastapi_request_observability --cov-branch {{ args }}

[group('qa')]
check: lint typing test

[group('qa')]
fix:
    uvx ruff check --fix
    uvx ruff format

[group('package')]
build:
    uv build --clear --no-sources

[group('package')]
smoke-wheel: build
    uv run --isolated --no-project --with dist/*.whl tests/smoke_test.py

[group('package')]
smoke-sdist: build
    uv run --isolated --no-project --with dist/*.tar.gz tests/smoke_test.py

[group('package')]
inspect: build
    uv run tests/check_distribution.py

[group('package')]
package-check: inspect smoke-wheel smoke-sdist

[group('lifecycle')]
install:
    uv sync --locked

[group('lifecycle')]
update:
    uv sync --upgrade
