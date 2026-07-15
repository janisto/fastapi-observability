# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.1] - 2026-07-15

### Fixed

- Prevented permissive custom request-ID validators from admitting empty values.
- Formatted IPv6 ASGI server addresses correctly in GCP request URLs and avoided
  treating Unix-socket paths as URL authorities.
- Delayed access-record emission until final ASGI response trailers when a
  response declares them, preserving trailer-send failures and full duration.

### Internal

- Added the repository-local `adversarial-testing` skill and expanded
  mutation-resistant coverage across failure recovery, concurrency, protocol
  boundaries, and built distributions.
- Made local distribution builds clear stale artifacts before package
  validation.

## [0.2.0] - 2026-07-13

### Changed

- Expanded package discovery metadata; runtime behavior and the public API are
  unchanged.

### Internal

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

[Unreleased]: https://github.com/janisto/fastapi-observability/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/janisto/fastapi-observability/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/janisto/fastapi-observability/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/janisto/fastapi-observability/releases/tag/v0.1.0
