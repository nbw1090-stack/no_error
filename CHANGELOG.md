# Changelog

本项目所有版本变更记录于此。版本号与 [VERSION](VERSION) 文件**严格绑定**：每次提交
都必须同步更新 `VERSION`（末位字段 +1）与本文件，并在对应版本下用一句话写明本次改动。
版本格式 `主.次.补`，当前约定每次提交 **补丁位 +1**（如 0.0.0 → 0.0.1）。

每个版本的效果存档放在 [result/](result/)（如 `result/v0.0.0.md`）。

---

## 0.0.4 — 2026-07-12

**claw-code 风格上下文压缩替换丢弃式省略**：修复 v0.0.3 定位的前缀缓存命中率问题
（direct 主 Agent 仅 20%——旧省略机制每轮改写历史前缀）。@40 压力实测：命中率
50%→**76%**、总 token -34%，触发节奏约 5-6 轮一次；可解 case 质量满分（elide 下
全 0 + DSML 泄漏）。详见 `result/v0.0.4.md`。

- 新增 `agent/compaction.py`：**确定性模板摘要**（七段式：Scope/工具/最近请求/
  中英关键词待办/关键文件（含 `file:line`）/当前工作/时间线；纯函数不调 LLM，
  零延迟零成本零幻觉）、边界安全分割（tool 消息绝不与其 assistant(tool_calls)
  拆散）、二次压缩合并（Scope 累加、并集去重、超 4000 字符按 pending > current >
  files > requests > timeline 优先级行级裁剪）。参照 claw-code 的
  检测→摘要→压缩→恢复流水线。
- `context.py` 新增 compact 模式（默认）：超 24k 预算才触发压缩，分割点与摘要
  持久化在 `Session.compaction` 并在两次触发之间**冻结**——发给 LLM 的前缀逐
  字节稳定（有专门测试断言）；`session.messages` 保持 append-only，前端历史
  不受影响。旧省略机制保留为 `AGENT_CONTEXT_COMPACTION=elide` 回退路径。
- **churn 修复**（首版实测暴露）：巨型工具结果卡在固定保留窗内导致轮轮重压
  （命中率 13%）。重写分割点选择：**低水位跳变**（压到预算×60% 即停）+
  **显著性护栏**（砍不掉 30% 尾部就冻结）+ 保留窗改「最少 4 条」语义
  （`AGENT_COMPACTION_PRESERVE_RECENT=4`）。
- 测试：新增 `tests/test_compaction.py` 31 条（含前缀逐字节冻结、边界回退、
  merge 裁剪优先级、Session 兼容、Agent 集成），全套 377 通过。
- CLAUDE.md 同步 context 管理机制与新环境变量说明。

## 0.0.3 — 2026-07-11

**多 Agent 架构落地**：主 Agent + 检索子 Agent（代码 + wiki 统一取证入口），与单
Agent 架构同题对比评测——配对 12 条上质量指标全面提升（diagnosis_acc 0.58→0.92、
rootcause_correctness 0.35→0.68、DSML 泄漏病态回复 5→0），主 Agent 上下文瘦身 8 倍
（382k→48k 名义 token/case），代价是取证侧总 token ≈2.9×。详见 `result/v0.0.3.md`。

- 新增 `agent/subagent.py`：**无状态**检索子 Agent（≤6 轮小 ReAct 循环，复用
  ToolRegistry 的 source/wiki 工具组与 LoopGuard 熔断），查完即散，只带回
  ≤2KB 摘要 + **真实到访**的出处（从工具结果提取 file:line / wiki slug，而非
  LLM 自报），附轮次/空手而归健康统计。标准查法（代码先调用链骨架后函数体、
  wiki 先目录后整页）写在提示词（`agent/prompts/retrieval.py`）而非代码——
  评测不理想可原地换回两步管线实现，主 Agent 无感（取证契约不变）。
- 新增 `retrieve_evidence` 工具（group="retrieval"，`agent/tools/retrieval_tool.py`），
  启动时 `configure_retrieval(llm)` 装配；架构开关 `AGENT_SOURCE_MODE=direct|subagent`
  （config/core/main 全链路，`SUBAGENT_MAX_ITERATIONS`/`SUBAGENT_SUMMARY_MAX_CHARS`
  可调）。subagent 模式下主 Agent 只见 log 工具 + 取证工具（定位问题仍是主 Agent
  自己查日志），system prompt 换用取证指导段、不走 Langfuse 托管 prompt。
- 评测框架适配：`--arch`/`--user-id` CLI 开关；轨迹拉平子 Agent 内层工具调用
  （source_tool_used / expected_tools_invoked 两架构可比）；新增 total_tokens
  （主+子）、subagent_rounds、subagent_empty_handed 指标。
- **修复历史评测失真 bug**：`run_eval` 构造 `LogDataset` 未注入 `user_id`，导致
  v0.0.0/v0.0.1 评测中源码工具调用 100% 报「需登录」（xc-full：88/88 全错）——
  旧基线根因类低分主要源于此，与本版数字不可直接纵向比较。
- 测试：新增 15 条子 Agent/架构开关/评测适配用例，全套 346 通过。

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
