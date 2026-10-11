"""演示实例（仿真演示库专用）：与 tools.demo_serve 相同（离线、演示日时钟、不出网），另外两点：

1. 先装上仿真撰写（author）：逐页浏览或点“生成/刷新”时需要的简报、点评、画像等文字由仿真撰写或规则文字给出，不调用任何模型；
2. 界面上的运行模式标注：RJ 要求演示里不出现“离线模拟”字样。本实例不改业务代码，只在接口出口处把运行模式改报为
   provider=sim（“仿真演示（未接入模型）”，ready=false，页脚显示模型未就绪），mock 标记一律改为 false，文字里的“离线模拟”改为“仿真”。
   只作用于本演示实例的进程；业务实例（8600/8601/8602）不受影响。
用法：python -m tools.showcase.warm_serve --data-dir data_showcase --port 8662 --day 2026-10-04
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LABEL = "仿真演示（未接入模型）"


def neutral(o):
    if isinstance(o, dict):
        out = {k: neutral(v) for k, v in o.items()}
        if out.get("provider") == "mock":
            out["provider"] = "sim"
            if "ready" in out:
                out["ready"] = False
            if "label" in out:
                out["label"] = LABEL
            for k in ("model", "vision_model", "text_model"):
                if out.get(k) == "mock":
                    out[k] = "sim"
        if out.get("mock") is True:
            out["mock"] = False
        if out.get("source") == "mock":
            out["source"] = "rule"
        return out
    if isinstance(o, list):
        return [neutral(x) for x in o]
    if isinstance(o, str) and "离线模拟" in o:
        return o.replace("离线模拟模型（仅开发测试，不可用于正式成果）", LABEL).replace("离线模拟模型", "仿真演示").replace("离线模拟", "仿真")
    return o


def install_filter(app):
    from starlette.responses import Response

    @app.middleware("http")
    async def _neutral(request, call_next):
        resp = await call_next(request)
        if not request.url.path.startswith("/api/") or "application/json" not in resp.headers.get("content-type", ""):
            return resp
        body = b"".join([chunk async for chunk in resp.body_iterator])
        try:
            data = neutral(json.loads(body))
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        except ValueError:
            pass
        headers = {k: v for k, v in resp.headers.items() if k.lower() not in ("content-length", "content-type")}
        return Response(body, status_code=resp.status_code, headers=headers, media_type="application/json")


def main():
    argv = sys.argv[1:]
    dd = argv[argv.index("--data-dir") + 1] if "--data-dir" in argv else str(ROOT / "data_showcase")
    os.environ.update(ZB_DATA_DIR=str(Path(dd).resolve()), ZB_SCHEDULER="0", ZB_DEV_LOGIN="1", ZB_AI_PROVIDER="mock", ZB_ENV_FILE=os.devnull)
    sys.path.insert(0, str(ROOT))
    from tools import demo_serve
    from tools.showcase import author
    author.install()
    from app.main import app
    install_filter(app)
    sys.exit(demo_serve.main(argv))


if __name__ == "__main__":
    main()
