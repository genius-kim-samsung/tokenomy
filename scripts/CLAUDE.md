# scripts/ — 개발용 스크립트

전부 dev 전용(런타임/배포 번들 미포함). 개인 DB(`data/`)를 오염시키지 않는 격리 원칙을 지킨다.
전역 지도는 [루트 CLAUDE.md](../CLAUDE.md) 참고.

## 소유 범위 (owns)

- `scripts/make_icon.py` — 앱/트레이 아이콘 일회성 생성. Pillow는 이 스크립트에만 필요(런타임 의존성 아님 — tokenomy.spec의 PIL excludes 유지).
- `scripts/seed_official_enterprise.py` — 엔터프라이즈 공식 사용량 view를 개인 계정 PC에서 미리보기. 실측 fixture를 격리 DB(`~/.tokenomy-ent-preview`)에 시드.
- `scripts/seed_official_showcase.py` — 공식 카드 시각 상태 쇼케이스(ADR 0002). 시나리오 3종(A 게이지 / B 에러+폴백 / C rate-window)을 격리 DB 3개로 시드(포트 8801~8803).
- `scripts/check_context_paths.py` — 컨텍스트 문서(CLAUDE.md 등)의 경로 참조 검증. CI(context-check 워크플로)와 pytest(`tests/test_context_paths.py`)가 실행.

## 실행 (quick commands)

```powershell
.venv\Scripts\python scripts\seed_official_enterprise.py   # 시드 후 실행 안내 출력
.venv\Scripts\python scripts\check_context_paths.py        # 경로 검증 (exit 0=통과)
```

## 수정 패턴 (common patterns)

- 새 시드 스크립트는 `TOKENOMY_DATA` 격리 디렉토리 + `TOKENOMY_SKIP_OFFICIAL_FETCH=1` 조합을 따른다(개인 DB·라이브 API 미오염).
- 공식 fixture는 tests 쪽 커밋 정본을 재사용한다 — 실측 원문(`fixtures/official/local/`)은 커밋 금지.

## 비자명 게시 (gotchas)

- 주의: 시드는 반드시 격리 데이터 디렉토리로 — 기본 `data/`를 가리키게 하지 말 것(프라이버시 경계).
- 시드 스크립트는 official_fetch(아웃바운드)를 우회하고 파서→DB 경로만 재사용한다 — 네트워크 없음.

## 모듈 간 의존성 (cross-module dependencies)

- `tokenomy` 패키지를 레포 루트 기준 sys.path 삽입으로 import한다(레포 루트에서 실행 가정).
- 골든 fixture는 [tests/](../tests/CLAUDE.md)와 공유.
