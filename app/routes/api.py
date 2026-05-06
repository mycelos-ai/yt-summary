"""REST API. Mounted at /api/v1.

Each handler authenticates via the api dependency, then delegates to
services/api.py.
"""

from typing import Any

import aiosqlite
from fastapi import APIRouter, Depends, Request

from app.main import get_db
from app.services.auth import authenticate

router = APIRouter(prefix="/api/v1")

API_VERSION = "0.4.0"


async def current_user(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
) -> int:
    return await authenticate(db, request)


@router.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "version": API_VERSION}
