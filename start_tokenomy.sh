#!/usr/bin/env bash
# Tokenomy 실행(Ubuntu/Linux, ADR 0013) — venv python으로 launcher를 띄운다.
# launcher가 ingest 1회 → uvicorn(127.0.0.1) → pywebview GTK 창 + AppIndicator 트레이까지
# 담당한다(상주 모드). 미니 뷰는 Linux에서 비활성(mini_view_available=False, Wayland).
# Windows의 start_tokenomy.bat(브라우저+uvicorn)와 달리 여기선 네이티브 창이 목적이라 launcher를 쓴다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PY="$HERE/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "[Tokenomy] .venv를 찾지 못했습니다. 먼저 ./install.sh를 실행하세요." >&2
  exit 1
fi

exec "$PY" -m tokenomy.launcher "$@"
