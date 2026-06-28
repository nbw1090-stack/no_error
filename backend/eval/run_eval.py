"""
Langfuse Dataset 回归评测脚本（骨架）。

用途：在 Langfuse 平台建好 dataset 后，一键把里面的 case 跑过本项目的 ReAct
agent，用 evaluator 打分并把分数上报回 Langfuse，便于在 Datasets → Runs 对比
不同 prompt / 模型版本的回归表现。

前置（在 Langfuse UI 操作）：
1. 新建 dataset，例如 bmc-diagnosis-golden。
2. 为每个回归 case 加一个 item，约定 schema：
     input           = {"question": "<要问 agent 的问题>", "dataset_id": "<本地数据集 id>"}
     expected_output = "<期望命中的根因关键词（用于确定性打分）>"
   其中 dataset_id 指向 backend/data/datasets/<id>.json —— 先在前端上传一份
   dump_info.tar.gz 得到 dataset_id，再把它的 case 写进 Langfuse dataset。

运行（必须在 backend/ 下、且用 venv，否则 tree-sitter 依赖缺失）：
    cd backend && source venv/bin/activate
    python -m eval.run_eval --dataset bmc-diagnosis-golden --run-name prompt-v2

注意：
- Langfuse 需已配置（LANGFUSE_ENABLED=true + keys），否则实验无 trace/分数。
- 本脚本串行执行（max_concurrency=1）以保证稳定性；可按需调大并发。
- 这是骨架：dataset item schema、evaluator 逻辑都可按实际回归需求微调。
"""

import argparse
import asyncio
import json
import logging
import os

from config import AppConfig
from agent.llm.factory import create_llm
from agent.session import SessionManager
from agent.core import Agent
from agent.dataset import LogDataset
from observability import init_observability, get_client
import agent.tools  # noqa: F401  # 导入即触发工具注册（与 main.py 一致）
from langfuse import Evaluation

logger = logging.getLogger(__name__)


def _load_dataset(dataset_id: str, data_dir: str) -> LogDataset:
    """读取本地数据集 JSON 并构造 LogDataset（不校验归属：eval 用的是黄金集）。"""
    path = os.path.join(data_dir, "datasets", f"{dataset_id}.json")
    with open(path) as f:
        data = json.load(f)
    return LogDataset(entries=data["entries"], summary=data["summary"])


def diagnosis_accuracy(*, input, output, expected_output, metadata, **kwargs):
    """确定性打分：期望根因关键词是否出现在 agent 回复里（命中=1.0）。"""
    out = (output or "").lower()
    if expected_output and str(expected_output).lower() in out:
        return Evaluation(name="diagnosis_acc", value=1.0, comment="命中期望根因")
    return Evaluation(name="diagnosis_acc", value=0.0, comment="未命中期望根因")


def main(dataset_name: str, run_name: str) -> None:
    config = AppConfig.from_env()
    init_observability(config.langfuse)
    # 复用 observability 已初始化的 Langfuse 实例：保证 dataset 实验 API 与
    # agent.run 内部的 tracing 共用同一 OTEL provider，task 执行会自动嵌套到
    # run_experiment 为每个 item 创建的 trace 下。
    langfuse = get_client()._langfuse
    if langfuse is None:
        raise SystemExit("Langfuse 未启用/未初始化，请检查 LANGFUSE_* 配置")

    llm = create_llm(config.llm)
    if llm is None:
        raise SystemExit("LLM 未配置（LLM_API_KEY 缺失），无法跑评测")
    session_manager = SessionManager(os.path.join(config.data_dir, "sessions"))
    agent = Agent(
        llm=llm,
        session_manager=session_manager,
        max_iterations=config.max_tool_iterations,
        max_history=config.max_history_messages,
    )
    data_dir = config.data_dir

    def task(*, item, **kwargs):
        """跑一条 dataset case：加载日志 → 建临时会话 → agent.run → 返回回复。"""
        inp = item.input  # {"question": ..., "dataset_id": ...}
        dataset = _load_dataset(inp["dataset_id"], data_dir)
        # 每条 case 独立临时 session，不污染真实会话；eval 不归属任何真实用户
        session = session_manager.create(
            dataset_id=inp["dataset_id"], user_id=0, username="eval"
        )
        reply = asyncio.run(
            agent.run(
                session_id=session.session_id,
                user_message=inp["question"],
                dataset=dataset,
            )
        )
        return reply

    try:
        dataset = langfuse.get_dataset(dataset_name)
        result = dataset.run_experiment(
            name=run_name,
            task=task,
            evaluators=[diagnosis_accuracy],
            max_concurrency=1,
        )
        print(result.format())
    finally:
        langfuse.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Langfuse dataset 回归评测")
    parser.add_argument("--dataset", required=True, help="Langfuse dataset 名称")
    parser.add_argument(
        "--run-name", default="eval", help="本次实验 run 名称（用于 UI 对比）"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    main(args.dataset, args.run_name)
