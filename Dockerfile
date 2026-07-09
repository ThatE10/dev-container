# ══════════════════════════════════════════════════════════════════════════════
# Stage 1 — smoke-test
# Purpose: validate our additional packages install cleanly on Python 3.12.
# Used exclusively by CI (no GPU, no CUDA kernel build, fast).
# ══════════════════════════════════════════════════════════════════════════════
FROM python:3.12-slim AS smoke-test

# Only install packages that have no torch/CUDA dependency.
# peft / trl / sae-lens are torch-dependent — validated in the full GPU image only.
COPY requirements-smoke.txt /tmp/requirements-smoke.txt
RUN pip install --no-cache-dir -r /tmp/requirements-smoke.txt

RUN python -c "\
import plotly; \
import marimo; \
import modal; \
import jupyterlab; \
print('smoke-test: all imports OK')"


# ══════════════════════════════════════════════════════════════════════════════
# Stage 2 — dev (full GPU container)
# Base: official vLLM image — ships torch, transformers, huggingface-hub,
#       tokenizers, CUDA kernels (FlashAttention, PagedAttention) pre-built.
# Pin this tag. Check https://hub.docker.com/r/vllm/vllm-openai/tags
# ══════════════════════════════════════════════════════════════════════════════
FROM vllm/vllm-openai:v0.6.4 AS dev

LABEL org.opencontainers.image.source="https://github.com/ThatE10/dev-container"
LABEL org.opencontainers.image.description="GPU dev container — vLLM, Claude Code, PyTorch, Marimo, Jupyter"

# ── System packages ───────────────────────────────────────────────────────────
# Remove the NVIDIA CUDA apt repo that ships inside the vLLM base image.
# CUDA is already installed; keeping the repo just causes transient mirror
# failures during apt-get update (mirror sync races on NVIDIA's CDN).
RUN rm -f /etc/apt/sources.list.d/cuda*.list \
          /etc/apt/sources.list.d/nvidia*.list \
          /etc/apt/sources.list.d/*cuda* \
    && apt-get update && apt-get install -y --no-install-recommends \
        openssh-server \
        curl \
        git \
        vim \
        nano \
        tmux \
        htop \
        wget \
        unzip \
        ca-certificates \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] \
        https://cli.github.com/packages stable main" \
        | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# ── SSH daemon ────────────────────────────────────────────────────────────────
RUN mkdir -p /var/run/sshd \
    && sed -i \
        -e 's/#PermitRootLogin prohibit-password/PermitRootLogin yes/' \
        -e 's/#PasswordAuthentication yes/PasswordAuthentication yes/' \
        -e 's/PasswordAuthentication no/PasswordAuthentication yes/' \
        /etc/ssh/sshd_config \
    && printf '\nX11Forwarding yes\nAllowTcpForwarding yes\n' >> /etc/ssh/sshd_config

# ── Additional Python packages ────────────────────────────────────────────────
# Do NOT list torch / transformers / huggingface-hub / tokenizers here —
# they ship with the base image; re-listing risks pip downgrading them.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# ── Jupyter kernel ────────────────────────────────────────────────────────────
RUN python3 -m ipykernel install \
        --name "dev-container" \
        --display-name "Dev Container (GPU)"

# ── JupyterLab config — bind to all interfaces, no token (SSH tunnel = auth) ─
RUN mkdir -p /root/.jupyter && cat > /root/.jupyter/jupyter_lab_config.py << 'EOF'
c.ServerApp.ip = '0.0.0.0'
c.ServerApp.port = 8888
c.ServerApp.open_browser = False
c.ServerApp.token = ''
c.ServerApp.password = ''
c.ServerApp.allow_root = True
c.ServerApp.allow_origin = '*'
EOF

# ── Marimo config — bind to all interfaces ───────────────────────────────────
RUN mkdir -p /root/.config/marimo && cat > /root/.config/marimo/marimo.toml << 'EOF'
[server]
host = "0.0.0.0"
port = 2719
headless = true

[display]
theme = "dark"
EOF

# ── HuggingFace cache (mount from host for weight persistence) ────────────────
ENV HF_HOME=/root/.cache/huggingface
ENV TRANSFORMERS_CACHE=/root/.cache/huggingface/transformers

# ── Claude Code credentials location ─────────────────────────────────────────
# The entrypoint writes CLAUDE_CREDENTIALS → ~/.claude/.credentials.json
# so `claude` works without re-authenticating inside the container.

# ── Scripts ───────────────────────────────────────────────────────────────────
COPY scripts/ /opt/scripts/
RUN chmod +x /opt/scripts/*.sh

# ── Ports ────────────────────────────────────────────────────────────────────
# 22   — SSH
# 8888 — JupyterLab
# 2719 — Marimo
# 8080 — general / vLLM OpenAI-compat API
EXPOSE 22 8888 2719 8080

ENTRYPOINT ["/opt/scripts/entrypoint.sh"]
CMD ["tail", "-f", "/dev/null"]
