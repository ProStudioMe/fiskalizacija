from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from sepko import __version__
from sepko.db import get_db, ping_async
from sepko.schemas import HealthOut

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut)
async def health(db: Session = Depends(get_db)) -> HealthOut:
    _ = db
    db_status = "ok"
    try:
        await ping_async()
        db_status = "asyncpg"
    except Exception:
        db_status = "skipped"
    return HealthOut(status="ok", service="sepko", version=__version__, db=db_status)
