"""
对话上下文管理

负责构建发送给 LLM 的消息列表，支持两种超预算收缩策略（AGENT_CONTEXT_COMPACTION）：

1. **compact（默认）—— claw-code 风格上下文压缩**（build_messages_compacted）：
   - 超 token 预算时**一次性**把旧历史折叠成确定性模板摘要（见 compaction.py），
     分割点 upto 与摘要持久化在 Session.compaction 上；
   - 触发之间 upto/摘要**冻结**，发给 LLM 的前缀逐字节稳定 → DeepSeek 前缀
     缓存可持续命中（旧机制"每轮多省一条"逐轮改写前缀，实测命中率仅 ~20%）；
   - 摘要保留待办/文件/时间线等要点，而非丢弃式占位符——难 case 里模型不再
     因证据被抹掉而反复重查。

2. **elide（回退）—— 陈旧工具结果省略**（build_messages / _elide_stale_tool_results）：
   - 超预算时把「最近 N 条之外」的工具结果 content 替换为短占位符
     （保留 role/tool_call_id，结构合法、可追溯——原文仍在 session 存档里）；
   - 保留此路径用于 A/B 对照与紧急回退（AGENT_CONTEXT_COMPACTION=elide）。
"""

import logging

from agent.compaction import (
    build_compaction_message,
    merge_compact_summaries,
    safe_split_point,
    summarize_messages,
)

logger = logging.getLogger(__name__)

# 被省略的旧工具结果在发给 LLM 时的占位文本（原文仍存于 session 文件，可追溯）。
ELIDED_TOOL_PLACEHOLDER = (
    "[旧工具结果已省略以节省上下文——其要点已并入后续分析；"
    "如确需原始数据，请用更精确的参数重新调用对应工具]"
)

# 压缩的「低水位」：触发时把尾部一次压到预算 × 此比例以下，给后续轮次留出
# 增长空间——若只压到刚好贴预算，下一轮新增即再触发、前缀又被改写（churn），
# 冻结就名存实亡（v0.0.3 实测：贴线触发时 10 轮压 3 次，命中率掉回 13%）。
COMPACTION_LOW_WATER_RATIO = 0.6
# 「显著性护栏」：低水位达不到时（巨型工具结果仍在最少保留窗口内），只有本次
# 推进能砍掉 ≥ 此比例的尾部 token 才值得改写一次前缀；否则保持冻结，等巨型
# 消息滑出保留窗口后一次扫进摘要——避免为省 2% 的 token 付一次全量重算。
_MIN_SWEEP_RATIO = 0.3


def estimate_tokens(obj) -> int:
    """
    粗略估算一段内容的 token 数。

    混合中英文 + JSON 经验值约 3 字符/token；只用于相对比较与预算控制，
    不要求与计费 token 精确一致（精确值以 API usage 为准）。
    """
    if obj is None:
        return 0
    if isinstance(obj, str):
        return len(obj) // 3
    # dict / list：按其 JSON 文本长度估算
    import json

    try:
        return len(json.dumps(obj, ensure_ascii=False)) // 3
    except (TypeError, ValueError):
        return len(str(obj)) // 3


def _message_tokens(msg: dict) -> int:
    """估算单条消息（含 content 与 tool_calls）的 token 数。"""
    total = estimate_tokens(msg.get("content"))
    if msg.get("tool_calls"):
        total += estimate_tokens(msg["tool_calls"])
    return total


class ConversationContext:
    """
    对话上下文构建器。

    职责：
    1. 注入系统提示词
    2. 按条数裁剪历史（防御性上限）
    3. 超 token 预算时省略陈旧工具结果
    4. 组装完整的消息列表供 LLM 消费
    """

    def __init__(
        self,
        system_prompt: str,
        max_history: int = 40,
        token_budget: int = 24000,
        recent_tools_keep: int = 3,
        compaction_mode: str = "compact",
        preserve_recent_messages: int = 4,
    ):
        """
        Args:
            system_prompt: 系统提示词
            max_history: 最大保留的历史消息数（防御性上限，默认 40）。
                compact 模式下不做滑动窗口裁剪（会破坏前缀稳定），改为
                「尾部条数超限也触发压缩」来兜同一个底。
            token_budget: 历史 token 软预算；超过才收缩（压缩或省略）。
                <=0 表示禁用按 token 收缩。
            recent_tools_keep: elide 模式下保留多少条最近的工具结果不动。
            compaction_mode: "compact"（claw-code 风格压缩，默认）或
                "elide"（回退到旧的陈旧工具结果省略）。
            preserve_recent_messages: compact 模式下压缩**最少**保留的最近
                消息条数（分割点的上界 = len(messages) - 该值，再做边界回退）。
                实际保留量通常更多：分割点只推进到「尾部估算 ≤ 低水位」即停，
                保住尽量多的近期上下文。
        """
        self.system_prompt = system_prompt
        self.max_history = max_history
        self.token_budget = token_budget
        self.recent_tools_keep = max(0, recent_tools_keep)
        self.compaction_mode = (
            compaction_mode if compaction_mode in ("compact", "elide") else "compact"
        )
        # 至少保留 1 条：分割点必须真在消息区间内，不允许"把全部历史压掉"
        self.preserve_recent_messages = max(1, preserve_recent_messages)

    def build_messages(
        self,
        history: list[dict],
        user_message: str | None = None,
    ) -> list[dict]:
        """
        构建完整的消息列表。

        顺序：系统提示词 → 裁剪并（必要时）省略后的历史 → 当前用户消息（如有）

        Args:
            history: 会话中的历史消息列表
            user_message: 当前用户消息（首次调用时提供，后续 ReAct 循环中为 None）

        Returns:
            标准化的消息列表（system 在首位，工具/助手配对结构保持合法）
        """
        # 1) 按最大条数裁剪历史（保留最近的），作为防御性上限
        if len(history) > self.max_history:
            logger.debug(
                "Trimming history from %d to %d messages",
                len(history),
                self.max_history,
            )
            trimmed = history[-self.max_history :]
            # 条数裁剪可能从一组 assistant(tool_calls) → tool 结果 中间切开，使
            # 裁剪后历史以「孤儿 tool 消息」开头（其对应的 tool_calls 已被切掉）。
            # OpenAI/DeepSeek 要求 tool 消息必须紧跟带 tool_calls 的 assistant，
            # 否则报 400（"Messages with role 'tool' must be a response to a
            # preceding message with 'tool_calls'"）。丢弃开头的孤儿 tool 消息。
            drop = 0
            while drop < len(trimmed) and trimmed[drop].get("role") == "tool":
                drop += 1
            if drop:
                logger.debug("Dropping %d leading orphan tool message(s)", drop)
                trimmed = trimmed[drop:]
        else:
            trimmed = history

        # 2) 超 token 预算时，省略陈旧工具结果（保留最近 N 条工具结果完整）
        trimmed = self._elide_stale_tool_results(trimmed)

        messages = [{"role": "system", "content": self.system_prompt}]
        messages.extend(trimmed)

        # 3) 添加当前用户消息（仅首轮）
        if user_message:
            messages.append({"role": "user", "content": user_message})

        return messages

    def _elide_stale_tool_results(self, history: list[dict]) -> list[dict]:
        """
        当历史 token 估算超过预算时，把「最近 recent_tools_keep 条之外」的工具
        结果 content 替换为短占位符。

        - 仅作用于 role == "tool" 的消息；保留 tool_call_id（结构合法）。
        - 从最旧的工具结果开始省略，直到降到预算内或只剩最近 N 条。
        - 不删除任何消息，因此 assistant(tool_calls) ↔ tool 的配对始终成立。
        - 预算 <=0 或未超预算时原样返回（保持前缀稳定，吃满前缀缓存）。
        """
        if self.token_budget <= 0:
            return history

        total = sum(_message_tokens(m) for m in history)
        if total <= self.token_budget:
            return history

        # 找出所有工具结果消息的下标（按出现顺序）
        tool_idxs = [
            i for i, m in enumerate(history) if m.get("role") == "tool"
        ]
        # 最近 N 条工具结果保留完整，只省略更早的（最旧优先）
        elidable = tool_idxs[: len(tool_idxs) - self.recent_tools_keep]
        if not elidable:
            return history

        placeholder_tokens = estimate_tokens(ELIDED_TOOL_PLACEHOLDER)
        result = list(history)
        elided_count = 0
        for i in elidable:
            if total <= self.token_budget:
                break
            original = result[i].get("content")
            saved = estimate_tokens(original) - placeholder_tokens
            if saved <= 0:
                continue  # 本就很短，省不出 token，跳过
            new_msg = dict(result[i])
            new_msg["content"] = ELIDED_TOOL_PLACEHOLDER
            result[i] = new_msg
            total -= saved
            elided_count += 1

        if elided_count:
            logger.debug(
                "Elided %d stale tool results; est tokens now ~%d (budget %d)",
                elided_count,
                total,
                self.token_budget,
            )
        return result

    # ============================================================
    # claw-code 风格压缩（compact 模式）
    # ============================================================

    def build_messages_compacted(
        self,
        session_messages: list[dict],
        compaction: dict | None = None,
        user_message: str | None = None,
    ) -> tuple[list[dict], dict | None]:
        """
        压缩感知的消息构建（compact 模式入口）。

        发给 LLM 的历史 = [摘要 system 消息] + session_messages[upto:]（有
        compaction 时）；否则为全量历史。构建后估算 token，仅在超预算
        （或尾部条数超 max_history 的防御场景）时触发**一次**新压缩并返回
        新的 compaction 状态——由调用方（Agent）持久化到 Session。

        冻结不变式（本机制的目的）：两次触发之间 upto/摘要不变，且
        session_messages 本身 append-only，因此连续构建之间除了末尾新增
        消息，前缀序列化后**逐字节相同**——DeepSeek 前缀缓存持续命中。

        Args:
            session_messages: Session.messages（append-only 的完整存档）
            compaction: Session.compaction（{"upto","summary","count"} 或 None）
            user_message: 当前用户消息（与 build_messages 语义一致，可省）

        Returns:
            (messages, new_compaction)：new_compaction 为 None 表示未触发新
            压缩（沿用旧状态）；非 None 时 messages 已按新状态重建。
        """
        messages = self._assemble_compacted(
            session_messages, compaction, user_message
        )

        # 触发判断：预算作用于历史部分（含摘要消息，不含主 system prompt，
        # 与 elide 模式的口径一致）。compact 模式不做滑动窗口条数裁剪
        # （那正是破坏前缀稳定的根源），max_history 改为条数触发压缩兜底。
        history_part = messages[1:]
        total = sum(_message_tokens(m) for m in history_part)
        over_budget = self.token_budget > 0 and total > self.token_budget
        over_count = len(history_part) > self.max_history
        if not (over_budget or over_count):
            return messages, None

        new_compaction = self._compute_compaction(
            session_messages, compaction, over_count=over_count
        )
        if new_compaction is None:
            # 无法推进分割点（新消息不足 / 边界回退退无可退），或推进收益
            # 不显著（巨型消息仍在保留窗口内）→ 保持冻结，宁可暂时超预算
            # 也不改写前缀
            return messages, None

        logger.info(
            "Context compacted: upto %d -> %d, count=%d, est tokens ~%d (budget %d)",
            (compaction or {}).get("upto", 0),
            new_compaction["upto"],
            new_compaction["count"],
            total,
            self.token_budget,
        )
        return (
            self._assemble_compacted(session_messages, new_compaction, user_message),
            new_compaction,
        )

    def _assemble_compacted(
        self,
        session_messages: list[dict],
        compaction: dict | None,
        user_message: str | None,
    ) -> list[dict]:
        """按给定压缩状态组装消息：system → [摘要] → 保留的历史 → [user]。"""
        messages = [{"role": "system", "content": self.system_prompt}]
        if compaction:
            messages.append(build_compaction_message(compaction["summary"]))
            messages.extend(session_messages[compaction["upto"]:])
        else:
            messages.extend(session_messages)
        if user_message:
            messages.append({"role": "user", "content": user_message})
        return messages

    def _compute_compaction(
        self,
        session_messages: list[dict],
        compaction: dict | None,
        over_count: bool = False,
    ) -> dict | None:
        """
        计算一次新压缩（低水位跳变 + 显著性护栏）。

        分割点选择——三种情形：
        1. 条数兜底（over_count）：一次推进到上界（只剩 preserve_recent 条），
           给条数留出最大增长空间；
        2. 超 token 预算：从旧分割点向后找**最小推进量**，使尾部估算 ≤
           预算 × COMPACTION_LOW_WATER_RATIO 即停——既保住尽量多的近期上下文，
           又留出增长空间避免下一轮又贴线触发（churn）；
        3. 低水位达不到（巨型消息仍在最少保留窗口内）：只有推进到上界能砍掉
           ≥ _MIN_SWEEP_RATIO 的尾部 token 才压缩，否则返回 None 冻结——
           为省一点点 token 改写前缀是净亏损（v0.0.3 churn 教训）。

        上界 = len - preserve_recent 经边界安全回退（分割点不能落在 tool 消息
        上，否则与其 assistant(tool_calls) 拆散、DeepSeek 直接 400）；下界为
        旧 upto（不能倒退进已压缩区）。返回 None 表示本次不压缩。
        """
        old_upto = compaction["upto"] if compaction else 0
        n = len(session_messages)
        max_upto = safe_split_point(
            session_messages, n - self.preserve_recent_messages, lower=old_upto
        )
        if max_upto <= old_upto or max_upto <= 0:
            return None

        if over_count or self.token_budget <= 0:
            # 条数兜底（或按 token 收缩被禁用）：直接推进到上界
            upto = max_upto
        else:
            # 尾部 token 后缀和：tail_tokens[i] = messages[i:] 的估算 token
            tail_tokens = [0] * (n + 1)
            for i in range(n - 1, -1, -1):
                tail_tokens[i] = tail_tokens[i + 1] + _message_tokens(
                    session_messages[i]
                )
            low_water = int(self.token_budget * COMPACTION_LOW_WATER_RATIO)

            upto = None
            for cand in range(old_upto + 1, max_upto + 1):
                # 落在 tool 消息上的候选点经回退等价于更小的候选（已试过），跳过
                if safe_split_point(session_messages, cand, lower=old_upto) != cand:
                    continue
                if tail_tokens[cand] <= low_water:
                    upto = cand
                    break
            if upto is None:
                # 推进到上界也到不了低水位 → 显著性护栏：砍不掉 30% 就冻结
                saved = tail_tokens[old_upto] - tail_tokens[max_upto]
                if tail_tokens[old_upto] <= 0 or (
                    saved / tail_tokens[old_upto] < _MIN_SWEEP_RATIO
                ):
                    return None
                upto = max_upto

        new_part = summarize_messages(session_messages[old_upto:upto])
        merged = merge_compact_summaries(
            compaction["summary"] if compaction else None, new_part
        )
        return {
            "upto": upto,
            "summary": merged,
            "count": (compaction["count"] + 1) if compaction else 1,
        }
