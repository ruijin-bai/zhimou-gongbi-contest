# 展示位 ← 唯一来源（生成前清单）

依据：工程管理标准第2部分 No.1–34、152–167；仓库 2026-10-09 合规审查；2026-10-10 改版原则；当前 app 源码。完整源码索引见 data_codex/code_inventory.json，表结构直接由当前建表函数生成，不复制其他数据库。

|展示位|表 / 字段 / 聚合|来源约束|
|---|---|---|
|项目设置、页头|projects.name/code/config_json.platform|固定仿真名称、SIM-ROAD；平台标准编码只存映射，不冒充真实平台接入|
|岗位与单位|users.role/company/wbs_scope|虚构人员，权限沿用现有七种角色|
|工程主管日报、日报文书|logs(period=day).content_json|当日事实；逐日连续|
|周确认|logs(period=week)、review_fields|人确认字段与原稿分离|
|工程量、内部产值|log_progress → boq_map(confirmed) → boq_items|只在周确认写正式工程量，日报不重复写 log_progress|
|周/月/累计产值、经理产值卡|output.compute、output_records、output_plan|实际周记录由同一正式工程量汇总；禁止另造实际数|
|年度完成率|output_plan.year_plan + 同年度核定产值|年度目标独立来源，不能套项目完成百分比|
|项目完成%|开累产值 / output_plan.boq_total|清单总产值由 BOQ 汇总，美元同币种|
|P6历史、计划、甘特|p6_imports → activities/relations/p6_calendars/p6_act_ext|基准 + 多期更新，使用真实导入解析函数|
|预测与赶工|forecasts.result_json、knowledge(kind=progress_analysis)|预测不当审批实绩|
|计划体检|plan_insight.health + knowledge(kind=plan_health)|检查数量来自当前计划，文字预先撰写|
|预警、判断|alerts、alert_decisions、alert_tags|红橙黄；候选判断与人工决定分开|
|安全候选 / 台账|findings.confirmed_at/status/body_json|未确认不入台账，不派发、不催办|
|整改前后照片|findings.ref_id/rectify_media_id → media|提交人与复核人不同，闭环有人工复核|
|质量问题、工序检验|findings(agent=quality)、conceal_records|现有模块无独立检验批表，按实际可展示字段记录|
|隐蔽验收|conceal_records.fields_json/photos_json/history_json|草稿、待报验、通过、退回状态完整|
|材料检验|material_batches.retest_status/report_no|确认结果、报告号与单据关联|
|混凝土小票|tickets.confirmed_json → pours/specimens|未确认小票不计入；设计量与实收量区分|
|照片|media.file/thumb/purpose/taken_at|本目录本地程序生成 4:3 素材；不冒充实拍证据|
|随手记、语音转写|notes.source/text/media_json、voice_pending|文字由本任务撰写，无 ASR 调用|
|现场圈动态|notes / media / logs / circle_notices 派生 feed|动态键 note:N、snap:N、log:N；不是另造帖子总表|
|评论、点赞|feed_comments/feed_likes|引用有效动态键|
|讨论组|talk_groups/talk_posts/talk_comments/talk_likes|工程主题讨论，状态与闭环注记|
|私信|dm_threads/dm_messages/dm_reads|仅会话双方可见|
|公告 / 通知|circle_notices/circle_notice_reads、notifications|链接使用现有路由与对象编号|
|催办|nudge_log → notifications|日志催办止于工程主管；经理只收到进度、质量、安全|
|经理 / 工程 / 安全首页结论|briefs.content_json.refs / knowledge|预写内容，记录 authored 来源，不伪造外部模型调用|
|周月报|reports.facts_json/narrative_json|facts 用 reports.build_facts 实算再冻结|
|英文周月报、函件、例会、分析|gongbi_docs.facts_json/narrative_json|当前注册类型栏目；源事实共享|
|签收与回复|sub_letters → gongbi_docs|只仿真内部工程往来，不造合同与索赔认证|
|例会行动项|meetings/meeting_items → findings|已决定 / 待决定、完成记录分离|
|分包履约|sub_profile 聚合正式日志、隐患、预警、函件|分包公司名称与人员、WBS一致|
|公司领导总览|projects + 各项目 output/P6/findings|两个独立虚构小项目，不混合主项目指标|
|管理员|users/ai_settings/audit|无密钥，无网络模型调用|
|工人扫码|quick_tokens/quick_submissions|免登录，不显示项目名|
|合同页|contract_docs/contract_clauses/claim_notices/claim_packs|全部留空|

只用当前业务模型支持的展示位。五类函件若注册表不足，以登记类型和附加双语工程函件表述补齐并明确限制，不增加业务类型或界面代码。
