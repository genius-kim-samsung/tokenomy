# ccusage 오픈소스 분석 보고서

> **대상**: [github.com/ccusage/ccusage](https://github.com/ccusage/ccusage) — "Analyze coding (agent) CLI token usage and costs from local data"
> **조사일**: 2026-06-11
> **조사 방법**: 저장소 전체 shallow clone 후 Rust/TS 소스(약 31K LOC) + 공식 docs 직접 분석. 이 프로젝트(`tokenomy`)의 현재 구현(`parser.py`, `codex_parser.py`)과 코드 레벨 대조.
> **목적**: 우리 `tokenomy` 프로젝트의 "AI 사용량 가계부" 기능 구현 시 주의점·참고점·차별화 포인트 도출.

---

## 0. Executive Summary

ccusage는 **15개 코딩 에이전트 CLI(Claude Code / Codex / Copilot / Gemini / Goose 등)의 로컬 세션 로그를 읽어 일·주·월·세션 단위 토큰/비용 리포트를 내는 read-only 분석기**다. npm `ccusage` 패키지로 배포되지만 실제 엔진은 **Rust 네이티브 바이너리**이고, npm 쪽은 플랫폼별 바이너리를 spawn하는 얇은 wrapper(`cli.ts`)에 불과하다. 원래 TypeScript 프로젝트였다가 Rust로 재작성된 흔적이 코드 구조에 그대로 남아 있다.

우리 프로젝트와의 관계를 한 줄로 요약하면:

> **ccusage = "여러 CLI의 사용량을 정확히 집계·표시"에 올인한 도구. 우리 tokenomy = "사용자 예산 대비 번다운 + 효율 복기"가 핵심.** 집계 엔진 레이어(파싱·dedup·비용계산)는 ccusage가 압도적으로 성숙해 있어 **반드시 참고**해야 하고, 예산/티어/복기 레이어는 ccusage에 아예 없는 **우리만의 차별화 영역**이다.

가장 중요한 3가지 takeaway:

1. **(주의) 중복 제거(dedup)를 반드시 구현하라.** ccusage는 `messageId + requestId` 조합 해시로 dedup하고, sidechain(`/btw` 등) 메시지가 부모를 재생(replay)하는 케이스까지 별도 처리한다. 우리 `parser.py`는 `message_id`만 추출하고 `request_id`/`is_sidechain`을 안 보므로 **중복·과대 집계 위험**이 있다.
2. **(참고) 비용 모드를 3가지로 분리하라.** `auto`(로그의 costUSD 우선) / `calculate`(항상 토큰×단가) / `display`(공식 costUSD만). 이 분리가 우리 미해결 이슈 **요금 환산율 vs 공개 단가** 를 깔끔히 다루는 패턴이다.
3. **(차별화) ccusage는 "텍스트로 토큰을 추정하지 않는다"는 철학을 명시.** 로컬에 실제 토큰 카운트가 없는 소스는 지원을 거부한다. 이는 우리 **Codex per-message 토큰 로컬 기록 여부** 검증의 당위성을 그대로 뒷받침한다.

---

## 1. 프로젝트 개요

| 항목 | 내용 |
|---|---|
| 정체성 | 코딩 에이전트 CLI의 로컬 사용량 로그 → 토큰/비용 리포트 (CLI 도구) |
| 저자/라이선스 | @ryoppippi 외, **MIT** |
| 배포 | npm `ccusage` (`bunx ccusage`, `npx ccusage@latest`), Nix flake |
| 지원 소스 | Claude Code, Codex, OpenCode, Amp, Droid, Codebuff, Hermes, pi-agent, Goose, OpenClaw, Kilo, Kimi, Qwen, GitHub Copilot CLI, Gemini CLI (**15종**) |
| 리포트 종류 | `daily` / `weekly` / `monthly` / `session` / `blocks`(5시간 빌링창) / `statusline`(beta) |
| 주요 옵션 | `--mode`(cost mode), `--since/--until`, `--json`, `--no-cost`, `--offline`, `--breakdown`, `--instances`, `--project`, `--timezone`, `--compact` |
| 인기 | npm 다운로드 수십만/월, Trendshift 등재, awesome-claude-code 수록 (커뮤니티 표준격) |

핵심 설계 의도: **"각 CLI가 로컬에 남기는 usage 메타를 신뢰할 수 있게 정규화하고, 정확한 비용으로 환산해 보여준다."** 클라우드 API를 긁지 않고, 추정하지 않는다.

---

## 2. 아키텍처 분석

### 2.1 모노레포 + TS→Rust 재작성

```
apps/ccusage/          # npm 패키지. src/cli.ts = 네이티브 바이너리 spawn 래퍼뿐
packages/              # ccusage-{darwin,linux,win32}-{arm64,x64}  ← 플랫폼별 바이너리 배포 단위
rust/crates/
  ccusage/             # ★ 엔진 본체 (~31K LOC): adapter/ cost/ pricing/ blocks/ ...
  ccusage-cli/         # 인자 파서(clap 미사용, 자체 arg_parser) + help 생성
  ccusage-terminal/    # 터미널 테이블/스타일/폭 계산 렌더러
docs/                  # VitePress 문서 (ccusage.com)
```

**핵심 통찰 — npm 호환성 + Rust 성능을 동시에 취한 배포 전략:**
`cli.ts`(296줄)는 비즈니스 로직이 **전혀 없다.** 하는 일은 ① `process.platform/arch`로 알맞은 `@ccusage/ccusage-<os>-<arch>` optional dependency를 찾고 ② 그 안의 네이티브 바이너리(`bin/ccusage[.exe]`)를 `spawn(stdio:'inherit')`로 실행하며 ③ 유닉스에서 실행 비트가 없으면 `chmod 755`로 복구하는 것뿐이다. 즉 **사용자는 `npx ccusage`라는 익숙한 진입점을 그대로 쓰지만, 실제 연산은 Rust가 한다.** 대용량 JSONL을 수십~수백 개 파싱해야 하므로 성능이 곧 UX인 도구에서 합리적인 선택.

> 우리에게의 함의: 우리는 Python으로 충분하다(폐쇄망 PC·PoC·단일 사용자). 다만 **"동료 → 플랫폼" 확산 단계에서 파싱이 느려지면** ccusage처럼 핫패스만 네이티브로 빼는 길이 있음을 기억해 둘 것. 지금 단계에선 over-engineering.

### 2.2 어댑터 패턴 (가장 따라 할 만한 구조)

소스마다 `adapter/<source>/` 디렉토리에 동일한 4분할 구조를 둔다:

```
adapter/claude/   { mod.rs(로드+dedup), daily.rs(집계), paths.rs(경로탐색) }
adapter/codex/    { parser.rs, aggregate.rs, paths.rs, report.rs, speed.rs, types.rs }
adapter/<나머지>/  { loader.rs, parser.rs, paths.rs, report.rs }
adapter/all/      { loader.rs, report.rs, types.rs }  ← 모든 소스 통합 리포트
```

각 어댑터는 "**경로 탐색(paths) → 라인 파싱(parser) → 정규화된 공통 타입(UsageEntry/LoadedEntry) → 집계(report)**"라는 동일 파이프라인을 구현하고, 공통 타입으로 수렴시킨 뒤 `all` 어댑터가 합친다.

> 우리 프로젝트는 이미 `parser.py`(claude) + `codex_parser.py`(codex)로 **사실상 같은 플러그인 패턴**을 시작했다. ccusage는 이를 15개로 확장하면서도 깨지지 않는 형태를 보여준다. 향후 Gemini/Copilot 등 추가 시 **"공통 `UsageRecord`로 수렴 → provider별 어댑터만 추가"** 원칙을 유지하면 된다(이미 잘 가고 있음).

---

## 3. 핵심 기능 심층 분석

> 아래는 우리 구현과의 관련도가 높은 순서로 정리했다. 각 항목 끝에 **우리 프로젝트 적용 메모**를 붙였다.

### 3.1 JSONL 파싱 & 성능 최적화 — `adapter/claude/mod.rs`

ccusage의 라인 파싱은 단순 `serde_json::from_slice`가 아니다. 비용 발생 라인만 빠르게 걸러내는 **다단계 필터**가 있다:

1. **바이트 마커 prefilter** — `memmem::Finder::new(br#""usage":{"#)`로 `"usage":{` 가 없는 라인은 JSON 파싱 자체를 건너뜀. (대부분의 transcript 라인엔 usage가 없음 → 큰 절약)
2. **null 필드 거부** — `has_unsupported_null_field()`가 바이트 레벨로 `:null`을 스캔, `id/model/speed/costUSD/sessionId/requestId/cache_*` 등이 `null`이면 그 라인 폐기. (구 TypeScript 로더와 동일 동작을 의도적으로 재현 — 호환성 회귀 방지)
3. **유효성 검사** — `is_valid_usage_entry()`: `version`이 semver(`x.y.z`) 형태가 아니면 거부, 빈 `sessionId/requestId/messageId/model` 거부.
4. **병렬 파일 읽기** — 파일을 **크기 기준으로 워커 스레드에 밸런싱**(`chunk_file_indexes_by_size`: 큰 파일부터 가장 가벼운 워커에 배정하는 greedy bin-packing) 후 스레드 스코프로 병렬 처리.
5. **메모리 절약** — `project`/`session_id`/`project_path`를 `Arc<str>`로 공유.

```rust
// usage 마커가 없으면 serde를 아예 호출하지 않는다
let usage_marker = memmem::Finder::new(br#""usage":{"#);
for line in byte_lines(&content) {
    if usage_marker.find(line).is_none() { continue; }
    if has_unsupported_null_field(line) { continue; }
    let Ok(data) = serde_json::from_slice::<UsageEntry>(line) else { continue; };
    ...
}
```

> **우리 적용 메모**: 우리 `parser.py`의 `parse_usage_line`은 "usage 블록 존재로 비용 라인 판단"이라는 **같은 발상**을 이미 쓰고 있다(좋음). 다만 ① 우리는 `version` semver 검증, 빈 필드 검증이 없어 **부분 기록/손상 라인을 그대로 집계**할 수 있다. ② ccusage의 `:null` 거부 로직은 "Claude가 `costUSD:null`처럼 명시적 null을 쓰는 케이스"를 의도적으로 폐기하는 것 — 우리도 `cache_creation_input_tokens: null` 같은 케이스에서 `_int(None)=0`으로 조용히 0 처리하는데, **null과 0을 구분해야 하는 분석(예: 캐시 미사용 vs 필드 부재)** 에서 주의. Python에선 성능 최적화(바이트 prefilter)는 PoC 단계에선 불필요하지만, **검증 로직은 지금 추가해도 가치 있음.**

### 3.2 중복 제거(Deduplication) — ⚠️ 가장 중요한 주의점

Claude Code의 JSONL은 **같은 메시지가 여러 파일/세션/리트라이로 중복 기록**될 수 있다. ccusage는 `push_deduped_entry`에서 이를 정교하게 처리한다:

- **dedup 키 = `hash(messageId, requestId)`** (FxHasher). 두 값 조합이 같으면 동일 사용으로 간주.
- **충돌 시 교체 규칙**(`should_replace_deduped_entry`):
  - sidechain 여부가 다르면 → **비(非)sidechain(부모)을 유지**
  - 토큰 총합이 다르면 → **더 큰 쪽 유지** (부분 기록보다 완전 기록 선호)
  - 같으면 → `speed` 정보가 있는 쪽 선호
- **sidechain replay 처리**: `/btw` 같은 sidechain 로그가 부모 메시지를 **새 requestId로 재생**하는 케이스를 잡기 위해, `hash(messageId, None)`(requestId 무시) 보조 인덱스를 따로 유지한다.

```rust
fn usage_dedupe_hash(message_id: &str, request_id: Option<&str>) -> u64 {
    let mut hasher = FxHasher::default();
    message_id.hash(&mut hasher);
    request_id.hash(&mut hasher);   // ← messageId 단독이 아니라 (messageId, requestId) 조합
    hasher.finish()
}
```

> **우리 적용 메모 — 현재 갭**: 우리 `parser.py`는 `message_id`만 추출하고 **`request_id`·`is_sidechain`을 추출하지 않는다.** `db.py`에서 `message_id`로 dedup한다 해도:
> - **같은 messageId + 다른 requestId**(리트라이/재생)를 무엇으로 처리하는지 불명확 — messageId만으로 dedup하면 리트라이가 합쳐져 **과소 집계**되거나, 반대로 sidechain replay가 중복으로 남아 **과대 집계**될 수 있다.
> - **권고**: `UsageRecord`에 `request_id`, `is_sidechain` 필드를 추가하고, dedup 키를 `(message_id, request_id)`로 바꾸는 것을 검토. 최소한 **현재 dedup 정책이 무엇을 합치고 무엇을 남기는지** db.py 기준으로 명시적 테스트를 두자. (ccusage는 이 로직에만 단위 테스트 2개를 둘 만큼 함정이 많은 영역이다.)

### 3.3 비용 계산 — `cost.rs` (3-mode + tiered + cache 세분화)

**(a) 3가지 Cost Mode** (우리 요금 환산율 이슈와 직결):

| 모드 | 동작 | 용도 |
|---|---|---|
| `auto`(기본) | 로그에 `costUSD` 있으면 그대로, 없으면 토큰×단가 계산 | 일반 |
| `calculate` | `costUSD` 무시, **항상** 토큰×단가 | 기간 비교·일관성 |
| `display` | `costUSD`만, 없으면 $0.00 | 공식 빌링 검증·감사 |

**(b) Tiered pricing** — Claude의 200K 토큰 초과 구간 단가:

```rust
fn tiered_cost(tokens, base, above) -> f64 {
    const THRESHOLD: u64 = 200_000;
    if let Some(above) = above {
        if tokens > THRESHOLD {
            return (THRESHOLD as f64 * base) + ((tokens - THRESHOLD) as f64 * above);
        }
    }
    tokens as f64 * base
}
```

**(c) 캐시 토큰을 단가별로 세분화**:
- `cache_read` — 별도(저렴) 단가
- `cache_creation`을 **5분/1시간으로 분리**: `cache_creation.ephemeral_5m_input_tokens`(기본 단가) vs `ephemeral_1h_input_tokens`(input 단가 × **2배**). breakdown이 없으면 전부 표준 cache creation 단가로 fallback.
- `speed=fast`(Codex fast 모드 등)면 `fast_multiplier` 배수 적용.

> **우리 적용 메모**:
> - **요금 환산율 해결 패턴**: 조직 예산 등급 한도 달러가 "공개 API 단가"인지 "별도 요금 환산율"인지 불명확한 문제는, ccusage식 **mode 분리 + pricing override**로 그대로 흡수 가능하다. 즉 `config/pricing.json`을 공개 단가로 두되, 요금 환산율이 확인되면 **override 레이어**로 덮고, "공식 vs 계산" 두 값을 나란히 보여주면 사용자가 차이를 직접 검증할 수 있다.
> - **현재 갭**: 우리 `parser.py`는 `cache_creation_input_tokens`만 보고 **5m/1h breakdown을 안 본다.** Opus/Sonnet에서 1h 캐시는 단가가 2배라, breakdown을 무시하면 **비용을 과소 추정**한다. 정확한 가계부를 표방한다면 `cache_creation.{ephemeral_5m,ephemeral_1h}_input_tokens`를 파싱해 단가를 분리하는 것을 검토.
> - **200K tiered**: 장기 세션이 200K 입력을 넘으면 단가가 바뀐다. PoC에선 무시 가능하나 "효율 복기" 수치의 정확도를 위해 인지해 둘 것.

### 3.4 가격 데이터 파이프라인 — `pricing.rs` (2,298 LOC)

ccusage는 가격을 한 곳에 하드코딩하지 않고 **다층 소스 + 빌드 임베드 + 런타임 페치 + override**로 운영한다:

- **빌드 타임 임베드**: `include_str!`로 LiteLLM 가격 JSON(`model_prices_and_context_window.json`)과 models.dev 가격을 바이너리에 박아 넣음 → **오프라인에서도 즉시 동작**(`--offline`).
- **런타임 페치**: 온라인이면 LiteLLM raw URL / `models.dev/api.json`에서 최신 가격 fetch(타임아웃 10s, 최대 64MB, 실패 시 60초 backoff 후 재시도).
- **compact 포맷**: 임베드 크기를 줄이려 `{i,o,cc,cr,ia,oa,...}` 약어 스키마도 지원.
- **fast multiplier override**, **사용자 override**: `ccusage.json`에서 raw 모델명별 단가를 재빌드 없이 덮어쓸 수 있음.
- **모델명 매칭**: Anthropic의 `YYYYMMDD` 날짜 suffix 별칭을 인지해 정규화.

> **우리 적용 메모**: 폐쇄망 PC는 외부망이 막혀 있으므로 **"빌드/패키지에 가격을 동봉 + 오프라인 우선"** 전략이 우리에게 더 critical하다. ccusage의 `--offline` + 임베드 가격 모델을 그대로 차용할 가치가 크다. 우리 `config/pricing.json`이 이미 그 자리. 추가로 **override 레이어**(요금 환산율)를 pricing.json 위에 얹는 2단 구조를 권장(3.3의 요금 환산율 해결과 맞물림). LiteLLM JSON 포맷을 그대로 빌려오면 모델 추가가 쉬워진다.

### 3.5 Codex 누적 토큰 처리 — `adapter/codex/parser.rs` ⚠️ 두 번째 주의점

Codex는 Claude와 **토큰 회계 방식이 근본적으로 다르다.** Codex의 `token_count` 이벤트는 `total_token_usage`(**세션 누적**)와 `last_token_usage`(**해당 턴 델타**)를 함께 기록한다. ccusage는:

```rust
let raw_usage = info.and_then(|i| i.last_token_usage.copied())   // ① 턴 델타 우선
    .or_else(|| total_usage.map(|u| subtract_codex_raw_usage(&u, previous_totals))); // ② 없으면 누적 차분
if let Some(t) = total_usage { *previous_totals = Some(t); }      // 다음 차분 위해 누적 저장
```

즉 **턴별 델타(`last_token_usage`)를 우선 사용**하고, 없으면 직전 누적과의 차이로 델타를 복원한다. 추가로:
- **subagent replay dedup**: `thread_spawn` 마커가 있는 subagent 세션에서 같은 초(second)에 재생되는 token_count를 감지해 건너뜀.
- **모델 fallback 체인**: payload → turn_context의 current_model → 최종 `gpt-5` fallback(이 경우 `is_fallback_model=true` 플래그).
- **reasoning_output_tokens를 별도 필드로 추적**(output과 구분).
- **headless(exec) 모드** 로그를 별도 경로로 처리.

> **우리 적용 메모 — 현재 갭**: 우리 `codex_parser.py`는 **세션당 마지막 `total_token_usage`(누적) 1개만** `UsageRecord`로 만든다. 결과적으로 세션 총량은 맞지만:
> - **일/시간별 분해 불가**: 한 세션이 여러 날에 걸치면(장기 세션) 마지막 날에 전부 귀속되어 **일별 번다운이 왜곡**된다. ccusage는 턴별 델타라 정확히 날짜에 분배된다.
> - **subagent replay 미처리**: subagent를 쓰는 Codex 세션에서 동일 토큰이 중복 집계될 수 있음(ccusage는 `thread_spawn`/같은 초 감지로 방어).
> - **reasoning 토큰**: 주석엔 "output에 포함"이라 했으나, Codex의 `output_tokens`가 reasoning을 포함하는지 실제 스키마로 **반드시 검증** 필요. ccusage는 둘을 분리해 추적한다.
> - **권고**: "월 예산 번다운"이 핵심 가치라면, 세션 총량만으로도 월 합계는 맞다(PoC OK). 하지만 "**이대로면 N일에 소진**" 같은 일별 추세 정확도를 위해선 ccusage처럼 **턴별 token_count 이벤트를 개별 레코드화**하는 방향을 로드맵에 둘 것. 최소한 장기 세션의 날짜 귀속 왜곡을 알고 있어야 한다.

### 3.6 5시간 블록 + 번다운 + Projection — `blocks.rs` (우리 핵심 가치와 동형)

ccusage `blocks` 리포트는 **우리의 "번다운" 가치와 사실상 같은 알고리즘**을, 시간 축만 달리해 구현해 놓았다:

- **세션 블록화**(`identify_session_blocks`): 엔트리를 시간순 정렬 후, **5시간(Claude 빌링창)** 윈도우로 그룹화. 시작 후 5h 초과 또는 직전 활동 후 5h 공백이면 새 블록 시작(블록 시작은 시(hour) 단위로 floor). 공백은 `gap block`으로 명시 표시.
- **Burn rate**(`calculate_burn_rate`): `토큰/분`, `비용/시간`. 흥미롭게도 **캐시 제외 토큰(input+output)으로 별도 indicator**도 계산 — 캐시 read가 burn 체감을 부풀리는 걸 보정.
- **Projection**(`project_block_usage`): 현재 burn rate로 블록 종료까지 외삽 → **예상 총 토큰/비용**.
- **한도 대비 상태**: `ok` / `warning`(임계 초과) / `exceeds`. ACTIVE 블록엔 elapsed/remaining 시간, REMAINING/PROJECTED 행을 표 안에 함께 렌더.
- **usage limit reset time**: Claude의 `"Claude AI usage limit reached|<epoch>"` 에러 메시지에서 **리셋 시각을 바이트 파싱으로 추출** — "언제 풀리는지"를 보여줌.

```rust
// 현재 속도로 블록 끝까지 외삽
let total_tokens = block.token_counts.total() as f64
    + burn.tokens_per_minute * remaining_minutes;
```

> **우리 적용 메모**: 이건 **우리가 그대로 빌려와 "월 단위"로 치환할 핵심 알고리즘**이다.
> - ccusage: 시간 축 = **5시간 rate-limit 창**, 한도 = 토큰 limit.
> - 우리: 시간 축 = **월(billing month)**, 한도 = **티어 예산($)**. burn rate = "최근 N일 평균 소비/일", projection = "월말 예상 소진", 상태 = "이대로면 N일에 한도 도달".
> - `calculate_burn_rate`의 **"캐시 제외 indicator"** 발상은 우리 "효율 복기"에 직접 유용 — 캐시 read 비중이 높으면 실질 비용은 낮으니, **명목 토큰 vs 실효 비용**을 분리해 보여주는 신호로 쓸 수 있다.
> - `usage limit reset time` 파싱은 우리 "월말 차단 방지"(예산 한도 리셋일)로 대응시킬 수 있다.

### 3.7 설계 철학 — "추정하지 않는다" (우리 로컬 토큰 검증의 근거)

`docs/guide/source-support-qa.md`에 ccusage의 입장이 명문화돼 있다:

> *"If a tool stores only prompts, transcripts, quota percentages, or opaque cloud state, ccusage does not estimate token usage from text length. That would make reports look precise while being based on guesses."*

- 소스 지원 최소 요건: **로컬 타임스탬프 + 세션 ID + 모델 ID + (토큰 카운트 또는 토큰에 매핑되는 비용)**.
- transcript 텍스트만으론 불충분 — 토크나이저 동작·숨은 시스템 컨텍스트·캐시 입력·tool-call 오버헤드를 알 수 없으므로.
- **로컬·read-only**: 클라우드 스크래핑이나 비공개 인증 API에 의존하지 않음. (Devin/Grok/Antigravity CLI는 로컬 토큰 회계가 없어 **지원 거부**.)

> **우리 적용 메모**: 이 철학은 우리 미해결 이슈 **"Codex CLI가 per-message 토큰을 로컬 로그로 남기는지"** 검증의 정당성을 그대로 준다. 로컬 환경에서 CLI가 **로컬에 실제 토큰을 안 남기면, 텍스트 길이로 추정하지 말고 "미지원"으로 두는 것**이 정직한 설계다. 또한 우리 README의 "메타만 저장, 대화 원문 미저장(프라이버시)" 원칙과 ccusage의 "read-only·로컬" 원칙이 일치 — 조직 보안 심사에서 인용할 수 있는 레퍼런스가 된다.

---

## 4. tokenomy vs ccusage 비교표

| 영역 | ccusage | 우리 tokenomy (현재) | 판정 |
|---|---|---|---|
| 언어/런타임 | Rust 엔진 + npm 래퍼 | Python | 우리 단계엔 Python 적정 |
| 다중 소스 | 15종 어댑터 | Claude + Codex 2종 (플러그인 시작) | 같은 패턴, 확장 여지 |
| JSONL 파싱 | 바이트 prefilter + 병렬 + 검증 | usage 블록 판단(증분 offset) | 발상 동일, **검증 부재** |
| **Dedup** | `(messageId, requestId)` + sidechain replay | `message_id`만 추출 | ⚠️ **갭 — 보강 필요** |
| 비용 모드 | auto/calculate/display 3종 | 단일 계산 | 참고하여 도입 권장 |
| 캐시 단가 | read / create-5m / create-1h(×2) 분리 | `cache_creation` 단일 | ⚠️ 과소추정 위험 |
| tiered(200K) | 지원 | 미지원 | PoC 무시 가능 |
| 가격 소스 | LiteLLM+models.dev, offline 임베드, override | `pricing.json` 정적 | offline·override 차용 권장 |
| **Codex 토큰** | 턴별 델타(누적 차분) + replay dedup | 세션 마지막 누적 1건 | ⚠️ 일별 분배 왜곡 |
| **예산** | ❌ 없음 | ✅ 사용자 입력 예산 | **우리 차별화** |
| **번다운/projection** | 5시간 창 단위 | 월 예산 단위(목표) | 알고리즘 차용 → 월로 치환 |
| **효율 복기** | ❌ (집계만) | ✅ 업무별/캐시 신호 목표 | **우리 차별화** |
| web_search/fetch | ❌ | ✅ `server_tool_use` 추출 | 우리 강점 |
| 출력 | 컬러 테이블/JSON/compact/statusline | 터미널 요약 + 웹 대시보드(예정) | 웹은 우리 강점 |
| 프라이버시 | 로컬·read-only | 메타만 저장, 원문 미저장 | 일치 |

---

## 5. 인사이트 & 권고 (우선순위순)

### 🔴 반드시 주의할 점 (정확도 직결)

1. **Dedup 키를 `(message_id, request_id)`로 확장.** 현재 `message_id` 단독은 리트라이/sidechain replay에서 과대·과소 집계를 일으킨다. `UsageRecord`에 `request_id`, `is_sidechain` 추가 → db dedup 정책 명시 + 단위 테스트.
2. **Codex 누적 토큰의 날짜 귀속 왜곡 인지.** 세션당 마지막 누적값만 쓰면 장기 세션의 일별 번다운이 틀어진다. "월 합계"는 맞지만 "이대로면 N일 소진"의 입력이 되는 **일별 추세는 부정확**. 턴별 `last_token_usage` 이벤트 레코드화를 로드맵에.
3. **캐시 5m/1h 단가 분리.** 1h 캐시 생성은 input 단가의 2배. 무시하면 비용 과소 추정 → "가계부"의 신뢰도 훼손.
4. **파싱 검증 추가.** semver `version` 체크, 빈 `session/message/model` 거부, 손상 라인 폐기. ccusage가 회귀 테스트까지 둔 영역이다.

### 🟡 참고하면 좋은 것 (구조/UX)

5. **Cost mode 3분할 도입** → 요금 환산율 문제를 "공식 costUSD vs 토큰 계산" 두 축으로 분리해 검증.
6. **Offline 우선 + override 2단 pricing** → 폐쇄망 대응 + 요금 환산율 흡수. LiteLLM JSON 포맷 차용으로 모델 추가 비용 절감.
7. **`blocks.rs`의 burn rate/projection 알고리즘을 "월" 축으로 이식** → 우리 핵심 가치인 "월말 차단 방지" 번다운의 검증된 레퍼런스 구현.
8. **캐시 제외 indicator**(input+output만) → "명목 토큰 vs 실효 비용" 분리로 효율 복기 신호 강화.
9. **`--json` 출력 + statusline** → 자동화/공유 친화. 우리 웹 대시보드와 별개로 CI/스크립트 연동 포인트.

### 🟢 우리만의 차별화 (ccusage엔 없음 — 여기에 집중)

10. **예산 대비 번다운** — ccusage는 "한도" 개념이 약하다(blocks의 token limit 정도). 우리의 **사용자 예산 × 월 예산 × 차단 방지**는 명백한 차별점.
11. **효율 복기** — 업무별 비용, 캐시 활용도, web_search/fetch 같은 "행동 개선 신호"는 ccusage가 의도적으로 다루지 않는 영역(그들은 "집계"에 집중). 우리의 2대 가치(번다운 + 복기) 중 복기는 온전히 우리 몫.
12. **멀티 프로바이더 통합 가계부**(Claude+Codex 한 화면) — provider별로 예산 주기가 갈리는 정책을 반영한 통합 뷰.

---

## 6. 결론 & 액션 아이템

ccusage는 **"로컬 사용량을 정확히 집계·환산"하는 엔진 레이어의 사실상 표준 구현**이다. 우리는 이 레이어에서 검증된 패턴(dedup, cost mode, offline pricing, 캐시 단가 분리, burn/projection)을 **선별적으로 차용**하되, ccusage가 비워둔 **예산·티어·복기 레이어에 우리 역량을 집중**하는 것이 최적 전략이다. "또 하나의 ccusage"를 만들 이유는 없다.

**즉시 착수(다음 스프린트 후보):**
- [ ] `UsageRecord`에 `request_id`/`is_sidechain` 추가 + dedup 키 `(message_id, request_id)`로 변경, 테스트 작성 (3.2)
- [ ] 파서 검증 로직(semver/빈 필드/손상 라인) 추가 (3.1)
- [ ] cache_creation 5m/1h 분리 파싱 + 단가 적용 (3.3)

**요금 환산율 / 로컬 토큰 검증 시 활용:**
- [ ] 요금 환산율: cost mode 3분할 + override 2단 pricing 설계로 흡수 (3.3, 3.4)
- [ ] 로컬 토큰: Codex 로컬 토큰 검증 시 "텍스트 추정 금지" 철학 채택, 턴별 token_count 레코드화 검토 (3.5, 3.7)

**중기 로드맵:**
- [ ] `blocks.rs` burn/projection을 "월 예산" 축으로 이식 (3.6)
- [ ] offline 임베드 가격 + LiteLLM 포맷 차용 (3.4)

---

## 부록 A. 참고 링크

- 저장소: https://github.com/ccusage/ccusage
- 공식 문서: https://ccusage.com / DeepWiki: https://deepwiki.com/ccusage/ccusage
- 핵심 소스(분석 기준): `rust/crates/ccusage/src/`
  - `adapter/claude/mod.rs` — 로드 + dedup (3.1, 3.2)
  - `cost.rs` — 비용 모드·tiered·캐시 단가 (3.3)
  - `pricing.rs` — LiteLLM/models.dev·offline·override (3.4)
  - `adapter/codex/parser.rs` — 누적 토큰 델타 (3.5)
  - `blocks.rs` — 번다운/projection (3.6)
- 설계 철학: `docs/guide/source-support-qa.md`, `docs/guide/cost-modes.md`
- 가격 데이터 원천: LiteLLM `model_prices_and_context_window.json`, models.dev `api.json`

## 부록 B. 우리 코드 대조 위치

| 갭 항목 | 우리 파일:심볼 | ccusage 대응 |
|---|---|---|
| dedup 키 | `tokenomy/parser.py:UsageRecord` (message_id만) | `adapter/claude/mod.rs:push_deduped_entry` |
| 파싱 검증 | `tokenomy/parser.py:parse_usage_line` | `adapter/claude/mod.rs:is_valid_usage_entry` |
| 캐시 단가 | `tokenomy/pricing.py`, `parser.py`(cache_creation 단일) | `cost.rs:calculate_cost_from_tokens` |
| Codex 토큰 | `tokenomy/codex_parser.py:parse_rollout` (세션 누적 1건) | `adapter/codex/parser.rs:visit_codex_session_entry` |
| 번다운 | (목표 기능) `aggregate.py` | `blocks.rs:calculate_burn_rate / project_block_usage` |
| 단가 정합 | `config/pricing.json` 단가 정합 | `cost.rs` cost mode + pricing override |
