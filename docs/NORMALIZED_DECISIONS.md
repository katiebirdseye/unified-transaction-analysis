# Normalized transaction decisions (ARND-490)

POST `/api/analyze/proof/v2` adds `decision_contract_version: unified-decisions.v1`,
`decision`, `transaction`, `field_comparisons`, and `flags`. The response remains
`unified-proof.v2`; this is an additive product layer, not a new proof certificate.
All existing proof facts/system_outputs, v1, CSV, and legacy analysis serialization
retain their meanings. A v2 consumer needs the decision contract version to use
the product layer; older v2 responses without it cannot authorize a decision.

```json
{
  "decision_contract_version": "unified-decisions.v1",
  "decision": {"detection_result": "DETECTED", "detection_reason": "MATCHED"},
  "transaction": {
    "yard_id": "example", "uuid": "example-event", "transaction_id": "42",
    "ground_truth_source": "AIQ", "timestamp_utc": "2026-10-01T19:31:30+00:00",
    "timestamp_local": "2026-10-01T15:31:30", "gate_id": "Main Exit",
    "gate_name": "Main Gate", "direction": "OUT"
  },
  "field_comparisons": [{
    "field": "truck_number", "ground_truth": "T5031", "prediction": null,
    "raw_ground_truth": "T5031", "raw_prediction": null,
    "result": "MISSING_PREDICTION"
  }],
  "flags": []
}
```

The example abbreviates the comparison list; a selected transaction returns four
entries: truck_number, trailer_number_1, trailer_number_2, vehicle_type. No selected
transaction returns null transaction and an empty comparison list.

## Detection

- One usable MATCHED candidate -> DETECTED / MATCHED.
- One usable GROUND_TRUTH_ONLY candidate -> MISSED / GROUND_TRUTH_ONLY.
- AMBIGUOUS status/ambiguity flags or multiple usable candidates -> AMBIGUOUS.
- No usable candidate, failed or incomplete analysis -> INSUFFICIENT_DATA.
- CONTAINER_LOGS_ONLY -> INSUFFICIENT_DATA / CONTAINER_LOGS_ONLY: runtime records
  without correlated ground truth do not establish whether the expected business
  transaction was detected, missed, irrelevant, or outside ground-truth inputs.

Usable candidates have a timezone-aware UTC timestamp and UUID or transaction ID.
There is no nearest-candidate promotion when several candidates are plausible.
AFRS does not map these match statuses itself.

Analysis completion means required source queries and the container-file read
finished successfully. Missing container files, query failures and parser exceptions
cannot yield MISSED. The parser's read flag is basic acquisition telemetry, not a
certificate of retention, continuous runtime coverage or absence. A successfully
read empty file is an empty analysis input. Source coverage proof may remain unknown
while the completed analysis supplies a normalized product conclusion. No forensic
certificate is required by this product contract.

## Fields

Null, empty/whitespace-only strings and `/` are unavailable. Both unavailable ->
NOT_APPLICABLE; only prediction unavailable -> MISSING_PREDICTION; only truth
unavailable -> MISSING_GROUND_TRUTH. Otherwise compare trimmed strings. Vehicle
categories compare case-insensitively; identifiers remain case-sensitive with all
meaningful characters and leading zeros retained. No O/0 substitution, punctuation
removal, numeric conversion, or type-alias mapping is applied. Equal -> MATCH;
different -> MISMATCH. Raw values remain separate; the PostgreSQL provider retains
original SQL comparator values in a private, non-exported field before legacy
cleaning. Other providers preserve their supplied pipeline values.

## AFRS responsibility

AFRS validates version/request scope and decision/comparison vocabulary, retains
and caches the service response, and presents it. It never reconstructs Unified
transaction correlation or comparisons from raw match status or proof fields.
For a missed-detection complaint, MISSED supports confirmed_miss and DETECTED
supports resolved. Relevant number/type mismatches support OCR/classification
confirmed_issue for those complaint categories. MATCH, NOT_APPLICABLE, and missing
prediction do not automatically confirm an OCR issue. AMBIGUOUS/INSUFFICIENT_DATA
remain needs_review. False-alarm/duplicate eligibility and same-visit semantics
are not supplied by this contract and are not automatically resolved by DETECTED.

Avian, report images, and cached visual review remain available for context and
uncertain cases. A normalized conclusion need not wait for visual/model review.
Saved conclusions work offline without visuals or a compatibility reference;
changed request scope requires an explicit recheck/reacquisition, never a remote
request during saved review. The existing perception-only queue intake is unchanged;
standalone OCR/classification intake expansion is a separate product choice.

Origin/stage/artifact proof limitations from SYSTEM_OUTPUT_PROOF.md describe the
certificate layer only. They are not prerequisites for AFRS to consume these
normalized product conclusions. No final UI classification is emitted by Unified.
