"""수집 실행 상태(ADR 0023) — 인프로세스 running 플래그(+lock).

배너 게이트(`is_ingesting()`)와 동시-수집 가드(`begin_ingest()`가 진행 중이면 False)를
겸한다. 모든 수집 진입점(시작 지연수집·창 복원 re-ingest·수동 /ingest)이 공유한다.
"""
import threading

from tokenomy.web import control


def test_initial_not_ingesting():
    control.end_ingest()                        # 다른 테스트 영향 정규화
    assert control.is_ingesting() is False


def test_begin_marks_ingesting():
    control.end_ingest()
    assert control.begin_ingest() is True
    try:
        assert control.is_ingesting() is True
    finally:
        control.end_ingest()


def test_begin_while_running_returns_false():
    control.end_ingest()
    assert control.begin_ingest() is True
    try:
        assert control.begin_ingest() is False   # 진행 중 재진입 차단(동시 writer 방지)
    finally:
        control.end_ingest()


def test_end_allows_rebegin():
    control.end_ingest()
    control.begin_ingest()
    control.end_ingest()
    assert control.begin_ingest() is True        # 끝났으면 다시 획득
    control.end_ingest()


def test_begin_is_thread_safe_single_winner():
    """동시 진입 — 정확히 하나만 True(threading.Lock)."""
    control.end_ingest()
    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        got = control.begin_ingest()
        with lock:
            results.append(got)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    control.end_ingest()
    assert results.count(True) == 1
