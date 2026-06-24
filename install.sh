#!/usr/bin/env bash
# Tokenomy Ubuntu(24.04 LTS) 설치 — 소스 실행 우선(ADR 0013, 단일 바이너리 미생성).
# apt 시스템 의존성 → venv(--system-site-packages) → pip 런타임 → 앱 메뉴(.desktop) 등록.
# 코어(parser/db/aggregate/official_fetch 등)는 OS 중립. 미니 뷰는 Linux 제외(Wayland).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

echo "[1/4] apt 시스템 의존성 설치 (sudo 필요)..."
# pywebview GTK 백엔드는 PyGObject(python3-gi)+WebKit2GTK에, pystray 트레이는
# AppIndicator(ayatana)에 의존한다. 이들은 pip 빌드가 아프므로 apt로 받는다.
sudo apt-get update
sudo apt-get install -y \
  python3-venv python3-pip python3-gi \
  gir1.2-gtk-3.0 \
  gir1.2-webkit2-4.1 libwebkit2gtk-4.1-0 \
  libayatana-appindicator3-1 gir1.2-ayatanaappindicator3-0.1

echo "[2/4] venv 생성(--system-site-packages — apt python3-gi 가시화)..."
# PyGObject를 pip로 빌드하면 libgirepository1.0-dev·libcairo2-dev·build-essential이
# 필요해 고통스럽다. 가장 덜 아픈 길은 venv가 시스템 site-packages(apt python3-gi)를
# 보게 하는 것(ADR 0013). 그래서 PyGObject는 pip로 설치하지 않는다(apt로 제공).
if [ ! -d .venv ]; then
  python3 -m venv --system-site-packages .venv
fi

echo "[3/4] pip 런타임 의존성 설치..."
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
# pywebview(GTK)·pystray·pillow는 requirements.txt에 포함. PyGObject는 위 apt가 제공.

echo "[4/4] 앱 메뉴(.desktop) 등록..."
chmod +x start_tokenomy.sh
APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"
# 템플릿의 @TOKENOMY_DIR@를 실제 설치 경로로 치환해 설치.
sed "s#@TOKENOMY_DIR@#$HERE#g" tokenomy.desktop > "$APPS/tokenomy.desktop"
update-desktop-database "$APPS" 2>/dev/null || true

echo
echo "완료. 앱 메뉴에서 'Tokenomy'를 실행하거나, 터미널에서 ./start_tokenomy.sh"
