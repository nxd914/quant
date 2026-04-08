from __future__ import annotations

import json
import re
import threading
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .config import OUTPUT_DIR, WEB_DIR   # config.py loads .env automatically
from .pipeline import run_pipeline
from .notifier import notifier
from .storage import VequilStorage

import os

_API_KEY: str | None = os.getenv("DASHBOARD_API_KEY")
_AUTH_REQUIRED: bool = os.getenv("VEQUIL_REQUIRE_AUTH", "0").strip() != "0"
_CORS_ALLOW_ORIGIN: str = os.getenv("VEQUIL_CORS_ALLOW_ORIGIN", "*")
_PUBLIC_RATE_LIMIT_PER_MINUTE = int(os.getenv("VEQUIL_PUBLIC_RATE_LIMIT", "60"))
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_EVENT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]{1,80}$")
_RESOLUTION_MAX_LEN = 2000
_rate_limit_lock = threading.Lock()
_request_timestamps: dict[tuple[str, str], list[float]] = {}
_storage = VequilStorage()


class VequilHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def _resolve_event_output_dir(self, event_id: str | None) -> Path:
        """
        Map an event id to an output directory.

        `latest` is treated as the root output if it already exists, otherwise the
        newest generated event directory is used as a fallback for public report links.
        """
        normalized = (event_id or "").strip()
        if not normalized or normalized == "latest":
            root_dashboard = OUTPUT_DIR / "dashboard.json"
            if root_dashboard.exists():
                return OUTPUT_DIR

            events_dir = OUTPUT_DIR / "events"
            if events_dir.exists():
                candidates = [
                    path for path in events_dir.iterdir()
                    if path.is_dir() and (path / "dashboard.json").exists()
                ]
                if candidates:
                    return max(candidates, key=lambda path: path.stat().st_mtime)

            return OUTPUT_DIR

        return OUTPUT_DIR / "events" / normalized

    # ── Auth check ────────────────────────────────────────────

    def _authorized(self) -> bool:
        """Returns True when auth is disabled or key matches."""
        if not _AUTH_REQUIRED:
            return True
        if not _API_KEY:
            return False
        return self.headers.get("X-API-Key") == _API_KEY

    def _require_auth(self) -> bool:
        """Write a 401 and return False if not authorized."""
        if not self._authorized():
            self._write_json({"error": "Unauthorized"}, status=HTTPStatus.UNAUTHORIZED)
            return False
        return True
    
    def _normalize_event_id(self, event_id: str | None) -> str | None:
        candidate = (event_id or "").strip()
        if not candidate:
            return None
        if candidate == "latest":
            return "latest"
        if not _EVENT_ID_PATTERN.match(candidate):
            return None
        return candidate

    def _read_json_body(self) -> dict:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length <= 0:
            raise ValueError("Request body is required")
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid JSON body") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON payload must be an object")
        return data

    def _rate_limited(self, endpoint: str) -> bool:
        now_ts = datetime.now().timestamp()
        key = (self.client_address[0], endpoint)
        with _rate_limit_lock:
            bucket = _request_timestamps.setdefault(key, [])
            window_start = now_ts - 60.0
            while bucket and bucket[0] < window_start:
                bucket.pop(0)
            if len(bucket) >= _PUBLIC_RATE_LIMIT_PER_MINUTE:
                return True
            bucket.append(now_ts)
        return False

    def _audit_log(self, action: str, **fields: object) -> None:
        payload = {
            "at": datetime.now().isoformat(),
            "action": action,
            "method": self.command,
            "path": self.path,
            "ip": self.client_address[0],
            **fields,
        }
        print(json.dumps(payload, ensure_ascii=True))

    # ── Routing ───────────────────────────────────────────────

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/api/health":
            if not self._require_auth():
                return
            self._write_json({"status": "ok", "auth": bool(_API_KEY)})
            return

        if parsed.path == "/api/reconciliation":
            if not self._require_auth():
                return
            force_run = "run" in qs and qs["run"][0] == "1"
            event_id = self._normalize_event_id(qs["event_id"][0] if "event_id" in qs else None)
            if "event_id" in qs and event_id is None:
                self._write_json({"error": "Invalid event_id format"}, status=HTTPStatus.BAD_REQUEST)
                return

            dashboard_dir = self._resolve_event_output_dir(event_id)
            dashboard_path = dashboard_dir / "dashboard.json"

            # Re-run the full pipeline if explicitly requested or no output exists
            if force_run or not dashboard_path.exists():
                run_pipeline(event_id=event_id)

            payload = json.loads(dashboard_path.read_text(encoding="utf-8"))
            
            # Inject resolutions
            resolutions = _storage.get_resolutions_map()
            for finding in payload.get("discrepancies", []):
                fid = f"{finding['processor']}_{finding['reference_id']}_{finding['discrepancy_type']}"
                if fid in resolutions:
                    finding["resolution"] = resolutions[fid]
            
            self._write_json(payload)
            return

        if parsed.path == "/api/history":
            if not self._require_auth():
                return
            events_dir = OUTPUT_DIR / "events"
            history = []
            if events_dir.exists():
                for d in events_dir.iterdir():
                    if d.is_dir() and (d / "dashboard.json").exists():
                        history.append({
                            "event_id": d.name,
                            "created_at": d.stat().st_mtime
                        })
            self._write_json({"history": sorted(history, key=lambda x: x["created_at"], reverse=True)})
            return

        if parsed.path == "/api/export":
            if not self._require_auth():
                return
            event_id = qs.get("event_id", [None])[0]
            report_dir = self._resolve_event_output_dir(event_id)
            report_path = report_dir / "reconciliation_report.xlsx"
            
            if not report_path.exists():
                run_pipeline(event_id=event_id)
            
            with report_path.open("rb") as f:
                content = f.read()
            
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", f'attachment; filename="vequil_report_{event_id or "latest"}.xlsx"')
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Expose-Headers", "Content-Disposition")
            self.end_headers()
            self.wfile.write(content)
            return

        if parsed.path.startswith("/report/"):
            # Public route for standalone report cards
            self.path = "/report.html"
            return super().do_GET()

        if parsed.path == "/api/public/report":
            # Public API: no _require_auth here. 
            # Serves only summary metrics for the shareable report card.
            if self._rate_limited("public_report"):
                self._write_json({"error": "Rate limit exceeded"}, status=HTTPStatus.TOO_MANY_REQUESTS)
                return
            event_id = self._normalize_event_id(qs.get("event_id", [None])[0])
            if "event_id" in qs and event_id is None:
                self._write_json({"error": "Invalid event_id format"}, status=HTTPStatus.BAD_REQUEST)
                return
            dashboard_dir = self._resolve_event_output_dir(event_id)
            dashboard_path = dashboard_dir / "dashboard.json"
            
            if not dashboard_path.exists():
                self._write_json({"error": "Report not found"}, status=HTTPStatus.NOT_FOUND)
                return
            
            full_data = json.loads(dashboard_path.read_text(encoding="utf-8"))
            # Filter for public consumption: Metrics, Stats, and Anomaly Summary only.
            public_payload = {
                "metrics": full_data.get("metrics"),
                "processor_summary": full_data.get("processor_summary", []),
                "generated_at": full_data.get("generated_at"),
                "anomaly_count": len(full_data.get("discrepancies", [])),
                "top_anomaly": full_data.get("discrepancies", [{}])[0].get("discrepancy_type", "None") if full_data.get("discrepancies") else "None"
            }
            self._write_json(public_payload)
            return

        if parsed.path == "/":
            self.path = "/index.html"

        return super().do_GET()

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path.startswith("/report/"):
            self.path = "/report.html"
            return super().do_HEAD()

        if parsed.path == "/":
            self.path = "/index.html"

        return super().do_HEAD()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/resolve":
            if not self._require_auth():
                return
            data = self._read_json_body()
            
            ref_id = str(data.get("finding_id", "")).strip()
            resolution = str(data.get("resolution", "")).strip()
            
            if not ref_id:
                self._write_json({"error": "Missing finding_id"}, status=HTTPStatus.BAD_REQUEST)
                return
            if not resolution:
                self._write_json({"error": "Resolution is required"}, status=HTTPStatus.BAD_REQUEST)
                return
            if len(resolution) > _RESOLUTION_MAX_LEN:
                self._write_json({"error": "Resolution too long"}, status=HTTPStatus.BAD_REQUEST)
                return

            _storage.upsert_resolution(ref_id, resolution)
            
            self._audit_log("resolution_saved", finding_id=ref_id)
            self._write_json({"status": "ok"})
            return
        if parsed.path == "/api/demo":
            # This is a public endpoint, no auth required.
            if self._rate_limited("demo"):
                self._write_json({"error": "Rate limit exceeded"}, status=HTTPStatus.TOO_MANY_REQUESTS)
                return
            data = self._read_json_body()
            email = str(data.get("email", "")).strip().lower()
            
            if not email:
                self._write_json({"error": "Email is required"}, status=HTTPStatus.BAD_REQUEST)
                return
            if not _EMAIL_PATTERN.match(email):
                self._write_json({"error": "Invalid email format"}, status=HTTPStatus.BAD_REQUEST)
                return
            
            _storage.insert_lead(email=email, ip=self.client_address[0])
            
            # Send alert
            notifier.notify_lead(email)
            
            self._audit_log("lead_captured", email=email)
            self._write_json({"status": "ok", "message": "Signup captured"})
            return

        if parsed.path == "/api/log":
            if not self._require_auth():
                return
            try:
                data = self._read_json_body()
                self._log_action(data)
                self._write_json({"status": "ok", "message": "Action logged"})
            except Exception as e:
                self._write_json({"error": str(e)}, status=HTTPStatus.BAD_REQUEST)
            return

    # ── Helpers ───────────────────────────────────────────────

    def _log_action(self, data: dict) -> None:
        """Appends a single agent action to durable local storage + CSV export."""
        from .config import RAW_DATA_DIR
        import csv

        fields = [
            "Timestamp", "Project", "SessionID", "ActionID", 
            "ToolUsed", "Model", "ComputeCost", "TaskStatus", "Deployment"
        ]
        _storage.insert_action_log(data)

        log_file = RAW_DATA_DIR / "openclaw_logs.csv"
        file_exists = log_file.exists()
        with open(log_file, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            if not file_exists:
                writer.writeheader()
            
            # Ensure all fields are present to avoid DictWriter errors
            row = {f: data.get(f, "—") for f in fields}
            # Auto-timestamp if missing
            if row["Timestamp"] == "—":
                row["Timestamp"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
                
            writer.writerow(row)
        self._audit_log("agent_action_logged", action_id=row["ActionID"], tool=row["ToolUsed"])

    def _write_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", _CORS_ALLOW_ORIGIN)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A002
        # Custom compact log: timestamp + method + path + status
        print(f"  {self.log_date_time_string()}  {args[0]}  →  {args[1]}")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if _AUTH_REQUIRED and _API_KEY:
        print("🔐 Auth enabled")
    elif _AUTH_REQUIRED:
        print("❌ Auth required but DASHBOARD_API_KEY is not set")
        raise SystemExit(1)
    else:
        print("⚠️  Auth disabled (set VEQUIL_REQUIRE_AUTH=1 to enforce key auth)")

    host = "0.0.0.0"
    port = int(os.getenv("PORT", 8000))
    server = ThreadingHTTPServer((host, port), VequilHandler)
    print(f"🚀 Vequil server → http://{host}:{port}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
