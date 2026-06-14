# Tailwind CSS 빌드 — dev-time 1회. 산출물 app.css는 커밋한다(런타임은 무빌드 유지).
# 사용: .\build_css.ps1   (CSS/템플릿 클래스 바꿨을 때만 재실행)
& "$PSScriptRoot\tools\tailwindcss.exe" `
  -i "$PSScriptRoot\tokenomy\web\static\src\input.css" `
  -o "$PSScriptRoot\tokenomy\web\static\app.css" --minify
Write-Host "built: tokenomy\web\static\app.css"
