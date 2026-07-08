# PyInstaller onefile spec — Tokenomy.exe
# 선행: pip install pyinstaller   (런타임 아님 — requirements.txt 미포함, CI는 별도 설치)
# 빌드 환경 주의: pywebview가 설치된 환경(.venv)에서 빌드할 것. pywebview 없는 Python으로
#   빌드하면 webview가 번들에서 빠져, exe가 네이티브 창 대신 브라우저로 fallback한다.
# 빌드: .venv\Scripts\python -m PyInstaller tokenomy.spec   →   dist/Tokenomy.exe
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

a = Analysis(
    ['tokenomy/launcher.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('config/pricing.json', 'config'),
        ('config/bucket_catalog.json', 'config'),   # 코드네임 버킷 큐레이션(ADR 0016) — 번들 누락 시 큐레이션 미적용
        ('config/saver_catalog.json', 'config'),    # 토큰 절약 카탈로그(ADR 0026) — 번들 누락 시 절약 화면 빈 목록
        ('assets/tokenomy.ico', 'assets'),
        ('tokenomy/web/templates', 'tokenomy/web/templates'),
        ('tokenomy/web/static', 'tokenomy/web/static'),
    ] + collect_data_files('webview'),
    hiddenimports=(
        collect_submodules('uvicorn')
        + collect_submodules('webview')
        + collect_submodules('pystray')
        + ['clr', 'PIL.Image']  # pywebview→pythonnet, pystray→PIL 이미지 로드
    ),
    hookspath=[],
    runtime_hooks=[],
    # 런타임 미사용 의존성 제외. PIL은 pystray 트레이 아이콘 로드에 사용하므로 제외 해제.
    excludes=['pytest', '_pytest', 'httpx', 'numpy', 'setuptools'],
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Tokenomy',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                 # 콘솔 제거 — 네이티브 창
    icon='assets/tokenomy.ico',
    disable_windowed_traceback=False,
)
