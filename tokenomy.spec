# PyInstaller onefile spec — Tokenomy.exe
# 빌드: pyinstaller tokenomy.spec   →   dist/Tokenomy.exe
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

a = Analysis(
    ['tokenomy/launcher.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('config/pricing.json', 'config'),
        ('tokenomy/web/templates', 'tokenomy/web/templates'),
        ('tokenomy/web/static', 'tokenomy/web/static'),
    ] + collect_data_files('webview'),
    hiddenimports=(
        collect_submodules('uvicorn')
        + collect_submodules('webview')
        + ['clr']  # pythonnet(.NET interop) — Windows EdgeChromium 백엔드
    ),
    hookspath=[],
    runtime_hooks=[],
    # 런타임 미사용 의존성 제외. PIL/numpy는 Tokenomy/pywebview 런타임이 import하지 않음.
    excludes=['pytest', '_pytest', 'httpx', 'numpy', 'PIL', 'setuptools'],
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
