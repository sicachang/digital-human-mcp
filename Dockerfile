# syntax=docker/dockerfile:1
# 智慧數字人 MCP — offline-ready image.
# Build this on a machine WITH internet, then `docker save` and move the tar into the intranet
# (see scripts/build_offline.sh). At runtime the container needs no internet at all.
FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

ARG DEBIAN_FRONTEND=noninteractive
ARG PIP_INDEX_URL=https://pypi.org/simple
# Mandarin neural voice for Piper (≈60 MB). The second URL is a fallback mirror (lower quality).
ARG PIPER_VOICE_BASE=https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/zh/zh_CN/huayan/medium
ARG PIPER_VOICE_NAME=zh_CN-huayan-medium
ARG PIPER_FALLBACK_TGZ=https://github.com/rhasspy/piper/releases/download/v0.0.2/voice-zh-cn-huayan-x-low.tar.gz

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg fonts-noto-cjk fonts-noto-color-emoji espeak-ng curl ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# Optional: corporate CA for TLS-intercepting proxies →  docker build --secret id=ca_cert,src=/path/ca.crt .
RUN --mount=type=secret,id=ca_cert,required=false \
    if [ -s /run/secrets/ca_cert ]; then export PIP_CERT=/run/secrets/ca_cert; fi; \
    pip install --no-cache-dir --break-system-packages -i ${PIP_INDEX_URL} -r requirements.txt

# Voice model: try the medium-quality voice, fall back to the x-low voice; never fail the build.
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
    OUTPUT_DIR=/data/outputs \
    PROJECTS_DIR=/workspace \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    TTS_ENGINE=piper \
    SUBTITLE_FONT="Noto Sans CJK TC" \
    LLM_BASE_URL=http://172.28.26.96:9192/v1 \
    NO_PROXY=localhost,127.0.0.1

RUN mkdir -p /data/outputs /workspace && python -c "import dh_mcp.server"
VOLUME ["/data/outputs"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD curl -fs http://127.0.0.1:${MCP_PORT}/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "dh_mcp"]
CMD ["--transport", "http"]
