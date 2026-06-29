"""
LLM Wiki A/B 评测：同一组 openUBMC 论坛问答，分别在「不使用 wiki」与「使用 wiki」两种
配置下跑同一个 ReAct agent，对比答案质量。

- 不使用 wiki：把 core._wiki_available 打补丁返回 False → 不暴露 wiki 工具，agent 仅凭
  LLM 参数化知识作答（dataset=None、user_id=0 → 无 log/source 工具，纯净对照）。
- 使用 wiki：core._wiki_available 返回真实值（需先编译 wiki）→ 暴露 get_wiki_index /
  read_wiki_page / search_wiki，agent 可读知识库再答。

打分：
- kw：答案命中 expected_output.answer_keywords 的比例（确定性，子串不区分大小写）。
- judge：LLM 裁判对照 reference_answer 给 0-5 正确性分（归一到 0-1）。
另记录每问的工具调用、ReAct 轮数、token。

用法（backend/，venv）：
    venv/bin/python -m eval.wiki_ab_eval --cases eval/datasets/forum_cases.json --limit 0
"""
import argparse, asyncio, json, logging, os, re, time
from dataclasses import replace

from config import AppConfig
from agent.llm.factory import create_llm
from agent.session import SessionManager
from agent.core import Agent
import agent.core as core
import agent.tools  # noqa: F401 触发工具注册

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("wiki_ab")
log.setLevel(logging.INFO)

WIKI_TOOLS = {"get_wiki_index", "read_wiki_page", "search_wiki"}


def _kw_coverage(reply: str, keywords) -> float:
    if not keywords:
        return 0.0
    low = (reply or "").lower()
    hit = sum(1 for k in keywords if str(k).lower() in low)
    return hit / len(keywords)


def _inspect_session(session) -> dict:
    """从会话消息里抽取：assistant 轮数、调用过的工具、是否用了 wiki。"""
    msgs = session.messages if session else []
    assistant_turns, tools = 0, []
    for m in msgs:
        if m.get("role") == "assistant":
            assistant_turns += 1
            for tc in (m.get("tool_calls") or []):
                tools.append((tc.get("function") or {}).get("name", "?"))
    return {
        "assistant_turns": assistant_turns,
        "tools_called": tools,
        "wiki_used": any(t in WIKI_TOOLS for t in tools),
    }


async def _run_one(agent, sm, q: str, uid: int) -> dict:
    sess = sm.create(dataset_id="wiki-eval", user_id=uid, username="eval-wiki")
    usage = {}
    t0 = time.time()
    try:
        reply = await agent.run(
            session_id=sess.session_id, user_message=q, dataset=None, usage=usage
        )
    except Exception as e:  # noqa: BLE001
        return {"reply": f"[ERROR] {e}", "error": str(e), "ms": int((time.time()-t0)*1000),
                "assistant_turns": 0, "tools_called": [], "wiki_used": False, "usage": usage}
    info = _inspect_session(sm.get(sess.session_id))
    info.update(reply=reply, ms=int((time.time()-t0)*1000), usage=usage)
    return info


async def _run_phase(agent, sm, cases, uid, wiki_on: bool, concurrency: int) -> list:
    """整相切换 wiki 可用性（全局补丁），相内并发跑所有问题。"""
    async def fixed():  # 覆写 core._wiki_available
        return wiki_on
    core._wiki_available = fixed  # type: ignore
    sem = asyncio.Semaphore(concurrency)
    async def guarded(c):
        async with sem:
            r = await _run_one(agent, sm, c["input"]["question"], uid)
            log.info("[%s] %s turns=%d wiki=%s",
                     "ON " if wiki_on else "OFF", c["metadata"]["topic_id"],
                     r["assistant_turns"], r["wiki_used"])
            return r
    return await asyncio.gather(*[guarded(c) for c in cases])


_JUDGE_SYS = (
    "你是 openUBMC（BMC 固件）领域的严格评审。给你一个【问题】、一个【参考答案】（来自社区"
    "采纳回复，视为正确）、以及一个【待评答案】。只评待评答案在技术上与参考答案的一致性/正确性，"
    "忽略措辞与篇幅。按 0-5 打分：5=关键结论与参考一致且无明显错误；3=部分正确或方向对但缺关键点；"
    "0=错误/答非所问/未给出实质方法。只输出 JSON：{\"score\": <0-5 整数>, \"reason\": \"<20字内>\"}。"
)


async def _judge(judge_llm, q: str, ref: str, cand: str) -> dict:
    user = f"【问题】{q}\n\n【参考答案】{ref}\n\n【待评答案】{cand}"
    try:
        resp = await judge_llm.chat(
            [{"role": "system", "content": _JUDGE_SYS}, {"role": "user", "content": user}]
        )
        txt = (resp.get("content") or "").strip()
        m = re.search(r"\{.*\}", txt, re.DOTALL)
        obj = json.loads(m.group(0)) if m else {}
        s = float(obj.get("score", 0))
        return {"score": max(0.0, min(5.0, s)) / 5.0, "raw": obj.get("score"), "reason": obj.get("reason", "")}
    except Exception as e:  # noqa: BLE001
        return {"score": None, "raw": None, "reason": f"judge-err:{e}"}


async def main_async(args):
    cfg = AppConfig.from_env()
    with open(args.cases, encoding="utf-8") as f:
        cases = json.load(f)
    if args.limit:
        cases = cases[: args.limit]

    llm = create_llm(cfg.llm)
    assert llm is not None, "LLM 未配置"
    judge_llm = create_llm(replace(cfg.llm, temperature=0.0))
    sm = SessionManager(os.path.join(cfg.data_dir, "sessions"))
    agent = Agent(llm=llm, session_manager=sm,
                  max_iterations=cfg.max_tool_iterations, max_history=cfg.max_history_messages)
    uid = 0  # 无 source 组件，隔离出 wiki 的纯效果

    from wiki import store as wiki_store
    log.info("wiki indexed=%s pages=%d", wiki_store.is_indexed(), len(wiki_store.list_pages()))

    log.info("=== 阶段 A：不使用 wiki ===")
    off = await _run_phase(agent, sm, cases, uid, wiki_on=False, concurrency=args.concurrency)
    log.info("=== 阶段 B：使用 wiki ===")
    on = await _run_phase(agent, sm, cases, uid, wiki_on=True, concurrency=args.concurrency)

    log.info("=== 阶段 C：LLM 裁判 ===")
    jsem = asyncio.Semaphore(args.concurrency)
    async def jrun(q, ref, cand):
        async with jsem:
            return await _judge(judge_llm, q, ref, cand)
    off_j = await asyncio.gather(*[jrun(c["input"]["question"], c["expected_output"]["reference_answer"], r["reply"]) for c, r in zip(cases, off)])
    on_j = await asyncio.gather(*[jrun(c["input"]["question"], c["expected_output"]["reference_answer"], r["reply"]) for c, r in zip(cases, on)])

    rows = []
    for c, ro, rn, jo, jn in zip(cases, off, on, off_j, on_j):
        kws = c["expected_output"]["answer_keywords"]
        rows.append({
            "topic_id": c["metadata"]["topic_id"],
            "category": c["metadata"]["category"],
            "difficulty": c["metadata"]["difficulty"],
            "question": c["input"]["question"],
            "off": {"reply": ro["reply"], "kw": _kw_coverage(ro["reply"], kws),
                    "judge": jo["score"], "judge_raw": jo["raw"], "turns": ro["assistant_turns"],
                    "tools": ro["tools_called"], "tokens": ro["usage"].get("total")},
            "on": {"reply": rn["reply"], "kw": _kw_coverage(rn["reply"], kws),
                   "judge": jn["score"], "judge_raw": jn["raw"], "turns": rn["assistant_turns"],
                   "tools": rn["tools_called"], "wiki_used": rn["wiki_used"], "tokens": rn["usage"].get("total")},
        })

    out_path = os.path.join(cfg.data_dir, "eval_runs", f"{args.run_name}.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"cases": rows}, f, ensure_ascii=False, indent=2)
    print("\n明细写入:", out_path)
    _report(rows)


def _avg(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return sum(xs) / len(xs) if xs else float("nan")


def _report(rows):
    def col(mode, key): return _avg([r[mode][key] for r in rows])
    n = len(rows)
    print("\n" + "=" * 64)
    print(f"LLM Wiki A/B 评测结果  (n={n} 论坛问答)")
    print("=" * 64)
    print(f"{'指标':<22}{'不用wiki':>12}{'用wiki':>12}{'Δ':>12}")
    for label, key in [("关键词覆盖率", "kw"), ("裁判分(0-1)", "judge")]:
        a, b = col("off", key), col("on", key)
        print(f"{label:<24}{a:>11.3f}{b:>12.3f}{b-a:>+12.3f}")
    print(f"{'平均 ReAct 轮数':<22}{col('off','turns'):>11.2f}{col('on','turns'):>12.2f}")
    print(f"{'平均 token/问':<23}{col('off','tokens'):>11.0f}{col('on','tokens'):>12.0f}")
    wiki_used = sum(1 for r in rows if r["on"].get("wiki_used"))
    print(f"\nON 模式实际调用 wiki 工具: {wiki_used}/{n} ({wiki_used/n*100:.0f}%)")
    # 提升/持平/退步计数（裁判分）
    up = sum(1 for r in rows if (r['on']['judge'] or 0) > (r['off']['judge'] or 0) + 1e-9)
    dn = sum(1 for r in rows if (r['on']['judge'] or 0) < (r['off']['judge'] or 0) - 1e-9)
    print(f"裁判分 用wiki更好: {up}  更差: {dn}  持平: {n-up-dn}")
    # 分类别
    cats = {}
    for r in rows:
        cats.setdefault(r["category"], []).append(r)
    print(f"\n{'按类别(裁判分)':<22}{'n':>4}{'不用':>10}{'用wiki':>10}{'Δ':>10}")
    for cat, rs in sorted(cats.items(), key=lambda kv: -len(kv[1])):
        a = _avg([x["off"]["judge"] for x in rs]); b = _avg([x["on"]["judge"] for x in rs])
        print(f"{cat:<24}{len(rs):>4}{a:>10.2f}{b:>10.2f}{b-a:>+10.2f}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--cases", default="eval/datasets/forum_cases.json")
    p.add_argument("--limit", type=int, default=0, help="只跑前 N 条，0=全部")
    p.add_argument("--concurrency", type=int, default=6)
    p.add_argument("--run-name", default="wiki_ab")
    args = p.parse_args()
    asyncio.run(main_async(args))
