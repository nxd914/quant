from __future__ import annotations

import json
import os
import re
import time
from collections import defaultdict, deque
from http import HTTPStatus
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .config import OUTPUT_DIR, ROOT, WEB_DIR
from .notifier import notifier
from .pipeline import run_pipeline
from .storage import VequilStorage


app = FastAPI(title="Vequil", version="0.1")

_storage = VequilStorage()

_API_KEY: str | None = os.getenv("DASHBOARD_API_KEY")
_AUTH_REQUIRED: bool = os.getenv("VEQUIL_REQUIRE_AUTH", "0").strip() != "0"
_CORS_ALLOW_ORIGIN: str = os.getenv("VEQUIL_CORS_ALLOW_ORIGIN", "*")
_PUBLIC_RATE_LIMIT_PER_MINUTE = int(os.getenv("VEQUIL_PUBLIC_RATE_LIMIT", "60"))

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_EVENT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]{1,80}$")
_RESOLUTION_MAX_LEN = 2000
_WORKSPACE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9 _\-]{2,80}$")
_WORKSPACE_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9\-]{1,62}[a-z0-9]$")

# per-ip per-endpoint request timestamps (seconds)
_buckets: dict[tuple[str, str], deque[float]] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    # Keep it simple; if you later put this behind a proxy, add trusted proxy logic.
    return request.client.host if request.client else "unknown"


def _rate_limited(ip: str, endpoint: str) -> bool:
    now = time.time()
    key = (ip, endpoint)
    bucket = _buckets[key]
    window_start = now - 60.0
    while bucket and bucket[0] < window_start:
        bucket.popleft()
    if len(bucket) >= _PUBLIC_RATE_LIMIT_PER_MINUTE:
        return True
    bucket.append(now)
    return False


def _normalize_event_id(event_id: str | None) -> str | None:
    candidate = (event_id or "").strip()
    if not candidate:
        return None
    if candidate == "latest":
        return "latest"
    if not _EVENT_ID_PATTERN.match(candidate):
        return None
    return candidate


class WorkspaceCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    slug: str = Field(min_length=3, max_length=64)


class IngestEventRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    source: str = Field(min_length=1, max_length=80)
    event_type: str = Field(min_length=1, max_length=80)
    event_status: str = Field(min_length=1, max_length=40)
    event_at: str = Field(min_length=10, max_length=64)
    agent_id: str = Field(min_length=1, max_length=120)
    session_id: str | None = Field(default=None, max_length=120)
    tool_name: str | None = Field(default=None, max_length=120)
    cost_usd: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def _resolve_event_output_dir(event_id: str | None) -> Path:
    normalized = (event_id or "").strip()
    if not normalized or normalized == "latest":
        root_dashboard = OUTPUT_DIR / "dashboard.json"
        if root_dashboard.exists():
            return OUTPUT_DIR

        events_dir = OUTPUT_DIR / "events"
        if events_dir.exists():
            candidates = [
                path
                for path in events_dir.iterdir()
                if path.is_dir() and (path / "dashboard.json").exists()
            ]
            if candidates:
                return max(candidates, key=lambda path: path.stat().st_mtime)
        return OUTPUT_DIR

    return OUTPUT_DIR / "events" / normalized


def _audit_log(action: str, request: Request, **fields: Any) -> None:
    payload = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "action": action,
        "method": request.method,
        "path": str(request.url.path),
        "ip": _client_ip(request),
        **fields,
    }
    print(json.dumps(payload, ensure_ascii=True))


def require_auth(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    if not _AUTH_REQUIRED:
        return
    if not _API_KEY or x_api_key != _API_KEY:
        raise HTTPException(status_code=int(HTTPStatus.UNAUTHORIZED), detail="Unauthorized")


@app.middleware("http")
async def add_cors_headers(request: Request, call_next):
    resp: Response = await call_next(request)
    resp.headers["Access-Control-Allow-Origin"] = _CORS_ALLOW_ORIGIN
    return resp


# Static assets (JS/CSS/images). We do NOT mount at "/" because that would shadow /api/*.
app.mount("/static", StaticFiles(directory=str(WEB_DIR), html=False), name="static")


def _file_or_404(path: Path) -> FileResponse:
    if not path.exists():
        raise HTTPException(status_code=int(HTTPStatus.NOT_FOUND), detail="Not found")
    return FileResponse(path)


@app.get("/")
def landing():
    return _file_or_404(WEB_DIR / "index.html")


@app.get("/dashboard.html")
def dashboard():
    return _file_or_404(WEB_DIR / "dashboard.html")


@app.get("/app.js")
def app_js():
    # Backwards-compat: existing HTML may reference /app.js
    return _file_or_404(WEB_DIR / "app.js")


@app.get("/logo.png")
def logo_png():
    return _file_or_404(WEB_DIR / "logo.png")


@app.get("/api/health")
def health(_: None = Depends(require_auth)):
    return {"status": "ok", "auth": bool(_API_KEY) if _AUTH_REQUIRED else False}


@app.get("/api/workspaces")
def list_workspaces(_: None = Depends(require_auth)):
    return {"workspaces": _storage.list_workspaces()}


@app.post("/api/workspaces")
def create_workspace(body: WorkspaceCreateRequest, _: None = Depends(require_auth)):
    name = body.name.strip()
    slug = body.slug.strip().lower()
    if not _WORKSPACE_NAME_PATTERN.match(name):
        raise HTTPException(status_code=int(HTTPStatus.BAD_REQUEST), detail="Invalid workspace name")
    if not _WORKSPACE_SLUG_PATTERN.match(slug):
        raise HTTPException(status_code=int(HTTPStatus.BAD_REQUEST), detail="Invalid workspace slug")
    try:
        created = _storage.create_workspace(name=name, slug=slug)
    except Exception as exc:
        raise HTTPException(
            status_code=int(HTTPStatus.CONFLICT),
            detail="Workspace name or slug already exists",
        ) from exc
    return {"workspace": created}


@app.get("/api/workspaces/{workspace_id}/keys")
def list_workspace_keys(workspace_id: int, _: None = Depends(require_auth)):
    if not _storage.workspace_exists(workspace_id):
        raise HTTPException(status_code=int(HTTPStatus.NOT_FOUND), detail="Workspace not found")
    return {"keys": _storage.list_workspace_api_keys(workspace_id)}


@app.post("/api/workspaces/{workspace_id}/keys")
def create_workspace_key(workspace_id: int, _: None = Depends(require_auth)):
    if not _storage.workspace_exists(workspace_id):
        raise HTTPException(status_code=int(HTTPStatus.NOT_FOUND), detail="Workspace not found")
    return {"key": _storage.create_workspace_api_key(workspace_id)}


@app.delete("/api/workspaces/{workspace_id}/keys/{key_id}")
def revoke_workspace_key(workspace_id: int, key_id: int, _: None = Depends(require_auth)):
    if not _storage.workspace_exists(workspace_id):
        raise HTTPException(status_code=int(HTTPStatus.NOT_FOUND), detail="Workspace not found")
    revoked = _storage.revoke_workspace_api_key(workspace_id, key_id)
    if not revoked:
        raise HTTPException(status_code=int(HTTPStatus.NOT_FOUND), detail="Active key not found")
    return {"status": "ok", "revoked_key_id": key_id}


@app.get("/api/onboarding/quickstart")
def onboarding_quickstart(_: None = Depends(require_auth)):
    return {
        "steps": [
            "Create a workspace with POST /api/workspaces",
            "Copy ingest_api_key from the response",
            "Send first event with POST /api/ingest and X-Workspace-Key header",
            "Open /dashboard.html and run sync",
        ],
        "example_ingest_event": {
            "source": "openclaw",
            "event_type": "tool_call",
            "event_status": "success",
            "event_at": "2026-04-08T01:30:00Z",
            "agent_id": "ops-agent-1",
            "session_id": "session-123",
            "tool_name": "bash",
            "cost_usd": 0.012,
            "metadata": {"action_id": "abc123", "project": "vequil"},
        },
    }


@app.get("/api/history")
def history(_: None = Depends(require_auth)):
    events_dir = OUTPUT_DIR / "events"
    items: list[dict[str, Any]] = []
    if events_dir.exists():
        for d in events_dir.iterdir():
            if d.is_dir() and (d / "dashboard.json").exists():
                items.append({"event_id": d.name, "created_at": d.stat().st_mtime})
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return {"history": items}


@app.get("/api/reconciliation")
def reconciliation(run: str | None = None, event_id: str | None = None, _: None = Depends(require_auth)):
    force_run = (run or "") == "1"
    normalized = _normalize_event_id(event_id)
    if event_id is not None and normalized is None:
        raise HTTPException(status_code=int(HTTPStatus.BAD_REQUEST), detail="Invalid event_id format")

    dashboard_dir = _resolve_event_output_dir(normalized)
    dashboard_path = dashboard_dir / "dashboard.json"
    if force_run or not dashboard_path.exists():
        run_pipeline(event_id=normalized)

    payload = json.loads(dashboard_path.read_text(encoding="utf-8"))

    resolutions = _storage.get_resolutions_map()
    for finding in payload.get("discrepancies", []):
        fid = f"{finding['processor']}_{finding['reference_id']}_{finding['discrepancy_type']}"
        if fid in resolutions:
            finding["resolution"] = resolutions[fid]
    return payload


@app.get("/api/export")
def export(event_id: str | None = None, _: None = Depends(require_auth)):
    normalized = _normalize_event_id(event_id)
    if event_id is not None and normalized is None:
        raise HTTPException(status_code=int(HTTPStatus.BAD_REQUEST), detail="Invalid event_id format")

    report_dir = _resolve_event_output_dir(normalized)
    report_path = report_dir / "reconciliation_report.xlsx"
    if not report_path.exists():
        run_pipeline(event_id=normalized)

    filename = f'vequil_report_{normalized or "latest"}.xlsx'
    return FileResponse(
        report_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
        headers={"Access-Control-Expose-Headers": "Content-Disposition"},
    )


@app.post("/api/resolve")
async def resolve(request: Request, _: None = Depends(require_auth)):
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=int(HTTPStatus.BAD_REQUEST), detail="JSON payload must be an object")

    finding_id = str(data.get("finding_id", "")).strip()
    resolution = str(data.get("resolution", "")).strip()
    if not finding_id:
        raise HTTPException(status_code=int(HTTPStatus.BAD_REQUEST), detail="Missing finding_id")
    if not resolution:
        raise HTTPException(status_code=int(HTTPStatus.BAD_REQUEST), detail="Resolution is required")
    if len(resolution) > _RESOLUTION_MAX_LEN:
        raise HTTPException(status_code=int(HTTPStatus.BAD_REQUEST), detail="Resolution too long")

    _storage.upsert_resolution(finding_id, resolution)
    _audit_log("resolution_saved", request, finding_id=finding_id)
    return {"status": "ok"}


@app.post("/api/demo")
async def demo(request: Request):
    ip = _client_ip(request)
    if _rate_limited(ip, "demo"):
        raise HTTPException(status_code=int(HTTPStatus.TOO_MANY_REQUESTS), detail="Rate limit exceeded")

    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=int(HTTPStatus.BAD_REQUEST), detail="JSON payload must be an object")

    email = str(data.get("email", "")).strip().lower()
    if not email:
        raise HTTPException(status_code=int(HTTPStatus.BAD_REQUEST), detail="Email is required")
    if not _EMAIL_PATTERN.match(email):
        raise HTTPException(status_code=int(HTTPStatus.BAD_REQUEST), detail="Invalid email format")

    _storage.insert_lead(email=email, ip=ip)
    notifier.notify_lead(email)
    _audit_log("lead_captured", request, email=email)
    return {"status": "ok", "message": "Signup captured"}


@app.post("/api/log")
async def log(request: Request, _: None = Depends(require_auth)):
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=int(HTTPStatus.BAD_REQUEST), detail="JSON payload must be an object")

    _storage.insert_action_log(data)
    _audit_log("agent_action_logged", request, action_id=data.get("ActionID"), tool=data.get("ToolUsed"))
    return {"status": "ok", "message": "Action logged"}


@app.post("/api/ingest")
def ingest(
    body: IngestEventRequest,
    request: Request,
    x_workspace_key: str | None = Header(default=None, alias="X-Workspace-Key"),
):
    workspace_key = (x_workspace_key or "").strip()
    if not workspace_key:
        raise HTTPException(status_code=int(HTTPStatus.UNAUTHORIZED), detail="Missing X-Workspace-Key")
    workspace = _storage.resolve_workspace_by_key(workspace_key)
    if not workspace:
        raise HTTPException(status_code=int(HTTPStatus.UNAUTHORIZED), detail="Invalid workspace key")

    payload = body.model_dump()
    event_id = _storage.insert_ingest_event(
        workspace_id=workspace["id"],
        event_type=body.event_type,
        event_status=body.event_status,
        event_at=body.event_at,
        source=body.source,
        agent_id=body.agent_id,
        session_id=body.session_id,
        tool_name=body.tool_name,
        cost_usd=body.cost_usd,
        payload=payload,
    )
    _audit_log(
        "ingest_event",
        request,
        workspace_id=workspace["id"],
        event_id=event_id,
        event_type=body.event_type,
    )
    return {
        "status": "ok",
        "workspace": {"id": workspace["id"], "slug": workspace["slug"]},
        "event_id": event_id,
    }


@app.get("/api/public/report")
def public_report(request: Request, event_id: str | None = None):
    ip = _client_ip(request)
    if _rate_limited(ip, "public_report"):
        return JSONResponse({"error": "Rate limit exceeded"}, status_code=int(HTTPStatus.TOO_MANY_REQUESTS))

    normalized = _normalize_event_id(event_id)
    if event_id is not None and normalized is None:
        return JSONResponse({"error": "Invalid event_id format"}, status_code=int(HTTPStatus.BAD_REQUEST))

    dashboard_dir = _resolve_event_output_dir(normalized)
    dashboard_path = dashboard_dir / "dashboard.json"
    if not dashboard_path.exists():
        return JSONResponse({"error": "Report not found"}, status_code=int(HTTPStatus.NOT_FOUND))

    full_data = json.loads(dashboard_path.read_text(encoding="utf-8"))
    public_payload = {
        "metrics": full_data.get("metrics"),
        "processor_summary": full_data.get("processor_summary", []),
        "generated_at": full_data.get("generated_at"),
        "anomaly_count": len(full_data.get("discrepancies", [])),
        "top_anomaly": full_data.get("discrepancies", [{}])[0].get("discrepancy_type", "None")
        if full_data.get("discrepancies")
        else "None",
    }
    return public_payload


@app.get("/report/{event_id}")
def report_card(event_id: str):
    # Just serve the static report page; it calls /api/public/report?event_id=...
    path = WEB_DIR / "report.html"
    if not path.exists():
        raise HTTPException(status_code=int(HTTPStatus.NOT_FOUND), detail="Missing report.html")
    return FileResponse(path)

