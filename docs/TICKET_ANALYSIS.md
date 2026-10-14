# Ticket identity analysis (`unified-ticket.v1`)

`POST /api/analyze/ticket` is an additive, identity-only API. Existing
`/api/analyze`, `/api/export/csv`, and proof v1/v2 routes remain unchanged.
AFRS is not changed by this service extension; switching its caller is a
separate integration task.

```json
{
  "transaction_id": "6840696",
  "yard_id": "rmc_msg",
  "gate_name": "Incoming gate",
  "direction": "IN"
}
```

IDs must be strings, with surrounding whitespace trimmed and leading zeros
preserved. Numeric input is rejected. Omitted/null/blank IDs produce
`MISSING_TRANSACTION_ID`. Report timestamps and other extra request fields
are rejected; no report clock or rolling lookback is consulted.

The PostgreSQL provider queries `TRIM(tt.id::text) = %s` with a bound string
parameter. It queries globally before yard validation, retaining transactions
without automation rows and without the timestamp path's FINISHED/Alisa filters.
This permits explicit yard mismatch and avoids hiding manual records. A found
record that cannot be converted is insufficient data, not transaction absence.

Gate validation reuses the matcher's deterministic uppercase/alphanumeric
normalization (rule `unified-gate-alphanumeric-uppercase.v1`). Thus spacing,
case and punctuation differences are tolerated, but different normalized names
are rejected. There is no existing alias registry; no new fuzzy aliases or
approximate matching are introduced. Yard comparison trims and ignores case.
Direction is blocking if both values exist and differ. If the transaction lacks
direction, identity lookup may succeed with `direction_validation=UNKNOWN` and
`TRANSACTION_DIRECTION_UNAVAILABLE`. It is an optional consistency check.

## Response

The versioned response contains:

- `ticket_match`: requested and matched identity, status, UUID, authoritative
  transaction UTC time, matching rule, direction validation and `row_index`.
- `authoritative_transaction`: original AIQ identity, gate, direction, actual
  UTC/local time and ground-truth truck/trailer/vehicle values. No ticket fallback.
- `decision_contract_version`, `decision`, `transaction`, `field_comparisons`,
  `flags`: the existing normalized product contract, applied only to the exact
  AIQ row. Identity mismatch never supplies a usable detection conclusion.
- `headers`, positional `rows`, `csv_text`, `raw_source`, `raw_format`: canonical
  DataFrameExporter output. Arrays are parsed directly from that exact CSV,
  retaining all normal 26 columns and blank values.
- `correlation`: AIQ lookup outcome, actual transaction anchor/UUID, derived
  yard-local date and UTC interval, Archive outcome/count and container outcome.
- `reason_codes`: stable failure/uncertainty codes, without database secrets.

`ticket_match.status` is one of `EXACT_MATCH`, `TRANSACTION_NOT_FOUND`,
`YARD_MISMATCH`, `GATE_MISMATCH`, `DIRECTION_MISMATCH`, `AMBIGUOUS`,
`MISSING_TRANSACTION_ID`, or `INSUFFICIENT_DATA`.

No correlation is performed for mismatch, absence or ambiguous identity. After
an exact match, the existing yard-local full-day interval builder uses only the
AIQ transaction's actual time. Archive acquisition, filtering and duplicate
suppression then use that day. The existing matcher correlates AIQ by its UUID
first and Archive against remaining logs. Only the exact AIQ transaction's row
is exported for this ticket; unrelated Archive rows are not ticket candidates.

Identity and correlation are separate: `EXACT_MATCH` may accompany
`INSUFFICIENT_DATA` detection when Archive/container acquisition is incomplete
or the AIQ UUID is unavailable. No coverage or absence certification is added.
Successful acquisition diagnostics do not certify complete evidence coverage.

Uncertainty codes include `EXACT_ID_LOOKUP_UNAVAILABLE`, `AIQ_LOOKUP_FAILED`,
`AIQ_IDENTITY_CONTRADICTION`, `TRANSACTION_GATE_UNAVAILABLE`,
`TRANSACTION_DIRECTION_UNAVAILABLE`, `TRANSACTION_TIME_UNAVAILABLE`,
`TRANSACTION_TIMEZONE_UNAVAILABLE`, `TRANSACTION_UUID_UNAVAILABLE`,
`ARCHIVE_ACQUISITION_FAILED`, `CONTAINER_LOG_ACQUISITION_INCOMPLETE`,
`CONTAINER_CORRELATION_FAILED` and `CORRELATION_IDENTITY_CONTRADICTION`.
Non-exact ticket statuses also appear as reason codes.

This endpoint returns transaction conclusions, not final AFRS review outcomes.
No worker, deployment or Docker restart is required for offline tests.
