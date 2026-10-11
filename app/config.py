"""运行配置：环境变量优先，其次读取数据目录或程序目录下的 .env。

只保留部署真正需要的开关。密钥只从环境/本地 .env 读取，不写库、不回显。
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent

# 交融大模型 OpenAI 兼容接口（集团内网/互联网均可达的官方地址）
JIAORONG_BASE_URL = "https://c4ai.ccccltd.cn/api/compatible/v1"
# 内部数据只允许发往交融；如集团另有交融私有化地址，用 ZB_AI_ALLOWED_HOSTS 追加
DEFAULT_ALLOWED_AI_HOSTS = ("c4ai.ccccltd.cn",)


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    data_dir: Path
    secret: str
    ai_provider: str  # jiaorong | mock
    ai_base_url: str
    ai_api_key: str = field(repr=False)
    ai_vision_model: str
    ai_text_model: str
    ai_timeout: float
    ai_trust_env: bool
    ai_allowed_hosts: tuple[str, ...]
    ai_max_images: int
    ai_asr_model: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str = field(repr=False)
    smtp_from: str
    smtp_ssl: bool
    webhook_url: str
    host: str
    port: int

    @property
    def db_path(self) -> Path:
        return self.data_dir / "zhimou.db"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "exports"

    def ai_host_allowed(self) -> bool:
        host = (urlparse(self.ai_base_url).hostname or "").lower()
        return any(host == h or host.endswith("." + h) for h in self.ai_allowed_hosts)


def load_settings() -> Settings:
    _load_dotenv(Path(os.environ.get("ZB_ENV_FILE", ROOT / ".env")))
    data_dir = Path(os.environ.get("ZB_DATA_DIR", ROOT / "data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    secret = os.environ.get("ZB_SECRET", "")
    if not secret:
        # 首次运行生成本机会话密钥并保存在数据目录；不进入 Git
        secret_file = data_dir / ".secret"
        if secret_file.is_file():
            secret = secret_file.read_text().strip()
        else:
            secret = secrets.token_urlsafe(48)
            secret_file.write_text(secret)

    extra_hosts = tuple(h.strip().lower() for h in os.environ.get("ZB_AI_ALLOWED_HOSTS", "").split(",") if h.strip())
    api_key = os.environ.get("ZB_AI_API_KEY") or os.environ.get("ZHIMOU_AI_API_KEY", "")
    provider = os.environ.get("ZB_AI_PROVIDER", "jiaorong" if api_key else "mock").strip().lower()

    return Settings(
        data_dir=data_dir,
        secret=secret,
        ai_provider=provider,
        ai_base_url=os.environ.get("ZB_AI_BASE_URL", JIAORONG_BASE_URL).rstrip("/"),
        ai_api_key=api_key,
        ai_vision_model=os.environ.get("ZB_AI_VISION_MODEL", "jiaorong-qwen3-vl-32b-instruct"),
        ai_text_model=os.environ.get("ZB_AI_TEXT_MODEL", "") or os.environ.get("ZB_AI_VISION_MODEL", "jiaorong-qwen3-vl-32b-instruct"),
        ai_timeout=float(os.environ.get("ZB_AI_TIMEOUT", "180")),
        ai_trust_env=_bool("ZB_AI_TRUST_ENV", False),
        ai_allowed_hosts=DEFAULT_ALLOWED_AI_HOSTS + extra_hosts,
        ai_max_images=int(os.environ.get("ZB_AI_MAX_IMAGES", "8")),
        ai_asr_model=os.environ.get("ZB_AI_ASR_MODEL", ""),
        smtp_host=os.environ.get("ZB_SMTP_HOST", ""),
        smtp_port=int(os.environ.get("ZB_SMTP_PORT", "465")),
        smtp_user=os.environ.get("ZB_SMTP_USER", ""),
        smtp_password=os.environ.get("ZB_SMTP_PASSWORD", ""),
        smtp_from=os.environ.get("ZB_SMTP_FROM", "") or os.environ.get("ZB_SMTP_USER", ""),
        smtp_ssl=_bool("ZB_SMTP_SSL", True),
        webhook_url=os.environ.get("ZB_WEBHOOK_URL", ""),
        host=os.environ.get("ZB_HOST", "0.0.0.0"),
        port=int(os.environ.get("ZB_PORT", "8600")),
    )


settings = load_settings()
