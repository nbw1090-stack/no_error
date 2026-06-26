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

import json
import logging
import os
import uuid

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from extractor import extract_logs
from parser import parse_logs

# ---- Agent 模块 ----
from config import AppConfig
from agent.llm.factory import create_llm
from agent.session import SessionManager
from agent.core import Agent
from agent.dataset import LogDataset

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
# 上传目录（相对于 backend/ 目录）
# ============================================================
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


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
    temp_path = None
    try:
        # ---- 1. 保存上传文件到临时目录 ----
        safe_filename = file.filename or "dump_info.tar.gz"
        temp_path = os.path.join(UPLOAD_DIR, safe_filename)

        with open(temp_path, "wb") as buffer:
            content = await file.read()
            buffer.write(content)

        # ---- 2. 解压提取日志内容 ----
        app_content, framework_content = extract_logs(temp_path)

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
        with open(dataset_path, "w") as f:
            json.dump(dataset_data, f, ensure_ascii=False)

        logger.info(
            "Dataset saved: %s (%d entries)",
            dataset_id,
            len(result["entries"]),
        )

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
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "summary": None,
                "entries": [],
                "errors": [str(e)],
            },
        )

    finally:
        # ---- 6. 清理临时文件 ----
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


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
        summary=data["summary"],
    )

    # ---- Agent 模式 ----
    if agent is not None:
        try:
            reply = await agent.run(
                session_id=req.session_id,
                user_message=message,
                dataset=dataset,
            )
            return {"reply": reply}
        except Exception as e:
            logger.exception("Agent run failed")
            raise HTTPException(
                status_code=500, detail=f"Analysis failed: {str(e)}"
            )

    # ---- 降级模式：规则匹配 ----
    logger.info("Agent unavailable, using fallback rule-based reply")
    reply = _generate_reply(message, dataset.summary)
    # 仍需将消息记录到会话
    session_manager.add_message(
        req.session_id, {"role": "user", "content": message}
    )
    session_manager.add_message(
        req.session_id, {"role": "assistant", "content": reply}
    )
    return {"reply": reply}


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
# 启动入口（开发调试用）
# ============================================================
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
