import io
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="zb_test_")
os.environ["ZB_DATA_DIR"] = _TMP
os.environ["ZB_AI_PROVIDER"] = "mock"
os.environ["ZB_SCHEDULER"] = "0"
os.environ["ZB_ENV_FILE"] = os.path.join(_TMP, "none.env")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db as dbmod  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.security import hash_password  # noqa: E402
from sample_xer import make_xer  # noqa: E402

TODAY = date.today()
START = (TODAY - timedelta(days=10)).isoformat()
DATA_DATE = (TODAY - timedelta(days=2)).isoformat()
PW = "secret123"


@pytest.fixture()
def env():
    for p in (settings.db_path, Path(str(settings.db_path) + "-wal"), Path(str(settings.db_path) + "-shm")):
        if p.exists():
            p.unlink()
    dbmod._initialized = False
    dbmod.init_db()
    conn = dbmod.connect()
    pid = dbmod.insert(conn, "projects", {"name": "示例大桥（合成测试）", "code": "T1", "config_json": '{"cadence": "day"}', "created_at": dbmod.now_iso()})
    users = {}
    for username, role, scope in (("admin", "admin", ""), ("pm", "manager", ""), ("tech1", "tech", "3#墩"), ("tech2", "tech", "路基")):
        users[username] = dbmod.insert(conn, "users", {
            "username": username, "name": {"admin": "管理员", "pm": "项目经理", "tech1": "张工", "tech2": "李工"}[username],
            "role": role, "password_hash": hash_password(PW), "project_id": pid, "wbs_scope": scope,
            "email": "", "active": 1, "created_at": dbmod.now_iso()})
    conn.close()
    yield {"project_id": pid, "users": users}


def login(username: str) -> TestClient:
    c = TestClient(app)
    c.headers.update({"x-zb": "1"})
    r = c.post("/api/login", json={"username": username, "password": PW})
    assert r.status_code == 200, r.text
    return c


def confirm_hazards() -> None:
    """测试辅助：把 AI 候选隐患直接置为已确认，用于不关心确认环节的既有用例（真实流程是安全员/安全主管点“确认并派单”）。"""
    conn = dbmod.connect()
    conn.execute("UPDATE findings SET confirmed_at=created_at WHERE confirmed_at IS NULL AND status='open'")
    conn.close()


def sample_xer(data_date: str = DATA_DATE, **kw) -> bytes:
    progress = kw.pop("progress", {"M0000": 100, "A1010": 80, "A2010": 60, "A3010": 50, "A4010": 10})
    return make_xer(data_date, start=START, progress=progress, **kw).encode("utf-8")


def jpeg(color=(120, 110, 100), exif_date: str | None = None) -> bytes:
    from PIL import Image
    img = Image.new("RGB", (640, 480), color)
    buf = io.BytesIO()
    if exif_date:
        exif = Image.Exif()
        exif[306] = exif_date
        img.save(buf, "JPEG", exif=exif.tobytes())
    else:
        img.save(buf, "JPEG")
    return buf.getvalue()


def backdate(log_id: int, minutes: int = 60) -> None:
    """测试中模拟“编写已持续一段时间”，否则页面活跃时长会被墙钟时间截断。"""
    from datetime import datetime
    conn = dbmod.connect()
    started = (datetime.now().astimezone() - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    conn.execute("UPDATE logs SET started_at=? WHERE id=?", (started, log_id))
    conn.close()
