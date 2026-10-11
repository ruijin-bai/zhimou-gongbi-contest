"""仿真演示库（showcase）：在独立副本里把仿真项目补成“每页都有数据”的演示库（演示日 2026-10-04）。

只写演示副本目录（默认 data_showcase/，已 gitignore），不碰业务库 data/zhimou.db，不含真实项目的任何行；不调用任何外部模型
（需要 AI 文字处由 author/texts 仿真撰写，ai_runs 记 provider=sim）。在回放（tools/sim_replay）跑完之后，按顺序：
  1. python -m tools.showcase.finalize --data-dir data_showcase      改名“仿真公路项目 / SIM-ROAD”、照片重印水印
  2. python -m tools.showcase.fix --data-dir data_showcase           一数一源纠正（结果追加到 fix_report.json）
  3. python -m tools.showcase.cutoff --data-dir data_showcase        时间线截到演示日 10-04
  4. python -m tools.showcase.fix --data-dir data_showcase           截止后再核一遍
  5. python -m tools.showcase.post --data-dir data_showcase          复原按当时重跑的预警、清理离线前缀与 Key
  6. python -m tools.showcase.fill --data-dir data_showcase          补齐各展示位（按业务日期时间、按当时 P6 期次）
  7. python -m tools.showcase.minis --data-dir data_showcase [--agents]   领导页两个虚构小项目
  8. python -m tools.showcase.post --data-dir data_showcase
演示：python -m tools.showcase.warm_serve --data-dir data_showcase --port 8662 --day 2026-10-04（launch.json 的 showcase-demo）
"""
