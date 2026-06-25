@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM 개발용 — 코드(.py) 변경 시 uvicorn이 자동 재시작(--reload).
REM reload가 필요 없으면 start_tokenomy.bat 을 쓰세요(reload 오버헤드 없음). 둘 다 개발/소스 실행용 — Windows 최종 사용자는 exe.

if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

echo [Tokenomy DEV] 세션 로그 수집 중...
"%PY%" -m tokenomy.cli ingest

echo [Tokenomy DEV] 대시보드 기동 중 (--reload: 코드 변경 시 자동 재시작).
echo 종료하려면 이 창에서 Ctrl+C. (http://127.0.0.1:8765)
start "" cmd /c "@echo off & for /l %%i in (1,1,30) do (timeout /t 1 /nobreak >nul & curl -s -o nul http://127.0.0.1:8765/ >nul 2>&1 && (start "" http://127.0.0.1:8765 & exit))"
"%PY%" -m uvicorn tokenomy.web.app:app --host 127.0.0.1 --port 8765 --reload
