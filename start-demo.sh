#!/usr/bin/env bash
# 演示实例（D8/D16）：业务库只读复制成演示副本 data_demo/，服务器时钟定在演示日 2026-09-22，调度关闭；业务库不动。
# 副本含原始照片（media/），只在本机使用、不外传，录完即可删除整个 data_demo/。
# 用法：./start-demo.sh            用已有副本启动（还没有副本时先制作），离线模拟模型（mock）
#       ./start-demo.sh copy       重新制作副本（覆盖旧副本）后启动
#       ./start-demo.sh real-ai    临时切到真实交融模型：须用户当次授权；Key 从 .env 导入，退出后删除副本里的 Key 文件
# 其他参数（端口、手机访问等）直接用：.venv/bin/python -m tools.demo_serve --help
set -e
cd "$(dirname "$0")"
PY=.venv/bin/python
[ -x "$PY" ] || { echo "请先运行 start.sh 创建虚拟环境"; exit 2; }
"$PY" -c "import time_machine" 2>/dev/null || { echo "缺少演示依赖 time-machine，请先运行：$PY -m pip install -r requirements-demo.txt"; exit 2; }
AI=""
[ "${1:-}" = "real-ai" ] && AI="--real-ai"
if [ "${1:-}" = "copy" ] || [ ! -f data_demo/DEMO_COPY.json ]; then
  "$PY" -m tools.demo_copy --force
fi
if [ -n "$AI" ]; then
  echo
  echo "************************************************************************"
  echo "**  真实 AI 模式：将调用交融大模型，消耗额度，演示数据会发往交融。"
  echo "**  只在用户当次授权录制“AI 现场产出”时使用；录完按 Ctrl+C 退出。"
  echo "************************************************************************"
  echo
fi
# 无论怎样退出，都再删一次副本里的 Key 池文件（真实 AI 模式会生成）
trap 'rm -f data_demo/ai_keys.json data_demo/ai_keys.tmp' EXIT
"$PY" -m tools.demo_serve $AI
