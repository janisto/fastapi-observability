# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Added specification-defined GCP profile `0.1.0`, newest-installed resolution,
  exact pinning through `GcpProfileVersion`, and resolved-version introspection
  on both formatter and access configuration.
- Added independent `capture_path`, `capture_peer_ip`, and
  `capture_user_agent` opt-ins.
- Added explicit W3C Trace Context Level 2 configuration, including its
  `tracestate` key grammar and `trace_id_random` projection. Level 1 remains the
  default.

### Changed

- Fold every GCP logger level into the five profile severities, reject
  nonstandard access status levels, and canonicalize or omit direct peer IPs.
- Disabled concrete path, direct peer IP, and User-Agent capture by default;
  renamed the opt-in portable peer field from `remote_ip` to `peer_ip`, and
  narrowed GCP `requestUrl` to the opt-in query-free path without authority.
- Aligned the GCP health fixture with service version `1.0.0` and added exact,
  deterministic DEBUG and INFO route tests.
- Canonicalized retained `tracestate` field-lines and restricted custom
  request-ID validators to narrowing the package's URI-unreserved baseline.
- Treated dash-delimited future-version `traceparent` suffixes as opaque while
  retaining strict validation of the common 55-character prefix.
- Made access status authoritative to accepted ASGI response-start messages,
  removed synthetic 200/500 fallbacks, and added standard abnormal terminal
  reasons with `ERROR` severity while preserving original failures.
- Normalized nested mapping keys before JSON encoding and retained the first
  value on a normalized-name collision, preventing duplicate raw JSON members.
- **Breaking:** Canonicalized explicit FastAPI route converters to portable
  `{name}` and `{*name}` templates while omitting ambiguous native forms.

### Fixed

- Enforced the `traceparent` input ceiling in UTF-8 bytes and omitted malformed
  percent-escaped raw paths instead of emitting them.

## [1.0.1] - 2026-07-17

### Added

- Expanded the canonical GCP health-route example with structured application
  `INFO` and `DEBUG` events, and verified their stdout output alongside the
  correlated access record.
- Added automatic pull-request labeling and a dedicated workflow-security
  check.

### Changed

- Lowered the minimum supported FastAPI version from 0.139.0 to 0.130.0 and
  kept the declared floor covered by the dedicated minimum-version CI job.
- Standardized repository QA and maintainer guidance. Runtime package behavior
  and the public API remain unchanged from v1.0.0.

## [1.0.0] - 2026-07-16

### Added

- Added a focused mutmut campaign for the W3C traceparent parser.
- Added a public maintainer guide for draft-first GitHub releases and OIDC-based
  PyPI trusted publishing.
- Added package, Python, CI, and license status badges to the README.

### Changed

- Declared exported APIs, configuration defaults, structured log fields, and
  supported runtime versions stable compatibility contracts. Runtime behavior
  and the public API are unchanged from v0.2.1.
- Marked the package as Production/Stable in its distribution metadata.
- Expanded the README's package rationale and standard-output logging guidance.
- Renamed `justfile` to `Justfile` and added an explicit `clean-dist` recipe for
  repeatable package builds.

## [0.2.1] - 2026-07-15

### Fixed

- Prevented permissive custom request-ID validators from admitting empty values.
- Formatted IPv6 ASGI server addresses correctly in GCP request URLs and avoided
  treating Unix-socket paths as URL authorities.
- Delayed access-record emission until final ASGI response trailers when a
  response declares them, preserving trailer-send failures and full duration.

### Changed

- Added the repository-local `adversarial-testing` skill and expanded
  mutation-resistant coverage across failure recovery, concurrency, protocol
  boundaries, and built distributions.
- Made local distribution builds clear stale artifacts before package
  validation.

## [0.2.0] - 2026-07-13

### Changed

- Expanded package discovery metadata; runtime behavior and the public API are
  unchanged.

- Updated GitHub issue templates for clearer maintainer triage.

## [0.1.0] - 2026-07-13

### Added

- Pure ASGI middleware for validated request IDs and strict W3C trace context.
- Request-scoped standard-library JSON logging with default, GCP, AWS, and
  Azure presets.
- One structured access record for normal, handled-error, exception, and
  streaming response paths.
- FastAPI route-template and explicit-operation-ID fields.
- FastAPI 0.139.0-or-newer runtime compatibility.
- Python 3.13-or-newer package metadata, Python 3.13–3.14 CI, typed package
  metadata, isolated artifact smoke tests, and PyPI trusted-publishing
  automation.
- Full W3C `tracestate` validation and Google Cloud's preferred raw trace-ID
  correlation format.

[Unreleased]: https://github.com/janisto/fastapi-observability/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/janisto/fastapi-observability/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/janisto/fastapi-observability/compare/v0.2.1...v1.0.0
[0.2.1]: https://github.com/janisto/fastapi-observability/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/janisto/fastapi-observability/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/janisto/fastapi-observability/releases/tag/v0.1.0
