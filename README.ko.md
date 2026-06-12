# Tokenomy (토큰 가계부)

AI 코딩 토큰 지출을 가계부처럼 관리하는 **로컬** 도구. Claude Code / Codex CLI의
로컬 세션 로그를 파싱해 — 직접 설정한 예산 대비 월 번다운, 프로젝트/세션별 비용,
캐시 효율 신호를 보여준다. 종량제 사용자가 월말에 예산을 초과하지 않도록 돕는다.

> English README: [README.md](README.md)

## 누구를 위한 도구인가

Claude Code / Codex CLI를 **종량제(API 과금)** 로 쓰며 자기 월 지출을 추적·관리하려는
사용자. 구독(Pro/Max/Plus) 사용자도 사용량 추적은 가능하며, 비용은 *공개 단가 기준
추정치* 로 표시된다.

## 프라이버시

- 토큰 **메타데이터**(토큰/시간/프로젝트/모델)만 파싱한다. **대화 원문은 저장하지 않는다.**
- 완전 로컬 실행. 웹 대시보드는 `127.0.0.1` 에만 바인딩 — 외부에 노출하지 말 것.

## 빠른 시작

```bash
pip install -r requirements.txt
cp config/tokenomy.config.example.json config/tokenomy.config.json   # 예산 편집
python -m tokenomy.cli ingest
python -m tokenomy.cli report
python -m uvicorn tokenomy.web.app:app --host 127.0.0.1 --port 8765
```

Windows는 `start_tokenomy.bat` 더블클릭(ingest -> 대시보드 -> 브라우저 자동 오픈).

## 예산 설정

`config/tokenomy.config.json` 을 편집하거나 대시보드의 **설정**(`/settings`) 화면에서:

```json
{
  "user_label": "me",
  "budget": { "claude": 100, "codex": 50 },
  "pricing_overrides": {}
}
```

- `budget.claude` / `budget.codex`: 월 한도(USD). `0` = 한도 없음(추적 전용).
- `pricing_overrides`: 청구 단가가 공개 단가와 다르면 모델별로 덮어쓰기.

## 데이터 소스

- Claude Code: `~/.claude/projects/**/*.jsonl` (메시지별 usage + cache)
- Codex CLI: `~/.codex/sessions/**/rollout-*.jsonl` (세션별 누적)

## 가격(단가)

`config/pricing.json` 에 공개 API 단가가 기본값으로 제공된다. 단가가 바뀌면 갱신하거나,
`pricing_overrides` 로 사용자별로 덮어쓴다.

## 다른 도구용 파서 추가

Tokenomy는 각 도구의 로그를 `UsageRecord`(`tokenomy/parser.py` 참고)로 정규화한다.
다른 CLI를 지원하려면 그 도구의 로그 파일을 찾아 `UsageRecord`를 생성하는 모듈을
작성한 뒤 `tokenomy.db.ingest_records(conn, records, pricing)` 로 적재한다 —
`tokenomy/codex_parser.py` 를 참고 구현으로 보면 된다.

## 라이선스

MIT — [LICENSE](LICENSE) 참고.
