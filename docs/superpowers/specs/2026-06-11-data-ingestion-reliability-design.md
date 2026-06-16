# 설계: 데이터 수집·보존 신뢰성 (데이터 레이어)

> 작성일: 2026-06-11 · 프로젝트: tokenomy (토큰 가계부)
> 상위 문서: `상위 기획 문서`
> 관련: `docs/superpowers/specs/2026-06-11-web-dashboard-design.md` (대시보드 — 이 레이어가 채운 DB·아카이브를 *읽기*만 함)
> 참고: `docs/ccusage-분석-보고서.md`
> 출처: 브레인스토밍 세션 2026-06-11 (결정 D1~D4)

## 0. 위치

대시보드(`2026-06-11-web-dashboard-design.md`)와 **독립한 데이터 레이어** 설계다.
대시보드는 이 레이어가 채운 DB·아카이브를 읽기만 하며, 이 레이어는 대시보드의
**선행 조건이 아니다**(대시보드는 현 DB로 동작). 단 freshness 배지(§5)·
`attribution_skill`(§6)을 쓰려면 이 레이어가 먼저다.

## 1. 배경 / 문제

Claude Code raw JSONL(`~/.claude/projects/**/*.jsonl`)은 **기본 30일 후 삭제**된다
(이 PC 실측: 가장 오래된 raw가 정확히 오늘−30일, `cleanupPeriodDays` 미설정 = 기본 30).
가계부가 월 예산을 추적하는데 수집을 한 달 이상 거르면 **그 사이 생성+삭제된 데이터는
영구 유실**된다. 또한 향후 LLM 고도화 복기(토큰 절약법·잘한 방법 추출·요청 패턴 분석)는
메타가 아니라 **대화 원문**을 입력으로 요구한다 → README의 "메타만 저장" 원칙을
**계층 분리로 재정의**(원문은 로컬 고정, 메타만 반출). 구현 시 README·`parser.py`
docstring의 "원문 미저장" 문구도 이 계층 분리에 맞춰 갱신한다.

폐쇄망 PC라 `cleanupPeriodDays` 연장은 managed settings로 막힐 수 있어
**tokenomy가 직접 보존하는 것이 유일하게 통제 가능한 수단**이다.

## 2. 확정 결정 (D1~D4)

| # | 결정 | 선택 | 근거 |
|---|------|------|------|
| D1 | 수집 트리거 | **다중화** (hook + 기동 시 + 수동 + 폴백 스케줄러) | 단일 트리거 의존 시 누락=유실. `ingest`가 이미 idempotent라 중복 안전 |
| D2 | 데이터 모델 | **2계층** (L0 raw archive / L1 SQLite 메타) | raw = 진실의 원천(재파싱·LLM 복기), SQLite = 빠른 집계 파생 |
| D3 | 원문 경계 | **전부 로컬, 공유는 export만** | 중앙 push 미구현(차후) → 보안 표면 0. 원문 외부 반출 금지 |
| D4 | 메타 컬럼 | **report에 쓸 것만 + 재파싱** | raw 보존되면 컬럼을 미리 욕심낼 필요 없음 |

## 3. 수집 트리거 다중화

`db.ingest_root`/`codex_parser.ingest_codex`(offset 증분 + `dedup_key` UNIQUE)를
**단일 core**로 두고 진입점만 늘린다. 모두 같은 idempotent 경로를 호출하므로 몇 번
겹쳐 불려도 안전.

| 트리거 | 메커니즘 | 역할 |
|---|---|---|
| ① hook | `~/.claude/settings.json` SessionEnd hook → `python -m tokenomy.cli ingest` | 세션 종료 시 실시간(그 파일만 증분) |
| ② 기동 시 | 대시보드 §8 런처 시작 ingest + 웹/CLI 진입 시 1회 (`--no-ingest`로 끔) | hook 못 건 사용자도 **웹 열면 최신화** |
| ③ 수동 | 기존 `cli ingest` | 백스톱 |
| ④ 스케줄러 | (폴백) Windows Task Scheduler 가이드 | hook이 managed settings로 막힐 때 |

②는 대시보드 §8 런처의 "시작 시 1회 증분 ingest"와 동일 메커니즘이다(이미 설계됨).
hook 가능 여부는 **구현 1단계 스파이크**로 확인하고, 막혀도 ②가 커버하므로 치명적이지 않다.

## 4. 2계층 데이터 모델

```
ingest ─┬─▶ L1 SQLite (parse → messages/sessions)   [기존]
        └─▶ L0 Raw Archive (raw 라인 증분 복사)       [신규]
```

| 계층 | 위치 | 내용 | source of truth | 경계 |
|---|---|---|---|---|
| **L0 Raw Archive** | `data/archive/<provider>/<상대경로>.jsonl` | raw 원문 통째 | **예** | 로컬 고정 |
| **L1 SQLite** | `data/tokenomy.db` | 파싱 메타 | 아니오(L0서 재구축) | export로만 반출 |

- L0는 ingest가 raw를 읽는 김에 **아직 복사 안 된 라인만** archive로 append
  (`scan_offsets` 패턴 재활용 — 별도 archive offset 추적). 트리거 다중화로 자주
  도니 30일 휘발 전에 확보된다.
- L0는 **parser를 거치지 않는 바이트 복사**다(파싱 경로 L1과 분리). 따라서
  `parser.py`의 "원문 미추출"은 L1에만 해당하고, 원문은 L0에만 남는다.
- 비압축 복사로 시작(이 PC 30일 = 153MB → 월 ~150MB, 1인 부담 없음). 압축(.gz)은 옵션.
- raw가 진실의 원천이므로 L1 스키마를 미리 넓힐 필요가 줄고, 새 필드는 L0에서 재파싱한다.
- `data/`는 README 구조상 gitignore이므로 `data/archive/`도 자연히 버전관리 제외된다.

## 5. freshness(신선도) 가시화

트리거가 모두 실패해도 사람이 위험을 인지하게 만드는 안전벨트.

- **저장**: 마지막 성공 ingest/archive 시각 (작은 `meta` 테이블 또는 `scan_offsets`에 ts).
- **계산**: ① 마지막 수집 경과 ② 디스크상 **가장 오래된 raw 파일 나이** vs 30일.
- **표시**: `수집 최신(2h 전)`(초록) / `⚠ 마지막 수집 6일 전 · 가장 오래된 raw 27일째 —
  3일 내 ingest 안 하면 유실`(빨강). 경고 임계(기본 raw 나이 ≥ 25일)는 상수.
- **대시보드 연동**: 대시보드 §4.1 헤더 mockup의 "마지막 갱신 14:22"를 이 freshness
  배지로 **대체**하고, 대시보드 §6 엣지 케이스에 "freshness 경고 상태"를 추가한다.

## 6. 메타 확장 + 마이그레이션

효율 복기에 당장 쓸 것만 `messages`에 추가:

| 컬럼 | 출처(raw 필드) | 용도 |
|---|---|---|
| `attribution_skill` | `attributionSkill` | **어떤 스킬/명령이 토큰을 태웠나** (효율 복기 핵심 신호) |
| `git_branch` | `gitBranch` | 업무 귀속 보조 |

- `CREATE TABLE IF NOT EXISTS`는 기존 테이블 스키마를 안 바꾸므로 **컬럼 존재 확인 후
  `ALTER TABLE ADD COLUMN`** 하는 경량 마이그레이션 함수를 `db.py`에 추가.
- `service_tier`·캐시 5m/1h 분리 등 나머지는 **L0 archive에서 on-demand 재파싱**(D4).
  (대시보드 §9 "백엔드 엔진 정확도 개선"의 캐시 5m/1h 단가 이슈와 입력을 공유)

## 7. 공유 = export

중앙 push는 만들지 않는다(차후). 공유는 사용자가 명시적으로 떨구는 export로만:

- 리포트/복기 결과를 파일로 → 메일/메신저 첨부. 열람 = 대시보드, 공유 = export(별도 task).
- **산출물 포맷은 후속 결정(TBD)** — 시각 리포트(HTML/PNG) vs 데이터 파일(md·json·csv)은
  export task에서 확정한다. 대시보드 §9와 동일하게 TBD로 둔다.
- **raw 원문(L0)은 export 대상이 아니다** — 로컬 LLM 복기의 입력일 뿐, PC를 떠나지 않는다.

## 8. 미래 대비 (지금 구현 X, 문만 열어둠)

- **LLM 고도화 복기**: 입력은 L0 archive. 실행은 조직 = 로컬 모델 / 개인 데이터 = 집 Claude
  (원문을 외부 LLM으로 보내는 것은 보안 위반이므로 금지). 파이프라인은 차후 task.
- **중앙화**: `dedup_key`가 `provider:message_id`로 전역 유니크 → 미래 다(多)PC 병합
  충돌 없음. user_id는 `tiers.json` `default_user`. 추가 작업 없음.
- **dedup 키 정합 메모**: 대시보드 §9가 제안한 dedup 키 `(message_id, request_id)` 보강은
  백엔드 정확도 task 소관이다. `message_id`가 이미 전역 유니크라, 보강 여부와 무관하게
  위 중앙병합 안전성 결론은 불변이다.

## 9. 구현 순서

권장: **D1 트리거 → D2 L0 아카이브 → §5 freshness → §6 메타** (이후 대시보드가
신규 메타·배지를 소비). export(§7)·LLM 복기(§8)·중앙화(§8)는 후속.

## 10. 테스트 (`tests/test_ingest_reliability.py` 신규)

- **idempotency**: 같은 raw를 2회 ingest → `messages` 행 수·`dedup_key` 불변.
- **증분**: 파일에 라인 append 후 재-ingest → 신규 라인만 반영(offset 동작).
- **L0 아카이브**: ingest 후 `data/archive/`에 원문 라인 존재 + 2회 ingest 시 중복 복사 없음.
- **freshness**: 가장 오래된 raw mtime을 27일 전으로 둔 fixture → 경고 상태 산출.
- **마이그레이션**: 구(舊) 스키마 DB → 마이그레이션 후 신규 컬럼 존재, 기존 행 보존.
