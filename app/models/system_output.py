"""Opt-in output provenance vocabulary; source observation is not event truth."""
from typing import Any, Literal

from pydantic import Field, model_validator

from app.models.decision import DetectionDecision, FieldComparison
from app.models.proof import ProofCandidate, ProofFact, ProofModel, ProofResponse


class OutputValue(ProofModel):
    value: str | None = None
    status: Literal['known', 'unknown', 'not_applicable'] = 'unknown'
    source: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    reason_codes: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def validate_state(self):
        if self.status == 'known':
            if self.value is None or not self.source or not self.evidence:
                raise ValueError('Known output values need source-record evidence.')
        elif self.value is not None or not self.reason_codes:
            raise ValueError('Unknown/inapplicable output values need null and reasons.')
        return self


class OutputOrigin(OutputValue):
    value: Literal['automatic', 'manual', 'simulated'] | None = None


class OutputEvent(ProofModel):
    uuid: OutputValue
    transaction_id: OutputValue
    origin: OutputOrigin
    candidate_link_verified: ProofFact


class OutputField(ProofModel):
    name: Literal['truck_number', 'trailer_number_1', 'trailer_number_2', 'vehicle_type']
    raw_value: OutputValue
    normalized_value: OutputValue
    normalization_version: str | None = None


class OutputState(ProofModel):
    generated: ProofFact
    received: ProofFact
    saved: ProofFact


class OutputTimestamp(OutputValue):
    kind: Literal['generated_at', 'received_at', 'saved_at', 'artifact_capture_at']


class OutputTimestamps(ProofModel):
    generated_at: OutputTimestamp
    received_at: OutputTimestamp
    saved_at: OutputTimestamp
    artifact_capture_at: OutputTimestamp


class OutputArtifact(ProofModel):
    artifact_id: OutputValue
    source_system: OutputValue
    source_record_id: OutputValue
    event_uuid: OutputValue
    immutable_reference: OutputValue
    content_digest: OutputValue
    digest_algorithm: str | None = None
    capture_time: OutputTimestamp
    camera: OutputValue
    acquisition_provenance: OutputValue
    event_link_verified: ProofFact


class SystemOutput(ProofModel):
    output_id: str | None = None
    event: OutputEvent
    field: OutputField
    state: OutputState
    timestamps: OutputTimestamps
    artifact: OutputArtifact
    provenance: dict[str, Any]
    reason_codes: list[str]


class OutputProofCandidate(ProofCandidate):
    system_outputs: list[SystemOutput]


class OutputProofResponse(ProofResponse):
    schema_version: Literal['unified-proof.v2'] = 'unified-proof.v2'
    capabilities: list[Literal['retained-aiq-field-observation.v1']]
    candidates: list[OutputProofCandidate]

    decision_contract_version: Literal['unified-decisions.v1']
    decision: DetectionDecision
    transaction: dict[str, Any] | None
    field_comparisons: list[FieldComparison]
    flags: list[str]
