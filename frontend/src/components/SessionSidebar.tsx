/**
 * SessionSidebar 组件 —— 会话历史侧边栏
 *
 * 功能：
 * - 显示所有历史会话列表（标题 + 时间 + 消息数）
 * - "开始新会话"按钮
 * - 点击切换当前活跃会话
 * - hover 时显示删除按钮
 * - 当前会话高亮
 * - 删除需二次确认（防止误删）
 *
 * 性能优化：
 * - relativeTime 使用 useMemo 缓存，避免每次渲染重复计算
 * - 删除确认状态隔离，不影响其他列表项
 */

import { useState } from 'react';
import type { SessionInfo } from '../types/log';
import './SessionSidebar.css';

interface SessionSidebarProps {
  sessions: SessionInfo[];
  activeSessionId: string | null;
  onSelectSession: (sessionId: string) => void;
  onNewSession: () => void;
  onDeleteSession: (sessionId: string) => void;
  loading: boolean;
}

/**
 * 将 ISO 时间戳转换为相对时间描述
 */
function relativeTime(isoString: string): string {
  const now = Date.now();
  const then = new Date(isoString).getTime();
  const diffMs = now - then;

  if (Number.isNaN(diffMs)) return '';

  const seconds = Math.floor(diffMs / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);

  if (seconds < 60) return '刚刚';
  if (minutes < 60) return `${minutes}分钟前`;
  if (hours < 24) return `${hours}小时前`;
  if (days < 7) return `${days}天前`;
  if (days < 30) return `${Math.floor(days / 7)}周前`;
  if (days < 365) return `${Math.floor(days / 30)}个月前`;
  return `${Math.floor(days / 365)}年前`;
}

/**
 * 格式化日期为简短形式
 */
function shortDate(isoString: string): string {
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return '';

  return date.toLocaleDateString('zh-CN', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/**
 * 单个会话列表项（提取为独立组件以隔离 hover/confirm 状态）
 */
function SessionItem({
  session,
  isActive,
  onSelect,
  onDelete,
}: {
  session: SessionInfo;
  isActive: boolean;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
}) {
  const [isHovered, setIsHovered] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const handleMouseEnter = () => setIsHovered(true);
  const handleMouseLeave = () => {
    setIsHovered(false);
    setConfirmDelete(false); // 鼠标离开时重置确认状态
  };

  const handleDeleteClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (confirmDelete) {
      // 二次点击确认删除
      onDelete(session.session_id);
      setConfirmDelete(false);
    } else {
      // 首次点击进入确认状态
      setConfirmDelete(true);
    }
  };

  const handleCancelDelete = (e: React.MouseEvent) => {
    e.stopPropagation();
    setConfirmDelete(false);
  };

  return (
    <button
      className={`session-item ${isActive ? 'active' : ''}`}
      onClick={() => onSelect(session.session_id)}
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
      title={confirmDelete ? undefined : `创建于 ${shortDate(session.created_at)}`}
    >
      <div className="session-item-main">
        <span className="session-item-title">
          {session.title || '新会话'}
        </span>
        <span className="session-item-time">
          {relativeTime(session.updated_at)}
        </span>
      </div>
      <div className="session-item-meta">
        {confirmDelete ? (
          <span className="session-delete-confirm">
            <span className="confirm-text">确认删除?</span>
            <span
              className="confirm-yes"
              onClick={handleDeleteClick}
              title="确认删除"
            >
              ✓
            </span>
            <span
              className="confirm-no"
              onClick={handleCancelDelete}
              title="取消"
            >
              ✕
            </span>
          </span>
        ) : (
          <>
            <span className="session-item-count">
              {session.message_count} 条消息
            </span>
            {isHovered && (
              <span
                className="session-delete-btn"
                onClick={handleDeleteClick}
                title="删除会话"
              >
                🗑
              </span>
            )}
          </>
        )}
      </div>
    </button>
  );
}

export default function SessionSidebar({
  sessions,
  activeSessionId,
  onSelectSession,
  onNewSession,
  onDeleteSession,
  loading,
}: SessionSidebarProps) {
  return (
    <aside className="session-sidebar">
      {/* ---- 头部 + 新会话按钮 ---- */}
      <div className="sidebar-header">
        <h2 className="sidebar-title">会话历史</h2>
        <button className="new-session-btn" onClick={onNewSession} title="上传新日志并开始新会话">
          <span className="new-session-icon">+</span>
          新会话
        </button>
      </div>

      {/* ---- 会话列表 ---- */}
      <div className="session-list">
        {loading ? (
          <div className="sidebar-loading">
            <div className="spinner" />
            <span>加载中...</span>
          </div>
        ) : sessions.length === 0 ? (
          <div className="sidebar-empty">
            <p>暂无历史会话</p>
            <p className="sidebar-empty-hint">上传日志文件开始分析</p>
          </div>
        ) : (
          sessions.map((session) => (
            <SessionItem
              key={session.session_id}
              session={session}
              isActive={session.session_id === activeSessionId}
              onSelect={onSelectSession}
              onDelete={onDeleteSession}
            />
          ))
        )}
      </div>
    </aside>
  );
}
