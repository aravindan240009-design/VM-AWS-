# AWS Agent Chat

A fast, ChatGPT-style web interface paired with a **full tool-using Agent Harness** powered by an uncensored open-weight model (`huihui-ai/Qwen3-8B-abliterated` or `Huihui-Qwen3.6-35B-A3B-abliterated`) self-hosted on AWS EC2 (`ap-southeast-2`).

## Features

- **ChatGPT-style UI (`static/`)**: Conversation sidebar with search & delete, streaming Markdown & code blocks with copy buttons, collapsible tool-call trace cards, reasoning trace toggle, custom system instructions, long-term Memory Vault, and a live AWS Brain & Harness Settings modal.
- **Full Agent Harness (`app/harness.py`, `app/tools.py`)**:
  - Multi-step ReAct tool-calling loop with native OpenAI `tool_calls` and `<tool_call>` fallback parsing.
  - Built-in tools: `web_search`, `fetch_url`, `python_eval`, `shell_exec`, workspace-sandboxed `list_files` / `read_file` / `write_file` / `search_files`, `memory_save` / `memory_search`, and `get_current_time`.
  - Toggle between **Full Harness** (agent + tools) and **Fast Chat** (direct low-latency streaming with `enable_thinking: false`).
- **AWS EC2 Deployment + 200-Hour Budget Protection (`deploy/aws/`)**:
  - `user-data-vllm.sh`: Installs vLLM (`--enable-auto-tool-choice --tool-call-parser hermes`) and an **idle auto-stop watchdog** that powers down EC2 after 20 minutes of inactivity so 200+ active hours stay within AWS New Experience credits.
  - `deploy_ec2.ps1` / `brain_ctl.ps1`: Quota check, Spot/On-Demand launch in `ap-southeast-2`, SSH tunnel, and start/stop controls.

## Quick Start (Local UI + Harness)

```powershell
uv sync
Copy-Item .env.example .env
uv run uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080`.

## Deploy the Uncensored Model Brain on AWS (`ap-southeast-2`)

After completing `aws login --region ap-southeast-2 --profile aws-vm`:

```powershell
# Fastest + 200+ hrs budget friendly (g4dn.xlarge Spot + Qwen3-8B-abliterated)
.\deploy\aws\deploy_ec2.ps1 -Profile aws-vm -Region ap-southeast-2

# Start / Stop / Open SSH Tunnel
.\deploy\aws\brain_ctl.ps1 -Action tunnel
.\deploy\aws\brain_ctl.ps1 -Action stop
```

## Run Tests

```powershell
uv run pytest -v
```
