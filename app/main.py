from fastapi import FastAPI

from app.api.dependencies import (
    get_application_services,
)
from app.api.routes import router


app = FastAPI(
    title="Unified Transaction Analysis",
    description=(
        "Compares Alisa and container-log predictions against "
        "AIQ Google Sheets data or Archive fallback ground truth."
    ),
    version="0.2.0",
)

app.include_router(router)


@app.on_event("startup")
def initialize_application() -> None:
    """
    Construct dependencies and validate access during startup.

    This makes configuration or Google access failures visible before
    the first analysis request.
    """

    get_application_services()


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "healthy",
        "service": (
            "unified-transaction-analysis"
        ),
        "version": "0.2.0",
    }
