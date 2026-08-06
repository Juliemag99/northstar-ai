"""NorthStar AI FastAPI application entrypoint."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="NorthStar AI",
    description="Revenue Development Platform API for NorthStar Group",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "https://app.follownorthstar.com",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness check for local development and deployments."""
    return {"status": "ok", "service": "northstar-ai"}


@app.get("/api/v1/status")
def api_status() -> dict[str, str]:
    """Basic API status endpoint for the frontend shell."""
    return {
        "status": "ok",
        "product": "NorthStar AI",
        "message": "Backend is running",
    }
