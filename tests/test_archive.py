from tokenomy.db import connect
from tokenomy.archive import archive_tree


def _seed(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_archive_copies_raw_lines(tmp_path):
    conn = connect(":memory:")
    root = tmp_path / "projects"
    _seed(root, "proj/s.jsonl", '{"a":1}\n')
    archive_dir = tmp_path / "archive"
    n = archive_tree(root, conn, provider="claude", archive_root=archive_dir)
    assert n == 1
    dest = archive_dir / "claude" / "proj" / "s.jsonl"
    assert dest.read_text(encoding="utf-8") == '{"a":1}\n'


def test_archive_incremental_appends_only_new(tmp_path):
    conn = connect(":memory:")
    root = tmp_path / "projects"
    f = _seed(root, "p/s.jsonl", '{"a":1}\n')
    archive_dir = tmp_path / "archive"
    archive_tree(root, conn, archive_root=archive_dir)
    with open(f, "a", encoding="utf-8") as fh:
        fh.write('{"b":2}\n')
    archive_tree(root, conn, archive_root=archive_dir)
    dest = archive_dir / "claude" / "p" / "s.jsonl"
    assert dest.read_text(encoding="utf-8") == '{"a":1}\n{"b":2}\n'


def test_archive_second_run_no_dup(tmp_path):
    conn = connect(":memory:")
    root = tmp_path / "projects"
    _seed(root, "p/s.jsonl", '{"a":1}\n')
    archive_dir = tmp_path / "archive"
    archive_tree(root, conn, archive_root=archive_dir)
    n2 = archive_tree(root, conn, archive_root=archive_dir)  # 새 바이트 없음
    dest = archive_dir / "claude" / "p" / "s.jsonl"
    assert dest.read_text(encoding="utf-8") == '{"a":1}\n'  # 두 배 안 됨
    assert n2 == 0
