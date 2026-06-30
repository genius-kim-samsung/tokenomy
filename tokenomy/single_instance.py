"""단일 인스턴스 락(ADR 0023) — 가계부(data_dir)당 프로세스 하나를 OS 수준에서 보장.

`launcher.main()` 최상단에서 `SingleInstanceLock(data_dir).acquire()`로 "내가 첫
인스턴스인가"를 권위적으로 판정한다. 획득하면 핸들을 프로세스 생애 동안 보유하고,
프로세스가 죽으면 **OS가 자동으로 해제**한다(sentinel 파일과 달리 크래시 후 stale 없음).
런타임 파일(runtime.json)+`/app/ping`은 "어느 창을 띄울지" 라우팅에만 잔류한다.

플랫폼 분기는 이 모듈 안에만 있다(`mini_view_available()`처럼 단일 게이트):
- Windows: 명명 뮤텍스 `CreateMutexW` — 두 번째 생성은 `ERROR_ALREADY_EXISTS`.
- POSIX: `fcntl.flock`(data_dir 아래 락 파일에 LOCK_EX|LOCK_NB).

락 이름은 data_dir별로 키잉(`lock_name`)해 서로 다른 가계부(`TOKENOMY_DATA`로 분리)는
각자 한 인스턴스씩 공존한다.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

ERROR_ALREADY_EXISTS = 183
_LOCK_FILE = "instance.lock"   # POSIX flock 대상(data_dir 아래)


def lock_name(data_dir) -> str:
    """data_dir(가계부)에서 안정적인 락 이름을 파생한다(순수).

    경로 정규화(`resolve`)에 더해 **대소문자 무시 FS 보정**(`os.path.normcase`)을 거쳐
    sha256 — 같은 가계부는 같은 이름, 다른 가계부는 다른 이름. Windows의 대소문자/경로
    별칭 함정을 줄인다(Codex 리뷰)."""
    norm = os.path.normcase(str(Path(data_dir).resolve()))
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:32]
    return f"tokenomy-{digest}"


class SingleInstanceLock:
    """가계부 하나에 대한 단일 인스턴스 락. acquire()로 시도, release()로 해제.

    핸들(Windows 뮤텍스 HANDLE · POSIX 파일 fd)은 획득 후 보유한다 — 프로세스가
    살아 있는 동안 락이 유지되고, 종료/크래시 시 OS가 해제한다."""

    def __init__(self, data_dir) -> None:
        self._data_dir = Path(data_dir)
        self._name = lock_name(data_dir)
        self._handle = None         # win32: 뮤텍스 HANDLE / posix: 파일 fd
        self.acquired = False

    def acquire(self) -> bool:
        """첫 인스턴스면 True(락 보유), 이미 점유 중이면 False."""
        if sys.platform == "win32":
            return self._acquire_windows()
        return self._acquire_posix()

    def release(self) -> None:
        """락 해제(핸들 정리). 미획득이면 no-op. 보통은 프로세스 종료 시 OS가 해제하지만
        명시 해제도 지원한다(테스트의 release-후-재획득 등)."""
        if sys.platform == "win32":
            self._release_windows()
        else:
            self._release_posix()

    # ── Windows: 명명 뮤텍스 ──────────────────────────────────────────────────
    def _acquire_windows(self) -> bool:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE        # 64비트 핸들 절단 방지
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        handle = kernel32.CreateMutexW(None, False, self._name)
        last_error = ctypes.get_last_error()       # CreateMutexW 직후 즉시 읽는다
        if not handle:
            # 뮤텍스 생성 자체 실패는 드물다 — 단일 인스턴스 판정 불가 시 앱 기동을 막지
            # 않도록 첫 인스턴스로 진행한다(보수적 fail-open).
            return True
        if last_error == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)           # 즉시 닫아 named object 참조를 안 남긴다
            return False
        self._handle = handle
        self.acquired = True
        return True

    def _release_windows(self) -> None:
        if self._handle:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle(self._handle)
            self._handle = None
        self.acquired = False

    # ── POSIX: flock ─────────────────────────────────────────────────────────
    def _acquire_posix(self) -> bool:
        import fcntl

        self._data_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self._data_dir / _LOCK_FILE
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._handle = fd
        self.acquired = True
        return True

    def _release_posix(self) -> None:
        if self._handle is not None:
            import fcntl

            try:
                fcntl.flock(self._handle, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self._handle)
            self._handle = None
        self.acquired = False
