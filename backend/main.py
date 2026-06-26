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
