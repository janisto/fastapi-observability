# E2E consumer

This minimal FastAPI application installs the wheel built from this checkout
into a frozen production virtual environment. It accepts one of the five
central `OBS_E2E_CASE` values and exposes `GET /trace` on `0.0.0.0:$PORT`.

```sh
just e2e-image observability-e2e-local:ci
```

Only the central observability repository evaluates cross-repository parity.
