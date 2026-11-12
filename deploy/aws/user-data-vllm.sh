#!/usr/bin/env bash
set -euo pipefail

# Bootstrap fast OpenAI-compatible vLLM server for an uncensored Hugging Face model
# plus an idle auto-stop watchdog so AWS project credits last 200+ active hours.

MODEL_ID="${MODEL_ID:-huihui-ai/Qwen3-8B-abliterated}"
API_KEY="${VLLM_API_KEY:-aws-agent-brain-key}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-16384}"
IDLE_MINUTES="${IDLE_MINUTES:-20}"

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3-venv python3-pip curl jq

mkdir -p /opt/vllm-brain
python3 -m venv /opt/vllm-brain/.venv
/opt/vllm-brain/.venv/bin/pip install --upgrade pip
/opt/vllm-brain/.venv/bin/pip install "vllm>=0.8.0" huggingface_hub

cat >/opt/vllm-brain/env <<EOF
MODEL_ID=${MODEL_ID}
API_KEY=${API_KEY}
MAX_MODEL_LEN=${MAX_MODEL_LEN}
EOF

cat >/etc/systemd/system/vllm-brain.service <<'EOF'
[Unit]
Description=vLLM Uncensored Model Brain (OpenAI-compatible)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/opt/vllm-brain/env
ExecStart=/opt/vllm-brain/.venv/bin/vllm serve ${MODEL_ID} \
  --host 127.0.0.1 \
  --port 8000 \
  --dtype auto \
  --gpu-memory-utilization 0.92 \
  --max-model-len ${MAX_MODEL_LEN} \
  --enable-auto-tool-choice \
  --tool-call-parser hermes \
  --api-key ${API_KEY}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# Idle auto-stop watchdog: shuts down EC2 if no active TCP connections on :8000 for IDLE_MINUTES
cat >/opt/vllm-brain/idle_watchdog.sh <<EOF
#!/usr/bin/env bash
STATE_FILE=/tmp/vllm_last_active
NOW=\$(date +%s)
ACTIVE=\$(ss -tnH state established '( sport = :8000 )' | wc -l)
if [ "\$ACTIVE" -gt 0 ] || [ ! -f "\$STATE_FILE" ]; then
  echo "\$NOW" > "\$STATE_FILE"
  exit 0
fi
LAST=\$(cat "\$STATE_FILE")
IDLE_SEC=\$((NOW - LAST))
if [ "\$IDLE_SEC" -ge \$(( ${IDLE_MINUTES} * 60 )) ]; then
  logger -t vllm-watchdog "No traffic on :8000 for ${IDLE_MINUTES}m. Powering off to save AWS credits."
  /sbin/shutdown -h now
fi
EOF
chmod +x /opt/vllm-brain/idle_watchdog.sh

cat >/etc/systemd/system/vllm-idle-watchdog.service <<'EOF'
[Unit]
Description=Check vLLM idle time and power off when unused

[Service]
Type=oneshot
ExecStart=/opt/vllm-brain/idle_watchdog.sh
EOF

cat >/etc/systemd/system/vllm-idle-watchdog.timer <<'EOF'
[Unit]
Description=Run vLLM idle watchdog every 2 minutes

[Timer]
OnBootSec=15min
OnUnitActiveSec=2min

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now vllm-brain.service
systemctl enable --now vllm-idle-watchdog.timer
