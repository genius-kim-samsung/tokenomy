"""단일 인스턴스 OS 락(ADR 0023) — 가계부(data_dir)당 프로세스 하나.

Windows=CreateMutexW 뮤텍스, POSIX=fcntl.flock. 락 이름은 data_dir별로 키잉돼
서로 다른 가계부는 공존한다. acquire는 같은 가계부의 두 번째 시도를 막는다.
"""
import os
import subprocess
import sys
import textwrap

from tokenomy.single_instance import SingleInstanceLock, lock_name


def test_lock_name_deterministic_and_path_distinct(tmp_path):
    a = tmp_path / "book-a"
    b = tmp_path / "book-b"
    a.mkdir()
    b.mkdir()
    assert lock_name(a) == lock_name(a)        # 같은 가계부 → 같은 이름(결정적)
    assert lock_name(a) != lock_name(b)        # 다른 가계부 → 다른 이름


def test_second_acquire_same_dir_fails(tmp_path):
    """같은 가계부 — 첫 인스턴스만 획득, 두 번째는 막힌다."""
    first = SingleInstanceLock(tmp_path)
    second = SingleInstanceLock(tmp_path)
    try:
        assert first.acquire() is True
        assert second.acquire() is False
    finally:
        first.release()
        second.release()


def test_different_dirs_coexist(tmp_path):
    """다른 가계부(TOKENOMY_DATA로 분리)는 각자 한 인스턴스씩 공존."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    la = SingleInstanceLock(a)
    lb = SingleInstanceLock(b)
    try:
        assert la.acquire() is True
        assert lb.acquire() is True            # 다른 가계부 → 둘 다 획득
    finally:
        la.release()
        lb.release()


def test_release_then_reacquire(tmp_path):
    """해제하면 다시 획득 가능 — 크래시 후 OS 자동 해제와 동치(획득 실패 핸들을 안 남겨야 함)."""
    first = SingleInstanceLock(tmp_path)
    assert first.acquire() is True
    first.release()
    second = SingleInstanceLock(tmp_path)
    try:
        assert second.acquire() is True        # 해제됐으니 재획득
    finally:
        second.release()


def test_cross_process_conflict(tmp_path):
    """교차-프로세스 — 자식이 락을 쥔 동안 부모는 같은 가계부를 못 잡는다.

    POSIX flock은 같은 프로세스 두 번째 acquire가 구현에 따라 다르게 보일 수 있어,
    실제 시나리오(별개 프로세스)는 subprocess로 검증한다(Codex 리뷰)."""
    child_src = textwrap.dedent(f"""
        import sys, time
        from tokenomy.single_instance import SingleInstanceLock
        lk = SingleInstanceLock(r"{tmp_path}")
        ok = lk.acquire()
        print("ACQUIRED" if ok else "FAILED", flush=True)
        time.sleep(30)
    """)
    env = dict(os.environ)
    child = subprocess.Popen(
        [sys.executable, "-c", child_src],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    try:
        line = child.stdout.readline().strip()    # 자식이 락 잡을 때까지 대기
        assert line == "ACQUIRED"
        parent = SingleInstanceLock(tmp_path)
        try:
            assert parent.acquire() is False       # 자식이 점유 중 → 부모 실패
        finally:
            parent.release()
    finally:
        child.terminate()
        child.wait(timeout=10)

