# syntax=docker/dockerfile:1
# 智慧數字人 MCP — offline-ready image (no internet needed at runtime).
# Build on a machine WITH internet; ship with `docker save` (see scripts/build_offline.sh).

ARG PLAYWRIGHT_IMAGE=mcr.microsoft.com/playwright/python:v1.58.0-noble
ARG BASE_IMAGE=ubuntu:24.04

# Only used to copy the exact Chromium build that matches playwright==1.58.0
FROM ${PLAYWRIGHT_IMAGE} AS pw

FROM ${BASE_IMAGE}
ARG DEBIAN_FRONTEND=noninteractive
ARG PIP_INDEX_URL=https://pypi.org/simple
# Mandarin neural voice for Piper. The fallback is a lower-quality mirror on GitHub.
ARG PIPER_VOICE_BASE=https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/zh/zh_CN/huayan/medium
ARG PIPER_VOICE_NAME=zh_CN-huayan-medium
ARG PIPER_FALLBACK_TGZ=https://github.com/rhasspy/piper/releases/download/v0.0.2/voice-zh-cn-huayan-x-low.tar.gz

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python-is-python3 ffmpeg fonts-noto-cjk espeak-ng curl ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# Optional corporate CA for TLS-intercepting proxies:  docker build --secret id=ca_cert,src=/path/ca.crt .
RUN --mount=type=secret,id=ca_cert,required=false \
    if [ -s /run/secrets/ca_cert ]; then export PIP_CERT=/run/secrets/ca_cert; fi; \
    pip install --no-cache-dir --break-system-packages -i ${PIP_INDEX_URL} -r requirements.txt

# Chromium system libraries (apt) + the headless Chromium build and Playwright's video encoder
RUN playwright install-deps chromium && rm -rf /var/lib/apt/lists/*
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
COPY --from=pw /ms-playwright/chromium_headless_shell-1208 /ms-playwright/chromium_headless_shell-1208
COPY --from=pw /ms-playwright/ffmpeg-1011 /ms-playwright/ffmpeg-1011

# Voice model: try the medium-quality voice, then the fallback; never fail the build.
RUN --mount=type=secret,id=ca_cert,required=false \
    if [ -s /run/secrets/ca_cert ]; then export CURL_CA_BUNDLE=/run/secrets/ca_cert; fi; \
    mkdir -p /opt/voices && cd /opt/voices && \
    ( curl -fsSL -o ${PIPER_VOICE_NAME}.onnx "${PIPER_VOICE_BASE}/${PIPER_VOICE_NAME}.onnx" && \
      curl -fsSL -o ${PIPER_VOICE_NAME}.onnx.json "${PIPER_VOICE_BASE}/${PIPER_VOICE_NAME}.onnx.json" ) || \
    ( rm -f ./*.onnx* ; curl -fsSL -o /tmp/v.tgz "${PIPER_FALLBACK_TGZ}" && tar xzf /tmp/v.tgz -C /opt/voices && rm /tmp/v.tgz ) || \
    echo "WARNING: no Piper voice downloaded; TTS will fall back to espeak-ng" ; \
    ls -la /opt/voices

COPY dh_mcp ./dh_mcp
COPY demo ./demo
COPY tests ./tests

ENV PYTHONUNBUFFERED=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    OUTPUT_DIR=/data/outputs \
    PROJECTS_DIR=/workspace \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    TTS_ENGINE=piper \
    SUBTITLE_FONT="Noto Sans CJK TC" \
    LLM_BASE_URL=http://172.28.26.96:9192/v1 \
    LLM_API_KEY=EMPTY \
    NO_PROXY=localhost,127.0.0.1

RUN mkdir -p /data/outputs /workspace && python -c "import dh_mcp.server"
VOLUME ["/data/outputs"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD curl -fs http://127.0.0.1:${MCP_PORT}/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "dh_mcp"]
CMD ["--transport", "http"]
