---
name: release-exe
description: >-
  Tokenomy의 새 exe 버전을 릴리스한다. __version__ bump → 로컬 PyInstaller 빌드로 검증
  → git 태그(v<버전>) → main+태그 push → GitHub Actions(release.yml)가 태그==버전 검증·exe
  빌드·--version 스모크·GitHub Release 업로드까지 수행. "exe 배포", "새 버전 릴리스", "v0.1.x
  배포", "릴리스해줘", "버전 올려서 배포", "deploy new exe", "release new version" 같은
  요청이면 버전 번호를 명시하지 않았더라도 반드시 이 스킬을 사용한다. 배포 없이 로컬 exe만 빌드·검증하고
  싶을 때도 3·게시 절을 참고용으로 쓴다. (단순 코드 수정·테스트 실행·CI 디버깅은 대상 아님.)
---

# Tokenomy exe 릴리스

새 버전의 `Tokenomy.exe`를 GitHub Release로 배포하는 결정적 절차다. 공식 배포본은
**CI가 빌드한 산출물**이고, 로컬 빌드는 태그 전에 "정말 빌드·실행되는가"를 확인하는 검증 단계다.

> **이런 요청이면 수동으로 하지 말고 이 스킬을 끝까지 따른다.** "릴리스/배포/버전 올려" 류
> 요청을 손으로 처리하면 마지막 단계(릴리스 노트)를 빠뜨리기 쉽다 — 실제로 그렇게 빈 본문이
> 나간 적이 있다. CI 성공(5)은 릴리스의 **끝이 아니다.** 아래 완료 정의를 모두 채워야 끝이다.

## 완료 정의 (이게 다 ✓ 돼야 릴리스 끝 — 하나라도 비면 미완)

- [ ] `__version__` == 태그 `v<버전>` (불일치면 CI가 죽는다)
- [ ] 로컬 `.venv` 빌드 `--version` 스모크 통과
- [ ] CI `build-windows` success
- [ ] Release 에셋에 `Tokenomy-<버전>.exe` 존재 + draft 아님
- [ ] **릴리스 노트 본문 작성됨(`gh release view … --jq '.body|length'` > 0, 자동 변경로그만 있는 빈 껍데기 금지)**
- [ ] 사용자에게 표로 보고

마지막 두 항목이 가장 잘 누락된다. **CI가 초록불이어도 노트가 비면 릴리스는 미완**이다.

## 릴리스 메커니즘 (왜 이 순서인가)

`release.yml`은 **태그 push(`v*`)** 에만 트리거되고, 첫 스텝에서 **git 태그 == `tokenomy.__version__`**
를 검증한 뒤 어긋나면 빌드를 실패시킨다. 그래서 **반드시 `__version__`을 먼저 올리고, 같은 번호로
태그**해야 한다. 태그가 코드보다 앞서거나 버전이 안 맞으면 CI가 즉시 죽는다.

CI 파이프라인: `checkout → setup-python 3.12 → pip install -r requirements.txt pyinstaller
→ 태그==버전 검증 → pyinstaller tokenomy.spec → dist/Tokenomy.exe --version 스모크 →
버전명 복사(Tokenomy-<버전>.exe) → softprops/action-gh-release로 Release 업로드`.

## 0. 사전 점검 (dry-run — 되돌리기 어려우니 먼저 막는다)

릴리스는 태그 push·Release 발행이라 사실상 비가역이다. 시작 전에 아래를 모두 통과시킨다.

```bash
cd "<repo 루트>"                                   # 보통 C:/projects/samsung/tokenomy
git -C . rev-parse --abbrev-ref HEAD               # main 인지 확인
git status --porcelain                             # 워킹 트리 클린 — 출력 비어야 정상
grep -n '__version__' tokenomy/__init__.py         # 현재 버전 읽기
.venv/Scripts/python -c "import PyInstaller; print(PyInstaller.__version__)"  # 빌드 환경
ls tokenomy.spec                                   # spec 존재
```

> `git status --porcelain` 출력이 **비어 있지 않으면**(미커밋 추적 변경) 중단한다. 릴리스는
> 커밋된 상태만 반영하므로, 빠뜨린 변경(예: 도메인 문서·설정)이 없는지 먼저 커밋하거나 의도를
> 확인한다. 릴리스 직전에야 미커밋 변경을 발견하는 사고를 막는다.

목표 태그가 **이미 있으면 중단**(중복 릴리스 방지):

```bash
git tag -l "v<버전>"                               # 로컬
git ls-remote --tags origin "v<버전>"              # 원격 — 출력 비어야 정상
```

## 1. 버전 결정

- 사용자가 버전을 명시했으면 그 값을 쓴다(예: "v0.2.0으로 배포" → minor 점프).
- **명시하지 않았으면 현재 `__version__`의 마지막 자리(patch) +1을 제안하고 확인을 받는다**
  (예: `0.1.10` → `0.1.11`). 자리 해석이 모호하면 묻는다 — "마지막 자리"는 보통 patch를 뜻한다.
- 버전은 SemVer 문자열 `MAJOR.MINOR.PATCH`, 태그는 접두사 `v`를 붙인 `v<버전>`.

## 2. 버전 bump

main에서 `tokenomy/__init__.py`의 `__version__`을 목표 버전으로 수정하고 커밋한다.

1. `tokenomy/__init__.py`의 `__version__`을 목표 버전으로 수정한다.
2. 커밋(저장소의 평소 커밋 컨벤션·푸터를 따른다):
   ```
   chore(release): v<버전> — __version__ <old> → <new>
   ```

> **환경이 주 워크트리 추적 파일 직접 편집을 막는 경우에만**(PreToolUse 가드 등) 전용 워크트리로
> 우회한다: `EnterWorktree`로 워크트리 생성(`.venv`가 없으니 빌드는 주 repo의 `.venv` 사용) →
> `__init__.py` 수정·커밋 → 주 워크트리에서 `git -C "<repo 루트>" merge --ff-only <워크트리-브랜치>`.
> ff가 아니면(main이 앞서면) 워크트리를 `git reset --hard main` 후 bump를 다시 올린다.
> (가드가 없으면 위 직접 편집이 기본이다.)

## 3. 로컬 exe 빌드 + 스모크 검증

main이 새 버전을 담은 상태에서 **주 repo 루트**에서 빌드한다. **반드시 `.venv`로** 빌드한다 —
시스템 Python으로 빌드하면 pywebview가 번들에서 빠져 네이티브 창 대신 브라우저로 fallback한다.

```bash
cd "<repo 루트>"
.venv/Scripts/python -m PyInstaller --noconfirm tokenomy.spec   # → dist/Tokenomy.exe
./dist/Tokenomy.exe --version                                   # 출력 == 새 버전이어야 함
.venv/Scripts/python -c "import tokenomy; print(tokenomy.__version__)"  # 기대값
```

`--version` 출력이 `__version__`과 다르면 **태그하지 말고** 원인부터 잡는다(CI 스모크와 동일 검증).
로컬 빌드는 검증용 — 실제 배포본은 CI가 다시 빌드한다.

## 4. 태그 + push

```bash
cd "<repo 루트>"
git tag -a v<버전> -m "v<버전>"
git push origin main v<버전>      # main과 태그를 함께 push (CI는 태그로 트리거)
```

main도 함께 push해 origin/main을 최신으로 유지한다(태그만 push해도 CI는 돌지만 origin이 뒤처진다).

## 5. CI 릴리스 모니터링 + 확인

```bash
gh run list --workflow=release.yml --limit 3        # 방금 run의 id 확인
gh run watch <run-id> --exit-status                 # 완료까지 대기(이전 릴리스 ~1.5~2분)
gh release view v<버전> --json tagName,isDraft,url,assets   # Release·에셋 확인
```

성공 기준: `build-windows` success(태그검증·빌드·스모크·업로드 전부 ✓) + Release가 **draft 아님** +
에셋에 **`Tokenomy-<버전>.exe`**(버전 표기 파일명) 존재. CI가 빌드 산출물 `Tokenomy.exe`를 태그
버전으로 복사("Stage versioned artifact" 스텝)해 업로드한다. CI가 실패하면 로그(`gh run view <id> --log-failed`)로 원인 분석.

> **CI를 기다리는 ~1.5~2분 동안 다음 단계(6)의 노트 초안을 미리 써 둔다.** 그러면 CI가
> 끝나자마자 `edit` 한 번으로 채워지고, "CI 성공 = 끝"으로 착각해 노트를 빠뜨리는 일이 없다.

## 6. 릴리스 노트 작성 (사용자용 — 릴리스의 일부, 건너뛰면 미완)

CI가 만든 Release는 본문이 **비어 있다**(`action-gh-release`가 `files`만 올림). 그대로 두지 말고
**사용자가 이해할 한국어 릴리스 노트를 작성해 채운다.** 커밋 메시지(개발자용)가 아니라 "이번
버전에서 사용자에게 무엇이 달라지나"를 평이하게 쓴다(커밋 로그 나열 금지). 직전 버전 노트를
형식 참고로 본다: `gh release view v<직전버전> --json body -q .body`.

1. 이번 버전의 변경을 사용자 관점으로 요약한다 — **주요 변경 / 설정·옵션 / 주의사항** 정도로 묶고,
   "왜 좋아지나"를 1~3줄로. 끝에 "아래 `Tokenomy-<버전>.exe`를 받아 실행" 같은 설치 한 줄.
2. Release가 CI로 **생성된 뒤**라 `edit`로 채운다(노트는 heredoc 문자열 또는 파일로):
   ```bash
   gh release edit v<버전> --notes "$(cat <<'EOF'
   ## <한 줄 제목 — 사용자 체감 변화>
   ...
   EOF
   )"
   # 또는: gh release edit v<버전> --notes-file <노트파일>
   ```
3. **검증**(누락 가드): 본문이 비지 않았는지 확인한다.
   ```bash
   gh release view v<버전> --json body --jq '.body | length'   # 0이면 누락 — 다시 채운다
   ```

> 안전망: `release.yml`의 `action-gh-release`에 `generate_release_notes: true`를 둬, 위 단계를
> 건너뛰어도 빈 본문은 안 나오게 한다(GitHub 자동 변경로그). 위 큐레이션 노트는 그걸 덮어쓴다.

## 7. 보고

표로 요약한다: 버전 bump 커밋, 로컬 스모크 결과, 태그, CI run 결과(소요시간), **릴리스 노트 작성(본문 길이>0)**, Release URL/에셋명(`Tokenomy-<버전>.exe`)·크기. 보고 전에 위 **완료 정의** 체크리스트가 전부 ✓인지 다시 본다.

## 게시(gotchas) 요약

- **태그 == `__version__`** 아니면 CI 실패 → 버전 먼저 bump, 같은 번호로 태그.
- **빌드는 `.venv`로** — 시스템 Python은 pywebview 누락 → 창 대신 브라우저 fallback.
- **로컬 빌드는 검증용**, 공개 배포본은 CI 산출물. 로컬 `dist/`는 gitignore(커밋 안 됨).
- **main + 태그 함께 push.** 태그만 올리면 origin/main이 뒤처진다.
- **릴리스 노트 누락 금지.** CI Release는 본문이 빈 채로 발행된다 — 단계 6에서 사용자용 한국어 노트로 채우고 본문 길이>0을 확인한다(완료 정의 항목). `release.yml`의 `generate_release_notes: true`는 안전망일 뿐 — 자동 변경로그는 사용자용 노트가 아니다.
- **Release 자산은 버전 표기 파일명** `Tokenomy-<버전>.exe`(CI "Stage versioned artifact" 스텝이 빌드 산출물 `Tokenomy.exe`를 복사). 내부 빌드/스모크는 안정적 이름 `Tokenomy.exe` 유지.
- **버전 bump은 main에서 직접 편집·커밋이 기본.** 환경이 추적 파일 직접 편집을 막을 때만(가드 존재 시) 워크트리로 우회.
- **워킹 트리 클린 확인** — 사전 점검에서 `git status --porcelain`이 비어야 한다. 미커밋 추적 변경(빠뜨린 문서·설정)을 릴리스 직전에 발견하는 사고를 막는다.
- **중복 태그 금지** — 사전 점검에서 `v<버전>` 부재 확인. 이미 있으면 중단.
- CI 로그의 **Node 20 deprecation 경고**는 빌드에 무해(액션 메이저 버전 업그레이드가 근본 해결).
