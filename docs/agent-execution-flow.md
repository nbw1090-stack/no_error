# Agent 执行流程详解

本文描述 BMC 日志分析系统中 **ReAct Agent** 从「用户发送一条消息」到「收到最终回复」的完整执行流程，覆盖 HTTP 入口、Agent 循环、LLM 适配器、工具层、会话持久化与 Langfuse 可观测性六个层次。配合源码阅读：[backend/main.py](../backend/main.py)、[backend/agent/core.py](../backend/agent/core.py)。

---

## 1. 概述

Agent 采用 **ReAct（Reasoning + Acting）** 模式：

1. LLM 分析用户问题，决定调用哪些工具；
2. 执行工具，把结果反馈给 LLM；
3. LLM 基于工具结果继续推理，直到给出最终文本回复。

整个循环封装在 `Agent.run()`（同步）与 `Agent.run_stream()`（SSE 流式）中，由 `backend/agent/core.py` 实现。两类工具支撑分析能力：

- **日志工具**（[tools/log_tools.py](../backend/agent/tools/log_tools.py)）：对已上传数据集做搜索 / 过滤 / 统计 / 时间线 / 上下文检索；
- **源码工具**（[tools/source_tools.py](../backend/agent/tools/source_tools.py)）：当组件已构建 AST 索引时，按 `file:line` 反查函数体、兄弟符号、跨文件调用点，做源码级根因诊断。

工具按 **group** 暴露（`log` / `source`），由此派生三种运行模式：

| 模式 | 触发条件 | 暴露的工具组 | 系统提示词 |
|------|----------|--------------|------------|
| **数据分析** | 会话绑定 `dataset_id` | `log`（+ `source`，若该用户已索引组件） | `ROLE` + `DATA_CONTEXT` + `TOOL_GUIDANCE`（+ `SOURCE_GUIDANCE`） |
| **纯源码问答** | 无数据集，但用户已构建 AST 索引 | `source` | `QA_SOURCE_ROLE` + `SOURCE_GUIDANCE` |
| **通用问答** | 无数据集、无任何索引 | 无（`tool_schemas=[]`） | `QA_ROLE` + `QA_GUIDANCE` |

> 关键：**「无数据集」不等于「无工具」**——只要用户为某组件建过源码索引，即使没上传日志，Agent 仍会拿到源码工具做代码级问答。

---

## 2. 涉及模块全景

| 模块 | 职责 |
|------|------|
| [main.py](../backend/main.py) | HTTP 入口 `/api/chat`、`/api/chat/stream`；鉴权、加载数据集、构造 `LogDataset`、调用 Agent、降级兜底 |
| [agent/core.py](../backend/agent/core.py) | ReAct 循环（`run` / `run_stream`），编排会话 / 提示词 / LLM / 工具 |
| [agent/session.py](../backend/agent/session.py) | 会话 CRUD + 文件持久化，每步消息落盘 |
| [agent/dataset.py](../backend/agent/dataset.py) | 日志数据集类型化封装，携带 `user_id` 供源码工具隔离 |
| [agent/context.py](../backend/agent/context.py) | 组装消息列表 + 历史裁剪 |
| [agent/prompts/system.py](../backend/agent/prompts/system.py) | 模块化系统提示词，动态注入数据统计与源码组件清单 |
| [agent/llm/](../backend/agent/llm/) | OpenAI 兼容适配器（`base` / `openai_adapter` / `factory`） |
| [agent/tools/](../backend/agent/tools/) | 装饰器式工具注册表 + 日志工具 + 源码工具 |
| [observability/](../backend/observability/) | Langfuse v4 追踪（OTEL 自动嵌套） |

---

## 3. 入口层：HTTP 端点

两个等价入口，差别仅在输出方式：

| 端点 | 行为 |
|------|------|
| `POST /api/chat` | 同步：跑完整个 ReAct 循环后一次性返回 `{"reply": "..."}` |
| `POST /api/chat/stream` | SSE 流式：实时推送工具调用进度 + 逐词回复，支持前端 `AbortSignal` 中断 |

请求体统一为 `{"message": "...", "session_id": "..."}`。两个端点的前置处理一致（以 `/api/chat` 为例，[main.py:452](../backend/main.py#L452)）：

1. **鉴权**：`user: dict = Depends(get_current_user)` 校验 Bearer Token，拿到 `user["id"]` / `user["username"]`。
2. **校验会话归属**：`session_manager.get(session_id, user["id"])` —— 不存在或不属于该用户一律 404（隔离且不泄漏存在性）。
3. **加载数据集**：
   - 若 `session.dataset_id` 非空 → 从 `backend/data/datasets/<id>.json` 读盘 → 构造 `LogDataset(entries, summary, user_id=user["id"])`；
   - 若为空 → `dataset = None`（纯对话模式）。
4. **进入 Agent 或降级**（见下一节）。

---

## 4. Agent 单例与降级

Agent 在应用启动时以**模块级单例**初始化（[main.py:90](../backend/main.py#L90)）：

```python
agent = Agent(
    llm=create_llm(config.llm),
    session_manager=session_manager,
    max_iterations=config.max_tool_iterations,  # 默认 10
    max_history=config.max_history_messages,   # 默认 40
) if llm else None
```

- 若 `LLM_API_KEY` 未配置 → `create_llm` 失败 → `llm = None` → `agent = None`；
- 此时 `/api/chat*` **自动降级**为规则匹配回复（`_generate_reply`），仍把 user / assistant 消息写入会话，功能不中断，仅失去 LLM 推理与工具调用能力。

> 这意味着：**Agent 是否可用取决于 `llm` 是否初始化成功**，而与单次请求无关。

---

## 5. 单轮执行流程（`run` / `run_stream`）

`Agent.run()` 与 `run_stream()` 共享同一套 ReAct 逻辑，区别仅在于后者用 `async generator` 实时 `yield` SSE 事件。以下逐步拆解（行号对应 [core.py](../backend/agent/core.py)）。

### 步骤 ① 加载会话

```python
session = self.session_manager.get(session_id)
```

会话不存在则抛 `ValueError`（404 已在 HTTP 层拦截，此为防御性校验）。

### 步骤 ② 构建系统提示词

数据分析模式优先用 **Langfuse 托管 prompt**（`_resolve_system_prompt`：拉取成功就 `compile` 注入变量，任何异常/非数据分析模式都回退本地 `build_system_prompt()`，绝不抛错阻断 LLM 调用）。本地 `build_system_prompt()`（[prompts/system.py](../backend/agent/prompts/system.py)）按模式分支：

- **数据分析**：`ROLE`（角色）+ `DATA_CONTEXT`（注入实时统计：总行数 / 各级别计数 / 组件清单 / 时间范围）+ `TOOL_GUIDANCE`（工具使用指导）。若用户已索引过源码组件，再追加 `SOURCE_GUIDANCE`，引导 LLM 在涉及代码的问题上**优先调用** `gather_code_context` 拿代码证据再下结论。
- **纯源码问答**（无数据集但有索引）：`QA_SOURCE_ROLE` + `SOURCE_GUIDANCE`，明确「无日志、但可查源码」。
- **通用问答**（无数据集、无索引）：仅 `QA_ROLE` + `QA_GUIDANCE`，明确告知「未上传日志、无工具」，做通用知识问答。

源码组件清单通过 `_load_source_components(user_id)` 查询 AST 库得到（同步 SQLite 查询经 `asyncio.to_thread` 调度，避免阻塞事件循环）；以 `session.user_id` 为准，无登录时返回空列表。

### 步骤 ③ 写入用户消息

```python
self.session_manager.add_message(session_id, {"role": "user", "content": user_message})
```

**立即落盘**——即便后续循环崩溃，用户消息也已持久。

### 步骤 ④ 决定工具集合

按 group 计算 `exposed_groups`，再据此取 schema：

```python
exposed_groups = set()
if dataset is not None:        exposed_groups.add("log")     # 日志工具需要数据集
if source_components:          exposed_groups.add("source")  # 源码工具只需已索引组件
tool_schemas = ToolRegistry.get_schemas(groups=exposed_groups) if exposed_groups else []
```

源码工具执行时需要一个带 `user_id` 的 dataset，因此无日志会话会构造一个空壳 `effective_dataset = LogDataset(entries=[], summary={}, user_id=uid)`。只有 `exposed_groups` 为空（既无数据集又无索引）时，`tool_schemas=[]`，LLM 不会、也无法发起工具调用。

### 步骤 ⑤ ReAct 循环（见下一节）

---

## 6. ReAct 循环细节

循环上限 `max_iterations`（默认 10），每一轮（[core.py:122](../backend/agent/core.py#L122)）：

### 6.1 组装消息

- **第 0 轮**：用 `ConversationContext.build_messages(history=session.messages[:-1], user_message=...)`，即「系统提示词 + 裁剪后的历史 + 当前用户消息」。历史超过 `max_history`（默认 40）时只保留最近 N 条，防超 token。
- **第 ≥1 轮**：直接「系统提示词 + 完整 `session.messages`」（此时 messages 已包含上一轮的工具调用与工具结果）。

### 6.2 调用 LLM

- 同步：`response = await self.llm.chat(messages, tool_schemas)`；
- 流式：`async for evt in self.llm.chat_stream(messages, tool_schemas)`，收到 `content_delta` 立即 `yield {"type":"delta","text":...}` 转发前端（**真流式逐词渲染**），收尾时从 `done` 事件取完整 `content` / `tool_calls` / `finish_reason`。

### 6.3 分支判断（关键）

**注意：按 `tool_calls` 是否存在判断，而非 `finish_reason`。** 某些 OpenAI 兼容服务在返回 tool_calls 时仍给出非 `"tool_calls"` 的 `finish_reason`，若依赖它会工具永不执行（[core.py:336-340](../backend/agent/core.py#L336) 有专门注释）。

- **有 `tool_calls` 且 `exposed_groups` 非空** → 工具调用轮：
  1. 保存含 `tool_calls` 的 assistant 消息到会话（流式版只存一次）；
  2. 逐个解析工具调用（`function.name` + `json.loads(arguments)`，解析失败兜底为 `{}`）；
  3. 流式版先 `yield {"type":"tool_progress","status":"start"}`；
  4. `await ToolRegistry.execute(name, args, dataset)` 执行工具；
  5. 把工具结果作为 `{"role":"tool","tool_call_id":...,"content":...}` 写入会话；
  6. 流式版再 `yield {"type":"tool_progress","status":"done"}`；
  7. `continue` 进入下一轮——把工具结果喂回 LLM 继续推理。
- **无 `tool_calls`** → 最终回复轮：
  - `final_reply = content`；非空则存会话、`break` 退出；
  - 空回复（无工具调用却无内容）→ 记警告并重试本轮。

### 6.4 超限兜底

若循环耗尽 `max_iterations` 仍未得到最终回复，返回固定提示「分析过程较为复杂，已超出当前处理轮次限制……」（流式版逐词输出）。

### 6.5 上下文装配（分层视图）

每次调用 LLM，发送的并不是单一文本，而是 **`messages[]`（对话上下文）+ `tools[]`（工具 schema）** 两个并列结构。`messages[]` 由 `ConversationContext.build_messages()` 按固定顺序「系统提示词 → 裁剪后历史 → 当前用户消息」分层装配，各层数据来源不同：

```
                          一次 LLM 请求 = messages[]  +  tools[]
                                          ▲              ▲
        ┌─────────────────────────────────┘              └────────────────┐
        │                                                                  │
╔═══════╧══════════════════════════════════════════════════╗   ╔══════════╧═══════════╗
║  messages[] —— 对话上下文（自顶向下分层）                  ║   ║  tools[] —— 工具清单  ║
╠══════════════════════════════════════════════════════════╣   ╠══════════════════════╣
║                                                            ║   ║ ToolRegistry         ║
║ ┌─ Layer 0 ─ System Prompt（1 条 role=system）──────────┐ ║   ║   .get_schemas(      ║
║ │  build_system_prompt() 按模式拼装 PromptTemplate 块：  │ ║   ║     groups=          ║
║ │                                                        │ ║   ║     exposed_groups)  ║
║ │  ● 数据分析模式                                        │ ║   ║                      ║
║ │     ROLE_PROMPT                角色定义                │ ║   ║ exposed_groups:      ║
║ │   + DATA_CONTEXT_PROMPT  ◀── 动态注入 dataset.summary │ ║   ║   "log"  ◀ 有数据集   ║
║ │       total_lines / error·warn·notice·launch_count    │ ║   ║   "source" ◀ 有索引   ║
║ │       component_count / components[:15] / time_range  │ ║   ║                      ║
║ │   + TOOL_GUIDANCE_PROMPT       工具使用指导            │ ║   ║ ⇒ 日志工具(9) /       ║
║ │  (+ SOURCE_GUIDANCE_PROMPT ◀── 注入 indexed_components │ ║   ║   源码工具(6) 的      ║
║ │       仅当该用户已构建 AST 索引)                       │ ║   ║   OpenAI schema      ║
║ │                                                        │ ║   ╚══════════════════════╝
║ │  ● 纯源码问答     QA_SOURCE_ROLE + SOURCE_GUIDANCE     │ ║
║ │  ● 通用问答       QA_ROLE       + QA_GUIDANCE          │ ║      数据来源（旁路）
║ └────────────────────────────────────────────────────────┘ ║   ┌────────────────────┐
║                                                            ║   │ dataset.summary     │
║ ┌─ Layer 1 ─ History（裁剪后）──────────────────────────┐ ║   │   ← datasets/*.json │
║ │  session.messages 去掉当前用户消息后的全部历史：       │ ║◀──┤ indexed_components   │
║ │    user / assistant(+tool_calls) / tool(result) 交错   │ ║   │   ← ast.db (per-user)│
║ │  len > max_history(40) ⇒ 只保留 history[-40:]          │ ║   │ history             │
║ └────────────────────────────────────────────────────────┘ ║   │   ← sessions/*.json │
║                                                            ║   └────────────────────┘
║ ┌─ Layer 2 ─ Current User Message（role=user）──────────┐ ║
║ │  仅第 0 轮由 build_messages() 显式追加；               │ ║
║ │  后续轮它已在 session.messages 里，不再单独追加        │ ║
║ └────────────────────────────────────────────────────────┘ ║
╚══════════════════════════════════════════════════════════╝
```

**随迭代的演化**——上下文单调增长，每轮把上一轮的工具调用与结果并入历史：

```
第 0 轮   messages = [system] + 裁剪历史(session.messages[:-1]) + [当前 user]
          └ ConversationContext.build_messages(history=..., user_message=...)

第 ≥1 轮  messages = [system] + 完整 session.messages
          └ 历史已追加上一轮的 assistant(tool_calls) 与 tool(result)
            ↑ 每执行一次工具就 add_message 落盘，下一轮 LLM 即可见
            ↑ 直到某轮 LLM 不再返回 tool_calls → 收尾 break
```

> 三层中只有 **Layer 0 的 `DATA_CONTEXT` / `SOURCE_GUIDANCE` 是每轮重新注入的动态内容**（来自 dataset summary 与 AST 库）；Layer 1/2 是会话历史的累积回放。`tools[]` 与 `messages[]` 并列传入，**不是** `messages` 的成员——它由 `exposed_groups` 决定，模式不变则全程不变。

---

## 7. LLM 适配器层

所有提供商归一化为 OpenAI 形状消息 `{"role","content","tool_calls"}`（[llm/base.py](../backend/agent/llm/base.py)）。`factory.create_llm` 把 `LLM_PROVIDER`（`openai`/`ollama`/`custom`）统一映射到 `OpenAIAdapter`——任意 OpenAI 兼容端点靠 `LLM_API_BASE` 切换。

`OpenAIAdapter`（[openai_adapter.py](../backend/agent/llm/openai_adapter.py)）两个方法：

| 方法 | 行为 |
|------|------|
| `chat` | 非流式，`tool_choice="auto"`，返回归一化的 `{role, content, tool_calls}` |
| `chat_stream` | 流式，`stream=True` + `include_usage` |

**流式工具调用分片累积**（`chat_stream` 的核心难点）：工具调用在流中以分片到达（`function.arguments` 是半截 JSON），适配器按 `delta.tool_calls[].index` 在 `tool_calls_acc` 字典中累积 `id` / `name` / `arguments`，**直到 `finish_reason` 出现才整体排序产出** `done.tool_calls`。这避免下游 `json.loads` 拿到半截 JSON 而失败。

两个方法都在 Langfuse 的 `generation` observation 下执行，自动嵌套到当前活跃 span。

---

## 8. 工具层

### 8.1 注册机制

工具是 `async` 函数，**第一个位置参数恒为 `dataset: LogDataset`**，其余为 LLM 提供的 kwargs。用 `@ToolRegistry.register(name, description, parameters, required)` 在导入时注册（[registry.py](../backend/agent/tools/registry.py)）。`main.py` 的 `import agent.tools` 触发 `tools/__init__.py` 导入 `log_tools` + `source_tools` 完成注册——**无需其它接线**。

### 8.2 日志工具（8 个）

`search_logs`、`filter_by_component`、`filter_by_level`、`get_summary`、`get_time_range`、`get_errors_by_component`、`get_error_digest`、`get_context_around`。全部对 `dataset.entries` 做内存计算，返回结构化 JSON。

- `get_summary` 是**一站式概览**：合并了「整体级别计数」与「按组件级别分布」（原先分别由 `get_summary` / `get_component_stats` 提供，两者返回大量重合内容 → 合并为一次调用、仅保留非零字段以省 token）。
- `get_error_digest` 取代原 `get_error_timeline`：返回所有 ERROR **按消息模板去重**后的精简清单（每种错误一组，含 `count`/首末时间/`interval`/`sample_ids`），而非逐条带时间戳的明细，避免前期分析回喂成百上千条仅时间不同的重复错误。精确原文/上下文用 `sample_ids` 调 `get_context_around` 或明细工具 `dedup=false` 按需下钻。

### 8.3 源码工具（6 个，`group="source"`）

`get_function_source`、`search_symbols`、`list_source_files`、`summarize_component`、`get_file_source`、`gather_code_context`。它们读取 `dataset.user_id`，调用 `ast_analysis.db` 按 `(user_id, component, ...)` 查 AST，从源码快照切出带行号的函数体。`gather_code_context` 是**高层聚合入口**：一次调用汇聚「目标函数体 + 同文件兄弟符号 + 跨文件调用点」，替代多轮 `search → get_source → 找调用方`。

### 8.4 错误隔离

`ToolRegistry.execute` 捕获一切异常，把错误序列化为 `{"error": "..."}` JSON 字符串返回——**工具异常永远不会击穿 ReAct 循环**，LLM 会收到错误描述并据此调整策略（例如提示「组件未索引，请先在组件管理页构建 AST 索引」）。

---

## 9. 会话持久化

每条消息（user / assistant / tool）在产生时**立即追加并落盘**到 `backend/data/sessions/<session_id>.json`（[session.py](../backend/agent/session.py)）。这意味着：

- **跨轮上下文**：下一轮 LLM 能看到完整历史（含工具调用与结果）；
- **会话恢复**：刷新页面 / 重启服务后，`GET /api/sessions/{id}` 回放完整消息流；
- **崩溃安全**：即便循环中途异常，已发生的推理与工具结果不丢。

> 数据集与组件等 SQLite 写入使用各自的原子写策略；数据集 JSON 使用 `atomic_write_json`（写 `.tmp` → `fsync` → `os.replace`），保证 `/api/chat` 每次读盘绝不会读到半成品。

---

## 10. Langfuse 可观测性

启用条件：`LANGFUSE_ENABLED=true` 且配置了 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`。否则 `tracer.observation(...)` 降级为空操作（`_NoopObservation`），**零开销、零影响**。

利用 OpenTelemetry 上下文，`tracer.observation()` 的嵌套调用自动形成父子关系，无需手动传递 trace 对象。一次 `/api/chat` 的 trace 结构：

```
chat                         （顶层 trace，HTTP 层开启）
└── react-iteration-0        （第 0 轮迭代 span）
    ├── llm-chat             （generation：模型、输入消息、tools、usage、耗时）
    └── tool-search_logs     （工具 span：工具名、参数、结果预览）
└── react-iteration-1        （第 1 轮，基于工具结果再推理）
    └── llm-chat             （generation，最终回复）
```

每个请求结束时 HTTP 层调用 `tracer.flush()` 立即上报。在 Langfuse 控制台可观测：每轮 LLM 的输入/输出/token、工具调用参数与返回、整体耗时与迭代轮数。

### Token 用量统计

每轮 LLM 返回的 `usage`（`input` / `output` / `total`）由 `_accumulate_usage` 累加进整轮 `totals`：

- **同步 `run`**：调用方传入一个空 `usage` dict，方法把整轮 `totals` 写回；HTTP 层将其挂到 chat trace 的 metadata（`total_input_tokens` 等），并随 `/api/chat` 响应的 `usage` 字段返回前端。
- **流式 `run_stream`**：在 `done` 之前先 `yield {"type":"usage", input, output, total}` 事件；HTTP 层捕获后同样写入 trace metadata，并经 SSE 透传前端展示本轮 token 消耗。

> `usage` 是调用级元数据，**不进会话历史**（`response.pop("usage")`），避免回放给 LLM 时混入多余字段。Agent 为单例，token 累加器每个请求新建独立 dict，不暂存为实例属性。

---

## 11. 时序图

下图展示一次「有数据集、调用一个工具、两轮迭代」的典型流式对话：

```
前端              /api/chat/stream           Agent.run_stream            LLM 适配器          工具注册表         会话管理器        Langfuse
 │  POST message  │                              │                          │                   │                │                │
 │───────────────▶│  鉴权+加载会话+加载 dataset  │                          │                   │                │                │
 │                │  agent.run_stream(...)       │                          │                   │                │                │
 │                │─────────────────────────────▶│  开启 chat trace         │                   │                │                │
 │                │                              │  add_message(user)       │                   │                │                │
 │                │                              │────────────────────────────────────────────────────────────▶│                │
 │                │                              │  迭代0: 构建消息          │                   │                │                │
 │                │                              │  iter-span ──────────────────────────────────────────────────────────────────────▶│
 │                │                              │  chat_stream(messages)   │                   │                │                │
 │                │                              │─────────────────────────▶│  generation span  │                │                │
 │  yield delta   │◀─content_delta(逐词)─────────│◀─────────────────────────│                   │                │                │
 │                │                              │  done(tool_calls=[...])  │                   │                │                │
 │                │                              │  add_message(assistant+tool_calls)────────────────────────▶│                │
 │  tool_progress │                              │  execute(search_logs)    │                   │                │                │
 │  (start)       │◀─────────────────────────────│─────────────────────────────────────────────▶│                │                │
 │                │                              │  tool-span ───────────────────────────────────────────────────────────────────▶│
 │                │                              │                          │                   │  结果 JSON     │                │
 │                │                              │  add_message(tool,result)──────────────────────────────────▶│                │
 │  tool_progress │                              │  continue → 迭代1         │                   │                │                │
 │  (done)        │◀─────────────────────────────│                          │                   │                │                │
 │                │                              │  iter-span(1)            │                   │                │                │
 │                │                              │  chat_stream(无 tool_calls)│                  │                │                │
 │  yield delta   │◀─content_delta(逐词最终回复)─│◀─────────────────────────│                   │                │                │
 │                │                              │  add_message(assistant)──────────────────────────────────▶│                │
 │                │                              │  break                   │                   │                │                │
 │  done          │  tracer.flush()              │                          │                   │                │                │
 │◀───────────────│◀─────────────────────────────│                          │                   │                │                │
```

---

## 12. 关键不变式与边界

| 不变式 | 说明 |
|--------|------|
| **按 `tool_calls` 判定，而非 `finish_reason`** | OpenAI 兼容服务 finish_reason 不可靠，否则工具永不执行 |
| **工具按 group 暴露** | `log` 需数据集、`source` 需已索引组件；两者皆无才 `tool_schemas=[]` 杜绝工具调用 |
| **工具异常不击穿循环** | `ToolRegistry.execute` 捕获一切，错误以 JSON 回喂 LLM |
| **每步落盘** | user/assistant/tool 消息即时持久，跨轮 / 恢复 / 崩溃皆安全 |
| **流式 tool_calls 按 index 累积** | 半截 JSON 不可见，`finish_reason` 后才整体产出 |
| **用户隔离** | 会话 / 数据集 / 源码查询全部按 `user_id` 过滤，「非己」等同「不存在」 |
| **降级零中断** | `LLM_API_KEY` 缺失 → 规则匹配；`LANGFUSE_ENABLED!=true` → 空操作 trace |
| **迭代有界** | 上限 `max_iterations`（默认 10），防无限循环 |

---

## 附：扩展指引

- **加日志工具**：在 [tools/log_tools.py](../backend/agent/tools/log_tools.py) 写 `async def(dataset, ...)` + `@ToolRegistry.register`，自动进入工具列表，无需改 Agent 或 HTTP 层。
- **加源码工具**：同上，写在 [tools/source_tools.py](../backend/agent/tools/source_tools.py)，读 `dataset.user_id` 做 AST 查询。
- **换 LLM 提供商**：任意 OpenAI 兼容端点只需改 `.env`（`LLM_PROVIDER` / `LLM_API_BASE` / `LLM_MODEL`）；非兼容接口需在 [llm/](../backend/agent/llm/) 新增适配器并加 `factory` 分支。
- **调循环参数**：`AGENT_MAX_ITERATIONS` / `AGENT_MAX_HISTORY`（`.env`）。
