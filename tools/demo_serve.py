"""演示实例入口（T-07，D8/D16）：演示副本 + 进程级演示时钟。

用法（在仓库根目录；先用 python -m tools.demo_copy 制作副本）：
  python -m tools.demo_serve                  # 离线模拟模型（mock），http://127.0.0.1:8650，演示日 2026-09-22
  python -m tools.demo_serve --real-ai        # 临时切到真实交融模型：须用户当次授权
  可选：--data-dir data_demo  --port 8650  --host 0.0.0.0（手机同网访问）  --https  --day 2026-09-22  --no-dev-login

启动顺序（G-07①）：先校验库（必须是 demo_copy 做出的演示副本，只含仿真项目；不能是业务数据目录 data/，
不能占用业务端口 8600/8601；任一不满足退出码 2）→ 强制 ZB_SCHEDULER=0 → time_machine.travel(演示日 + 当前钟点, tick=True)
→ 在同一进程里启动 uvicorn（不开 reload）。服务器上所有“今天/现在”、响应头 Date 都按演示日走。

AI（D16）：
- 默认离线模拟模型：ZB_AI_PROVIDER=mock、ZB_ENV_FILE=NUL，并清掉环境变量里的 Key，副本里不会出现 Key 文件；
- --real-ai：从仓库 .env 导入 Key 到副本自己的 Key 池（不复制业务数据目录的 ai_keys.json），启动时打印醒目提示；
  进程退出时删除副本里的 Key 池文件 ai_keys.json 和 Key 登记。启动时也会先清掉上次异常退出残留的 Key。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # 允许 python tools/demo_serve.py 直接运行
    sys.path.insert(0, str(ROOT))

from tools import demo_copy as dc  # noqa: E402

DEFAULT_PORT = 8650
RESERVED_PORTS = (8600, 8601)           # 业务实例
KEY_ENV = ("ZB_AI_API_KEY", "ZB_AI_API_KEYS", "ZHIMOU_AI_API_KEY")
KEY_FILES = ("ai_keys.json", "ai_keys.tmp")


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m tools.demo_serve", description="启动演示实例（演示副本 + 演示日时钟）")
    ap.add_argument("--data-dir", default=str(ROOT / "data_demo"), help="演示副本目录（python -m tools.demo_copy 制作），默认 data_demo/")
    ap.add_argument("--day", default=dc.DEFAULT_DAY, help=f"演示日，默认 {dc.DEFAULT_DAY}，须与副本一致")
    ap.add_argument("--host", default="127.0.0.1", help="默认只本机访问；手机同网访问用 0.0.0.0")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--https", action="store_true", help="自签名证书启用 HTTPS（手机浏览器录音需要；证书生成在副本目录 tls/）")
    ap.add_argument("--real-ai", action="store_true", help="临时切到真实交融模型（须用户当次授权；Key 从仓库 .env 导入，退出时删除）")
    ap.add_argument("--no-dev-login", action="store_true", help="关闭本机一键登录（默认开启，便于切换岗位录制）")
    return ap.parse_args(argv)


def preflight(args: argparse.Namespace) -> tuple[Path, date]:
    data_dir = Path(args.data_dir).resolve()
    if data_dir == (ROOT / "data").resolve():
        raise dc.CheckError("不能直接用业务数据目录 data/ 启动演示实例；先运行 python -m tools.demo_copy 制作副本")
    if args.port in RESERVED_PORTS:
        raise dc.CheckError(f"端口 {args.port} 留给业务实例，演示实例请换一个端口")
    try:
        day = date.fromisoformat(args.day)
    except ValueError as exc:
        raise dc.CheckError(f"演示日格式不对：{args.day}") from exc
    dc.startup_check(data_dir, day.isoformat())
    return data_dir, day


def drop_key_files(data_dir: Path) -> None:
    for n in KEY_FILES:
        (Path(data_dir) / n).unlink(missing_ok=True)


def reset_key_pool(db_path: Path) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        dc.reset_key_pool(conn)
    finally:
        conn.close()


def configure_env(data_dir: Path, day: date, *, real_ai: bool, env_file: Path, dev_login: bool = True) -> None:
    """必须在导入 app 之前调用：app.config 在导入时读取环境变量。"""
    os.environ["ZB_DATA_DIR"] = str(data_dir)
    os.environ["ZB_SCHEDULER"] = "0"            # AC-7.2：演示实例不起调度，不按真实日期产生“超期/没报”
    os.environ["ZB_DEMO_DAY"] = day.isoformat()
    if dev_login:
        os.environ["ZB_DEV_LOGIN"] = "1"
    if real_ai:
        os.environ["ZB_AI_PROVIDER"] = "jiaorong"
        os.environ["ZB_ENV_FILE"] = str(env_file)
    else:
        os.environ["ZB_AI_PROVIDER"] = "mock"
        os.environ["ZB_ENV_FILE"] = os.devnull
        for k in KEY_ENV:
            os.environ.pop(k, None)


def real_ai_banner(data_dir: Path) -> str:
    bar = "!" * 72
    return "\n".join([bar,
                      "!!  真实 AI 模式：演示实例将调用交融大模型（消耗额度，演示数据会发往交融）。",
                      "!!  只在用户当次授权录制“AI 现场产出”时使用；录完请退出，改回默认的离线模拟模型。",
                      "!!  Key 从仓库 .env 导入到演示副本自己的 Key 池，不复制业务数据目录的 ai_keys.json；",
                      f"!!  退出时删除 {Path(data_dir) / 'ai_keys.json'}。",
                      bar])


def ssl_options(data_dir: Path, *, https: bool, port: int = DEFAULT_PORT) -> dict:
    """--https：证书放在副本自己的 tls/ 下；_self_signed（T-10b）返回字典，叶子证书路径在 key / cert。"""
    if not https:
        return {}
    from app.__main__ import _print_https, _self_signed
    tls = _self_signed(Path(data_dir) / "tls")
    _print_https(tls, port)
    return {"ssl_keyfile": str(tls["key"]), "ssl_certfile": str(tls["cert"])}


def start_clock(day: date):
    import time_machine
    real = datetime.now().astimezone()
    dest = datetime.combine(day, real.time().replace(microsecond=0), tzinfo=real.tzinfo)
    traveller = time_machine.travel(dest, tick=True)
    traveller.start()
    return traveller


def main(argv: list[str] | None = None) -> int:
    args = parse(argv)
    env_file = ROOT / ".env"
    try:
        data_dir, day = preflight(args)
        if args.real_ai and not env_file.is_file():
            raise dc.CheckError(f"真实 AI 模式要从仓库 .env 导入交融 Key，但没有找到 {env_file}")
    except dc.CheckError as exc:
        print(f"演示实例未启动：{exc}", file=sys.stderr)
        return 2
    try:
        import time_machine  # noqa: F401
    except ImportError:
        print("演示实例未启动：缺少演示依赖 time-machine，请先运行 python -m pip install -r requirements-demo.txt", file=sys.stderr)
        return 2

    db_path = data_dir / dc.DB_NAME
    drop_key_files(data_dir)        # 上次异常退出残留的 Key 一律先清掉
    reset_key_pool(db_path)
    configure_env(data_dir, day, real_ai=args.real_ai, env_file=env_file, dev_login=not args.no_dev_login)
    if args.real_ai:
        print(real_ai_banner(data_dir), flush=True)
    traveller = start_clock(day)
    try:
        import uvicorn

        from app.ai.client import provider_status
        from app.db import init_db
        from app.main import app
        init_db()                   # 真实 AI 模式在这里把 .env 里的 Key 导入副本的 Key 池
        if date.today() != day:
            print(f"演示实例未启动：演示时钟没有生效（今天是 {date.today()}）", file=sys.stderr)
            return 2
        st = provider_status()
        ai = "离线模拟模型（mock）" if st["provider"] == "mock" else f"真实交融模型（{'已就绪' if st['ready'] else '未就绪：' + st.get('reason', '')}）"
        scheme = "https" if args.https else "http"
        print(f"演示实例：{scheme}://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}", flush=True)
        print(f"演示日 {day.isoformat()}：服务器时钟定在 {datetime.now().strftime('%Y-%m-%d %H:%M')}，随真实时间走；调度已关闭；AI：{ai}")
        print(f"数据：{data_dir}。{dc.NOTE}", flush=True)
        ssl = ssl_options(data_dir, https=args.https, port=args.port)
        uvicorn.run(app, host=args.host, port=args.port, log_level="info", **ssl)
    finally:
        traveller.stop()
        if args.real_ai:
            drop_key_files(data_dir)
            reset_key_pool(db_path)
            print(f"已删除演示副本里的 Key 池文件与 Key 登记（{data_dir}）", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
