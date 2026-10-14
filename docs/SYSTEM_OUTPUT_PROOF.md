# Retained system-output provenance (ARND-490)

`POST /api/analyze/proof/v2` accepts the same local yard/window request as
`POST /api/analyze/proof`. It returns `schema_version: unified-proof.v2` and
`capabilities: ["retained-aiq-field-observation.v1"]`. Both versions use one shared
analysis/scope implementation. Version one, CSV, and `/api/analyze` retain their
previous response shapes and semantics. No deployment or startup is needed to
test the route.

The new capability certifies **source-row field observations**, not authenticated
immutable automatic output events. It does not enable an OCR decision. The
explicit version prevents existing v1 consumers from silently accepting new
positive source-record-link facts. All old candidate proof facts retain their
v1 meanings and conservative values.

## Capability audit

| Fact | Class | Current evidence / implemented behavior |
| --- | --- | --- |
| Native AIQ transaction ID, UUID, yard association | A | Direct PostgreSQL query reads `tt.id`, `tt.external_uuid`, yard; automation row joins on `tta.id = tt.id`. Known only when candidate/source IDs and yard agree and IDs are present. |
| Exact AIQ Alisa-attributed field value | A | Existing SQL CASE retains the value only for an explicit `*_source = 'alisa'`; query additionally retains the source column. Raw string is preserved, including whitespace/leading zeros. No comparator normalization is promoted. |
| Candidate-to-source-row link | A | Exact native transaction ID + UUID + yard + AIQ source agreement. Meaning is source record association only, not linkage to a physical incident or an immutable generated event. |
| Automatic/manual/simulated **event** origin | C | No queried event-origin field. Alisa attribution describes a field, not how the event was created. Null/unknown. |
| Immutable output ID/revision | C | AIQ fields can change; no retained revision/immutable output record is available. `output_id = null`. |
| Normalized output value | C | No versioned normalization rule implemented here. Null/unknown, version null. |
| Generated, received, saved event stages | B/C | Container event names/confirmation timestamps are observations; no validated business receiver/persistence event linkage. AIQ FINISHED/filter/join does not certify automatic generation, receipt, or save. All stages independently unknown. |
| Stage/capture timestamps | B/C | `tt.started` is a candidate clock. Container timestamps are raw event observations without certified stage identity. Never copied into generated/received/saved/capture timestamps. |
| Artifact ID/immutable reference/camera | C | Active AIQ query and archive collector acquire no authenticated artifact relation, bytes or metadata. Raw log payload paths, if any, are not authenticated references. All unknown. |
| Event-to-artifact linkage | B/C | No authoritative foreign-key relation to a retained artifact is acquired. UUID labels, filenames and nearby timestamps cannot prove it. Unknown. |
| Artifact digest | C | No trusted byte acquisition or authoritative digest metadata exists. No digest calculation/path hashing; algorithm null. |
| Archive field outputs | C | Archive supplies operator/ground-truth observations, no Alisa output. Unknown. |
| Spreadsheet/Fenix/other AIQ provider values | B | They expose comparator values/source labels, but do not implement this direct-SQL collector contract. Not promoted by the serializer. |
| Coverage, absence, gate ownership, physical identity | C / outside this extension | Unchanged, unknown. |

No current source supports a genuinely authenticated automatic output or
artifact. Tests deliberately check these remain unknown rather than fabricating
positive fixtures. Typed models can represent known manual/simulated origins and
known-false/not-applicable links, but current collectors do not emit such facts.
An injectable metadata assertion is not a new authoritative source.

## Schema additions

Each v2 candidate gains `system_outputs`, with four field slots: truck number,
trailer numbers 1/2 and vehicle type. A slot is an observation or an explicit
unknown placeholder; membership in the array never proves output existence.

Each slot has:

- `output_id` (currently null: immutable record unavailable).
- `event.uuid`, `transaction_id`, `origin` and `candidate_link_verified`.
- `field.name`, `raw_value`, `normalized_value`, `normalization_version`.
- Independent `state.generated`, `received`, `saved` Boolean proof facts.
- `timestamps.generated_at`, `received_at`, `saved_at`, `artifact_capture_at`;
  each retains its explicit kind.
- `artifact.artifact_id`, `source_system`, `source_record_id`, `event_uuid`,
  `immutable_reference`, `content_digest`, `digest_algorithm`, `capture_time`,
  `camera`, `acquisition_provenance`, `event_link_verified`.
- `provenance` describing read-only SQL acquisition and field attribution;
  per-value evidence includes table, native record ID, value/source columns,
  join rule and collector version.
- Sorted deterministic `reason_codes`.

String facts use known/unknown/not_applicable with string/null value, source and
evidence. Event origin has only automatic/manual/simulated known values (unknown
is null). Boolean link/stage facts use existing strict Boolean tri-state models.
False, unknown and not_applicable are distinct. Unknowns require reasons; known
values require source-record evidence. Model validity alone is not authentication.

Collector observations live in a private dataclass field, excluded from legacy
`to_dict`, CSV/metadata exports and equality, but preserved by the existing
matcher's deepcopy. Raw log metadata cannot populate this field. The serializer
never reads origins, output claims, artifact paths, bytes or hashes from general
metadata. No new artifact network request is introduced.

## Reason codes

- `OUTPUT_ORIGIN_UNKNOWN`
- `OUTPUT_EVENT_LINK_UNKNOWN`
- `OUTPUT_RECORD_IMMUTABILITY_UNKNOWN`
- `OUTPUT_ARTIFACT_UNKNOWN`
- `OUTPUT_ARTIFACT_LINK_UNKNOWN`
- `OUTPUT_DIGEST_UNAVAILABLE`
- `OUTPUT_FIELD_UNKNOWN`
- `OUTPUT_NORMALIZATION_UNAVAILABLE`
- `OUTPUT_GENERATION_STATE_UNKNOWN`
- `OUTPUT_RECEIPT_STATE_UNKNOWN`
- `OUTPUT_SAVE_STATE_UNKNOWN`
- `OUTPUT_TIMESTAMP_UNKNOWN`

Response-level codes describe capabilities still missing somewhere in the
response; individual slots remove field/link unknown codes where those facts
are established. Reasons never imply a final review classification.

## Future OCR validator prerequisite

This is insufficient to authorize a positive one-field OCR finding. Before that
can be done, a real collector must acquire explicit event origin, an immutable
output revision/value, authoritative event-to-output/artifact relationships,
trusted artifact bytes (or an authoritative digest/reference with validated
acquisition), and a capture clock/camera identity that can support independent
visual linkage. The review app must independently establish readable truth and
compare the exact field. A generated/received/saved claim must come from its own
stage source; coverage/absence is unnecessary for an OCR mismatch and stays
outside this change.

The synthetic example `proof_examples/retained_output.json` shows what is
actually supported today; it intentionally has unknown origin and artifact.
