# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

The changes in this section target `2.0.0` and must not be published on the
`1.x` release line. Version 2 intentionally does not preserve v1 positional
constructor layouts or compatibility-only options.

### Migration from 1.x

- Enable `capture_path`, `capture_peer_ip`, `capture_user_agent`, and
  `capture_error` explicitly where those privacy-sensitive fields are still
  required. The new defaults omit them.
- Rename consumers of `remote_ip` to `peer_ip`. The new value is the direct ASGI
  peer only; GCP `requestUrl` contains at most the query-free path and never an
  authority.
- Remove the v1 access-record `message` setting. Version 2 always uses
  `"request completed"`; move application-specific text to application log
  events.
- Refactor `AccessLogConfig`, `RequestContextConfig`, `RequestContext`, and
  `TraceContext` construction to keyword arguments; v1 positional call shapes
  are rejected.
- Use Python-registered integer levels for access status callbacks. Update
  abnormal-record queries for authoritative response status and the
  `service_error`, `body_error`, `cancelled`, `client_disconnect`, and
  `response_dropped` terminal reasons; every abnormal record now uses `ERROR`.
- Custom request-ID validators apply only to caller input and may broaden it
  within the ASGI adapter's visible-ASCII response-header boundary. Generated
  IDs always retain the package baseline grammar, including after callback
  failure.
- Update route dimensions to canonical `{name}` and `{*name}` templates and
  configure one identical `trace_context_level` on both middleware components;
  mismatches now fail deterministically.

### Added

- Added independent `capture_path`, `capture_peer_ip`, `capture_user_agent`, and
  `capture_error` opt-ins.
- Added explicit W3C Trace Context Level 2 configuration, including its
  `tracestate` key grammar and `trace_id_random` projection. Level 1 remains the
  default.
- Added a conditional consumer-image build as a packaging and integration
  diagnostic, with Podman-first local builds and Docker fallback. Optional
  independent audits are informational and never a publication requirement.

### Changed

- Defined LF-terminated NDJSON at the standard stream-handler boundary.
- Folded every GCP logger level into the five documented GCP severities and
  accepted registered native access status levels.
- Disabled concrete path, direct peer IP, and User-Agent capture by default;
  renamed the opt-in portable peer field from `remote_ip` to `peer_ip`, and
  narrowed GCP `requestUrl` to the opt-in query-free path without authority.
  Direct peer IP literals are canonicalized or omitted.
- Canonicalized retained `tracestate` field-lines without treating 512
  characters as a maximum.
- Let custom request-ID validators broaden caller input within the native
  response-header boundary.
- Treated dash-delimited future-version `traceparent` suffixes as opaque while
  retaining strict validation of the common 55-character prefix.
- Made access status authoritative to accepted ASGI response-start messages,
  removed synthetic 200/500 fallbacks, and added `service_error`, `body_error`,
  `cancelled`, `client_disconnect`, and `response_dropped` with `ERROR` severity
  while preserving original failures.
- Normalized nested mapping keys before JSON encoding and retained the first
  value on a normalized-name collision, preventing duplicate raw JSON members.
- Canonicalized simple whole-segment FastAPI route converters to
  portable `{name}` and `{*name}` templates while preserving richer
  authoritative matched templates in native syntax.

### Removed

- Removed v1 positional-constructor and fixed-value option shims so the v2
  surface has one explicit configuration form.

### Fixed

- Enforced exact, contextual field ownership: application extras may use
  access-only names, exact aliases owned only by an inactive preset,
  and unrelated names, while access callbacks cannot replace fields written by
  access enrichment.
- Emitted GCP `httpRequest.latency` with canonical ProtoJSON precision across
  the representable range, omitting only an unrepresentable projection.
- Applied the RFC 9110 field-content boundary before custom request-ID
  validation, admitting internal space, tab, or a comma in one field-line while
  retaining a direct-construction guard for edge whitespace.
- Classified every incomplete ASGI return as `response_dropped` and preserved
  exact downstream-send disconnect classification.
- Preserved composite FastAPI route metadata, HTAB User-Agent values, and
  nonempty static operation IDs at their native framework boundaries.
- Preserved HTTP-safe opaque future `traceparent` suffixes without an invented
  length cap and rejected trace-level disagreement in either middleware order.
- Preserved the escaped representation of every nonempty ASGI raw-path byte
  sequence, including existing malformed percent triplets that reached
  middleware and the `*` request target.
- Called a configured request-ID generator once, then used the package fallback
  for an exception or invalid result.
- Preserved User-Agent projection as the lossless Latin-1 mapping of ASGI header
  bytes rather than claiming UTF-8 decoding.
- Ignored non-encodable Python strings instead of allowing malformed
  `traceparent` input to raise `UnicodeEncodeError`.
- Preserved sampling while omitting the Level 2 random flag for unknown future
  `traceparent` versions.

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
