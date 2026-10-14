"""Product conclusions from Unified correlation, separate from proof certificates."""
from typing import Literal
from app.models.proof import ProofModel


class DetectionDecision(ProofModel):
    detection_result: Literal['DETECTED', 'MISSED', 'AMBIGUOUS', 'INSUFFICIENT_DATA']
    detection_reason: str


class FieldComparison(ProofModel):
    field: Literal['truck_number', 'trailer_number_1', 'trailer_number_2', 'vehicle_type']
    ground_truth: str | None
    prediction: str | None
    raw_ground_truth: str | None
    raw_prediction: str | None
    result: Literal['MATCH', 'MISMATCH', 'MISSING_PREDICTION', 'MISSING_GROUND_TRUTH', 'NOT_APPLICABLE']
