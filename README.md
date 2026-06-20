# Tokenomy (토큰 가계부)

AI 코딩 토큰 지출을 가계부처럼 관리하는 **로컬** 도구. Claude Code / Codex CLI의
로컬 세션 로그를 파싱하고, 공식 사용량 API를 자동으로 읽어 — 공식 한도 대비
잔여·예측, 프로젝트/세션별 비용, 캐시 효율 신호를 보여준다.

> English README: [README.en.md](README.en.md)

## 누구를 위한 도구인가

Claude Code / Codex CLI를 쓰며 자기 사용량·한도를 추적·관리하려는 사용자.

- **엔터프라이즈/종량제**: 공식 API가 USD 한도를 제공하므로 공식 잔여·소진 예측이 바로 뜬다.
- **개인 구독제**: 정액제라 USD 예산은 없지만, 공식 API가 주는 rate-window(5h/7d 이용률 %)가 핵심 신호다.

공식 데이터가 없는 경우(크레덴셜 없음·취득 skip·또는 한도 미제공 계정)는 로컬 JSONL 기반 **사용량 전용 view**로 동작한다.

## 프라이버시

- 토큰 **메타데이터**(토큰/시간/프로젝트/모델)와 **세션 식별용 첫 프롬프트 발췌**만 저장한다. **전체 대화 기록은 저장하지 않는다.**
- 완전 로컬 실행. 웹 대시보드는 `127.0.0.1` 에만 바인딩 — 외부에 노출하지 말 것.

## 빠른 시작 (비개발자 — Windows)

1. [Releases](https://github.com/genius-kim-samsung/tokenomy/releases/latest)에서
   `Tokenomy.exe`를 내려받는다.
2. 더블클릭한다. (Windows SmartScreen 경고가 뜨면 **추가 정보 → 실행**을 누른다 —
   서명되지 않은 개인 도구라 뜨는 정상 경고다.)
3. Tokenomy 앱 창이 열리며 대시보드가 표시된다. 데이터는
   `C:\Users\<이름>\.tokenomy\`(`data\`·`config\` 하위)에 저장된다.
   **창을 닫으면 종료**된다.
4. 새 버전이 나오면 대시보드 상단에 알림 배너가 뜬다 — 눌러서 새 `Tokenomy.exe`를
   받아 기존 파일을 덮어쓰면 된다.

## 빠른 시작 (개발자 — 소스 실행)

```bash
pip install -r requirements.txt
cp config/tokenomy.config.example.json config/tokenomy.config.json
python -m tokenomy.cli ingest
python -m tokenomy.cli report
python -m uvicorn tokenomy.web.app:app --host 127.0.0.1 --port 8765
```

Windows는 `start_tokenomy.bat` 더블클릭(ingest → 대시보드 → 브라우저 자동 오픈).

## 설정

`config/tokenomy.config.json` 을 편집하거나 대시보드의 **설정**(`/settings`) 화면에서:

```json
{
  "user_label": "me",
  "tracked_providers": ["claude", "codex"],
  "pricing_overrides": {}
}
```

- `tracked_providers`: 공식 사용량을 취득하고 대시보드에 표시할 provider 목록.
  첫 실행 시 크레덴셜 파일(`~/.claude/.credentials.json`, `~/.codex/auth.json`) 존재로 자동 시드된다.
  한도·잔여는 공식 API 응답이 정본이다 — 엔터프라이즈/종량제는 USD 한도, 개인 구독제는 rate-window(%) 표시.
- `pricing_overrides`: 청구 단가가 공개 단가와 다르면 모델별로 덮어쓰거나, 앱 업데이트를
  기다리지 않고 **새 모델을 추가**한다(다음 ingest부터 반영):

  ```json
  "pricing_overrides": {
    "opus":    { "input": 4.0, "output": 20.0 },
    "gpt-5.6": { "provider": "codex", "input": 5.0, "output": 30.0, "cache_read": 0.5 }
  }
  ```

  키는 모델 id에 대한 부분일치 토큰이다. 새 키는 새 단가 항목으로 추가되고, 더 구체적인
  키가 더 거친 키보다 우선한다(예: `gpt-5.6`이 `gpt-5`를 앞선다). 미식별·의심 모델은
  설정 페이지의 **단가 커버리지(Pricing Coverage)** 카드에 노출된다.

> 내역·차원별 화면은 **주/월 토글**과 **사용자 지정 날짜 구간**으로 조회할 수 있다.

## 데이터 소스

- Claude Code: `~/.claude/projects/**/*.jsonl` (메시지별 usage + cache)
- Codex CLI: `~/.codex/sessions/**/rollout-*.jsonl` (세션별 누적)

## 가격(단가)

`config/pricing.json` 에 공개 API 단가가 기본값으로 제공된다. 단가가 바뀌면 갱신하거나,
`pricing_overrides` 로 사용자별로 덮어쓴다. 단가를 바꾸면 다음 ingest가 기존 비용을
자동으로 재계산한다 — raw 로그를 다시 적재할 필요가 없다.

## 공식 사용량 자동 취득

`tracked_providers`에 등록된 provider별로 로컬 OAuth 토큰을 읽기 전용으로 사용해
공식 API를 단발 호출(≤3s)하고, Claude 월 한도·Codex 주간 크레딧 등 공식 버킷을 미러링한다.
토큰은 읽기만 하고 refresh하지 않으며, **사용량 수치만 저장**한다(토큰·계정 식별자 미저장).
공식 데이터를 한 번도 못 얻은 경우 **사용량 전용 view**로 폴백.
환경변수 `TOKENOMY_SKIP_OFFICIAL_FETCH`로 전체 강제 차단 가능(오프라인/CI 용).

## 다른 도구용 파서 추가

Tokenomy는 각 도구의 로그를 `UsageRecord`(`tokenomy/parser.py` 참고)로 정규화한다.
다른 CLI를 지원하려면 그 도구의 로그 파일을 찾아 `UsageRecord`를 생성하는 모듈을
작성한 뒤 `tokenomy.db.ingest_records(conn, records, pricing)` 로 적재한다 —
`tokenomy/codex_parser.py` 를 참고 구현으로 보면 된다. 공식 사용량 파서는
`tokenomy/official_parser.py`(`OfficialBucket` + `credit_to_usd` 환산)를 참고.

## 라이선스

MIT — [LICENSE](LICENSE) 참고.
