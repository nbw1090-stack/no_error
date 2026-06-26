"""
FastAPI 应用入口

提供日志解析 API 服务：
- POST /api/parse  上传并解析 tar.gz 压缩包
- GET  /api/health  健康检查
"""

import os
import shutil
from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from extractor import extract_logs
from parser import parse_logs
from pydantic import BaseModel


# ============================================================
# 聊天请求模型
# ============================================================
class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []
    context: dict | None = None

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

# 确保上传目录存在
os.makedirs(UPLOAD_DIR, exist_ok=True)


@app.get("/api/health")
async def health_check():
    """健康检查接口"""
    return {"status": "ok"}


@app.post("/api/chat")
async def chat(req: ChatRequest):
    """
    Agent 对话接口。

    接收用户消息 + 历史对话 + 日志上下文，返回 AI 分析回复。
    当前版本基于规则和模板生成回复，后续可接入 LLM。
    """
    message = req.message.strip()
    context = req.context

    # ---- 根据消息内容生成回复 ----
    reply = _generate_reply(message, context)

    return {"reply": reply}


def _generate_reply(message: str, context: dict | None) -> str:
    """基于规则 + 上下文生成智能回复"""

    msg_lower = message.lower()

    # ---- 上下文相关查询 ----
    if context:
        total = context.get("totalLines", 0)
        errors = context.get("errorCount", 0)
        warnings = context.get("warningCount", 0)
        components = context.get("components", [])

        # 概览查询
        if any(kw in msg_lower for kw in ["概览", "总结", "overview", "总览", "汇总"]):
            return (
                f"📊 **日志分析概览**\n\n"
                f"- 总日志行数：**{total:,}** 条\n"
                f"- 错误 (ERROR)：**{errors:,}** 条\n"
                f"- 警告 (WARNING)：**{warnings:,}** 条\n"
                f"- 涉及组件：**{len(components)}** 个\n"
                f"- 主要组件：{', '.join(components[:5])}\n\n"
                f"错误占比 **{errors / total * 100:.1f}%**，"
                f"建议优先排查错误级别最高的组件。"
                f"你可以进一步问我某个具体组件或错误类型的详情。"
            )

        # 组件查询
        if any(kw in msg_lower for kw in ["组件", "component"]):
            comp_list = "\n".join(f"- {c}" for c in components[:10])
            more = f"\n...及其他 {len(components) - 10} 个组件" if len(components) > 10 else ""
            return (
                f"🔧 **涉及组件列表**（共 {len(components)} 个）\n\n"
                f"{comp_list}{more}\n\n"
                f"你可以让我分析某个具体组件的错误情况。"
            )

        # 错误分析
        if any(kw in msg_lower for kw in ["错误", "error", "异常", "故障"]):
            return (
                f"⚠️ **错误分析**\n\n"
                f"共检测到 **{errors:,}** 条错误日志，占总日志的 **{errors / total * 100:.1f}%**。\n\n"
                f"**建议排查方向：**\n"
                f"1. 查看「组件错误分布」面板，定位错误最集中的组件\n"
                f"2. 使用筛选栏按 ERROR 级别过滤，逐条分析错误消息\n"
                f"3. 关注启动阶段的错误（前面时间戳），可能是初始化失败\n"
                f"4. 对比 WARNING 和 ERROR 的时间关联性\n\n"
                f"需要我深入分析某个具体组件吗？"
            )

        # 时间范围
        if any(kw in msg_lower for kw in ["时间", "time", "范围", "跨度"]):
            return (
                f"⏰ 日志时间跨度覆盖从启动到最新记录。"
                f"你可以在筛选栏中使用时间范围选择器缩小分析窗口。"
            )

    # ---- 通用问候 ----
    if any(kw in msg_lower for kw in ["你好", "hello", "hi", "嗨", "帮助", "help"]):
        return (
            "👋 你好！我是 **BMC 日志分析助手**。\n\n"
            "我可以帮你：\n"
            "- 📊 查看日志**概览**和统计摘要\n"
            "- 🔍 分析**错误**分布和排查方向\n"
            "- 🔧 了解涉及的**组件**列表\n"
            "- ⏰ 了解日志时间范围\n"
            "- 💡 提供问题排查建议\n\n"
            "请告诉我你想了解什么？"
        )

    # ---- 感谢 ----
    if any(kw in msg_lower for kw in ["谢谢", "thank", "thanks", "感谢"]):
        return "不客气！如果还有其他问题，随时问我。😊"

    # ---- 默认回复 ----
    return (
        f"收到你的问题。根据已上传的日志数据，"
        f"我可以帮你分析错误趋势、组件分布和排查方向。\n\n"
        f"你可以试试问我：\n"
        f"- 「给我一个概览」\n"
        f"- 「有哪些组件出错了」\n"
        f"- 「错误集中在哪些方面」\n"
        f"- 「帮我分析一下异常」"
    )


@app.post("/api/parse")
async def parse_log(file: UploadFile = File(...)):
    """
    上传并解析 tar.gz 日志压缩包。

    接收 multipart/form-data，字段名为 file。
    内部流程：保存文件 → 解压提取 → 解析日志 → 清理临时文件。
    """
    temp_path = None
    try:
        # ---- 1. 保存上传文件到临时目录 ----
        # 使用原始文件名或默认名
        safe_filename = file.filename or "dump_info.tar.gz"
        temp_path = os.path.join(UPLOAD_DIR, safe_filename)

        # 以二进制写入模式保存
        with open(temp_path, "wb") as buffer:
            content = await file.read()
            buffer.write(content)

        # ---- 2. 解压提取日志内容 ----
        app_content, framework_content = extract_logs(temp_path)

        # ---- 3. 解析日志 ----
        result = parse_logs(app_content, framework_content)

        # ---- 4. 返回成功响应 ----
        return JSONResponse(
            content={
                "success": True,
                "summary": result["summary"],
                "entries": result["entries"],
                "errors": [],
            }
        )

    except Exception as e:
        # 捕获所有异常，返回错误信息
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
        # ---- 5. 清理临时文件 ----
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass  # 清理失败不阻塞响应


# ============================================================
# 启动入口（开发调试用）
# ============================================================
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
