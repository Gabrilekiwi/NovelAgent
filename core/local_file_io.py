"""Native path spelling for file I/O; never use it as a stored book identity."""
import os
from pathlib import Path


def local_io_path(path: Path) -> Path:
    if os.name != "nt":
        return path
    absolute = os.path.abspath(path)
    if len(absolute.encode("utf-16-le")) // 2 < 248 or absolute.startswith(("\\\\?\\", "\\\\.\\")):
        return path
    parts = Path(absolute).parts[1:]
    if any(part.endswith((".", " ")) or Path(part).is_reserved() or ":" in part for part in parts):
        raise ValueError("Noncanonical Windows path cannot use extended-length I/O")
    if absolute.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + absolute[2:])
    return Path("\\\\?\\" + absolute)
