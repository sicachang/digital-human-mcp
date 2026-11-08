#!/usr/bin/env bash
# 在「公司內網」主機執行：載入映像檔並啟動
#   ./scripts/load_offline.sh dist/digital-human-mcp-1.0.0.tar.gz
set -euo pipefail
TAR="${1:-dist/digital-human-mcp-1.0.0.tar.gz}"
echo "載入映像檔 $TAR ..."
gunzip -c "$TAR" | docker load
cd "$(dirname "$0")/.."
[[ -f .env ]] || { cp .env.example .env; echo "已建立 .env，請確認 LLM_BASE_URL 與 PUBLIC_BASE_URL"; }
mkdir -p outputs projects
docker compose up -d
sleep 5
curl -fs "http://127.0.0.1:${DH_PORT:-8000}/healthz" && echo " ← 服務已啟動"
echo "健康檢查（含 LLM 連線）：在 Qwen Code 中請呼叫 dh_check_environment"
