"""预警标记（回放产生等）的建表；标记由运维脚本写入，页面只读，见 services/alert_tags.py。"""
from ..services.alert_tags import SCHEMA  # noqa: F401
