# Task 6 Report: 설정 화면 — 예산 입력 제거, tracked_providers 선택

## Task 2에서 이미 완료된 항목

Task 2는 settings.html에서 `official_enabled`/`official_claude`/`official_codex` 체크박스를 제거하고,
`settings_post`에서 해당 파라미터를 제거했다. 그러나 **budget/budget_start 관련 코드는 그대로 남아 있었다.**

구체적으로 Task 2 이후 잔여 상태:
- `settings_get`: `budget_from_config` import 및 `claude`/`codex`/`budget_start` 컨텍스트 여전히 존재
- `settings_post`: `claude`/`codex`/`budget_start` Form 파라미터가 여전히 `config["budget"]`에 쓰고 있었음
- `settings.html`: `월 예산` 카드(Claude USD, Codex USD, 예산 도입일)가 여전히 존재; `tracked_providers` 언급은 "공식 사용량 자동 취득" 카드 안에 static 문단으로만 존재

## Task 6 순 신규 작업 (Net-new)

### `tests/test_web.py`

**삭제한 테스트 (3개):**
- `test_settings_post_writes_config` — budget.claude/codex 저장 검증
- `test_settings_post_invalid_number_falls_back_zero` — budget 숫자 검증
- `test_settings_get_shows_budget_start_field` — budget_start 필드 존재 검증

아울러 budget_start 관련 2개 추가 삭제:
- `test_settings_post_writes_budget_start`
- `test_settings_post_blank_budget_start_is_null`

**수정한 테스트 (1개):**
- `test_settings_get_renders_form`: `name="claude"`/`name="codex"` 체크 → `name="track_claude"`/`name="track_codex"` 체크, "예산" 텍스트 체크 제거

**추가한 테스트 (2개):**
- `test_settings_post_writes_tracked_providers`: track_claude=on → tracked_providers==["claude"], budget 키 없음
- `test_settings_get_has_provider_checkboxes`: name="track_claude"/track_codex 있고 "월 예산" 없음

### `tokenomy/web/app.py`

- `budget_from_config` import 제거
- `settings_get`: `budget_from_config(config)` 제거, `tracked_providers(config)` 추가; 컨텍스트를 `tracked`/`providers` 로 교체 (claude/codex/budget_start 제거)
- `_valid_date_or_none` 헬퍼 삭제 (다른 곳에서 미사용)
- `settings_post`: 시그니처를 `track_claude`/`track_codex`/`credit_to_usd`/`min_interval` 으로 변경; `config["tracked_providers"]` 저장; 레거시 `budget`/`budget_start` 키 `pop`
- `official_refresh`: `targets` 폴백을 `list(PROVIDERS)` → `list(tracked_providers(config))` 로 변경

### `tokenomy/web/templates/settings.html`

- `<h2>월 예산</h2>` 카드 전체 → `<h2>사용하는 AI</h2>` 카드로 교체 (track_claude/track_codex 체크박스 + credit_to_usd 입력)
- "공식 사용량 자동 취득" 카드: 구 tracked_providers 문단 제거, 첫 문단 "옵트인(기본 꺼짐)…" → "체크한 AI에 대해 자동 취득합니다"로 갱신
- "데이터·프라이버시" 카드: "옵트인 시에만" → "위 '사용하는 AI'에 체크된 provider에 대해서만"으로 갱신

## 테스트 결과

```
$ python -m pytest tests/test_web.py -k settings -v
11 passed, 56 deselected (1.04s)

$ python -m pytest tests/ -q
2 failed, 409 passed (10.32s)
```

2 failed는 pre-existing test_launcher 포트-8765 환경 충돌 (앱이 8765 점유 시 발생, 회귀 아님).
신규 실패 없음.

## 파일 변경 목록

- `tokenomy/web/app.py` — settings_get/settings_post 교체, _valid_date_or_none 삭제, official_refresh 수정
- `tokenomy/web/templates/settings.html` — 예산 카드 → AI 선택 카드 교체, 문구 갱신
- `tests/test_web.py` — 5개 구 테스트 삭제, 1개 수정, 2개 신규 추가

## 커밋

`ef024dc` — feat(settings): 예산 입력 제거, 사용 AI(tracked_providers) 선택 UI

## 셀프 리뷰

- `official_refresh` fallback: `tracked_providers(config)`가 빈 리스트를 반환하면 refresh 대상이 없다. 이는 체크박스를 하나도 선택하지 않은 의도적 상태이므로 적절한 동작.
- `test_official_refresh_calls_fetch_and_redirects`(기존)는 config에 tracked_providers 미설정 시 크레덴셜 파일 존재 기반으로 claude+codex가 모두 반환되므로 통과 유지됨 (개발 머신에 두 credential 파일 모두 존재).
- `test_settings_post_persists_credit_to_usd`(기존)는 `claude`/`codex`/`budget_start` 폼 데이터를 보내지만 새 settings_post 시그니처에서 무시되고 credit_to_usd만 저장됨 → 테스트는 0.05 조회만 확인하므로 통과.
- `test_settings_post_saves_official_fetch`(기존)도 동일하게 무시 → min_interval만 확인하므로 통과.

## 우려사항

없음. 모든 신규 테스트 통과, 사전 기존 실패 2건 외 회귀 없음.

## Fix pass

리뷰 반영 사항 4건 처리.

### 1. `test_official_refresh_calls_fetch_and_redirects` 결정화 (환경 의존 제거)

`_client` → `_client_with_config`로 교체해 config 파일 경로를 취득하고,
`cfg_path.write_text('{"tracked_providers": ["claude", "codex"]}', ...)` 로 POST 전에 명시 설정.
크레덴셜 파일 유무와 무관하게 항상 `{"claude", "codex"}`가 호출됨.

### 2. `test_settings_post_writes_tracked_providers` 상태 코드 어서션 추가

`client.post(...)` 반환값을 `r`로 받고 `assert r.status_code == 303` 추가.
`follow_redirects=False` 도 명시해 리디렉트 상태를 직접 확인.

### 3. `test_settings_post_persists_credit_to_usd` 불필요 폼 키 제거

POST data에서 `claude`/`codex`/`budget_start` 키 삭제.
현재 `settings_post` 시그니처가 해당 파라미터를 받지 않으므로 무의미한 잡음이었음.
`credit_to_usd`만 남겨 테스트 의도 명확화.

### 4. `tokenomy/budget.py` `tracked_providers` 빈 리스트 동작 주석

함수 말미 크레덴셜 시드 return 앞에 2줄 한국어 주석 추가:
- 빈 리스트·None → 크레덴셜 파일 존재 기반 시드(UI 전체 해제 시 자동 복구) 설명
- 완전 비활성화는 `TOKENOMY_SKIP_OFFICIAL_FETCH` 환경변수 사용 안내
동작 변경 없음(주석 전용).

### 커버링 테스트 결과

```
$ .venv/Scripts/python.exe -m pytest tests/test_web.py -k "settings or official_refresh" -v
14 passed, 53 deselected, 1 warning in 1.18s
```

### 전체 스위트 결과

```
$ .venv/Scripts/python.exe -m pytest -q
2 failed, 409 passed, 1 warning in 10.24s
```

2 failed는 pre-existing `test_launcher` 포트-8765 환경 충돌. 신규 실패 없음.

### 커밋

`fix(settings): 리뷰 반영 — official_refresh 테스트 결정화 + 테스트 위생`
