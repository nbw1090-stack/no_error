"""
FastAPI 应用入口

提供日志解析和 Agent 对话 API 服务：
- POST /api/parse        上传并解析 tar.gz 压缩包
- POST /api/chat         Agent 对话（支持多轮对话 + 工具调用）
- POST /api/sessions     创建会话
- GET  /api/sessions     列出所有会话
- GET  /api/sessions/{id} 获取会话详情（含完整消息）
- DELETE /api/sessions/{id} 删除会话
- GET  /api/health       健康检查
"""

import asyncio
import json
import logging
import os
import uuid

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from extractor import extract_logs
from parser import parse_logs

# ---- Agent 模块 ----
from config import AppConfig
from agent.llm.factory import create_llm
from agent.session import SessionManager
from agent.core import Agent
from agent.dataset import LogDataset
from observability import init_observability, get_client

# 触发工具注册（导入即注册）
import agent.tools  # noqa: F401

# ============================================================
# 日志
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ============================================================
# 配置
# ============================================================
config = AppConfig.from_env()

# ============================================================
# 可观测性（Langfuse）
# ============================================================
observability = init_observability(config.langfuse)
logger.info(
    "Observability: langfuse_enabled=%s, host=%s",
    config.langfuse.enabled,
    config.langfuse.host,
)

# ============================================================
# 初始化 Agent 组件（模块级别，全应用共享）
# ============================================================
try:
    llm = create_llm(config.llm)
    logger.info(
        "LLM initialized: provider=%s, model=%s, base=%s",
        config.llm.provider,
        config.llm.model,
        config.llm.api_base or "default",
    )
except Exception as e:
    logger.warning("LLM initialization failed: %s. Chat will use fallback mode.", e)
    llm = None

session_manager = SessionManager(os.path.join(config.data_dir, "sessions"))

agent = Agent(
    llm=llm,
    session_manager=session_manager,
    max_iterations=config.max_tool_iterations,
    max_history=config.max_history_messages,
) if llm else None

# ============================================================
# 数据集存储目录
# ============================================================
DATASETS_DIR = os.path.join(config.data_dir, "datasets")
os.makedirs(DATASETS_DIR, exist_ok=True)

# ============================================================
# 请求模型
# ============================================================


class ChatRequest(BaseModel):
    """Agent 对话请求"""
    message: str
    session_id: str


class CreateSessionRequest(BaseModel):
    """创建会话请求"""
    dataset_id: str


# ============================================================
# 创建 FastAPI 应用
# ============================================================
app = FastAPI(title="BMC 日志分析系统")

# ============================================================
# CORS 配置（开发阶段允许所有来源）
# ============================================================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# 辅助函数
# ============================================================
def atomic_write_json(path: str, data: dict) -> None:
    """
    原子写入 JSON：先写临时文件 → fsync → os.replace 原子替换。

    保证并发读盘的端点（如 /api/chat 每次请求 json.load）绝不会读到
    写到一半的半成品 JSON。os.replace 在同文件系统内是原子的。
    """
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def sse(event: dict) -> str:
    """将事件 dict 序列化为 SSE data 行。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


# ============================================================
# 健康检查
# ============================================================
@app.get("/api/health")
async def health_check():
    """健康检查接口"""
    return {
        "status": "ok",
        "llm_available": llm is not None,
        "agent_available": agent is not None,
    }


# ============================================================
# 日志解析
# ============================================================
@app.post("/api/parse")
async def parse_log(file: UploadFile = File(...)):
    """
    上传并解析 tar.gz 日志压缩包。

    接收 multipart/form-data，字段名为 file。
    流程：保存文件 → 解压提取 → 解析日志 → 持久化数据集 → 清理临时文件。
    """
    tracer = get_client()

    with tracer.observation(
        name="parse-log",
        input={
            "filename": file.filename,
            "content_type": file.content_type,
        },
    ) as parse_trace:
        try:
            # ---- 1. 读取上传文件（内存处理，不落盘）----
            content = await file.read()

            # ---- 2. 解压提取日志内容 ----
            app_content, framework_content = extract_logs(content)

            # ---- 3. 解析日志 ----
            result = parse_logs(app_content, framework_content)

            # ---- 4. 持久化数据集 ----
            dataset_id = uuid.uuid4().hex[:12]
            dataset_path = os.path.join(DATASETS_DIR, f"{dataset_id}.json")
            dataset_data = {
                "dataset_id": dataset_id,
                "entries": result["entries"],
                "summary": result["summary"],
            }
            atomic_write_json(dataset_path, dataset_data)

            logger.info(
                "Dataset saved: %s (%d entries)",
                dataset_id,
                len(result["entries"]),
            )

            parse_trace.update(
                output={
                    "dataset_id": dataset_id,
                    "entries_count": len(result["entries"]),
                    "total_lines": result["summary"].get("totalLines", 0),
                    "error_count": result["summary"].get("errorCount", 0),
                    "components_count": len(
                        result["summary"].get("components", [])
                    ),
                },
            )

            tracer.flush()

            # ---- 5. 返回成功响应 ----
            return JSONResponse(
                content={
                    "success": True,
                    "dataset_id": dataset_id,
                    "summary": result["summary"],
                    "entries": result["entries"],
                    "errors": [],
                }
            )

        except Exception as e:
            logger.exception("Parse failed")
            parse_trace.update(
                output={"error": str(e)},
                level="ERROR",
                status_message=str(e)[:200],
            )
            tracer.flush()
            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "summary": None,
                    "entries": [],
                    "errors": [str(e)],
                },
            )


# ============================================================
# 日志解析（SSE 流式输出）
# ============================================================
@app.post("/api/parse/stream")
async def parse_log_stream(file: UploadFile = File(...)):
    """
    上传并解析 tar.gz 日志压缩包（SSE 流式版本）。

    与 /api/parse 解析流程相同，但通过 Server-Sent Events 渐进推送，
    让前端可以边解析边渲染：

        progress(extracting) → progress(parsing) → summary →
        entries_chunk × N → done

    不变量：summary 事件到达 ⟺ dataset.json 已完整原子落盘。
    因此前端收到 summary 即可安全创建会话，chat 端点读盘也绝不会拿到半成品。
    """
    tracer = get_client()

    # ---- 读取上传文件到内存（不落盘）----
    # 必须在 generator 外完成：StreamingResponse 开始迭代时底层 UploadFile 可能
    # 已被关闭，在 generator 内 await file.read() 会抛 "read of closed file"。
    content = await file.read()

    async def event_generator():
        with tracer.observation(
            name="parse-log-stream",
            input={
                "filename": file.filename,
                "content_type": file.content_type,
            },
        ) as parse_trace:
            try:
                yield sse({"type": "progress", "stage": "extracting"})

                # ---- 2. 解压提取（线程池，避免阻塞事件循环）----
                app_content, framework_content = await asyncio.to_thread(
                    extract_logs, content
                )

                yield sse({"type": "progress", "stage": "parsing"})

                # ---- 3. 解析日志（线程池）----
                result = await asyncio.to_thread(
                    parse_logs, app_content, framework_content
                )
                entries = result["entries"]
                summary = result["summary"]

                # ---- 4. 原子落盘（在推送 summary 之前完成）----
                dataset_id = uuid.uuid4().hex[:12]
                dataset_path = os.path.join(DATASETS_DIR, f"{dataset_id}.json")
                atomic_write_json(
                    dataset_path,
                    {
                        "dataset_id": dataset_id,
                        "entries": entries,
                        "summary": summary,
                    },
                )

                logger.info(
                    "Dataset saved [stream]: %s (%d entries)",
                    dataset_id,
                    len(entries),
                )
                parse_trace.update(
                    output={
                        "dataset_id": dataset_id,
                        "entries_count": len(entries),
                        "total_lines": summary.get("totalLines", 0),
                        "error_count": summary.get("errorCount", 0),
                    },
                )

                # ---- 5. 推送 summary（落盘已完成，前端据此渲染 KPI 并按需拉首屏 entries）----
                yield sse(
                    {
                        "type": "summary",
                        "dataset_id": dataset_id,
                        "summary": summary,
                    }
                )

                # ---- 6. 流结束（日志条目由前端经分页接口按需加载）----
                yield sse({"type": "done", "dataset_id": dataset_id})

            except Exception as e:
                logger.exception("Parse stream failed")
                parse_trace.update(
                    output={"error": str(e)},
                    level="ERROR",
                    status_message=str(e)[:200],
                )
                yield sse({"type": "error", "message": str(e)})

            finally:
                tracer.flush()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ============================================================
# Agent 对话
# ============================================================
@app.post("/api/chat")
async def chat(req: ChatRequest):
    """
    Agent 对话接口。

    使用 ReAct 模式：Agent 分析问题 → 调用工具 → 生成回复。
    如果 LLM 不可用，降级为规则匹配模式。
    """
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    # ---- 验证会话存在 ----
    session = session_manager.get(req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # ---- 加载数据集 ----
    dataset_path = os.path.join(DATASETS_DIR, f"{session.dataset_id}.json")
    if not os.path.exists(dataset_path):
        raise HTTPException(status_code=404, detail="Dataset not found")

    with open(dataset_path) as f:
        data = json.load(f)

    dataset = LogDataset(
        entries=data["entries"],
        summary=_ensure_summary_complete(data["summary"], data["entries"]),
    )

    tracer = get_client()

    # ---- Agent 模式 ----
    if agent is not None:
        with tracer.observation(
            name="chat",
            session_id=req.session_id,
            input={
                "message": message,
                "session_id": req.session_id,
                "dataset_id": session.dataset_id,
            },
            metadata={
                "dataset_entries": len(dataset.entries),
                "llm_model": config.llm.model,
                "llm_provider": config.llm.provider,
            },
        ) as trace:
            try:
                reply = await agent.run(
                    session_id=req.session_id,
                    user_message=message,
                    dataset=dataset,
                )
                trace.update(output={"reply": reply[:2000]})
            except Exception as e:
                logger.exception("Agent run failed")
                trace.update(
                    output={"error": str(e)},
                    level="ERROR",
                    status_message=str(e)[:200],
                )
                raise HTTPException(
                    status_code=500, detail=f"Analysis failed: {str(e)}"
                )

        # 确保 trace 数据立即发送到 Langfuse
        tracer.flush()
        return {"reply": reply}

    # ---- 降级模式：规则匹配 ----
    logger.info("Agent unavailable, using fallback rule-based reply")

    with tracer.observation(
        name="chat-fallback",
        session_id=req.session_id,
        input={
            "message": message,
            "session_id": req.session_id,
            "dataset_id": session.dataset_id,
        },
        metadata={
            "mode": "rule_based",
            "dataset_entries": len(dataset.entries),
        },
    ) as trace:
        reply = _generate_reply(message, dataset.summary)
        # 将消息记录到会话
        session_manager.add_message(
            req.session_id, {"role": "user", "content": message}
        )
        session_manager.add_message(
            req.session_id, {"role": "assistant", "content": reply}
        )
        trace.update(output={"reply": reply[:2000]})

    tracer.flush()
    return {"reply": reply}


# ============================================================
# Agent 对话（SSE 流式输出）
# ============================================================
@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    """
    Agent 对话接口（SSE 流式输出版本）。

    使用 Server-Sent Events 实时推送：工具调用进度 + 逐词回复文本。
    """
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    # ---- 验证会话存在 ----
    session = session_manager.get(req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # ---- 加载数据集 ----
    dataset_path = os.path.join(DATASETS_DIR, f"{session.dataset_id}.json")
    if not os.path.exists(dataset_path):
        raise HTTPException(status_code=404, detail="Dataset not found")

    with open(dataset_path) as f:
        data = json.load(f)

    dataset = LogDataset(
        entries=data["entries"],
        summary=_ensure_summary_complete(data["summary"], data["entries"]),
    )

    tracer = get_client()

    async def event_generator():
        """
        SSE 事件生成器：将 Agent.run_stream() 的 yield 事件
        转换为 SSE 格式的字符串。
        """
        try:
            # ---- Agent 流式模式 ----
            if agent is not None:
                with tracer.observation(
                    name="chat-stream",
                    session_id=req.session_id,
                    input={
                        "message": message,
                        "session_id": req.session_id,
                        "dataset_id": session.dataset_id,
                    },
                    metadata={
                        "dataset_entries": len(dataset.entries),
                        "llm_model": config.llm.model,
                        "llm_provider": config.llm.provider,
                    },
                ) as trace:
                    async for event in agent.run_stream(
                        session_id=req.session_id,
                        user_message=message,
                        dataset=dataset,
                    ):
                        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

                    trace.update(output={"status": "stream_complete"})
            else:
                # ---- 降级模式：规则匹配 ----
                logger.info(
                    "Agent unavailable, using fallback rule-based reply [stream]"
                )
                reply = _generate_reply(message, dataset.summary)
                session_manager.add_message(
                    req.session_id,
                    {"role": "user", "content": message},
                )
                session_manager.add_message(
                    req.session_id,
                    {"role": "assistant", "content": reply},
                )
                # 逐词输出降级回复
                words = reply.split(" ")
                for i, word in enumerate(words):
                    separator = " " if i < len(words) - 1 else ""
                    yield f"data: {json.dumps({'type': 'delta', 'text': word + separator}, ensure_ascii=False)}\n\n"
                    await asyncio.sleep(0.01)
                yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except Exception as e:
            logger.exception("Stream error")
            error_event = json.dumps(
                {"type": "error", "text": str(e)}, ensure_ascii=False
            )
            yield f"data: {error_event}\n\n"

        finally:
            tracer.flush()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _generate_reply(message: str, summary: dict) -> str:
    """
    降级模式：基于规则的回复生成。

    当 LLM 不可用时使用此函数提供基本交互能力。
    """
    msg_lower = message.lower()
    total = summary.get("totalLines", 0)
    errors = summary.get("errorCount", 0)
    warnings = summary.get("warningCount", 0)
    components = summary.get("components", [])

    # 概览查询
    if any(kw in msg_lower for kw in ["概览", "总结", "overview", "总览", "汇总"]):
        return (
            f"📊 **日志分析概览**\n\n"
            f"- 总日志行数：**{total:,}** 条\n"
            f"- 错误 (ERROR)：**{errors:,}** 条\n"
            f"- 警告 (WARNING)：**{warnings:,}** 条\n"
            f"- 涉及组件：**{len(components)}** 个\n"
            f"- 主要组件：{', '.join(components[:5])}\n\n"
            f"错误占比 **{errors / total * 100:.1f}%**（基于规则匹配，"
            f"配置 LLM 后可获得更智能的分析）。"
        )

    # 组件查询
    if any(kw in msg_lower for kw in ["组件", "component"]):
        comp_list = "\n".join(f"- {c}" for c in components[:10])
        more = (
            f"\n...及其他 {len(components) - 10} 个组件"
            if len(components) > 10
            else ""
        )
        return (
            f"🔧 **涉及组件列表**（共 {len(components)} 个）\n\n"
            f"{comp_list}{more}\n\n"
            f"配置 LLM 后可以深入分析某个组件的具体错误。"
        )

    # 错误分析
    if any(kw in msg_lower for kw in ["错误", "error", "异常", "故障"]):
        return (
            f"⚠️ **错误分析**（规则匹配模式）\n\n"
            f"共检测到 **{errors:,}** 条错误日志。\n\n"
            f"配置 LLM API Key 后，我可以：\n"
            f"1. 深入分析具体组件错误\n"
            f"2. 追踪错误时间线和爆发点\n"
            f"3. 提供精准的排查建议"
        )

    # 通用问候
    if any(kw in msg_lower for kw in ["你好", "hello", "hi", "嗨", "帮助", "help"]):
        return (
            "👋 你好！我是 **BMC 日志分析助手**。\n\n"
            "⚠️ 当前运行在**规则匹配模式**（LLM 未配置）。\n\n"
            "配置 LLM API Key 后可以启用智能分析。"
            "设置环境变量 `LLM_API_KEY` 即可。\n\n"
            "规则模式下你可以问我：\n"
            "- 📊 概览 / 汇总\n"
            "- 🔧 组件列表\n"
            "- ⚠️ 错误分析"
        )

    # 默认
    return (
        f"收到你的问题。当前为规则匹配模式，分析能力有限。\n\n"
        f"配置 LLM（设置 `LLM_API_KEY` 环境变量）后可以启用智能分析。\n\n"
        f"规则模式下支持：\n"
        f"- 「概览」\n"
        f"- 「有哪些组件」\n"
        f"- 「分析错误」"
    )


# ============================================================
# 数据集 API
# ============================================================


def _compute_component_errors(entries: list) -> list:
    """
    从 entries 按组件聚合 ERROR 数量（Top 20）。

    用于补齐旧数据集 summary 中缺失的 componentErrors 字段：这些数据集由
    更早版本（尚未统计 componentErrors）的 parser 落盘，磁盘上的 summary
    没有 componentErrors，导致前端 StatsPanel「组件错误分布为空但实际有值」。
    """
    counts: dict[str, int] = {}
    for e in entries:
        if e.get("level") == "ERROR":
            comp = e.get("component")
            if comp:
                counts[comp] = counts.get(comp, 0) + 1
    return [
        {"name": name, "count": count}
        for name, count in sorted(
            counts.items(), key=lambda x: x[1], reverse=True
        )[:20]
    ]


def _ensure_summary_complete(summary: dict, entries: list) -> dict:
    """就地补齐 summary 中缺失的 componentErrors（旧数据集兼容），返回 summary。"""
    if not summary.get("componentErrors"):
        summary["componentErrors"] = _compute_component_errors(entries)
    return summary


@app.get("/api/datasets/{dataset_id}")
async def get_dataset(dataset_id: str):
    """
    获取数据集的汇总统计（summary）。

    日志条目改由分页接口 /api/datasets/{dataset_id}/entries 按需提供，
    避免一次性传输全量 entries（大日志时首屏卡顿）。

    旧数据集（summary 缺 componentErrors）在此按 entries 现场补齐，
    保证前端 StatsPanel 始终能拿到组件错误分布。
    """
    dataset_path = os.path.join(DATASETS_DIR, f"{dataset_id}.json")
    if not os.path.exists(dataset_path):
        raise HTTPException(status_code=404, detail="Dataset not found")

    with open(dataset_path) as f:
        data = json.load(f)

    summary = _ensure_summary_complete(data["summary"], data["entries"])

    return {
        "dataset_id": data["dataset_id"],
        "summary": summary,
    }


def _load_filter_page(
    dataset_path: str,
    offset: int,
    limit: int,
    component: str | None,
    level: str | None,
    search: str | None,
) -> tuple[list, int, bool]:
    """
    加载数据集并按筛选条件过滤 + 分页（在线程池中执行，避免阻塞事件循环）。

    筛选逻辑与前端原 filteredEntries 一致。
    """
    with open(dataset_path) as f:
        data = json.load(f)

    entries = data["entries"]

    if component:
        entries = [e for e in entries if e["component"] == component]
    if level and level != "ALL":
        entries = [e for e in entries if e["level"] == level]
    if search:
        keyword = search.lower()
        entries = [
            e for e in entries if keyword in (e["message"] or "").lower()
        ]

    total = len(entries)
    page_limit = max(1, min(limit, 1000))
    page_offset = max(0, offset)
    page = entries[page_offset : page_offset + page_limit]
    has_more = page_offset + page_limit < total

    return page, total, has_more


@app.get("/api/datasets/{dataset_id}/entries")
async def get_dataset_entries(
    dataset_id: str,
    offset: int = 0,
    limit: int = 200,
    component: str | None = None,
    level: str | None = None,
    search: str | None = None,
):
    """
    分页获取数据集的日志条目（支持筛选）。

    前端 LogTable 按需加载：首屏取前 limit 条，滚动到底加载下一批。
    """
    dataset_path = os.path.join(DATASETS_DIR, f"{dataset_id}.json")
    if not os.path.exists(dataset_path):
        raise HTTPException(status_code=404, detail="Dataset not found")

    page, total, has_more = await asyncio.to_thread(
        _load_filter_page,
        dataset_path,
        offset,
        limit,
        component,
        level,
        search,
    )
    page_limit = max(1, min(limit, 1000))

    return {
        "items": page,
        "total": total,
        "offset": max(0, offset),
        "limit": page_limit,
        "hasMore": has_more,
    }


# ============================================================
# 会话管理 API
# ============================================================


@app.post("/api/sessions")
async def create_session(req: CreateSessionRequest):
    """
    创建新的聊天会话。

    会话关联到已上传的数据集，不同会话之间完全隔离。
    """
    # 验证数据集存在
    dataset_path = os.path.join(DATASETS_DIR, f"{req.dataset_id}.json")
    if not os.path.exists(dataset_path):
        raise HTTPException(
            status_code=404,
            detail=f"Dataset not found: {req.dataset_id}",
        )

    session = session_manager.create(req.dataset_id)
    return {
        "session_id": session.session_id,
        "dataset_id": session.dataset_id,
        "created_at": session.created_at,
        "message_count": 0,
    }


@app.get("/api/sessions")
async def list_sessions():
    """列出所有会话（仅元数据，不含消息体）"""
    sessions = session_manager.list_all()
    return {"sessions": sessions}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    """
    获取会话详情，包含完整的消息历史。

    用于会话恢复：前端可通过此接口恢复之前的对话。
    """
    session = session_manager.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    return {
        "session_id": session.session_id,
        "dataset_id": session.dataset_id,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "messages": session.messages,
    }


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    """删除会话"""
    deleted = session_manager.delete(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"deleted": True}


# ============================================================
# 应用生命周期事件
# ============================================================


@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭时确保 Langfuse 数据刷新"""
    obs = get_client()
    if obs.enabled:
        logger.info("Flushing Langfuse data before shutdown...")
        obs.shutdown()


# ============================================================
# 启动入口（开发调试用）
# ============================================================
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
