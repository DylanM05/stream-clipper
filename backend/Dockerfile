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

# Run as uid 1000 (usually your own user on the host), so files in the data folder stay yours.
RUN userdel --remove ubuntu 2>/dev/null || true \
 && useradd --create-home --uid 1000 clipper \
 && mkdir -p /data /models /home/clipper/.claude-config \
 && chown clipper:clipper /data /models /home/clipper/.claude-config

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
    # Claude Code's login and settings live in a volume, so they survive rebuilds.
    CLAUDE_CONFIG_DIR=/home/clipper/.claude-config \
    # Updates come from rebuilding the image instead.
    DISABLE_AUTOUPDATER=1 \
    HF_HOME=/models \
    CLIPPER_DATA_DIR=/data \
    CLIPPER_WEB_DIR=/app/web

ENTRYPOINT ["clipper"]
