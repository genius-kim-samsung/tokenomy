"""원자적 JSON 파일 쓰기 — stdlib-only 저층 leaf(의존성 0, clock/domain 원칙).

config 영속(save_config)과 OAuth 토큰 write-back(official_fetch)이 각자 갖던
원자적 쓰기 구현을 합친 단일 프리미티브(v0.1.47 config 손상 브릭 후속).
같은 디렉터리의 **쓰기 주체별 고유**(PID+스레드) temp 파일에 완전히 쓴 뒤
`os.replace`로 원자 교체한다 — 리더는 항상 완전한 파일(옛것 또는 새것)만 보고,
동시 쓰기가 겹쳐도 last-wins일 뿐 손상은 불가능하다.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

# Windows에서 os.replace는 대상 파일을 다른 스레드/프로세스가 열고 있으면(리더의 read_text 등)
# PermissionError로 막힌다 — 리더의 open 창은 수 ms라 짧게 물러났다 재시도해 흡수한다.
_REPLACE_ATTEMPTS = 25
_REPLACE_BACKOFF = 0.004


def atomic_write_json(path: Path, data, *, perms: int | None = None) -> None:
    """data를 path에 원자적으로 JSON 기록(indent=2, ensure_ascii=False). 실패는 raise.

    perms를 주면 temp를 그 모드로 생성한다(비밀 파일용 — 토큰은 0o600, ADR 0021.
    POSIX 권한; Windows는 상위 디렉터리 ACL 상속이라 no-op). None이면 일반 텍스트 쓰기.
    `os.replace`의 일시적 PermissionError(Windows 리더 창)는 짧은 백오프로 재시도한다.
    최종 실패는 OSError를 전파하되 temp를 정리하고 **원본은 절대 건드리지 않는다** —
    bool 폴백이 필요한 호출부(토큰 write-back)는 밖에서 try/except로 감싼다.
    프로세스 내 직렬화 락은 두지 않는다 — 고유 temp명+원자 replace로 손상은 이미 불가능
    하고(last-wins), lost-update 방지가 필요하면 호출부가 자기 락으로 감싼다(save_config의
    `_SAVE_LOCK` 등).
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    text = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        if perms is None:
            tmp.write_text(text, encoding="utf-8")
        else:                             # 비밀 파일(토큰) — 생성 시점부터 권한 제한(fd 경로)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, perms)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:       # Windows 리더가 path를 연 순간 — 잠깐 뒤 재시도
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(_REPLACE_BACKOFF)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
