<!--
  PR 셀프리뷰 체크리스트. 단일 유지보수자·agent PR 모두 대상 —
  머지 전 independent critic 게이트로 사용한다. 해당 없는 항목은 취소선(~~ ~~) 또는 N/A 표기.
-->

## 무엇을 · 왜

<!-- 1~3줄. 변경의 의도와 근거(어떤 ADR/이슈/버그). -->

-

## 검증 (증거 붙일 것)

- [ ] `.venv\Scripts\python -m pytest` 통과 — 결과 요약:
- [ ] 문서 경로 참조 검증 통과 (`python scripts/check_context_paths.py`) — context-check CI가 PR마다 재확인
- [ ] `/verify` 또는 실제 플로우 구동으로 런타임 동작 확인(코드 변경 시)

## 불변식 체크 (해당 시)

- [ ] **프라이버시 경계** — 파서 변경이 토큰 메타 + 첫 프롬프트 120자 발췌선을 넘지 않음(대화 본문·프롬프트 전체 DB 미적재)
- [ ] **웹 바인딩** — `127.0.0.1`만, 네트워크 노출 없음. 쿼리 파라미터는 화이트리스트 fallback
- [ ] **계층 분리** — 라우트(app.py 얇게) ↔ views.py ↔ aggregate.py ↔ db.py 경계 유지
- [ ] **공식 사용량** — 취득(갱신)은 웹 라우트, 수집(ingest)은 로컬 재스캔만(ADR 0003) — 분리 유지
- [ ] **데이터 위치** — 소스=repo 루트 / exe=`~/.tokenomy/` 분기(`paths.data_dir()`) 깨지지 않음

## 프론트엔드 (CSS/템플릿 변경 시)

- [ ] `.\build_css.ps1` 재빌드 후 산출 `app.css` 커밋(런타임 무빌드 유지)

## 컨텍스트 문서 (구조·불변식·명령어 변경 시)

- [ ] 관련 `CLAUDE.md`(루트/모듈) 갱신 — 게시(gotcha)·소유 범위·명령어 정합

## 릴리스 (버전 배포 시)

- [ ] `tokenomy/__init__.py`의 `__version__`과 git 태그(`v<버전>`) 일치(release.yml 검증)
- [ ] exe 빌드는 `.venv`로(시스템 Python 금지 — pywebview 누락 fallback)

## 리스크 · 롤백

<!-- 되돌리기 난이도, 마이그레이션·데이터 영향. 없으면 "없음". -->

-
