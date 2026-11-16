from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from app.core.config import get_settings
from app.security.crypto import DecryptionError, decrypt_bytes, encrypt_bytes


class StorageError(Exception):
    pass


class Storage(Protocol):
    """Private blob store. A cloud object-store backend can implement the same interface."""

    def save(self, key: str, data: bytes) -> None: ...
    def read(self, key: str) -> bytes: ...
    def path_for(self, key: str) -> Path: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...


class LocalStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, key: str) -> Path:
        """Resolve a storage key to a path, refusing anything that escapes the root."""
        if not key or key.startswith(("/", "\\")) or ".." in Path(key).parts:
            raise StorageError("invalid storage key")
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise StorageError("invalid storage key")
        return path

    def save(self, key: str, data: bytes) -> None:
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        try:
            tmp.write_bytes(encrypt_bytes(data))
            tmp.replace(path)  # atomic: no partially written final file
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise StorageError("failed to store file") from exc

    def read(self, key: str) -> bytes:
        try:
            return decrypt_bytes(self.path_for(key).read_bytes())
        except OSError as exc:
            raise StorageError("failed to read file") from exc
        except DecryptionError as exc:
            raise StorageError("stored file cannot be decrypted") from exc

    def delete(self, key: str) -> None:
        try:
            self.path_for(key).unlink(missing_ok=True)
        except OSError as exc:
            raise StorageError("failed to delete file") from exc

    def exists(self, key: str) -> bool:
        return self.path_for(key).is_file()

    def iter_keys(self) -> Iterator[str]:
        for p in self.root.rglob("*"):
            if p.is_file():
                yield p.relative_to(self.root).as_posix()


@lru_cache
def get_storage() -> LocalStorage:
    return LocalStorage(get_settings().storage_dir)
