from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DASHBOARD = REPO_ROOT / "data" / "output" / "dashboard.json"


def _load_dashboard(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"Dashboard not found at {path}. Run the server and sync once, or generate output via /api/reconciliation."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _fmt_int(n: Any) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def _fmt_money(n: Any) -> str:
    try:
        return f"${float(n):,.2f}"
    except Exception:
        return str(n)


def generate_posts(dashboard: dict[str, Any], handle: str) -> list[str]:
    metrics = dashboard.get("metrics") or {}
    total = metrics.get("total_transactions", "—")
    anomalies = metrics.get("flagged_transactions", "—")
    at_risk = metrics.get("at_risk_volume", "—")
    variance = metrics.get("net_expected_variance", "—")

    top = "None"
    discrepancies = dashboard.get("discrepancies") or []
    if discrepancies:
        top = discrepancies[0].get("discrepancy_type") or "Anomaly"

    stamp = datetime.now(timezone.utc).strftime("%b %d")

    drafts = []
    drafts.append(
        "\n".join(
            [
                f"Weekly agent report ({stamp}).",
                f"- Activity: {_fmt_int(total)} actions",
                f"- Anomalies: {_fmt_int(anomalies)} flagged",
                f"- Top anomaly: {top}",
                "",
                "Vequil makes agent behavior auditable (tool calls, loops, spend) so operators can catch issues before bills explode.",
                "Comment “ledger” and I’ll share the OpenClaw setup.",
                f"— {handle}",
            ]
        )
    )
    drafts.append(
        "\n".join(
            [
                "One of the best agent ops upgrades: make anomalies *boring*.",
                f"This run: {_fmt_int(anomalies)} anomalies, net variance {_fmt_money(variance)}, at-risk {_fmt_money(at_risk)}.",
                "When an agent loops or goes off-script, you want a ledger + queue, not vibes.",
                "Comment “rules” if you want the exact discrepancy categories we flag.",
                f"— {handle}",
            ]
        )
    )
    drafts.append(
        "\n".join(
            [
                "If you’re running OpenClaw agents in the wild, you need a public trust signal.",
                "Vequil generates a shareable weekly report card + a private console for audits and operator reviews.",
                "We’re building the picks & shovels for the agentic economy.",
                "Comment “ledger” and I’ll DM the plugin snippet.",
                f"— {handle}",
            ]
        )
    )
    return drafts


def rewrite_with_openclaw(drafts: list[str], thinking: str) -> list[str]:
    openclaw = os.environ.get("OPENCLAW_BIN", "openclaw")
    prompt = (
        "Rewrite these Moltbook posts in a technical, direct voice. "
        "Keep each under 120 words. Preserve the numbers. "
        "No hype, no emojis. Return as 3 posts separated by '\\n---\\n'.\n\n"
        + "\n\n---\n\n".join(drafts)
    )
    proc = subprocess.run(
        [openclaw, "agent", "--thinking", thinking, "--message", prompt],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "openclaw agent failed")
    out = proc.stdout.strip()
    if not out:
        raise RuntimeError("openclaw returned empty output")
    parts = [p.strip() for p in out.split("\n---\n") if p.strip()]
    return parts if len(parts) >= 3 else [out]


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate Moltbook posts from Vequil stats.")
    ap.add_argument("--dashboard", default=str(DEFAULT_DASHBOARD), help="Path to dashboard.json")
    ap.add_argument("--handle", default="@contextoperator", help="Signature handle")
    ap.add_argument("--openclaw", action="store_true", help="Rewrite drafts using openclaw agent")
    ap.add_argument("--thinking", default="high", help="openclaw thinking level (off|low|medium|high|xhigh)")
    args = ap.parse_args()

    dashboard = _load_dashboard(Path(args.dashboard))
    drafts = generate_posts(dashboard, args.handle)

    final = drafts
    if args.openclaw:
        final = rewrite_with_openclaw(drafts, args.thinking)

    print("\n\n---\n\n".join(final))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

