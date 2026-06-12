"""임시 앱 아이콘 생성(일회성). 사용: pip install pillow && python scripts/make_icon.py

Pillow는 이 스크립트 실행에만 필요한 개발용 도구다(런타임/빌드 의존성 아님).
tokenomy.spec의 excludes에서 PIL 제외를 유지한다.
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "assets" / "tokenomy.ico"
OUT.parent.mkdir(parents=True, exist_ok=True)

img = Image.new("RGBA", (256, 256), (37, 99, 235, 255))  # Tokenomy 블루
d = ImageDraw.Draw(img)
# 폰트 비의존 'T' 마크(추후 교체)
d.rectangle([56, 64, 200, 100], fill=(255, 255, 255, 255))    # 가로 막대
d.rectangle([110, 100, 146, 196], fill=(255, 255, 255, 255))  # 세로 막대
img.save(OUT, sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
print(f"wrote {OUT}")
