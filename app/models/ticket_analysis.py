"""Identity-only request: report clocks are deliberately not accepted."""
from pydantic import BaseModel, ConfigDict


class TicketAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    transaction_id: str | None = None
    yard_id: str
    gate_name: str
    direction: str | None = None
