import pytest

from app.storage.local import LocalStorage, StorageError


def test_save_read_delete_roundtrip(tmp_path):
    s = LocalStorage(tmp_path)
    s.save("originals/a.txt", b"hello")
    assert s.exists("originals/a.txt") and s.read("originals/a.txt") == b"hello"
    s.delete("originals/a.txt")
    assert not s.exists("originals/a.txt")
    s.delete("originals/a.txt")  # idempotent


@pytest.mark.parametrize("key", ["../x", "a/../../x", "/etc/passwd", "\\windows\\x", "", "a/.."])
def test_path_traversal_blocked(tmp_path, key):
    s = LocalStorage(tmp_path / "root")
    with pytest.raises(StorageError):
        s.path_for(key)
    with pytest.raises(StorageError):
        s.save(key, b"x")


def test_no_partial_files_left(tmp_path):
    s = LocalStorage(tmp_path)
    s.save("k.bin", b"1")
    assert sorted(s.iter_keys()) == ["k.bin"]
