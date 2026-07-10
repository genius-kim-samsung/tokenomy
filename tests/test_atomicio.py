"""atomicio — 원자적 JSON 쓰기 공용 leaf 헬퍼 테스트.

config.save_config와 official_fetch 토큰 write-back이 각자 갖던 원자적 쓰기를
하나로 합친 프리미티브(v0.1.47 후속 리팩터). 손상 클래스(부분 기록·동시 쓰기
인터리브·temp 충돌)가 닫혀 있음을 이 파일이 고정한다.
"""
from __future__ import annotations

import json
import os

import pytest

from tokenomy.atomicio import atomic_write_json


def test_write_then_read_roundtrip(tmp_path):
    """완전한 유효 JSON이 기록되고(indent=2·비ASCII 보존) temp 잔재가 없다."""
    p = tmp_path / "out.json"
    atomic_write_json(p, {"user_label": "경식", "n": 1})
    assert json.loads(p.read_text(encoding="utf-8")) == {"user_label": "경식", "n": 1}
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_failure_raises_and_cleans_tmp_keeps_original(tmp_path, monkeypatch):
    """쓰기 실패(OSError)는 전파하되 temp를 정리하고 원본은 절대 건드리지 않는다."""
    import tokenomy.atomicio as aio
    p = tmp_path / "out.json"
    p.write_text('{"keep": true}', encoding="utf-8")
    def _boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(aio.os, "replace", _boom)
    with pytest.raises(OSError):
        atomic_write_json(p, {"new": 1})
    assert json.loads(p.read_text(encoding="utf-8")) == {"keep": True}  # 원본 무손상
    assert list(tmp_path.glob("*.tmp")) == []                           # temp 잔재 없음


def test_replace_permission_error_is_retried(tmp_path, monkeypatch):
    """Windows 리더가 대상을 연 순간의 일시적 PermissionError는 재시도로 흡수한다."""
    import tokenomy.atomicio as aio
    p = tmp_path / "out.json"
    real_replace = os.replace
    fails = {"left": 3}
    def _flaky(src, dst):
        if fails["left"] > 0:
            fails["left"] -= 1
            raise PermissionError("target open by reader")
        return real_replace(src, dst)
    monkeypatch.setattr(aio.os, "replace", _flaky)
    atomic_write_json(p, {"ok": 1})                     # raise 없이 성공해야 한다
    assert json.loads(p.read_text(encoding="utf-8")) == {"ok": 1}
    assert list(tmp_path.glob("*.tmp")) == []


def test_replace_permission_error_exhaustion_raises_and_cleans(tmp_path, monkeypatch):
    """재시도가 소진되면 PermissionError를 전파하되 temp는 정리한다(무한 재시도 금지)."""
    import tokenomy.atomicio as aio
    monkeypatch.setattr(aio, "_REPLACE_BACKOFF", 0)     # 소진 루프 대기 제거
    p = tmp_path / "out.json"
    def _always(src, dst):
        raise PermissionError("target open forever")
    monkeypatch.setattr(aio.os, "replace", _always)
    with pytest.raises(PermissionError):
        atomic_write_json(p, {"ok": 1})
    assert list(tmp_path.glob("*.tmp")) == []


def test_perms_creates_tmp_with_given_mode(tmp_path, monkeypatch):
    """perms 지정 시 temp를 그 모드로 생성한다(토큰 0600 — 평문 권한 노출 방지, ADR 0021)."""
    import tokenomy.atomicio as aio
    p = tmp_path / "auth.json"
    seen = {}
    real_open = os.open
    def _spy(path_arg, flags, mode=0o777):
        seen["mode"] = mode
        return real_open(path_arg, flags, mode)
    monkeypatch.setattr(aio.os, "open", _spy)
    atomic_write_json(p, {"tok": "secret"}, perms=0o600)
    assert seen["mode"] == 0o600
    assert json.loads(p.read_text(encoding="utf-8")) == {"tok": "secret"}


def test_atomic_under_concurrent_writers_without_lock(tmp_path):
    """두 스레드가 락 없이 다른 크기 payload를 동시 반복 기록해도 손상이 불가능하다.

    고유 temp명 + 원자 replace만으로 손상 클래스가 닫혀 있음을 고정한다(락은 직렬화
    목적일 뿐 손상 방지의 전제가 아니다). 쓰는 동안 계속 읽어도(리더 스레드) 어떤
    관찰자도 깨진 JSON을 보면 안 된다 — v0.1.46 config 브릭과 같은 클래스의 회귀 가드."""
    import threading
    p = tmp_path / "f.json"
    small = {"k": "a"}
    large = {"k": "b" * 400, "pad": ["x"] * 60}
    corrupt_seen, writer_errors = [], []
    stop = threading.Event()

    def hammer(payload):
        try:
            for _ in range(200):
                atomic_write_json(p, dict(payload))
        except BaseException as e:                    # 스레드 안 예외는 기본 무음 — 수집해 단언
            writer_errors.append(repr(e))

    def reader():
        while not stop.is_set():
            try:
                if p.exists():
                    json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                corrupt_seen.append(str(e))
            except (OSError, ValueError):
                pass

    r = threading.Thread(target=reader)
    r.start()
    writers = [threading.Thread(target=hammer, args=(pl,)) for pl in (small, large)]
    for t in writers:
        t.start()
    for t in writers:
        t.join()
    stop.set()
    r.join()
    assert writer_errors == []                 # 재시도 소진 등으로 writer가 죽지 않음
    assert corrupt_seen == []                  # 어떤 리더도 손상된 JSON을 못 봄
    json.loads(p.read_text(encoding="utf-8"))  # 최종 파일도 유효
    assert list(tmp_path.glob("*.tmp")) == []  # temp 잔재 없음(고유 temp명 포함)
