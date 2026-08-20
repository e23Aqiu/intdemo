from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path


def is_excel_file_read_only(file_path) -> bool:
    path = Path(str(file_path or ""))
    if not path.is_file():
        return False
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return (
        not bool(mode & stat.S_IWUSR)
        or not os.access(path, os.W_OK)
        or not os.access(path.parent, os.W_OK)
    )


def make_excel_file_writable(file_path) -> Path:
    """Remove a workbook's read-only attribute and verify in-place writes."""
    path = Path(file_path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise FileNotFoundError(f"表格文件不存在：{path}")

    try:
        current_mode = path.stat().st_mode
        path.chmod(current_mode | stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        raise PermissionError(f"无法解除表格的只读属性：{exc}") from exc

    if not bool(path.stat().st_mode & stat.S_IWUSR) or not os.access(
        path, os.W_OK
    ):
        raise PermissionError("系统权限不允许修改原表格")
    if not os.access(path.parent, os.W_OK):
        raise PermissionError("表格所在文件夹不可写，无法安全覆盖原表格")
    return path


def is_excel_file_open(file_path) -> bool:
    """Return whether another process is holding a writable workbook open."""
    path = Path(str(file_path or ""))
    if not path.is_file() or is_excel_file_read_only(path):
        return False
    if sys.platform.startswith("win"):
        try:
            with path.open("r+b"):
                pass
            return False
        except PermissionError:
            return True
        except OSError:
            return False
    try:
        result = subprocess.run(
            ["lsof", "-t", str(path)],
            capture_output=True,
            timeout=2,
            check=False,
        )
        return bool(result.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def copy_excel_as_writable(source, destination) -> Path:
    source_path = Path(source).resolve(strict=True)
    destination_path = Path(destination).expanduser().resolve()
    if source_path == destination_path:
        raise ValueError("另存路径不能与原表格相同")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_path, destination_path)
    return make_excel_file_writable(destination_path)
