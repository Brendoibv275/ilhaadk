from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
import uuid

from fastapi import APIRouter, HTTPException

from sdr_ilha_ar import repository as repo

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/overview")
def overview() -> dict[str, Any]:
    stage_counts = repo.dashboard_stage_counts()
    jobs = repo.dashboard_jobs(limit=100)
    appointments = repo.dashboard_upcoming_appointments(limit=100)
    return {
        "stage_counts": stage_counts,
        "jobs_pending": sum(1 for j in jobs if j.get("status") == "pending"),
        "jobs_failed": sum(1 for j in jobs if j.get("status") == "failed"),
        "appointments_total": len(appointments),
        "scheduled_leads": next((i["total"] for i in stage_counts if i["stage"] == "scheduled"), 0),
    }


@router.get("/funnel")
def funnel() -> dict[str, Any]:
    return {"items": repo.dashboard_stage_counts()}


@router.get("/appointments")
def appointments() -> dict[str, Any]:
    return {"items": repo.dashboard_upcoming_appointments(limit=200)}


@router.get("/jobs")
def jobs() -> dict[str, Any]:
    return {"items": repo.dashboard_jobs(limit=300)}


@router.get("/messages")
def messages() -> dict[str, Any]:
    return {"items": repo.dashboard_recent_messages(limit=500)}


@router.get("/callbacks")
def callbacks() -> dict[str, Any]:
    jobs = repo.dashboard_jobs(limit=500)
    callback_types = {"send_followup", "check_calendar", "nps"}
    return {
        "items": [
            j
            for j in jobs
            if str(j.get("job_type")) in callback_types
            and str(j.get("status")) == "pending"
            and str(j.get("stage")) not in {"scheduled", "completed"}
        ],
    }


@router.get("/clients/finalized")
def clients_finalized() -> dict[str, Any]:
    stage_counts = repo.dashboard_stage_counts()
    finalized = next((i["total"] for i in stage_counts if i["stage"] == "completed"), 0)
    return {"completed_total": finalized}


@router.get("/finance/summary")
def finance_summary() -> dict[str, Any]:
    return {
        "dedicated": repo.dashboard_finance_summary(),
        "forecast": repo.dashboard_finance_forecast_from_pipeline(),
    }


@router.get("/finance/entries")
def finance_entries() -> dict[str, Any]:
    return {"items": repo.dashboard_finance_entries(limit=300)}


@router.post("/finance/entries")
def create_finance_entry(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        lead_id = uuid.UUID(payload["lead_id"]) if payload.get("lead_id") else None
        appointment_id = (
            uuid.UUID(payload["appointment_id"]) if payload.get("appointment_id") else None
        )
        due_date = (
            datetime.fromisoformat(payload["due_date"])
            if payload.get("due_date")
            else None
        )
        entry_id = repo.create_finance_entry(
            lead_id=lead_id,
            appointment_id=appointment_id,
            entry_type=str(payload["entry_type"]),
            category=str(payload["category"]),
            description=str(payload.get("description") or ""),
            amount=Decimal(str(payload["amount"])),
            due_date=due_date,
            status=str(payload.get("status") or "pending"),
            metadata=payload.get("metadata") or {},
        )
        return {"status": "ok", "id": str(entry_id)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

