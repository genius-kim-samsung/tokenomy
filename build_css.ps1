# Tailwind CSS 빌드 — dev-time 1회. 산출물 app.css는 커밋한다(런타임은 무빌드 유지).
# 사용: .\build_css.ps1   (CSS/템플릿 클래스 바꿨을 때만 재실행)
# 바이너리(tools\tailwindcss.exe)는 gitignore — 최초 1회 내려받기:
#   Invoke-WebRequest "https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-windows-x64.exe" -OutFile tools\tailwindcss.exe
& "$PSScriptRoot\tools\tailwindcss.exe" `
  -i "$PSScriptRoot\tokenomy\web\static\src\input.css" `
  -o "$PSScriptRoot\tokenomy\web\static\app.css" --minify
if ($LASTEXITCODE -ne 0) { Write-Error "Tailwind 빌드 실패 (exit $LASTEXITCODE)"; exit 1 }
Write-Host "built: tokenomy\web\static\app.css"
