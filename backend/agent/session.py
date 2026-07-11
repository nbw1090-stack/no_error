"""
会话管理

提供会话数据模型和基于文件的持久化存储。
每个会话存储为独立的 JSON 文件，支持隔离和恢复。
"""

import json
import os
import uuid
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Session:
    """
    会话数据模型。

    存储完整的对话消息（包括工具调用和工具返回结果），
    确保会话恢复时 LLM 能看到完整的历史上下文。

    user_id / username 记录会话归属，实现用户隔离：list_all/get/delete
    可按 user_id 过滤，互不可见。两者给默认值以容忍历史 session JSON
    （无归属字段）；归属过滤时真实 id≠0 会自然排除这些孤儿会话。
    """

    session_id: str
    dataset_id: Optional[str]  # 关联的数据集 ID（None = 尚未上传日志的纯对话会话）
    created_at: str
    updated_at: str
    messages: list[dict] = field(default_factory=list)
    user_id: int = 0  # 归属用户 ID（0 表示无归属 / 历史数据）
    username: str = ""  # 归属用户名（便于排查，不参与隔离判定）
    # 每轮对话的 Langfuse trace 元数据（独立于 messages，不进回放给 LLM 的历史）。
    # 用于本地对账：trace_id 在此但 Langfuse 搜不到 = 未上报（disabled 或丢失）。
    traces: list[dict] = field(default_factory=list)
    # 上下文压缩状态（claw-code 风格 compaction，见 agent/compaction.py）：
    # {"upto": int, "summary": str, "count": int} —— messages[:upto] 已被折叠为
    # summary，发给 LLM 的历史 = [摘要消息] + messages[upto:]。默认 None 向后
    # 兼容旧 session 文件（无此字段）。messages 本身**永远 append-only 不改写**
    # （前端聊天记录展示依赖完整原文），压缩只改变"发给 LLM 看什么"。
    compaction: Optional[dict] = None
    # 每条消息格式：{"role": "user"|"assistant"|"tool", "content": ...,
    #                "tool_calls": [...], "tool_call_id": ...}

    @classmethod
    def create(
        cls,
        dataset_id: Optional[str] = None,
        user_id: int = 0,
        username: str = "",
    ) -> "Session":
        """创建新会话（dataset_id 可空：空数据集的纯对话会话）"""
        now = datetime.now(timezone.utc).isoformat()
        return cls(
            session_id=uuid.uuid4().hex[:12],
            dataset_id=dataset_id,
            created_at=now,
            updated_at=now,
            messages=[],
            user_id=user_id,
            username=username,
        )


class SessionManager:
    """
    会话管理器 —— 处理会话的 CRUD 和文件持久化。

    存储位置：{storage_dir}/{session_id}.json
    """

    def __init__(self, storage_dir: str):
        self.storage_dir = storage_dir
        os.makedirs(storage_dir, exist_ok=True)
        logger.info("SessionManager initialized: %s", storage_dir)

    # ---- 路径工具 ----

    def _path(self, session_id: str) -> str:
        """获取会话文件路径"""
        return os.path.join(self.storage_dir, f"{session_id}.json")

    # ---- CRUD ----

    def create(
        self,
        dataset_id: Optional[str] = None,
        user_id: int = 0,
        username: str = "",
    ) -> Session:
        """创建并持久化新会话（归属 user_id / username）"""
        session = Session.create(dataset_id, user_id, username)
        self._save(session)
        logger.info(
            "Session created: %s -> dataset %s (user=%s/%s)",
            session.session_id,
            dataset_id,
            user_id,
            username,
        )
        return session

    def link_dataset(
        self, session_id: str, dataset_id: str, user_id: int | None = None
    ) -> Session | None:
        """
        把数据集绑定到已有会话（用于「先对话后上传」的升级场景）。

        会话归属校验失败（不存在 / 非该用户）时返回 None（与 get 一致，
        不泄漏其他用户的会话存在性）。绑定后落盘，保持 1:1 不变式。

        注意：调用方应自行校验 dataset_id 归属（见 main._load_dataset_owned）。
        """
        session = self.get(session_id, user_id)
        if session is None:
            return None
        session.dataset_id = dataset_id
        session.updated_at = datetime.now(timezone.utc).isoformat()
        self._save(session)
        logger.info(
            "Session linked: %s -> dataset %s (user=%s)",
            session_id,
            dataset_id,
            user_id,
        )
        return session

    def get(
        self, session_id: str, user_id: int | None = None
    ) -> Session | None:
        """
        获取会话，不存在则返回 None。

        传入 user_id 时进行归属校验：会话不属于该用户则返回 None
        （对外与「不存在」无差异，避免泄漏其他用户的会话存在性）。
        """
        path = self._path(session_id)
        if not os.path.exists(path):
            return None

        try:
            with open(path, "r") as f:
                data = json.load(f)
            session = Session(**data)
        except (json.JSONDecodeError, TypeError) as e:
            logger.error("Failed to load session %s: %s", session_id, e)
            return None

        if user_id is not None and session.user_id != user_id:
            return None
        return session

    def add_message(self, session_id: str, message: dict) -> Session:
        """
        向会话追加一条消息并持久化。

        Args:
            session_id: 会话 ID
            message: 消息字典

        Returns:
            更新后的 Session

        Raises:
            ValueError: 会话不存在
        """
        session = self.get(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")

        session.messages.append(message)
        session.updated_at = datetime.now(timezone.utc).isoformat()
        self._save(session)
        return session

    def update_compaction(self, session_id: str, compaction: dict) -> Session:
        """
        更新会话的上下文压缩状态并持久化。

        由 Agent 在 ConversationContext 触发新压缩后调用；messages 不受影响
        （append-only），只更新 compaction 元数据。触发之间该状态冻结不变，
        保证发给 LLM 的前缀逐字节稳定（前缀缓存命中的前提）。

        Raises:
            ValueError: 会话不存在（与 add_message 行为一致）
        """
        session = self.get(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")

        session.compaction = compaction
        session.updated_at = datetime.now(timezone.utc).isoformat()
        self._save(session)
        return session

    def record_trace(
        self, session_id: str, trace_id: str, name: str, user_id: int | None = None
    ) -> Session | None:
        """
        记录一轮对话的 Langfuse trace id（独立于 messages，不进 LLM 历史）。

        name 形如 "chat" / "chat-stream" / "chat-fallback"，区分上报路径。
        归属校验失败返回 None（与 get 一致，不泄漏其他用户会话）。
        """
        session = self.get(session_id, user_id)
        if session is None:
            return None
        session.traces.append(
            {
                "trace_id": trace_id,
                "name": name,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        session.updated_at = datetime.now(timezone.utc).isoformat()
        self._save(session)
        return session

    def list_all(self, user_id: int | None = None) -> list[dict]:
        """
        列出会话（仅元数据，不含消息体），按更新时间降序。

        传入 user_id 时只返回该用户的会话（用户隔离）。
        """
        sessions = []
        try:
            for fname in os.listdir(self.storage_dir):
                if not fname.endswith(".json"):
                    continue
                path = os.path.join(self.storage_dir, fname)
                try:
                    with open(path, "r") as f:
                        data = json.load(f)
                    # 用户隔离：归属不符则跳过（历史无归属文件 user_id 默认 0）
                    if user_id is not None and data.get("user_id", 0) != user_id:
                        continue
                    # 提取首条用户消息的前15个字符作为标题
                    messages = data.get("messages", [])
                    title = ""
                    for msg in messages:
                        if msg.get("role") == "user" and msg.get("content"):
                            title = msg["content"][:15]
                            break

                    sessions.append(
                        {
                            "session_id": data["session_id"],
                            "dataset_id": data["dataset_id"],
                            "created_at": data["created_at"],
                            "updated_at": data["updated_at"],
                            "message_count": len(messages),
                            "title": title,
                        }
                    )
                except (json.JSONDecodeError, KeyError):
                    logger.warning("Skipping corrupt session file: %s", fname)
        except OSError:
            pass

        return sorted(sessions, key=lambda s: s["updated_at"], reverse=True)

    def delete(self, session_id: str, user_id: int | None = None) -> bool:
        """
        删除会话，返回是否成功。

        传入 user_id 时进行归属校验：不属于该用户返回 False
        （与「不存在」无差异，避免泄漏）。
        """
        path = self._path(session_id)
        if not os.path.exists(path):
            return False
        if user_id is not None:
            session = self.get(session_id)
            if session is None or session.user_id != user_id:
                return False
        os.remove(path)
        logger.info("Session deleted: %s", session_id)
        return True

    # ---- 内部 ----

    def _save(self, session: Session) -> None:
        """将会话写入 JSON 文件"""
        path = self._path(session.session_id)
        # 使用 dataclass 字段序列化
        data = {
            "session_id": session.session_id,
            "dataset_id": session.dataset_id,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "messages": session.messages,
            "user_id": session.user_id,
            "username": session.username,
            "traces": session.traces,
            "compaction": session.compaction,
        }
        with open(path, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
