#!/usr/bin/env bash
# 在「有網路」的電腦上執行：建置映像檔並匯出成離線安裝包
#   ./scripts/build_offline.sh            → dist/digital-human-mcp-1.0.0.tar.gz
# 公司有 TLS 攔截代理時：CA_CERT=/path/corp-ca.crt ./scripts/build_offline.sh
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION="${VERSION:-1.0.0}"
IMAGE="digital-human-mcp:${VERSION}"
ARGS=()
if [[ -n "${CA_CERT:-}" ]]; then ARGS+=(--secret "id=ca_cert,src=${CA_CERT}"); fi
docker build "${ARGS[@]}" -t "$IMAGE" .
mkdir -p dist
echo "匯出映像檔（壓縮後約 650MB），請稍候..."
docker save "$IMAGE" | gzip > "dist/digital-human-mcp-${VERSION}.tar.gz"
# 一起打包部署檔
tar czf "dist/digital-human-deploy-${VERSION}.tar.gz" docker-compose.yml .env.example configs scripts README.md
sha256sum dist/*.tar.gz | tee dist/SHA256SUMS
echo "完成。請把 dist/ 內所有檔案帶進公司內網，執行 scripts/load_offline.sh"
