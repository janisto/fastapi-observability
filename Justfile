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

# mutmut prefixes generated function trampolines with `x_`.
[group('test')]
mutation:
    uv run mutmut run "fastapi_request_observability.trace.x_parse_traceparent*"

[group('qa')]
check: lint typing test

[group('qa')]
workflow-check:
    actionlint
    zizmor --offline .

[group('qa')]
qa: workflow-check check

[group('qa')]
fix:
    uvx ruff check --fix
    uvx ruff format

[group('package')]
build: clean-dist
    uv build --no-sources

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

# Remove generated package distributions.
[group('lifecycle')]
clean-dist:
    rm -rf dist
