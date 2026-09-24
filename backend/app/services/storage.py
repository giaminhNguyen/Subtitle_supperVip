"""Subtitle file storage: portable relative paths + crash-safe writes."""
import os
import tempfile
from pathlib import Path

from ..config import settings


class UnsafePathError(ValueError):
    pass


def _data_root() -> Path:
    return Path(settings.data_dir).resolve()


def to_stored_path(path: Path | str) -> str:
    """Path inside DATA_DIR -> relative posix string for the database."""
    try:
        return Path(path).resolve().relative_to(_data_root()).as_posix()
    except ValueError:
        raise UnsafePathError("Đường dẫn nằm ngoài thư mục dữ liệu")


def resolve_subtitle_path(stored: str) -> Path:
    """Resolve a stored path against the *current* DATA_DIR.

    Relative paths must stay inside DATA_DIR. Legacy absolute paths are accepted when they
    are inside DATA_DIR, or (for old installs) when the file still exists where it was saved.
    """
    root = _data_root()
    path = Path(stored)
    if path.is_absolute():
        resolved = path.resolve()
        if resolved.is_relative_to(root) or resolved.exists():
            return resolved
        raise UnsafePathError("Không tìm thấy file subtitle cũ")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise UnsafePathError("Đường dẫn subtitle thoát khỏi thư mục dữ liệu")
    return resolved


def atomic_write_text(path: Path, content: str) -> None:
    """Write via a temp file in the same directory, then os.replace, so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
