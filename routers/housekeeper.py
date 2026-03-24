"""
routers/housekeeper.py
──────────────────────
Housekeeper 実行・ステータス・推定エンドポイント。
"""
from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from housekeeper import ButlyHousekeeper, housekeeper_store

router = APIRouter()

housekeeper_instance = ButlyHousekeeper()


@router.get("/housekeeper/estimate/{instance_name}")
def estimate_housekeeper(instance_name: str):
    """Get estimated workload for Housekeeper."""
    try:
        result = housekeeper_instance.estimate_workload(instance_name)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class HousekeeperRunRequest(BaseModel):
    instance_name: str


@router.post("/housekeeper/run")
async def run_housekeeper(request: HousekeeperRunRequest, background_tasks: BackgroundTasks):
    """Run Housekeeper in background."""
    instance_name = request.instance_name

    current_status = housekeeper_store.get(instance_name, {})
    if current_status.get("state") == "running":
        return {"message": "Housekeeper is already running", "status": current_status}

    housekeeper_instance.update_status(instance_name, "running", 0.0, "Starting...")

    background_tasks.add_task(run_in_threadpool, housekeeper_instance.run_with_progress, instance_name)

    return {"message": "Housekeeper started", "status": housekeeper_store[instance_name]}


@router.get("/housekeeper/status/{instance_name}")
def get_housekeeper_status(instance_name: str):
    """Get current Housekeeper status."""
    return housekeeper_store.get(instance_name, {"state": "idle", "progress": 0.0, "message": ""})
