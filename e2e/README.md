# Consumer image

This directory builds a production-shaped FastAPI application from the exact
package checkout. The image is a stable interface for optional independent
audits; building it verifies packaging and integration only and does not
validate emitted logs or approve a release.

## Interface

The container requires both of these environment variables:

- `OBS_E2E_CASE` selects exactly one supported configuration:
  `common_level1`, `common_level2`, `aws_level1`, `azure_level1`, or
  `gcp_level1`.
- `OBS_E2E_SECRET_CANARY` is a nonempty value used to authorize the request and
  detect accidental secret disclosure.

`PORT` is optional, defaults to `8080`, and must be an integer from 1 through
65535. The application listens on `0.0.0.0:$PORT` and exposes `GET /trace`.

Send the canary as `Authorization: Bearer <canary>`. A matching value returns
HTTP 200 with `ok: true`, a nonempty `request_id`, and
`canary_received: true`. A missing or incorrect value returns HTTP 401 with
`{"error":"unauthorized"}`. The canary value itself must not appear in the
response body, stdout, or stderr.

Application and access records are emitted as structured JSON lines on stdout.
Process-startup or fatal diagnostics belong on stderr. An auditor may validate
those records against this package's documented logging contract. Any
cross-implementation conclusions belong to the auditor, not this repository.

## Build and run

The Justfile prefers Podman when it is available and otherwise uses Docker:

```sh
just e2e-image observability-e2e-local:manual
runtime="$(command -v podman 2>/dev/null || command -v docker)"
"$runtime" run --rm --publish 127.0.0.1:8080:8080 \
  --env OBS_E2E_CASE=common_level1 \
  --env OBS_E2E_SECRET_CANARY=local-audit-canary \
  observability-e2e-local:manual
```

From another shell, exercise the endpoint:

```sh
curl --fail-with-body \
  --header 'Authorization: Bearer local-audit-canary' \
  http://127.0.0.1:8080/trace
```

Running an audit is optional and informational. It is not a release approval
or publication requirement.
