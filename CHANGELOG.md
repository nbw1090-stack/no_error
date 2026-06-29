# Changelog

本项目所有版本变更记录于此。版本号与 [VERSION](VERSION) 文件**严格绑定**：每次提交
都必须同步更新 `VERSION`（末位字段 +1）与本文件，并在对应版本下用一句话写明本次改动。
版本格式 `主.次.补`，当前约定每次提交 **补丁位 +1**（如 0.0.0 → 0.0.1）。

每个版本的效果存档放在 [result/](result/)（如 `result/v0.0.0.md`）。

---

## 0.0.2 — 2026-06-29

新增 **跨组件流程评测 groundtruth 数据集** + 评测器对多组件/多问法的支持：把 openUBMC
组件间「谁生产、谁订阅/读取/调用」的真实链路沉淀为标准答案，用于评估日志分析 agent 是否
真做了「跨组件根因溯源」而非仅复述日志。

- 新增 `backend/eval/datasets/cross_component_cases.json`：**11 条**跨组件流程用例、
  **45 个问法变体**。每条 = 一个跨组件依赖边（如 `pcie_device ⟵ bios` 的 PCIe BDF），
  含多问法（短句 / 引导长句 / 模糊故障 / **日志驱动**——引用代码里真实打印的日志串）、
  `expected_components`（多组件）、`expected_citations`（6 个跨双组件 file:line 锚点）、
  `root_cause_keywords`、`reference_answer`、`log_evidence`（真实日志模板→源码位置）。
  全部链路由 subagent **实地读两端源码核对**，其中 2 条纠正了初始假设方向
  （`storage⟵pcie_device` PcieAddrInfo 方向反置、`fault_diagnosis` 调用方实为 pcie_device）。
- `eval/evaluators.py`：`component_named` 改为支持多组件、按命中比例打分（单组件退化为
  1.0/0.0，向后兼容）；`coerce_expected` 归一 `expected_component(s)` 为去重保序列表。
- `eval/run_eval.py`：离线 runner 新增 `_expand_questions`，把 `input.questions`（多问法）
  展开成共享同一 `expected_output` 的独立子项（打 `case_id/variant_index` 标签），用于测
  意图识别鲁棒性；向后兼容旧的单 `input.question`。
- 详见 [result/v0.0.2.md](result/v0.0.2.md)。

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
