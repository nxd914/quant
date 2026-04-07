# 🔍 Vequil — AI Agent Ledger

<p align="center">
  <strong>See everything your agents do. Free, forever.</strong>
</p>

<p align="center">
  <a href="https://github.com/nxd914/clear-line-agent"><img src="https://img.shields.io/github/stars/nxd914/clear-line-agent?style=for-the-badge" alt="Stars"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge" alt="MIT License"></a>
  <a href="https://moltbook.com"><img src="https://img.shields.io/badge/Moltbook-Community-orange?style=for-the-badge" alt="Moltbook"></a>
</p>

Vequil is a free, open-source ledger for AI agent activity. Connect any agent runtime and every action, tool call, and anomaly is automatically logged and surfaced in a real-time dashboard.

> "847 agent actions last week. Operator approved 12." — that gap is what Vequil closes.

[Dashboard](web/static/dashboard.html) · [OpenClaw Plugin](misc/openclaw/README_OPENCLAW.md) · [Pricing](#pricing)

## Quick Start

Runtime: **Python 3.10+**

```bash
git clone https://github.com/nxd914/clear-line-agent.git
cd clear-line-agent

pip install -r requirements.txt

PYTHONPATH=src python -m vequil.server
```

Then open `web/static/dashboard.html` in your browser.

## OpenClaw Integration

Connect your OpenClaw agent to Vequil in under 60 seconds.

```bash
# 1. Copy the plugin into your OpenClaw workspace
cp misc/openclaw/vequil_plugin.py ~/.openclaw/workspace/skills/vequil/

# 2. Set your Vequil endpoint
export VEQUIL_ENDPOINT=http://localhost:8000/api/log

# 3. That's it — every tool_result_persist event now logs to Vequil
```

Full guide: [README_OPENCLAW.md](misc/openclaw/README_OPENCLAW.md)

## What Gets Logged

- Every tool call and result
- Session metadata (agent ID, model, timestamp)
- Anomalies: runaway loops, unauthorized sub-agent spend, orphaned tasks, duplicate execution
- Agent Quality Score — shareable weekly report card

## Integrations

| Runtime | Status |
|---|---|
| OpenClaw | ✅ Live |
| Anthropic API / Claude | 🔜 Coming soon |
| OpenAI API | 🔜 Coming soon |
| LangChain | 🔜 Coming soon |
| Moltbook | 🔜 Coming soon |

## Pricing

**Personal — Free forever**
- Unlimited agents
- Full activity ledger
- Anomaly detection
- 30-day history

**Pro — $9/month**
- Unlimited history
- Advanced anomaly alerts
- Team sharing
- Priority support

## Community

Built for the [OpenClaw](https://github.com/openclaw/openclaw) and [Moltbook](https://moltbook.com) communities.
Discuss in [m/openclaw-explorers](https://moltbook.com/m/openclaw-explorers).