"""扩展路由：每个功能一个模块（app/routers/<名>.py），主程序自动发现，不必改 main.py。

模块约定（全部可选）：
- SCHEMA: str            建表 SQL（CREATE TABLE IF NOT EXISTS …），init_db 时执行
- migrate(conn)          给既有表补列等幂等迁移，在 SCHEMA 之后执行
- register(app)          挂接路由。依赖在函数内取：`from .. import main as M`，用 M.current_user / M.db / M.admin 等，
                         避免与 main 循环导入
一个模块出错只记录日志，不拖垮其它功能。
"""
from __future__ import annotations

import importlib
import logging
import pkgutil

log = logging.getLogger("zhimou.routers")


def _modules():
    for m in pkgutil.iter_modules(__path__):
        if m.name.startswith("_"):
            continue
        try:
            yield importlib.import_module(f"{__name__}.{m.name}")
        except Exception:  # noqa: BLE001
            log.exception("扩展模块加载失败：%s", m.name)


def apply_schema(conn) -> None:
    for mod in _modules():
        try:
            if getattr(mod, "SCHEMA", None):
                conn.executescript(mod.SCHEMA)
            if getattr(mod, "migrate", None):
                mod.migrate(conn)
        except Exception:  # noqa: BLE001
            log.exception("扩展模块建表失败：%s", mod.__name__)


def load(app) -> None:
    for mod in _modules():
        try:
            if getattr(mod, "register", None):
                mod.register(app)
        except Exception:  # noqa: BLE001
            log.exception("扩展模块注册失败：%s", mod.__name__)
