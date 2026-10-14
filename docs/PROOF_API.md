# Unified proof API v1

`POST /api/analyze/proof` accepts `yard_id`, `start_local`, `end_local`, and the
existing optional CSV controls `include_container_logs_only` and
`normalized_ocr_deduplication`. Local values must be naive, increasing, and at
most 90 days apart. The interval is start-inclusive/end-exclusive. Unknown
request fields (including ticket gate/direction and export writes) are rejected.

The existing `/api/analyze` is a date-based analysis/export API. It and
`/api/export/csv` are unchanged; a separate route avoids breaking their clients.
The new route resolves the same yard configuration and dates, calls the same
`AnalysisService.analyze` once, and applies the same exact-range filter as CSV.
There are no extra DB/log queries, export writes, or review-app calls.

Response version: `unified-proof.v1`. OpenAPI exposes the Pydantic response and
fact models. The top-level fields are `request`, `acquisition`, `coverage`,
`candidates`, `facts`, `absence`, `reason_codes`, and `limitations`.

## Capability audit

| Fact or claim | Class | Current implementation evidence |
| --- | --- | --- |
| Pipeline returned/failed | A: available | Route observes the actual analysis return/exception. |
| AIQ/archive acquisition success/failure | A | GroundTruthSelector independently calls both providers and retains their error states in AnalysisResult. No exception text is published. |
| Source ground-truth record observed | A | Provider-created AIQ/archive record with a ground-truth identifier. This means a normalized source row was observed, not a physical incident or automatic transaction. IDs may be provider-synthesized. |
| Existing candidate values, matching status/method, flags | A, claims only | UnifiedTransaction contains the existing CSV values. Preserve them in raw_candidate, excluding metadata/raw payloads. |
| Automatic event origin | B: stronger validation | PostgreSQL filters FINISHED transactions with Alisa-attributed fields, not automatic event origins. Attribution of one field does not exclude manual/simulated event creation. Event-level validated origin is not retained. |
| Generation | B | Parser aggregates ALERT and other event types, but does not emit a validated generation certificate with source-event provenance. |
| Gate/direction ownership | B | Matcher may fill missing fields from preferred/last container gate and direction. Archive matching may accept missing direction and chooses a best match, flagging ties. Exact physical owner/camera binding is not established. |
| Receipt | C: unsupported | No business receiver acknowledgment/event source is represented. |
| Automatic transaction save/error | C | A FINISHED ground-truth row is a persisted row, but its linkage to automatic creation/save and explicit save failures is not established. Archive rows cannot certify AIQ persistence. |
| Stage completeness, gaps, retention/truncation | C | AIQ is filtered; archive records are retrieved/filtered; parser can silently return no rows for missing files or skip unparseable records. No completeness manifests exist. |
| Scoped absence | C | No certified stage inventory/coverage and subject matching rule. Empty results and GROUND_TRUTH_ONLY are not absence. |
| Physical identity, eligibility, camera ownership | C | No independent visual review, exact camera mapping or versioned response policy. |
| IANA timezone certificate | C | ResolvedYard has a descriptive timezone label and standard/DST offsets. TimezoneService uses North American DST rules. Do not invent an IANA zone from that label. |

Only A observations are populated as known. All B/C propositions are unknown.
The serializer accepts no caller-supplied proof flags. It ignores unvalidated
origin hints in metadata and never interprets match statuses as proof.

## Fact and coverage semantics

A ProofFact has `value`, `status`, `source`, `evidence`, and `reason_codes`.
Known propositions require a strict boolean and nonempty source/evidence.
Unknown and not_applicable require null and an explicit reason; neither means
false. The Pydantic model distinguishes all four combinations. Unsupported
facts in this endpoint use unknown, never not_applicable.

Candidate gate values are claims with field pointers and normalized-record
hashes. Gate verification always remains unknown. No ticket/Avian fallback is
accepted. Candidate timestamps are labeled candidate_reference, not incident,
receipt or save timestamps. A naive primary UTC timestamp is not made aware by
the proof serializer: its normalized UTC claim is null and it gets
TIMESTAMP_TIMEZONE_UNKNOWN. Raw candidate values remain available for inspection.

Coverage is separate for ground_truth, generation, receipt and save. Requested
UTC bounds are known. Covered bounds, completeness and gaps are unknown. `gaps:
null` means unmeasured; it does not mean an empty/no-gap list. Retention and
truncation are explicitly uncertified in limitations. No stage returns complete
true or false without a certificate.

Each absence entry contains a stage and yard/UTC interval scope. Subject,
gate/direction and matching_rule_version are null; absent and coverage_complete
are unknown. These are explicit unsupported requests for facts, not global
nothing-found proofs. No candidate and source failures can never create absence.

Acquisition status is partial on returned analyses because container acquisition
telemetry is missing in AnalysisResult. This is incomplete source telemetry,
not a claim that known coverage is partial or that candidate content is invalid.
AIQ/archive statuses report query outcomes, not completeness. There is currently
no basis to emit acquisition complete. A pipeline exception returns HTTP 502
with the versioned failed response and all proof stages unknown. Scope-validation
and timezone-resolution errors retain conventional HTTP 400/404/422/502 errors.

Normalized-record hashes bind the exported candidate projection and provide
stable candidate IDs. They are not raw-source hashes, signatures, or independent
source certificates. Known record observations reference the source and its
record ID. Metadata/raw container payloads and provider exception messages are
not published.

## Stable v1 reason codes

- MATCH_STATUS_INFORMATIONAL_ONLY
- COVERAGE_UNKNOWN
- AUTOMATIC_ORIGIN_UNKNOWN
- GENERATION_STATE_UNKNOWN
- RECEIPT_STATE_UNKNOWN
- SAVE_STATE_UNKNOWN
- GATE_UNVERIFIED
- PHYSICAL_INCIDENT_UNVERIFIED
- ABSENCE_UNCERTIFIED
- AUTOMATIC_EXPECTATION_UNKNOWN
- CONTAINER_ACQUISITION_UNKNOWN
- TIMEZONE_RULES_UNCERTIFIED
- GROUND_TRUTH_RECORD_UNESTABLISHED (on candidate fact)
- UUID_MISSING
- GATE_FIELDS_MISSING
- TIMESTAMP_TIMEZONE_UNKNOWN
- AMBIGUOUS_MATCH
- MULTIPLE_CANDIDATES
- NO_CANDIDATE (only successful returned analysis)
- SOURCE_FALLBACK_ARCHIVE
- AIQ_ACQUISITION_FAILED
- ARCHIVE_ACQUISITION_FAILED
- ANALYSIS_FAILED
- SOURCE_ACQUISITION_UNKNOWN (on failed-analysis source entries)

Reason lists are deduplicated and sorted deterministically. Diagnostic flags
retain the existing pipeline values separately. No positive review statuses
or causal delivery/save diagnoses are emitted.

## Offline validation and examples

Run `python -B -m pytest -q -p no:cacheprovider` with the service dependencies and
pytest/httpx installed. Tests mount the production router without main's startup
acquisition, override application services, and forbid real provider construction,
network calls and exports. An integration test uses the actual analysis/matching
pipeline with fake ground truth and a temporary missing container file.

Synthetic full response examples are in `proof_examples/matched.json`,
`ground_truth_only.json`, and `no_candidate.json`. They are not production evidence.

The review app still cannot enable positive outcomes from these responses.
Version one makes the gaps explicit. Later service work must provide stage-specific
coverage/inventories, authenticated event origin and receipt/save evidence, exact
mapping and output/artifact linkage. Independent visual proof and downstream guard
implementation remain separate tasks.
