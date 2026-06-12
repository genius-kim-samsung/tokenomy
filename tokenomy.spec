# PyInstaller onefile spec — Tokenomy.exe
# 빌드: pyinstaller tokenomy.spec   →   dist/Tokenomy.exe
from PyInstaller.utils.hooks import collect_submodules

a = Analysis(
    ['tokenomy/launcher.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('config/pricing.json', 'config'),
        ('tokenomy/web/templates', 'tokenomy/web/templates'),
        ('tokenomy/web/static', 'tokenomy/web/static'),
    ],
    hiddenimports=collect_submodules('uvicorn'),
    hookspath=[],
    runtime_hooks=[],
    excludes=['pytest', 'httpx'],
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
    console=True,           # 콘솔: 종료법 안내 + --version 스모크
    disable_windowed_traceback=False,
)
