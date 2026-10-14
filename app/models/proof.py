"""Version-one proof vocabulary. Unknown is never a negative observation."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


class ProofModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ProofFact(ProofModel):
    value: StrictBool | None = None
    status: Literal['known', 'unknown', 'not_applicable'] = 'unknown'
    source: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    reason_codes: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def validate_state(self):
        if self.status == 'known':
            if self.value is None or not self.source or not self.evidence:
                raise ValueError('Known facts need a boolean, source and evidence.')
        elif self.value is not None:
            raise ValueError('Unknown/not-applicable facts must have null values.')
        if self.status != 'known' and not self.reason_codes:
            raise ValueError('Uncertain/inapplicable facts need a reason code.')
        return self


class CandidateClaim(ProofModel):
    value: Any = None
    source: str
    evidence: dict[str, Any]


class CandidateGate(ProofModel):
    id: CandidateClaim
    name: CandidateClaim
    direction: CandidateClaim
    verified: ProofFact


class CandidateTimestamps(ProofModel):
    utc: dict[str, Any]
    local: dict[str, Any]
    container_first_utc: CandidateClaim
    container_last_utc: CandidateClaim


class ProofCandidate(ProofModel):
    candidate_id: str
    uuid: str | None
    ground_truth_transaction_id: str | None
    ground_truth_source: str
    transaction_match_status: str | None
    match_method: str
    match_time_difference_seconds: float | None
    timestamps: CandidateTimestamps
    gate: CandidateGate
    identity: dict[str, Any]
    ground_truth_record_observed: ProofFact
    physical_incident_verified: ProofFact
    automatic_origin: ProofFact
    generation: ProofFact
    receipt: ProofFact
    save: ProofFact
    diagnostics: dict[str, Any]
    provenance: dict[str, Any]
    raw_candidate: dict[str, Any]


class StageCoverage(ProofModel):
    requested_start_utc: str
    requested_end_utc: str
    covered_start_utc: str | None = None
    covered_end_utc: str | None = None
    complete: ProofFact
    gaps: list[dict[str, Any]] | None = None
    source: str
    limitations: list[str]


class ScopedAbsence(ProofModel):
    stage: Literal['ground_truth', 'generation', 'receipt', 'save']
    subject: dict[str, Any] | None = None
    scope: dict[str, Any]
    coverage_complete: ProofFact
    absent: ProofFact
    matching_rule_version: str | None = None
    source: str


class SourceAcquisition(ProofModel):
    status: Literal['succeeded', 'failed', 'unknown', 'not_applicable']
    evidence: dict[str, Any]
    reason_codes: list[str]


class AcquisitionSummary(ProofModel):
    status: Literal['complete', 'partial', 'failed']
    pipeline_succeeded: ProofFact
    sources: dict[str, SourceAcquisition]
    limitations: list[str]


class ProofResponse(ProofModel):
    schema_version: Literal['unified-proof.v1'] = 'unified-proof.v1'
    request: dict[str, Any]
    acquisition: AcquisitionSummary
    coverage: dict[str, StageCoverage]
    candidates: list[ProofCandidate]
    facts: dict[str, ProofFact]
    absence: list[ScopedAbsence]
    reason_codes: list[str]
    limitations: list[str]
