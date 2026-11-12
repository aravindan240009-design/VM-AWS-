#!/usr/bin/env bash
# Brain setup for CPU instances (c7i-flex etc.) using llama-cpp-python
# Serves an OpenAI-compatible API on port 8000
set -euo pipefail
exec > /var/log/brain-init.log 2>&1
echo "=== Brain CPU setup started at $(date) ==="

MODEL_DIR=/opt/brain/models
MODEL_FILE="Llama-3.2-3B-Instruct-uncensored-Q4_K_M.gguf"
MODEL_URL="https://huggingface.co/bartowski/Llama-3.2-3B-Instruct-uncensored-GGUF/resolve/main/${MODEL_FILE}"
VENV=/opt/brain/.venv
THREADS=$(nproc)

# 1. Install system deps
dnf install -y python3-pip python3-devel gcc gcc-c++ make cmake wget || true
pip3 install --upgrade pip

# 2. Create venv + install llama-cpp-python (CPU build)
mkdir -p /opt/brain
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
CMAKE_ARGS="-DLLAMA_BLAS=ON -DLLAMA_BLAS_VENDOR=OpenBLAS" \
  "$VENV/bin/pip" install llama-cpp-python[server] --no-cache-dir

# 3. Download model
mkdir -p "$MODEL_DIR"
if [ ! -f "$MODEL_DIR/$MODEL_FILE" ]; then
  echo "Downloading $MODEL_FILE ..."
  wget -q --show-progress -O "$MODEL_DIR/$MODEL_FILE" "$MODEL_URL"
fi
echo "Model ready: $(du -h $MODEL_DIR/$MODEL_FILE | cut -f1)"

# 4. Create systemd service
cat > /etc/systemd/system/brain.service << EOF
[Unit]
Description=llama-cpp-python OpenAI-compatible brain
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$VENV/bin/python -m llama_cpp.server \\
  --model $MODEL_DIR/$MODEL_FILE \\
  --host 0.0.0.0 \\
  --port 8000 \\
  --n_threads $THREADS \\
  --n_ctx 4096 \\
  --chat_format llama-3
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now brain.service
echo "=== Brain service started at $(date) ==="
echo "=== Check: systemctl status brain ==="
