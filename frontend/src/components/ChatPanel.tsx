/**
 * ChatPanel 组件 —— Agent 对话窗口（ChatGPT 风格）
 *
 * 功能：
 * - 用户/助手消息气泡（深色主题）
 * - 思考中加载动画
 * - 自动滚动到底部
 * - 对话历史持久保留（存在于组件生命周期内）
 * - Enter 发送，Shift+Enter 换行
 * - 快捷建议按钮（空状态）
 */

import { useState, useRef, useEffect, type KeyboardEvent, type FormEvent } from 'react';
import { sendChatMessage } from '../api/client';
import type { ChatMessage, ParseResult } from '../types/log';
import './ChatPanel.css';

interface ChatPanelProps {
  parseResult: ParseResult;
}

/** 生成唯一消息 ID */
function genId(): string {
  return `msg_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

/** 格式化时间戳 */
function formatTime(): string {
  const now = new Date();
  return now.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
}

/** 快捷建议 */
const SUGGESTIONS = [
  '给我一个概览',
  '有哪些组件出错了',
  '分析一下错误',
  '日志时间跨度',
];

export default function ChatPanel({ parseResult }: ChatPanelProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [isThinking, setIsThinking] = useState(false);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  /** 自动滚动到底部 */
  function scrollToBottom() {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }

  useEffect(() => {
    scrollToBottom();
  }, [messages, isThinking]);

  /** 发送消息 */
  async function handleSend() {
    const trimmed = input.trim();
    if (!trimmed || isThinking) return;

    // 1. 追加用户消息
    const userMsg: ChatMessage = {
      id: genId(),
      role: 'user',
      content: trimmed,
      timestamp: formatTime(),
    };
    setMessages((prev) => [...prev, userMsg]);
    setInput('');
    setIsThinking(true);

    // 2. 构建请求上下文
    const summary = parseResult.summary;
    const context = summary
      ? {
          totalLines: summary.totalLines,
          errorCount: summary.errorCount,
          warningCount: summary.warningCount,
          components: summary.components,
        }
      : undefined;

    const history = messages.map((m) => ({
      role: m.role,
      content: m.content,
    }));

    try {
      const res = await sendChatMessage({
        message: trimmed,
        history,
        context,
      });

      // 3. 追加助手回复
      const assistantMsg: ChatMessage = {
        id: genId(),
        role: 'assistant',
        content: res.reply,
        timestamp: formatTime(),
      };
      setMessages((prev) => [...prev, assistantMsg]);
    } catch {
      // 错误时追加系统提示
      const errorMsg: ChatMessage = {
        id: genId(),
        role: 'assistant',
        content: '⚠️ 请求失败，请检查后端服务是否正常运行。',
        timestamp: formatTime(),
      };
      setMessages((prev) => [...prev, errorMsg]);
    } finally {
      setIsThinking(false);
      // 恢复输入框焦点
      inputRef.current?.focus();
    }
  }

  /** 快捷建议点击 */
  function handleSuggestion(suggestion: string) {
    setInput(suggestion);
    // 延迟执行以等待 state 更新
    setTimeout(() => {
      handleSendWithText(suggestion);
    }, 50);
  }

  /** 使用指定文本发送（用于快捷建议） */
  async function handleSendWithText(text: string) {
    if (isThinking) return;

    const userMsg: ChatMessage = {
      id: genId(),
      role: 'user',
      content: text,
      timestamp: formatTime(),
    };
    setMessages((prev) => [...prev, userMsg]);
    setInput('');
    setIsThinking(true);

    const summary = parseResult.summary;
    const context = summary
      ? {
          totalLines: summary.totalLines,
          errorCount: summary.errorCount,
          warningCount: summary.warningCount,
          components: summary.components,
        }
      : undefined;

    const history = messages.map((m) => ({
      role: m.role,
      content: m.content,
    }));

    try {
      const res = await sendChatMessage({ message: text, history, context });
      const assistantMsg: ChatMessage = {
        id: genId(),
        role: 'assistant',
        content: res.reply,
        timestamp: formatTime(),
      };
      setMessages((prev) => [...prev, assistantMsg]);
    } catch {
      const errorMsg: ChatMessage = {
        id: genId(),
        role: 'assistant',
        content: '⚠️ 请求失败，请检查后端服务是否正常运行。',
        timestamp: formatTime(),
      };
      setMessages((prev) => [...prev, errorMsg]);
    } finally {
      setIsThinking(false);
      inputRef.current?.focus();
    }
  }

  /** 键盘事件：Enter 发送，Shift+Enter 换行 */
  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  /** 表单提交（兜底） */
  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    handleSend();
  }

  /** 渲染消息内容（支持简单 Markdown：**bold** 和换行） */
  function renderContent(content: string) {
    // 将 **text** 转换为 <strong>text</strong>
    const withBold = content.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    return { __html: withBold };
  }

  const hasMessages = messages.length > 0;

  return (
    <div className="chat-panel">
      {/* ---- 头部 ---- */}
      <div className="chat-panel-header">
        <h3>
          💬 AI 日志分析助手
          <span className="chat-badge">
            <span className="badge-dot" />
            在线
          </span>
        </h3>
      </div>

      {/* ---- 消息列表 / 空状态 ---- */}
      {hasMessages ? (
        <div className="chat-messages">
          {messages.map((msg) => (
            <div key={msg.id} className={`chat-message ${msg.role}`}>
              <div
                className="message-content"
                dangerouslySetInnerHTML={renderContent(msg.content)}
              />
              <span className="message-time">{msg.timestamp}</span>
            </div>
          ))}

          {/* 思考中... */}
          {isThinking && (
            <div className="chat-thinking">
              <div className="thinking-dots">
                <span className="thinking-dot" />
                <span className="thinking-dot" />
                <span className="thinking-dot" />
              </div>
              <span className="thinking-text">分析中...</span>
            </div>
          )}

          {/* 滚动锚点 */}
          <div ref={messagesEndRef} />
        </div>
      ) : (
        <div className="chat-empty">
          <div className="chat-empty-icon">🤖</div>
          <h4>BMC 日志分析助手</h4>
          <p>
            我可以帮你分析已上传的日志数据，包括错误趋势、组件分布和问题排查建议。
            试试下面的问题，或直接输入你想了解的内容。
          </p>
          <div className="chat-suggestions">
            {SUGGESTIONS.map((s) => (
              <button
                key={s}
                className="chat-suggestion"
                onClick={() => handleSuggestion(s)}
                disabled={isThinking}
              >
                {s}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* ---- 输入区域 ---- */}
      <form className="chat-input-area" onSubmit={handleSubmit}>
        <textarea
          ref={inputRef}
          className="chat-input"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="输入你的问题，Enter 发送，Shift+Enter 换行..."
          rows={1}
          disabled={isThinking}
        />
        <button
          type="submit"
          className="chat-send-btn"
          disabled={!input.trim() || isThinking}
          title="发送 (Enter)"
        >
          <span className="send-icon">➤</span>
        </button>
      </form>
    </div>
  );
}
