# Codex 独立仿真

固定种子 `20261011`，开工 `2026-01-05`，实绩截止 `2026-10-04`，演示日 `2026-10-11`。生成脚本只重建本仓库 `data_codex/`，旧库备份也仅留在该目录；8663 已运行时拒绝重建。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/codex_sim/rebuild.ps1
python -X utf8 tools/codex_sim/serve.py
# 另一个终端，服务运行后
python -X utf8 tools/codex_sim/validate.py
python -X utf8 tools/codex_sim/crosscheck.py
python -X utf8 tools/codex_sim/review.py
node tools/codex_sim/capture.mjs
```

推荐 `serve.py`：启动前隔离配置文件，关闭外部 AI 和定时任务，并固定演示日。现有业务启动器仅指定 `ZB_DATA_DIR` 时仍加载仓库默认配置文件；安全等价命令见交付报告。原巡检工具会访问业务首页，系统可能自动创建空白当前周草稿或失败的 AI 调用记录；这些不是生成的实绩，也没有网络模型调用。

`display_map.md` 是生成前的展示位与字段清单；`source_inventory.py` 只读 app 源码、在自己的目录建空库检查结构。`bootstrap.py` 用于首次空库页面走查；正式数据以 `generate.py` 为准。素材由 `images.py` 在本地绘制，不能作为现场实拍证据。

数据与全部验收中间文件不提交。脚本、说明及报告可本地提交。账号、未补齐项、真实巡检结果以 `docs/codex仿真数据报告_2026-10-11.md` 为准。
