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
    # 런타임 미사용 의존성 제외 — 빌드 그래프로만 새어 들어오는 군더더기 차단.
    # 'pytest'만으론 형제 패키지 '_pytest'(→numpy 동봉)를 못 막는다.
    # numpy/PIL은 Tokenomy 코드가 전혀 import하지 않음(각각 _pytest, pygments 경유 누수).
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
    console=True,           # 콘솔: 종료법 안내 + --version 스모크
    disable_windowed_traceback=False,
)
