import os
from pathlib import Path

APP_NAME = "营运信息批量查询工具"
APP_VERSION = "0.2.5"
ORGANIZATION_NAME = "IntDemo"

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "Admin@123"


def get_data_dir() -> Path:
    """返回客户端可写数据目录，测试时可通过环境变量覆盖。"""
    override = os.environ.get("INTDEMO_DATA_DIR", "").strip()
    if override:
        path = Path(override).expanduser().resolve()
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        path = base / "IntDemoClientOnlineTest"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        path = base / "intdemo-client-online-test"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_database_path() -> Path:
    return get_data_dir() / "client-v2.db"

