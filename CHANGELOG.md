# Changelog

本项目所有版本变更记录于此。版本号与 [VERSION](VERSION) 文件**严格绑定**：每次提交
都必须同步更新 `VERSION`（末位字段 +1）与本文件，并在对应版本下用一句话写明本次改动。
版本格式 `主.次.补`，当前约定每次提交 **补丁位 +1**（如 0.0.0 → 0.0.1）。

每个版本的效果存档放在 [result/](result/)（如 `result/v0.0.0.md`）。

---

## 0.0.1 — 2026-06-29

接入 openUBMC **LLM Wiki**（Karpathy 模式）：让 LLM 把官方文档提炼成结构化、互链的
markdown 知识库，agent 在日志分析 / 问答时按「索引页 → 整页」导航，结合 BMC 架构与源码作答。

- 新增 `backend/wiki/` 模块（**全局共享**，不按用户隔离）。不再对原文切片做关键词检索，
  而是 **LLM 逐篇提炼**：`compile` 规划「精选架构核心」源（design_reference / api /
  quick_start / glossary）→ 每篇调一次 LLM 提炼成结构化页（概述/关键设计/接口/相关）→
  互链 + LLM 写架构总览拼出 `index.md`。`store` 把结果落盘为 `index.md` +
  `pages/<slug>.md` + `manifest.json`。
- `service.sync_stream`：blobless+sparse 克隆 `docs/zh` → 有界并发提炼（按完成顺序推 SSE
  进度）→ 写索引/manifest；**增量可续编**（source_hash 未变的页直接复用，不重复调 LLM）。
- 新增 **wiki 工具组**：`get_wiki_index`（读索引）/ `read_wiki_page`（读整页）/
  `search_wiki`（关键词检索兜底）。core 在 wiki 已编译时把 `wiki` 加入 `exposed_groups`
  并注入 `WIKI_GUIDANCE_PROMPT`（引导「先读索引页、再读整页」），在「有日志」「无日志有源码」
  「纯问答」三种模式下均可用（含 Langfuse 托管 prompt 路径）。
- 新增 `/api/wiki/sync`（SSE 编译）/ `/status` / `/index` / `/page` / `/search` 端点
  （全局共享、仍需登录，编译需配置 LLM）；前端加「Wiki 知识库」面板：编译按钮 + 逐页进度
  + 已编译页清单（按主题分组）。
- 测试：新增 21 条 wiki 用例（store / compile 流水线含假 LLM / 工具 / 端点，全离线），
  更新工具分组用例；全套 322 通过。

## 0.0.0 — 2026-06-28

首版基线：BMC 日志分析 ReAct agent + 评测评估框架。

- 评测框架落地：13 条黄金用例（10 源码可定位 + 3 负例）、13 项指标（10 确定性 + 3 LLM 裁判），
  确定性指标可离线跑 CI，裁判缺失自动降级。
- 离线与在线评测**双模式均接入 Langfuse**：在线 `run_experiment` → Datasets→Runs 做版本对比；
  离线 `--offline` 出本地记分卡并回灌 Traces（按 `session=eval-<run_name>` 分组）。
- 工具轨迹采集（`eval/trajectory.py`，从持久化 session 重建，不改 agent 热路径）、
  固定数据集 bootstrap、黄金用例上传脚本。
- 修复：`run_experiment` 在运行中事件循环内驱动 task 导致的 `asyncio.run` 崩溃（新增 `eval/aio.py`
  线程化 runner）；Langfuse 拒收 `value=None` score（`na_to_none` 包装，不适用指标返回 None 跳过）。
- 修复：评测临时会话改用 `user_id=3` 创建，否则源码工具永不暴露、诊断退化为纯日志问答。
- 基线结果存档于 `result/v0.0.0.md`：source_tool_used 1.00，但 rootcause_correctness≈0.34、
  faithfulness≈0.36 为弱项，下一版重点改进。
