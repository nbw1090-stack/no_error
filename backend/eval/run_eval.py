"""
BMC 诊断 agent 回归评测（确定性 + LLM 裁判）。

两种运行模式：
- 在线（Langfuse 已配置且未 --offline）：把 Langfuse Dataset 里的 case 跑过本项目
  ReAct agent，用全套 evaluator 打分并回传 Langfuse，便于在 Datasets→Runs 对比
  不同 prompt/模型版本；task 执行自动嵌套到每个 item 的 trace 下。
- 离线（--offline 或 Langfuse 未启用）：直接读本地 eval/datasets/golden_cases.json，
  逐条跑 agent + 全套 evaluator，输出本地记分卡并 dump 到 data/eval_runs/<run>.json，
  无需 Langfuse 也能出分（CI 友好）。裁判 LLM 缺失时裁判项降级跳过，确定性指标照常。

唯一硬阻塞：agent 的 LLM 未配置（无 LLM_API_KEY）——没有它 agent 产不出回复。

前置（必须在 backend/ 下、用 venv，否则 tree-sitter 缺失、源码工具不可用）：
    cd backend && source venv/bin/activate
    python -m eval.bootstrap_dataset        # 先造出固定数据集 eval_bmc_applog

运行：
    python -m eval.run_eval --offline --run-name smoke
    python -m eval.run_eval --dataset bmc-diagnosis-golden --run-name prompt-v2
"""

import argparse
import json
import logging
import os
import uuid

from config import AppConfig
from agent.llm.factory import create_llm
from agent.session import SessionManager
from agent.core import Agent
from agent.dataset import LogDataset
from observability import init_observability, get_client
import agent.tools  # noqa: F401  # 导入即触发工具注册（与 main.py 一致）

from eval.trajectory import extract_trajectory
from eval.evaluators import DETERMINISTIC_EVALUATORS, make_llm_evaluators
from eval.aio import run_coro_blocking
from eval import scorecard

logger = logging.getLogger(__name__)

_DEFAULT_CASES = os.path.join(os.path.dirname(__file__), "datasets", "golden_cases.json")
# 黄金用例默认归属用户：source/3 下已建 AST 索引，源码工具以 session.user_id 为准
_DEFAULT_USER_ID = 3


def _load_dataset(dataset_id: str, data_dir: str, user_id: int | None) -> LogDataset:
    """
    读取本地数据集 JSON 并构造 LogDataset（黄金集不校验归属）。

    user_id 必须注入（对齐 /api/chat 的构造方式）：源码/wiki 工具执行时读的是
    dataset.user_id，不注入则所有源码工具调用都报「源码索引需登录后可用」——
    历史评测（≤ v0.0.1）就踩了这个坑：源码工具 100% 报错，根因类指标失真。
    """
    path = os.path.join(data_dir, "datasets", f"{dataset_id}.json")
    with open(path) as f:
        data = json.load(f)
    return LogDataset(
        entries=data["entries"], summary=data["summary"], user_id=user_id
    )


def _make_judge_llm(config: AppConfig):
    """
    构造裁判 LLM：默认复用 agent 同款配置；可用 JUDGE_LLM_MODEL 覆盖换更便宜的裁判。
    无 key 时返回 None（裁判项降级），不阻塞确定性评测。
    """
    judge_cfg = config.llm
    override_model = os.getenv("JUDGE_LLM_MODEL")
    if override_model:
        from dataclasses import replace

        judge_cfg = replace(config.llm, model=override_model)
    return create_llm(judge_cfg)


def _make_task(
    agent: Agent,
    session_manager: SessionManager,
    data_dir: str,
    user_id_override: int | None = None,
):
    """
    构造 task 闭包：跑一条 case → {reply, trajectory, usage}。

    关键修复：临时 session 用 input.user_id（默认 3）创建，否则
    core._load_source_components 拿不到已索引组件、源码工具永不暴露，
    诊断链路退化成纯日志问答。user_id_override（--user-id）优先于用例值——
    源码索引重建到了别的账号名下时，无需改用例文件即可跑通。

    usage 由 agent.run 累加（主 Agent 的 token）；子 Agent 的 token 在
    trajectory.sub_usage_total 里，total_tokens 指标把两者相加。
    """

    def task(*, item, **kwargs):
        inp = item.input  # {"question", "dataset_id", "user_id"?}
        uid = (
            user_id_override
            if user_id_override is not None
            else inp.get("user_id", _DEFAULT_USER_ID)
        )
        dataset = _load_dataset(inp["dataset_id"], data_dir, uid)
        session = session_manager.create(
            dataset_id=inp["dataset_id"], user_id=uid, username="eval"
        )
        usage: dict = {}
        reply = run_coro_blocking(
            agent.run(
                session_id=session.session_id,
                user_message=inp["question"],
                dataset=dataset,
                usage=usage,
            )
        )
        trajectory = extract_trajectory(session_manager, session.session_id)
        return {"reply": reply, "trajectory": trajectory, "usage": usage}

    return task


class _Item:
    """离线模式下模仿 Langfuse dataset item 的最小结构（.input/.expected_output/.metadata）。"""

    def __init__(self, case: dict):
        self.input = case["input"]
        self.expected_output = case.get("expected_output")
        self.metadata = case.get("metadata") or {}


def _expand_questions(cases: list) -> list:
    """一例多问法展开：input.questions（列表）→ 多条独立 case，共享同一 expected_output。

    用途：同一标准答案下，长句/短句/模糊等多种问法都应收敛到同一诊断——既测诊断
    质量，也测意图识别的鲁棒性。向后兼容旧用例的单 input.question。展开后给每条
    打 case_id/variant_index/variant_total 标签，便于按原始用例归组、看同例不同问法
    的得分离散度。
    """
    expanded = []
    for ci, case in enumerate(cases, 1):
        inp = dict(case.get("input") or {})
        questions = inp.get("questions")
        if not questions:
            single = inp.get("question")
            questions = [single] if single else []
        case_id = (case.get("metadata") or {}).get("case_id") or f"case-{ci}"
        for vi, q in enumerate(questions):
            new_inp = dict(inp)
            new_inp.pop("questions", None)
            new_inp["question"] = q
            new_meta = dict(case.get("metadata") or {})
            new_meta.update(case_id=case_id, variant_index=vi, variant_total=len(questions))
            expanded.append({
                "input": new_inp,
                "expected_output": case.get("expected_output"),
                "metadata": new_meta,
            })
    return expanded


def _run_offline(task, evaluators, cases_path: str, run_name: str, data_dir: str) -> None:
    """
    离线：读本地用例，逐条跑 task + 全套 evaluator，出本地记分卡；并回灌 Langfuse。

    Langfuse 启用时，每条 case 包在一个已知 trace_id 的 observation 里——task 里
    agent.run 的 react/llm 子节点经 OTEL 上下文自动嵌套其下，评测分数再以 score
    attach 到**同一条** trace。session_id=eval-<run_name> 把一个 run 的所有 case
    归到一个 session，便于在 Langfuse 里按 run_name 分组、对比不同版本改进效果。
    client 未启用时 observation/score 均 no-op，纯本地出分不受影响。
    """
    with open(cases_path, "r", encoding="utf-8") as f:
        cases = _expand_questions(json.load(f))

    tracer = get_client()
    results = []
    for i, case in enumerate(cases, 1):
        item = _Item(case)
        meta = item.metadata or {}
        q = item.input.get("question", "")
        logger.info("[%d/%d] %s", i, len(cases), q[:40])

        # 用已知 trace_id 包住 task：agent 内部 span 自动嵌套，scores 落到同一 trace
        trace_id = uuid.uuid4().hex
        with tracer.observation(
            name=f"eval-offline:{meta.get('category', 'case')}",
            input={"question": q, "expected": item.expected_output},
            trace_id=trace_id,
            session_id=f"eval-{run_name}",
            user_id="eval",
            trace_metadata={
                "run_name": run_name,
                "mode": "offline",
                "category": meta.get("category", ""),
                "difficulty": meta.get("difficulty", ""),
            },
        ) as obs:
            # 单条 case 跑挂（API 4xx/超时等）不应崩掉整轮评测：捕获后记为空回复
            # 继续，让其余 case 与记分卡照常产出。异常不得穿过 observation 上下文
            # 管理器（穿过会触发 OTEL teardown 的二次异常 "generator didn't stop"）。
            try:
                output = task(item=item)
            except Exception:  # noqa: BLE001 — 逐条隔离失败
                logger.exception("[%d/%d] case 执行失败，记为空回复继续", i, len(cases))
                output = {
                    "reply": "",
                    "trajectory": {
                        "tool_calls": [], "tool_names": [], "tool_call_count": 0,
                        "iterations": 0, "duplicate_calls": 0,
                    },
                }
            obs.update(output={"reply": (output.get("reply") or "")[:2000]})

        evals = []
        for ev in evaluators:
            try:
                result = ev(
                    input=item.input,
                    output=output,
                    expected_output=item.expected_output,
                    metadata=item.metadata,
                )
                if result is not None:  # None = 本例不适用/裁判缺失，跳过
                    evals.append(result)
            except Exception:  # noqa: BLE001 — 单个 evaluator 崩不应拖垮整轮
                logger.exception("evaluator %s 执行失败", getattr(ev, "__name__", ev))

        # 数值型分数回灌到同一 trace（value=None 的"不适用/裁判缺失"跳过）
        if tracer.enabled:
            for ev in evals:
                v = getattr(ev, "value", None)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    tracer.score(
                        trace_id=trace_id,
                        name=getattr(ev, "name", "?"),
                        value=float(v),
                        comment=getattr(ev, "comment", None),
                    )

        results.append(
            {
                "input": item.input,
                "expected_output": item.expected_output,
                "metadata": item.metadata,
                "output": output,
                "evaluations": evals,
            }
        )

    tracer.flush()
    summary = scorecard.aggregate(results)
    print(scorecard.render_table(summary))
    out_path = os.path.join(data_dir, "eval_runs", f"{run_name}.json")
    scorecard.dump_json(results, summary, out_path)
    logger.info("明细已写入 %s", out_path)
    if tracer.enabled:
        logger.info("离线分数已回灌 Langfuse（session=eval-%s，可按 run_name 对比版本）", run_name)


def _run_online(task, evaluators, dataset_name: str, run_name: str) -> None:
    """在线：走 Langfuse run_experiment，分数自动回传 Datasets→Runs。"""
    langfuse = get_client()._langfuse
    dataset = langfuse.get_dataset(dataset_name)
    try:
        result = dataset.run_experiment(
            name=run_name,
            task=task,
            evaluators=evaluators,
            max_concurrency=1,  # 串行保稳；按需调大
        )
        print(result.format())
    finally:
        langfuse.shutdown()


def main(
    dataset_name: str,
    run_name: str,
    offline: bool,
    cases_path: str,
    arch: str = "direct",
    user_id: int | None = None,
) -> None:
    config = AppConfig.from_env()
    init_observability(config.langfuse)

    llm = create_llm(config.llm)
    if llm is None:
        raise SystemExit("LLM 未配置（LLM_API_KEY 缺失），agent 无法产出回复，评测中止")

    # 架构开关（--arch 优先于 AGENT_SOURCE_MODE 环境变量）：subagent 模式需要
    # 装配检索子 Agent；direct 模式装配与否无影响（retrieval 组不会被暴露）。
    from agent.tools.retrieval_tool import configure_retrieval

    configure_retrieval(
        llm,
        max_iterations=config.subagent_max_iterations,
        summary_max_chars=config.subagent_summary_max_chars,
    )
    logger.info("评测架构：source_mode=%s", arch)

    session_manager = SessionManager(os.path.join(config.data_dir, "sessions"))
    agent = Agent(
        llm=llm,
        session_manager=session_manager,
        max_iterations=config.max_tool_iterations,
        max_history=config.max_history_messages,
        context_token_budget=config.context_token_budget,
        recent_tools_keep=config.recent_tools_keep,
        source_mode=arch,
        # 上下文收缩策略跟随 AppConfig（AGENT_CONTEXT_COMPACTION 环境变量），
        # 评测无需独立 CLI 参数——A/B 对照通过环境变量切换即可。
        context_compaction=config.context_compaction,
        compaction_preserve_recent=config.compaction_preserve_recent,
    )
    task = _make_task(agent, session_manager, config.data_dir, user_id)

    judge_llm = _make_judge_llm(config)
    if judge_llm is None:
        logger.warning("裁判 LLM 不可用，LLM-as-judge 指标将降级跳过")
    evaluators = DETERMINISTIC_EVALUATORS + make_llm_evaluators(judge_llm)

    langfuse = get_client()._langfuse
    online = (not offline) and langfuse is not None
    if online:
        logger.info("在线模式：Langfuse dataset=%s run=%s", dataset_name, run_name)
        _run_online(task, evaluators, dataset_name, run_name)
    else:
        reason = "--offline" if offline else "Langfuse 未启用"
        logger.info("离线模式（%s）：本地用例=%s run=%s", reason, cases_path, run_name)
        _run_offline(task, evaluators, cases_path, run_name, config.data_dir)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BMC 诊断 agent 回归评测")
    parser.add_argument("--dataset", default="bmc-diagnosis-golden", help="Langfuse dataset 名称（在线模式）")
    parser.add_argument("--run-name", default="eval", help="本次实验 run 名称")
    parser.add_argument("--offline", action="store_true", help="强制离线：读本地 golden_cases.json，出本地记分卡")
    parser.add_argument("--cases", default=_DEFAULT_CASES, help="本地用例 JSON 路径（离线模式）")
    parser.add_argument(
        "--arch",
        choices=["direct", "subagent"],
        default=os.getenv("AGENT_SOURCE_MODE", "direct"),
        help="取证架构：direct=单 Agent 直连源码/wiki 工具；subagent=检索子 Agent 取证",
    )
    parser.add_argument(
        "--user-id",
        type=int,
        default=None,
        help="覆盖用例里的 user_id（源码索引挂在别的账号名下时用，如 --user-id 1）",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    main(args.dataset, args.run_name, args.offline, args.cases, args.arch, args.user_id)
