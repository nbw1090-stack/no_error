"""
在「可能已有运行中事件循环」的上下文里，把一个协程跑到完成。

为什么需要：Langfuse 的 dataset.run_experiment 在一个**运行中的事件循环**里驱动
task/evaluator，此时直接 asyncio.run() 会抛 "cannot be called from a running event
loop"。而离线模式是纯同步上下文，asyncio.run() 又是最自然的选择。这个 helper 两边
通用：无运行中 loop → 直接 asyncio.run；已在 loop 内 → 另起线程跑新 loop，并复制
当前 contextvars（含 OTEL 当前 span），让 agent 内部 observation 仍能嵌套到外层
trace、scores 落到正确的 trace 上。
"""

import asyncio
import contextvars
import threading


def run_coro_blocking(coro):
    """同步阻塞地把 coro 跑完并返回结果，无论当前是否已有运行中事件循环。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 无运行中 loop：常规路径（离线模式 / 直接命令行）
        return asyncio.run(coro)

    # 已在运行中的 loop 内（run_experiment）：另起线程跑独立 loop，
    # 复制当前上下文以保留 OTEL trace 传播。
    ctx = contextvars.copy_context()
    box: dict = {}

    def _worker():
        try:
            box["value"] = ctx.run(asyncio.run, coro)
        except BaseException as e:  # noqa: BLE001 — 原样回传到调用线程
            box["error"] = e

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box["value"]
