from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from sepko.auth import get_tenant
from sepko.db import get_db
from sepko.models import Tenant
from sepko.schemas import FiscalizeRequest, FiscalizeResponse
from sepko.services import fiscalize_invoice, get_invoice_status

router = APIRouter(prefix="/v1/invoices", tags=["invoices"])


@router.post("/fiscalize", response_model=FiscalizeResponse)
def fiscalize(
    body: FiscalizeRequest,
    tenant: Tenant = Depends(get_tenant),
    db: Session = Depends(get_db),
) -> FiscalizeResponse:
    result = fiscalize_invoice(db, tenant, body)
    if result.status == "failed" and result.error_message and "required" in (result.error_message or ""):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=result.error_message)
    if result.status == "failed" and result.error_message and "!= " in (result.error_message or ""):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=result.error_message)
    return result


@router.get("/{external_id}", response_model=FiscalizeResponse)
def invoice_status(
    external_id: str,
    tenant: Tenant = Depends(get_tenant),
    db: Session = Depends(get_db),
) -> FiscalizeResponse:
    result = get_invoice_status(db, tenant, external_id)
    if not result:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Invoice not found")
    return result
