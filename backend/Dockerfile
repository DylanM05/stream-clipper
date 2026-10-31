# Built from the repo root (see docker-compose.yml): the phone app is built first, then copied in.

# --- Phone app ---
FROM node:24-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY frontend/ ./
RUN npm run build

# --- Worker and web app: CUDA 12 + cuDNN 9 for faster-whisper, ffmpeg with NVENC, Claude Code ---
FROM nvidia/cuda:12.9.1-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # "video" makes the NVIDIA runtime mount the NVENC/NVDEC libraries that ffmpeg needs.
    NVIDIA_DRIVER_CAPABILITIES=compute,video,utility

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv ffmpeg fonts-dejavu-core curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# yt-dlp needs a JavaScript runtime to download from YouTube; deno is the one it uses by default.
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

# Codex, for when the AI work goes through a ChatGPT plan (chosen in the app). A fixed release, checked against the
# checksums GitHub lists for it: change the version and both sums together.
RUN arch="$(uname -m)" \
 && case "$arch" in \
      x86_64) sum=beb339e8ecd9ba057a6bb2dc81ad8ca474812edfbf0fc5056ca818797648dde4 ;; \
      aarch64) sum=a7261e65f4a96d8f4ca65a069c6cf2dc2c33defc341e48078acefda9461fffe4 ;; \
      *) echo "No Codex build for $arch" >&2; exit 1 ;; \
    esac \
 && curl -fsSL -o /tmp/codex.tar.gz \
      "https://github.com/openai/codex/releases/download/rust-v0.163.0/codex-$arch-unknown-linux-musl.tar.gz" \
 && echo "$sum  /tmp/codex.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/codex.tar.gz -C /tmp \
 && install -m 755 "/tmp/codex-$arch-unknown-linux-musl" /usr/local/bin/codex \
 && rm -f /tmp/codex.tar.gz "/tmp/codex-$arch-unknown-linux-musl"

# Run as uid 1000 (usually your own user on the host), so files in the data folder stay yours.
RUN userdel --remove ubuntu 2>/dev/null || true \
 && useradd --create-home --uid 1000 clipper \
 && mkdir -p /data /models /home/clipper/.claude-config /home/clipper/.codex-config \
 && chown clipper:clipper /data /models /home/clipper/.claude-config /home/clipper/.codex-config

RUN python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
COPY backend/pyproject.toml ./
RUN mkdir clipper && touch clipper/__init__.py && pip install ".[transcribe]"
COPY backend/clipper ./clipper
RUN pip install --no-deps --force-reinstall .
COPY --from=web /web/dist /app/web

USER clipper
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH=/home/clipper/.local/bin:$PATH \
    # Claude Code's and Codex's sign-ins and settings live in volumes, so they survive rebuilds.
    CLAUDE_CONFIG_DIR=/home/clipper/.claude-config \
    CODEX_HOME=/home/clipper/.codex-config \
    # Updates come from rebuilding the image instead.
    DISABLE_AUTOUPDATER=1 \
    HF_HOME=/models \
    CLIPPER_DATA_DIR=/data \
    CLIPPER_WEB_DIR=/app/web

ENTRYPOINT ["clipper"]
