#!/usr/bin/env bash
# 개발 실행 — http://127.0.0.1:8090
#   ★GPS 는 HTTPS 에서만 동작한다. 127.0.0.1 은 브라우저가 보안 컨텍스트로 쳐 주므로
#     로컬 테스트에서는 GPS 도 된다. 다른 기기에서 IP 로 붙으면 GPS 만 안 된다.
set -euo pipefail
cd "$(dirname "$0")"
exec uvicorn server.app:app --host 0.0.0.0 --port "${PORT:-8090}" "$@"
