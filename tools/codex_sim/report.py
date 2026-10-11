"""Build the explicitly requested delivery report from our acceptance evidence."""
import json,re,subprocess
from pathlib import Path
from runtime import ROOT,DATA
m=json.loads((DATA/'manifest.json').read_text(encoding='utf-8'))
d=json.loads((DATA/'domain_checks.json').read_text(encoding='utf-8'))
x=json.loads((DATA/'crosscheck.json').read_text(encoding='utf-8'))
ui=json.loads((DATA/'ui_check_summary.json').read_text(encoding='utf-8'))
shots=ROOT.parent/'仿真数据赛马'/'codex截图'
test=(DATA/'pytest_full.log').read_text(encoding='utf-8')
last=[l for l in test.splitlines() if 'passed' in l and ('seconds' in l or ' in ' in l)]
test_result=last[-1].strip('= ') if last else '尚未结束，不能声明通过'
c=m['counts']
head=subprocess.check_output(['git','rev-parse','--short','HEAD'],cwd=ROOT,text=True).strip()
f1=(DATA/'fingerprint_1.txt').read_text(encoding='utf-8-sig').strip()
f2=(DATA/'fingerprint_2.txt').read_text(encoding='utf-8-sig').strip()
out=['# Codex 仿真数据报告 · 2026-10-11',
'> 交付状态：独立数据与演示实例已建立；数据核对结果见下。原界面版式存在未通过项，严格要求尚未全部达标。',
'## 项目、目录与隔离',
f'- 主线 `ui-integration`，本地代码起点 `d434c08`，本报告落盘前主线 `{head}`。工作期间主线有其他本地提交，已核对 app、tests 及三个原巡检工具相对验收起点无差异；沿用最新主线，既有成果保持。未读取远程分支、未推送。',
'- 项目：**仿真公路项目 / SIM-ROAD**；10 km 双向四车道、中桥 1 座（3×25 m）、小桥 1 座（1×16 m）、箱涵 5 道、路基、路面及交安。',
f'- 开工 {m["start"]}，计划完工 {m["end"]}，约 14 个月；演示日 {m["demo"]}，实绩、P6、已发布周月报截止 {m["cutoff"]}。截至日编制的下周计划允许日期在截止日之后，实际栏为空。',
'- 虚构业主晨溪交通发展局、监理雁桥工程咨询、总包澄岳国际工程、分包远辰路基公司 / 星湾桥梁公司 / 青岑路面公司、供应商晨洲建材。主项目 21 人，另两个项目各 1 人；人名均虚构。',
'- 辅助项目：仿真河谷连接线 / SIM-VALLEY；仿真山岭养护工程 / SIM-RIDGE。各有独立 BOQ、量值、P6 和隐患；领导总览遵循现有系统逐项目切换，不合并不同项目金额。',
'- `data_codex/.gitignore` 忽略数据、媒体、浏览器配置、临时文件和验收证据；数据未入 Git。脚本全部在 `tools/codex_sim/`。截图在同级 `仿真数据赛马/codex截图/`。',
'- 没有读取密钥、生产资料或其他演示库；只运行业务端口 8663。仅本地绘制素材，没有图片下载或外部 AI 调用。AI 密钥表为空，AI 暂停、定时任务关闭。界面模型名称由既有业务代码显示，不能当成真实模型运行证据。',
'## 启动与一键重建',
'推荐固定演示日并隔离配置的入口：',
'```powershell\n# 仓库根目录，先停止自己的 8663；有监听时生成器会拒绝重建\npowershell -NoProfile -ExecutionPolicy Bypass -File tools/codex_sim/rebuild.ps1\npython -X utf8 tools/codex_sim/serve.py\n# http://127.0.0.1:8663\n```',
'系统原启动命令也支持该数据目录，但仅设 `ZB_DATA_DIR` 会加载默认配置文件，不能保证模型与调度隔离。安全启动须同时显式指定以下隔离参数；实时钟不会被冻结，因此正式演示优先用上面的入口：',
'```powershell\n$env:ZB_DATA_DIR = (Resolve-Path data_codex).Path\n$env:ZB_ENV_FILE = Join-Path $env:ZB_DATA_DIR isolated.env\n$env:ZB_AI_PROVIDER = "disabled"\n$env:ZB_SCHEDULER = "0"\n$env:ZB_SECRET = "codex-simulation-session-only"\n$env:ZB_DEMO_DAY = "2026-10-11"\npython -X utf8 -m app serve --port 8663\n```',
'生成器固定种子 `20261011`；只允许操作自己的确定目录，重建前把已有独立库备份到 `data_codex/backups/`。依赖使用当前环境的项目依赖、Pillow 和 time-machine。',
'## 演示账号',
'以下仅为本任务虚构账号，统一密码 **`SimRoad2026!`**。',
'| 岗位 | 用户名 | 虚构姓名 |\n|---|---|---|',
*['| '+a['role']+' | `'+a['username']+'` | '+a['name']+' |' for a in m['accounts']],
'工人扫码免登录入口：`http://127.0.0.1:8663/q/SIMROAD2026WORKERQR001234567890`。已核对免登录且不显示项目名；工人只通过扫码上报，系统没有独立 worker 账号角色。',
'## 标准、展示位与源头',
'先读取公司一数一源标准的工程管理数据部分（411 字段），然后读仓库合规审查与改版原则；在生成前完成 app 源码和空库岗位走查。预先形成的清单见 `tools/codex_sim/display_map.md`；代码结构与表字段清单保存在自己的 `code_inventory.json`、`schema.json`。',
'| 标准字段 / 定义 | 本仿真源头 | 界面与边界 |\n|---|---|---|',
'| 工程主数据 1–34 | projects.config_json.platform | 虚构主数据底稿；在建 02、道路桥梁 09、总承包 03；不声称已接公司平台 |',
'| 152 年度营业额目标、153 年度营业收入目标 | 不仿真 | 不用产值充当财务营业额 / 收入 |',
'| 154 年、155 月、156 计划产值（万美元） | output_plan + output_records，month_source.json 汇总视图 | 系统内部保留 USD；展现转万美元，不额外建第二份实际台账 |',
'| 157 计划营业额及营业额预测 | 不仿真 | 不用施工产值代替营业额 |',
'| 工程量与内部产值 | 日事实底稿 → 人工核定周 log_progress → confirmed boq_map → BOQ 单价 | 日志正文保留日事实；日 log_progress 为空，防止日周双计；周产值记录为同源计算镜像 |',
'| 整体完成百分比 | 开累产值 ÷ 清单总产值 | 按产值口径，币种 USD；总体计划完成百分比及进度差不推导，显示 — |',
'| 年度完成率 | 2026 年内部产值 ÷ 本年度计划产值 | 与整体完成百分比使用不同分母，不替代财务目标 |',
'| 158–160 预测 | 当前 P6 + 正式量值 + 确定性预测 | 参考推演，非真实审批、承诺或计量认证；不填财务营业额预测 |',
'| 164–167 图片类别 / 文件 / 拍摄时间 / 描述 | media + 本地素材清单 | 4:3，全为程序生成的仿真场景 / 单据插图，不是实拍或正式检测证明 |',
'| 隐患与质量数量 | findings，confirmed_at 区分候选 | 人确认后入台账；候选无派发人、期限和闭环；整改提交者与闭环复核者不同 |',
'| 周月报及英文 | reports.build_facts 冻结事实；英文复用同一事实 | 全部预写文字，不伪造模型成功；周报十项指标按发布时冻结 |',
'## 逐展示位条数',
'以下以生成时主项目为准；运行验收产生的登录、失败调用和空白草稿另列，不混入实绩。',
'| 展示位 | 条数 / 状态 | 源表或字段 |\n|---|---|---|',
'| 日志 / 日报 | 273 个连续日 × 3 岗位 = 819；39 连续周 × 3 = 117 周核定 | logs / log_progress；日报为既有日志入口 |',
'| 随手记 / 语音转写 | 351 / 117 | notes / voice_pending；预写转写文本，不包含原始录音 |',
'| 现场圈帖子 | 351 随手记、84 场景图片、已有日志及公告可派生动态 | 现有 feed 查询；没有独立帖子总表，不能把动态多来源相加当唯一帖子数 |',
'| 评论 / 点赞 / 阅读 | 96 / 144 / 48 | feed_comments / feed_likes / feed_reads |',
'| 讨论组 / 组内帖子 / 评论 / 点赞 | 6 / 30 / 30 / 90 | talk_* |',
'| 私信会话 / 消息 | 8 / 48 | dm_threads / dm_messages；限参与人 |',
'| 公告 / 已读 | 6 / 36 | circle_notices / circle_notice_reads |',
'| 通知 / 催办 | 28 / 28 | notifications / nudge_log |',
'| 媒体 | 177：84 施工场景 + 42 隐患前 + 30 整改后 + 21 单据 | media；全部 1200×900，本地合成 |',
'| 工序 / 隐蔽 / 检验批 | 24：桩基、钢筋、模板、基坑等现有模板及通过、退回、待报验、待补状态 | conceal_records；没有独立工序、检验批台账，不能冒充三个独立模块 |',
'| 材料检验 | 18 批：15 合格、1 不合格隔离、1 已取样、1 待复检 | material_batches |',
'| 混凝土小票 / 浇筑 / 试件 | 48 小票；12 次浇筑各 32 m³；12 组试件 | tickets / pours / specimens |',
'| 进场 / 合格证票据 | 18（16 确认、2 草稿） | tickets；与批次关联 |',
'| 安全隐患 | 42：36 人工确认（25 关闭、5 整改待复核、6 未整改）+6 AI 候选 | findings；重复类别、原问题、整改及复核有链路 |',
'| 质量与质控提醒 | 15：10 关闭、4 未闭环问题、1 未执行提醒 | findings(agent=quality) |',
'| 预警 / 判断记录 | 18 / 13 | alerts / alert_decisions；已确认、暂缓、待确认区分 |',
'| P6 | 基准 1+连贯更新 6；每期 123 作业、135 关系 | 原系统 import_schedule 导入独立 XER，6 天工作制 |',
'| BOQ / 已确认映射 | 120 / 120 | boq_items / boq_map；无未映射量或单位不匹配 |',
'| 周 / 月 / 累计产值 | 39 实绩周+1 次周计划；10 月别视图（10 月仅至 10-04）；1 开累计算 | output_records / 同源量值；年度目标独立 |',
'| 预测 / 赶工 / 计划体检 | 各 1 | forecasts / knowledge；200 次固定种子推演、CPM复算 |',
'| 分包画像 | 3 家 | 既有 sub_profile 聚合；按岗位 WBS 分工 |',
'| 中文周报 / 月报 | 6 / 2（8、9 月） | reports，最新周 09-28～10-04 |',
'| 英文周报 / 月报 | 6 / 2 | gongbi_docs，与中文报告相同事实 |',
'| 中英工程函件 / 签收回复 | 10（5 个业务主题 × 2 个现有函件类型） | gongbi_docs / sub_letters；issued、acked、replied、returned、closed |',
'| 例会 / 行动项 | 6 / 24（16 完成、8 待办） | meetings / meeting_items |',
'| 例会底稿 / 三周滚动 / 月复盘 / 延误简报 | 2 / 1 / 1 / 1 | gongbi_docs；均预写，不走外部 AI |',
'| 公司领导总览 | 3 项目 | projects + 当前项目接口；两个小项目切换查看 |',
'| 扫码 token / 上报 | 1 / 9 | quick_tokens / quick_submissions |',
'| 合同、条款、索赔通知、索赔包 | 全部 0；合同派生事件 0 | 不仿真合同 |',
'## 金额与跨页核对',
f'- 清单总产值 **{m["boq_total"]:,.2f} USD**；开累内部产值 **{m["actual_usd"]:,.2f} USD**；项目整体完成 **{m["progress_pct"]:.2f}%（按产值口径）**。',
'- 雨季 5–7 月有效作业窗口收缩；8 月恢复，9 月工作面连续与运输赶工形成峰值。计划值来自独立仿真目标，不把实际曲线复制为计划承诺。',
'- 源明细按 USD 分统一精度，周产值、月别视图和开累须精确核对。金额 UI 按万美元显示；不能把 1,095.18 万与美元原值直接比末位。',
'| 指标 | 页面读数 | 核对结果 | 页面 / 证据 |\n|---|---|---|---|',
*['| '+r['metric']+' | '+ ' / '.join(str(v) for v in r['values'])+' | '+('一致' if r['consistent'] else '不一致')+' | '+', '.join(r['page_routes'])+'；'+r['detail']+' |' for r in x['rows']],
'**边界：已核对 10 项同源指标；其中部分产值指标在现有产品只有两个完整展示位。不能宣称“每一项都在三个不同页面完整展示”。** 安全五项核对首页接口底稿、主管总览和全状态台账；完整 UI 数字是否出现，依截图及既有页面布局。额外核对媒体 177、中文周报 6、月报 2、主项目 P6 7 期、候选 6，见 `data_codex/crosscheck.json`。',
'## 运行验收与截图',
f'- 自有数据检查 **{len(d["checks"])} 项**，失败 {len(d["failed"])}；岗位登录与页面接口错误 {len([r for r in d["api"] if r["status"]!=200])}；SQLite integrity / foreign_keys 均通过。',
f'- 全量 `python -m pytest tests`：**{test_result}**。测试设置缓存禁用，临时目录在自己的目录；不改业务代码。',
f'- 最终脚本连续两次完整重建，逻辑数据指纹一致：**{f1==f2}**；SHA-256 `{f1}`。金额精确至 USD 分核对，周记录累计与工程量计算均为 {x["extra"]["ledger_usd"]:,.2f} USD。',
'- 原工具按每个岗位先 `role_shots.mjs`，接着 `fit_check.mjs`、`home_links.mjs`；四档视口 1440×900、390×844、360×780、430×932。按角色落盘，没有只检查首页。',
'| 岗位 | 岗位截图 | fit_check | home_links |\n|---|---|---|---|',
*['| '+role+' | '+str(next((r['exit'] for r in ui if r['role']==role and r['check']=='role_shots'),'待跑'))+' | '+str(next((r['exit'] for r in ui if r['role']==role and r['check']=='fit'),'待跑'))+' | '+str(next((r['exit'] for r in ui if r['role']==role and r['check']=='links'),'待跑'))+' |' for role in ['tech','sub_safety','eng_sup','safety_sup','manager','leader','admin']],
'退出码 0 为通过，1 为未通过。详见截图目录 `版式巡检/<role>/fit.md`、`fit.json`；首页链接报告在 `data_codex/checks/<role>/links/`。',
'- 最终重建后另跑七岗位全部首页链接：退出码 0；受影响首页、分包画像等在 1440×900、390×844 复核：桌面超标 3、手机超标 1，仍为分包画像现有布局问题。详见截图目录 `最终复核/fit.md`。38 张交付视口截图回读无页面不存在、残留加载态、JavaScript 异常或视口内坏图。',
f'- 交付视口截图 {len(list((shots/"交付视口").glob("*.png")))} 张：7 个登录岗位首页+工人免登录首页，另有 P6、预测赶工、产值、安全闭环、质量分包、现场圈、材料小票、中文周报、隐蔽验收、例会、双语函件 11 个代表页面，每项桌面与手机。另有原岗位巡检全页面及版式证据。',
'- 截图使用实际 1440×900 和 390×844 视口；大于视口的页面仅截取视口，不能因此认为已达一屏化。未改 CSS、组件或分页逻辑。',
'## 已知问题、未补齐与默认取舍',
'1. **版式不满足严格验收。** 现有材料小票小结、隐蔽验收统计、例会卡片、分包画像、函件及分析会超高；手机产值图例横向溢出。报告与长表有未分页记录。为保留数据饱满，不通过删到空表来掩盖业务 UI 缺陷，且按用户边界不改业务代码。',
'2. **五类函件未作为五个独立系统类型补齐。** 本仿真提供五个专业工程主题、双语内容及签收回复链，但仅使用工作联系单和致分包函两个现有类型；合同通知类留空，其他注册类型尚未补齐。',
'3. **三个独立验收模块及独立日报文书不存在。** 24 份现有隐蔽验收带检验批编号与工序记录；273 日日报通过既有日志入口展示，没有虚构新报表类型或新建业务表。',
'4. **媒体是合成插图，录音未生成。** 177 张图片全部程序绘制，不宣称实拍或真实检测文件；117 段预写转写无音频和 ASR 证据。单据图片及检验影像为展示素材，不能当正式签认链。',
'5. **10 指标逐项三页面完整展示未全满足。** 已做真实接口与源行交叉核对；部分指标只有两个完整页面展示。遵守同一信息只有一个完整展示位，不增加重复卡片，不虚构第三页。',
'6. **无真实模型效果。** 本任务预写文字，AI 关闭；既有页面自动请求模型会生成“暂停”失败记录或规则兜底。首次重建 ai_runs=0；巡检后非零失败数不是模型成功次数或成效。',
'7. **当前周空白草稿。** 技术员首页 GET 自动创建演示周空白确认草稿，日期 10-05～10-11；既有实绩止于 10-04，没有把空草稿算入连续 39 周核定或产值。',
'8. **合同派生提醒采用数据层规避。** 原程序按日志问题分类自动推导索赔事件；本仿真把雨季及运输接口写入“施工组织”，天气字段与施工描述保留，使合同派生事件也为空；没有修改索赔业务逻辑或虚构放弃索赔决定。',
'9. **两个小项目仅为领导切换演示。** 量值和状态独立，体量较小；没有与主项目同等完整的日志与文书。领导页既有接口只返回当前项目，其他行保留切换入口，不支持跨项目实时金额汇总。',
'10. **无真实平台、试点、审批或业主计量认证。** 主数据按公司工程管理字段归属映射，未声称真实接入。全部事实与人工确认都是仿真情节，不编造效益、准确率或真实审批证据。',
'11. **测试临时目录保留。** 自动审批拒绝了清理测试临时目录，工具给出的原因为 blocked by policy；这些目录保留在已忽略的 data_codex 内，不参与服务读取和演示数据汇总。',
'## 本地提交范围',
'仅提交 `tools/codex_sim/`、本报告、`data_codex/.gitignore`。未提交数据库、媒体、截图、账号运行记录或其他人的成果；未 push。最终本地提交哈希以交付回复为准。']
path=ROOT/'docs'/'codex仿真数据报告_2026-10-11.md'
body='';prev=''
for block in out:
    body+=('' if not body else '\n' if block.startswith('|') and prev.splitlines()[-1].startswith('|') else '\n\n')+block
    prev=block
path.write_text(body+'\n',encoding='utf-8')
print(path)
