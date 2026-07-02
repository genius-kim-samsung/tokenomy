# tests/ — pytest 스위트

코어 모듈과 1:1 대응하는 25+개 파일. 대상 코드 지도는 [tokenomy/](../tokenomy/CLAUDE.md),
전역 관행 정본은 [루트 CLAUDE.md](../CLAUDE.md).

## 실행 (quick commands)

```powershell
.venv\Scripts\python -m pytest                      # 전체
.venv\Scripts\python -m pytest tests\test_web.py    # 단일 파일
.venv\Scripts\python -m pytest -k official          # 키워드 선택
```

## 소유 범위 (owns)

- `test_<모듈>.py`가 `tokenomy/<모듈>.py`를 커버 — 예: `tests/test_web.py` ↔ `tokenomy/web/app.py`+`tokenomy/web/views.py`.
- `fixtures/` — 공식 API 응답 골든 fixture(전부 2026-06 기준). 실측 원문은 `fixtures/official/local/`(gitignore, 커밋 금지).
- `tests/test_context_paths.py` — 컨텍스트 문서 경로 참조 회귀 가드(`scripts/check_context_paths.py` 실행).

## 수정 패턴 (common patterns)

- 새 시간 의존 테스트: 2026-06 시드를 따른다 — view 직접 호출은 `now_kst` 주입,
  라우트(TestClient) 경유는 `test_web.py`의 `_client`(datetime을 2026-06-20 12:00 KST 고정 대역으로 교체) 재사용.
- 웹 테스트 격리: `TOKENOMY_CONFIG`(tmp config) + `TOKENOMY_SKIP_UPDATE_CHECK` + `TOKENOMY_SKIP_OFFICIAL_FETCH`.
- 포트가 필요하면 임시 포트(`bind 0`)를 기준점으로 — 8765 하드코딩 금지(실행 중인 앱과 충돌).

## 비자명 게시 (gotchas)

- 반드시 실제 시계·고정 포트를 쓰지 않는다. 골든 fixture 날짜 변조 금지 — 테스트 쪽 시계를 고정한다.
- conftest.py 없음 — 파일별 자급자족(픽스처 헬퍼는 각 파일 상단에 둔다).

## 모듈 간 의존성 (cross-module dependencies)

- 골든 fixture는 dev 시드 스크립트([scripts/](../scripts/CLAUDE.md))와 공유한다.
- 릴리스 검증(버전-태그 일치·exe smoke)은 pytest 밖 — `.github/workflows/`의 release 워크플로 담당.
