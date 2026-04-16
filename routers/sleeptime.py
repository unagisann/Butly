"""
routers/sleeptime.py
──────────────────────
Sleeptime 実行・ステータス・推定エンドポイント。
"""
from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from sleeptime import ButlySleeptime, sleeptime_store

router = APIRouter()

sleeptime_instance = ButlySleeptime()


@router.get("/sleeptime/estimate/{instance_name}")
def estimate_sleeptime(instance_name: str):
    """Get estimated workload for Sleeptime."""
    try:
        result = sleeptime_instance.estimate_workload(instance_name)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class SleeptimeRunRequest(BaseModel):
    instance_name: str


@router.post("/sleeptime/run")
async def run_sleeptime(request: SleeptimeRunRequest, background_tasks: BackgroundTasks):
    """Run Sleeptime in background."""
    instance_name = request.instance_name

    current_status = sleeptime_store.get(instance_name, {})
    if current_status.get("state") == "running":
        return {"message": "Sleeptime is already running", "status": current_status}

    sleeptime_instance.update_status(instance_name, "running", 0.0, "Starting...")

    background_tasks.add_task(run_in_threadpool, sleeptime_instance.run_with_progress, instance_name)

    return {"message": "Sleeptime started", "status": sleeptime_store[instance_name]}


@router.get("/sleeptime/status/{instance_name}")
def get_sleeptime_status(instance_name: str):
    """Get current Sleeptime status."""
    return sleeptime_store.get(instance_name, {"state": "idle", "progress": 0.0, "message": ""})
