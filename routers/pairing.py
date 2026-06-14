"""ローカル管理用の external account pairing API。"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import dependencies as deps
from butly_core.external.account_mapping import ExternalAccountStore
from butly_core.external.pairing import PairingStore

router = APIRouter(prefix="/pairing")


def require_local_request(request: Request) -> None:
    host = request.client.host if request.client else ""
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get(
        "x-forwarded-for"
    )
    requested_host = (request.url.hostname or "").lower()
    allowed_hosts = {"127.0.0.1", "::1", "localhost", "testclient"}
    if forwarded or host not in allowed_hosts or requested_host not in allowed_hosts:
        raise HTTPException(status_code=403, detail="Pairing API is local-only.")


def _stores() -> tuple[ExternalAccountStore, PairingStore]:
    account_store = ExternalAccountStore(deps.DATA_DIR, deps.INSTANCES_DIR)
    return account_store, PairingStore(deps.DATA_DIR, account_store)


class ApprovePairingRequest(BaseModel):
    code: str
    instance_name: Optional[str] = None


class RejectPairingRequest(BaseModel):
    code: str


@router.get("/pending", dependencies=[Depends(require_local_request)])
def pending_pairings():
    account_store, pairing_store = _stores()
    return {
        "pending": [record.to_dict() for record in pairing_store.pending()],
        "instances": account_store.available_instances(),
        "default_instance": account_store.default_instance(),
    }


@router.post("/approve", dependencies=[Depends(require_local_request)])
def approve_pairing(request: ApprovePairingRequest):
    _, pairing_store = _stores()
    try:
        record = pairing_store.approve(request.code, request.instance_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"approved": True, "source": record.source}


@router.post("/reject", dependencies=[Depends(require_local_request)])
def reject_pairing(request: RejectPairingRequest):
    _, pairing_store = _stores()
    try:
        record = pairing_store.reject(request.code)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"rejected": True, "source": record.source}
