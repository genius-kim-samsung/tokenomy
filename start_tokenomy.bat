@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

echo [Tokenomy] 세션 로그 수집 중...
"%PY%" -m tokenomy.cli ingest

echo [Tokenomy] 대시보드 기동 중... 서버가 뜨면 브라우저가 자동으로 열립니다.
echo 종료하려면 이 창을 닫으세요. (http://127.0.0.1:8765)
start "" cmd /c "for /l %%i in (1,1,30) do (timeout /t 1 /nobreak >nul & curl -s -o nul http://127.0.0.1:8765/ >nul 2>&1 && (start "" http://127.0.0.1:8765 & exit))"
"%PY%" -m uvicorn tokenomy.web.app:app --host 127.0.0.1 --port 8765
