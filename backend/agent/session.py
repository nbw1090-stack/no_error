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

logger = logging.getLogger(__name__)


@dataclass
class Session:
    """
    会话数据模型。

    存储完整的对话消息（包括工具调用和工具返回结果），
    确保会话恢复时 LLM 能看到完整的历史上下文。
    """

    session_id: str
    dataset_id: str  # 关联的数据集 ID
    created_at: str
    updated_at: str
    messages: list[dict] = field(default_factory=list)
    # 每条消息格式：{"role": "user"|"assistant"|"tool", "content": ...,
    #                "tool_calls": [...], "tool_call_id": ...}

    @classmethod
    def create(cls, dataset_id: str) -> "Session":
        """创建新会话"""
        now = datetime.now(timezone.utc).isoformat()
        return cls(
            session_id=uuid.uuid4().hex[:12],
            dataset_id=dataset_id,
            created_at=now,
            updated_at=now,
            messages=[],
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

    def create(self, dataset_id: str) -> Session:
        """创建并持久化新会话"""
        session = Session.create(dataset_id)
        self._save(session)
        logger.info("Session created: %s -> dataset %s", session.session_id, dataset_id)
        return session

    def get(self, session_id: str) -> Session | None:
        """获取会话，不存在则返回 None"""
        path = self._path(session_id)
        if not os.path.exists(path):
            return None

        try:
            with open(path, "r") as f:
                data = json.load(f)
            return Session(**data)
        except (json.JSONDecodeError, TypeError) as e:
            logger.error("Failed to load session %s: %s", session_id, e)
            return None

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

    def list_all(self) -> list[dict]:
        """
        列出所有会话（仅元数据，不含消息体）。

        按更新时间降序排列。
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
                    sessions.append(
                        {
                            "session_id": data["session_id"],
                            "dataset_id": data["dataset_id"],
                            "created_at": data["created_at"],
                            "updated_at": data["updated_at"],
                            "message_count": len(data.get("messages", [])),
                        }
                    )
                except (json.JSONDecodeError, KeyError):
                    logger.warning("Skipping corrupt session file: %s", fname)
        except OSError:
            pass

        return sorted(sessions, key=lambda s: s["updated_at"], reverse=True)

    def delete(self, session_id: str) -> bool:
        """删除会话，返回是否成功"""
        path = self._path(session_id)
        if os.path.exists(path):
            os.remove(path)
            logger.info("Session deleted: %s", session_id)
            return True
        return False

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
        }
        with open(path, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
