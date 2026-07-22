# syntax=docker/dockerfile:1.19.0

ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.28-python3.14-trixie-slim
ARG RUNTIME_IMAGE=python:3.14-slim-trixie

FROM ${UV_IMAGE} AS package-builder
WORKDIR /source
COPY pyproject.toml uv.lock README.md CHANGELOG.md LICENSE ./
COPY src ./src
RUN uv build --wheel --no-sources --out-dir /dist

FROM ${UV_IMAGE} AS app-builder
ENV UV_COMPILE_BYTECODE=1 \
    UV_NO_INSTALLER_METADATA=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_NO_DEV=1 \
    UV_FROZEN=1
WORKDIR /app
COPY e2e/pyproject.toml e2e/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --no-install-project
COPY --from=package-builder /dist/*.whl /wheels/
RUN uv pip install --python /app/.venv/bin/python --no-deps /wheels/*.whl
COPY e2e/app ./app

FROM ${RUNTIME_IMAGE} AS runtime
RUN groupadd --system --gid 1001 app \
    && useradd --system --gid 1001 --uid 1001 --no-create-home --shell /usr/sbin/nologin app
ENV VIRTUAL_ENV=/app/.venv \
    PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080
WORKDIR /app
COPY --from=app-builder --chown=1001:1001 /app/.venv /app/.venv
COPY --from=app-builder --chown=1001:1001 /app/app /app/app
USER 1001:1001
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port $PORT --no-server-header --no-access-log"]
