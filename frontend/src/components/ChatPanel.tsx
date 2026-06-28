/**
 * ChatPanel 组件 —— Agent 对话窗口（ChatGPT 风格）
 *
 * 功能：
 * - 用户/助手消息气泡（深色主题）
 * - 思考中加载动画
 * - 自动滚动到底部
 * - 对话历史服务端持久化 + 会话恢复
 * - Enter 发送，Shift+Enter 换行
 * - 快捷建议按钮（空状态）
 * - 工具调用消息折叠展示
 *
 * 性能优化：
 * - 接受 initialMessages prop，父组件预加载时跳过 fetch
 */

import { useState, useRef, useEffect, type KeyboardEvent, type FormEvent } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { sendChatMessageStream, sendFeedback, createSession } from '../api/client';
import type { ChatMessage, SessionMessage, SessionInfo } from '../types/log';
import 'highlight.js/styles/github-dark.css';
import './ChatPanel.css';

interface ChatPanelProps {
  /** 当前会话 ID；null = 尚未创建（落地 / 新会话空白态）。
   *  由父组件持有，首条消息懒创建后经 onSessionCreated 回填。 */
  sessionId: string | null;
  /** 预加载的会话消息（父组件并行请求后传入，避免冗余 fetch）。
   *  undefined = 加载中，[...] = 已就绪 */
  initialMessages?: SessionMessage[];
  /** 当前会话是否已绑定日志数据集。false = 纯对话模式，空态文案/建议改为通用问答。 */
  hasDataset?: boolean;
  /** 首条消息触发懒创建后，把新会话同步给父组件（激活 + 加入侧栏 + 持久化）。 */
  onSessionCreated: (session: SessionInfo) => void;
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

/** 快捷建议：有日志数据集时（围绕已上传日志的分析） */
const SUGGESTIONS_WITH_DATASET = [
  '给我一个概览',
  '有哪些组件出错了',
  '分析一下错误',
  '日志时间跨度',
];

/** 快捷建议：纯对话模式（尚未上传日志，通用 BMC 问答） */
const SUGGESTIONS_NO_DATASET = [
  'BMC 日志分析助手能做什么？',
  '常见的 BMC 错误有哪些？',
  '如何解读 framework.log？',
  '上传日志后能分析什么？',
];

/**
 * 将服务端存储的消息转换为展示用的 ChatMessage 列表。
 * 过滤掉工具调用消息（用户不关心底层细节），只保留 user 和最终的 assistant 消息。
 */
function sessionMessagesToChatMessages(messages: SessionMessage[]): ChatMessage[] {
  const chatMessages: ChatMessage[] = [];

  for (const msg of messages) {
    if (msg.role === 'user') {
      chatMessages.push({
        id: genId(),
        role: 'user',
        content: msg.content || '',
        timestamp: '',
      });
    } else if (msg.role === 'assistant' && msg.content) {
      // 只保留有实际文本内容的 assistant 消息
      chatMessages.push({
        id: genId(),
        role: 'assistant',
        content: msg.content,
        timestamp: '',
      });
    }
    // 跳过 role=tool 和空 content 的 assistant 消息（纯工具调用）
  }

  return chatMessages;
}

export default function ChatPanel({
  sessionId,
  initialMessages,
  hasDataset = true,
  onSessionCreated,
}: ChatPanelProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [isThinking, setIsThinking] = useState(false);
  const [isRestoring, setIsRestoring] = useState(true);
  const [toolProgress, setToolProgress] = useState<string | null>(null);
  const [lastUsage, setLastUsage] = useState<{
    input: number;
    output: number;
    total: number;
    cacheHit: number;
    cacheMiss: number;
  } | null>(null);
  /** 用户对每条 assistant 消息的反馈（msgId -> like/dislike），用于高亮选中态 */
  const [feedback, setFeedback] = useState<Record<string, 'like' | 'dislike'>>({});

  const messagesContainerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const abortControllerRef = useRef<AbortController | null>(null);
  const streamingMsgIdRef = useRef<string | null>(null);
  const hasStreamingContentRef = useRef(false);
  /** 当前生效的会话 ID：与 sessionId prop 同步，懒创建后立即在此暂存，
   *  供同一轮 _sendMessage / handleFeedback 使用（不必等父组件回填 prop）。 */
  const sidRef = useRef<string | null>(sessionId);
  /** 懒创建会触发 sessionId 变化从而引发恢复 effect；置位后跳过下一次恢复，
   *  避免空历史覆盖正在进行的消息流。 */
  const suppressRestoreRef = useRef(false);

  /**
   * 滚动到消息列表底部。
   *
   * 只设置面板内部 .chat-messages 容器自身的 scrollTop，而不调用 scrollIntoView：
   * scrollIntoView 会连带滚动整个页面（window），把页面定位到位于底部的聊天面板，
   * 导致顶部的日志分析结果（KPI / 日志表 / StatsPanel）被滚出视口。
   * 容器自身已设 scroll-behavior: smooth，故平滑滚动体验不变。
   */
  function scrollToBottom() {
    const container = messagesContainerRef.current;
    if (container) {
      container.scrollTop = container.scrollHeight;
    }
  }

  useEffect(() => {
    scrollToBottom();
  }, [messages, isThinking]);

  // 切换会话时清空反馈选中态（避免上一会话的点赞高亮残留到新会话）
  useEffect(() => {
    setFeedback({});
  }, [sessionId]);

  /**
   * 父组件切换会话时 sessionId 变化，同步到 ref（_sendMessage / handleFeedback 取此值）。
   * 懒创建时父组件也会回填新值，这里跟进（值已一致，无害）。
   */
  useEffect(() => {
    sidRef.current = sessionId;
  }, [sessionId]);

  /**
   * 恢复 / 切换会话历史。
   *
   * initialMessages 协议：
   * - undefined  = 父组件正在加载中，等待（不自行 fetch）
   * - []          = 空会话（新会话），直接显示空状态
   * - [...]       = 预加载的消息，直接展示
   *
   * 懒创建会让 sessionId 从 null → 新值从而触发本 effect，但此时首轮消息流
   * 正在进行，绝不能用空历史覆盖 —— 用 suppressRestoreRef 跳过这一次。
   * 切换会话（用户主动切换）时 initialMessages 同步变化，正常恢复并重置瞬时态。
   */
  useEffect(() => {
    // 懒创建回填：sessionId 由 null → 新值触发本 effect，但首轮消息流正在进行，
    // 绝不能覆盖 —— 最优先跳过（无论 initialMessages 此刻是什么）。
    if (suppressRestoreRef.current) {
      suppressRestoreRef.current = false;
      return;
    }

    if (initialMessages === undefined) {
      // 父组件正在加载下一会话：立即清空并显示加载态，避免残留上一会话的消息
      setMessages([]);
      setIsRestoring(true);
      return;
    }

    // 父组件已提供消息（可能是空数组）
    const restored = sessionMessagesToChatMessages(initialMessages);
    setMessages(restored);
    setIsRestoring(false);
    // 切换会话：重置上一会话残留的瞬时态（原先靠 key 重挂载清理，现改为受控重置）
    setLastUsage(null);
    setFeedback({});
    abortControllerRef.current?.abort();
    setIsThinking(false);
  }, [sessionId, initialMessages]);

  /** 发送消息（内部实现，使用 SSE 流式） */
  async function _sendMessage(text: string) {
    if (isThinking) return;

    // 懒创建：落地 / 新会话空白态下首次发消息时才在后端建会话。
    // 建好后回填父组件（激活 + 侧栏 + 持久化），并抑制随之而来的恢复 effect，
    // 避免空历史覆盖刚发出的用户消息。
    let sid = sidRef.current;
    if (!sid) {
      try {
        const created = await createSession(null);
        sid = created.session_id;
        suppressRestoreRef.current = true;
        sidRef.current = sid;
        onSessionCreated({
          session_id: created.session_id,
          dataset_id: created.dataset_id,
          created_at: created.created_at,
          updated_at: created.created_at,
          message_count: 0,
          title: '',
        });
      } catch (e) {
        console.error('Failed to create session for chat:', e);
        setMessages([
          {
            id: genId(),
            role: 'assistant',
            content: '⚠️ 创建会话失败，请检查网络后重试。',
            timestamp: formatTime(),
          },
        ]);
        setIsRestoring(false);
        return;
      }
    }

    // 创建新的 AbortController
    const controller = new AbortController();
    abortControllerRef.current = controller;

    // 1. 追加用户消息
    const userMsg: ChatMessage = {
      id: genId(),
      role: 'user',
      content: text,
      timestamp: formatTime(),
    };
    setMessages((prev) => [...prev, userMsg]);
    setInput('');
    setIsThinking(true);
    setLastUsage(null); // 清空上一轮的 token 消耗

    // 2. 创建占位助手消息（流式内容将逐步填充）
    const placeholderId = genId();
    streamingMsgIdRef.current = placeholderId;
    const placeholderMsg: ChatMessage = {
      id: placeholderId,
      role: 'assistant',
      content: '',
      timestamp: formatTime(),
    };
    setMessages((prev) => [...prev, placeholderMsg]);

    try {
      // 3. 流式消费 SSE 事件
      for await (const event of sendChatMessageStream(
        { message: text, session_id: sid },
        controller.signal,
      )) {
        switch (event.type) {
          case 'tool_progress':
            if (event.status === 'start') {
              setToolProgress(`正在调用: ${event.tool}...`);
            } else {
              setToolProgress(null);
            }
            break;

          case 'delta':
            // 逐步追加文本到占位消息
            if (event.text) {
              hasStreamingContentRef.current = true;
              setMessages((prev) =>
                prev.map((msg) =>
                  msg.id === placeholderId
                    ? { ...msg, content: msg.content + event.text }
                    : msg,
                ),
              );
            }
            break;

          case 'done':
            // 流正常结束：把后端回传的 trace_id 挂到这条消息上，供点赞/踩打分关联
            if (event.trace_id) {
              setMessages((prev) =>
                prev.map((msg) =>
                  msg.id === placeholderId
                    ? { ...msg, traceId: event.trace_id }
                    : msg,
                ),
              );
            }
            break;

          case 'usage':
            // 整轮 token 消耗（Agent 在流末尾上报）
            if (
              typeof event.input === 'number' &&
              typeof event.output === 'number' &&
              typeof event.total === 'number'
            ) {
              setLastUsage({
                input: event.input,
                output: event.output,
                total: event.total,
                cacheHit: event.cache_hit ?? 0,
                cacheMiss: event.cache_miss ?? 0,
              });
            }
            break;

          case 'status':
            // 状态消息可用于未来扩展
            break;
        }
      }
    } catch (err: unknown) {
      // 用户主动取消
      if (err instanceof DOMException && err.name === 'AbortError') {
        setMessages((prev) =>
          prev.map((msg) =>
            msg.id === placeholderId
              ? { ...msg, content: msg.content || '⏹ 已停止生成。' }
              : msg,
          ),
        );
      } else {
        // 网络/服务端错误
        setMessages((prev) =>
          prev.map((msg) =>
            msg.id === placeholderId
              ? {
                  ...msg,
                  content:
                    msg.content ||
                    '⚠️ 请求失败，请检查后端服务是否正常运行，以及 LLM API Key 是否已配置。',
                }
              : msg,
          ),
        );
      }
    } finally {
      // 若流式正常结束但占位气泡始终为空（LLM 本轮只产出工具调用、未给最终文本），
      // 静默将其从消息列表移除，避免空占位残留在状态里。中断/错误分支已写入兜底
      // 文案，此处按 content 是否为空判断，不会误删这些兜底消息。
      setMessages((prev) =>
        prev.filter(
          (msg) => !(msg.id === placeholderId && msg.content.trim() === ''),
        ),
      );
      setIsThinking(false);
      setToolProgress(null);
      abortControllerRef.current = null;
      streamingMsgIdRef.current = null;
      hasStreamingContentRef.current = false;
      inputRef.current?.focus();
    }
  }

  /** 发送消息（从输入框） */
  function handleSend() {
    const trimmed = input.trim();
    if (!trimmed || isThinking) return;
    _sendMessage(trimmed);
  }

  /** 停止生成 */
  function handleStop() {
    abortControllerRef.current?.abort();
  }

  /** 快捷建议点击 */
  function handleSuggestion(suggestion: string) {
    if (isThinking) return;
    _sendMessage(suggestion);
  }

  /** 点赞/踩：乐观更新选中态，失败回退；不带 traceId 的消息忽略（如历史恢复的消息） */
  async function handleFeedback(
    msgId: string,
    traceId: string | undefined,
    kind: 'like' | 'dislike',
  ) {
    const sid = sidRef.current;
    if (!sid || !traceId || isThinking) return;
    const prev = feedback[msgId];
    setFeedback((f) => ({ ...f, [msgId]: kind }));
    try {
      await sendFeedback({
        trace_id: traceId,
        session_id: sid,
        feedback: kind,
      });
    } catch {
      setFeedback((f) => ({ ...f, [msgId]: prev }));
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

  // 过滤掉空内容的助手占位气泡：LLM 本轮可能只产出工具调用而未给最终文本，
  // 此时占位消息 content 仍为空，直接渲染会留下一个空白气泡。统一不展示这类
  // 消息，对用户无感知（加载态由下方的「思考中」动画单独承担）。
  const visibleMessages = messages.filter(
    (msg) => msg.role !== 'assistant' || msg.content.trim() !== '',
  );

  const hasMessages = visibleMessages.length > 0;

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
      {isRestoring ? (
        <div className="chat-restoring">
          <div className="thinking-dots">
            <span className="thinking-dot" />
            <span className="thinking-dot" />
            <span className="thinking-dot" />
          </div>
          <span className="thinking-text">恢复会话中...</span>
        </div>
      ) : hasMessages ? (
        <div className="chat-messages" ref={messagesContainerRef}>
          {visibleMessages.map((msg) => (
            <div key={msg.id} className={`chat-message ${msg.role}`}>
              <div className="message-content">
                {msg.role === 'assistant' ? (
                  <ReactMarkdown
                    // remark-gfm：启用 GFM 扩展语法（表格 / 删除线 / 任务列表 / 自动链接）
                    // —— 否则 LLM 输出的 markdown 表格会按普通文本显示管道符 "|"
                    remarkPlugins={[remarkGfm]}
                    rehypePlugins={[rehypeHighlight]}
                    components={{
                      // GFM 表格在窄气泡里可能超宽：包一层横向滚动容器，避免撑破气泡
                      table: ({ node: _node, ...props }) => (
                        <div className="md-table-wrap">
                          <table {...props} />
                        </div>
                      ),
                    }}
                  >
                    {msg.content}
                  </ReactMarkdown>
                ) : (
                  msg.content
                )}
              </div>
              {msg.role === 'assistant' && msg.traceId && (
                <div className="message-feedback">
                  <button
                    type="button"
                    className={`feedback-btn ${feedback[msg.id] === 'like' ? 'active like' : ''}`}
                    onClick={() => handleFeedback(msg.id, msg.traceId, 'like')}
                    disabled={isThinking}
                    title="有帮助"
                  >
                    👍
                  </button>
                  <button
                    type="button"
                    className={`feedback-btn ${feedback[msg.id] === 'dislike' ? 'active dislike' : ''}`}
                    onClick={() => handleFeedback(msg.id, msg.traceId, 'dislike')}
                    disabled={isThinking}
                    title="没帮助"
                  >
                    👎
                  </button>
                </div>
              )}
              <span className="message-time">{msg.timestamp}</span>
            </div>
          ))}

          {/* 思考中 / 工具调用进度（流式内容到达后自动隐藏） */}
          {isThinking && !hasStreamingContentRef.current && (
            <div className="chat-thinking">
              <div className="thinking-dots">
                <span className="thinking-dot" />
                <span className="thinking-dot" />
                <span className="thinking-dot" />
              </div>
              <span className="thinking-text">
                {toolProgress || '分析中...'}
              </span>
            </div>
          )}
        </div>
      ) : (
        <div className="chat-empty">
          <div className="chat-empty-icon">🤖</div>
          <h4>BMC 日志分析助手</h4>
          <p>
            {hasDataset
              ? '我可以帮你分析已上传的日志数据，包括错误趋势、组件分布和问题排查建议。试试下面的问题，或直接输入你想了解的内容。'
              : '我是 BMC 日志分析助手。你可以直接向我提问 BMC 相关问题，也可以上传 dump_info.tar.gz 启用基于真实日志的深度分析。'}
          </p>
          <div className="chat-suggestions">
            {(hasDataset ? SUGGESTIONS_WITH_DATASET : SUGGESTIONS_NO_DATASET).map(
              (s) => (
                <button
                  key={s}
                  className="chat-suggestion"
                  onClick={() => handleSuggestion(s)}
                  disabled={isThinking}
                >
                  {s}
                </button>
              )
            )}
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
        {isThinking ? (
          <button
            type="button"
            className="chat-stop-btn"
            onClick={handleStop}
            title="停止生成"
          >
            <span className="stop-icon">■</span>
          </button>
        ) : (
          <button
            type="submit"
            className="chat-send-btn"
            disabled={!input.trim()}
            title="发送 (Enter)"
          >
            <span className="send-icon">➤</span>
          </button>
        )}
      </form>

      {/* 上一轮对话的 token 消耗（Agent 在流末尾上报） */}
      {lastUsage && lastUsage.total > 0 && (
        <div className="chat-usage">
          本次消耗 {lastUsage.total} tokens（输入 {lastUsage.input} / 输出{' '}
          {lastUsage.output}）
          {lastUsage.cacheHit + lastUsage.cacheMiss > 0 && (
            <span className="chat-usage-cache">
              {' '}
              · 缓存命中{' '}
              {Math.round(
                (lastUsage.cacheHit /
                  (lastUsage.cacheHit + lastUsage.cacheMiss)) *
                  100,
              )}
              %（{lastUsage.cacheHit} 命中 / {lastUsage.cacheMiss} 未命中）
            </span>
          )}
        </div>
      )}
    </div>
  );
}
