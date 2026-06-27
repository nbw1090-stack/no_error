/**
 * App 根组件
 *
 * 管理全局状态：当前数据集 summary、筛选条件、会话生命周期、加载/错误状态
 *
 * 数据流（三路径归一，日志条目不再由 App 持有）：
 * - 上传：SSE summary 事件 → activateDataset(id, summary) + ensureSession(id)
 * - 切换：getSession → activateDataset(dataset_id)（内部 fetchSummary）
 * - 恢复：同切换
 * 日志条目改由 LogTable 内部经分页 API 按需加载（首屏 + 滚动加载）。
 */

import { useState, useEffect, useRef } from 'react';
import {
  checkHealth,
  createSession,
  linkDatasetToSession,
  getSession,
  listSessions,
  deleteSession,
  fetchSummary,
  uploadAndParseStream,
  getAuthToken,
  persistAuthToken,
  clearAuthToken,
  fetchMe,
  logout,
  setUnauthorizedHandler,
} from './api/client';
import type {
  ParseSummary,
  FilterState,
  SessionInfo,
  SessionMessage,
} from './types/log';
import type { AuthResponse, AuthUser } from './types/auth';
import UploadZone, { type UploadStreamState } from './components/UploadZone';
import KpiCards from './components/KpiCards';
import FilterBar from './components/FilterBar';
import LogTable from './components/LogTable';
import StatsPanel from './components/StatsPanel';
import ChatPanel from './components/ChatPanel';
import SessionSidebar from './components/SessionSidebar';
import ComponentsPanel from './components/ComponentsPanel';
import LoginScreen from './components/LoginScreen';

const SESSION_STORAGE_KEY = 'bmc_session_id';

export default function App() {
  const [summary, setSummary] = useState<ParseSummary | null>(null);
  const [activeDatasetId, setActiveDatasetId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [backendOnline, setBackendOnline] = useState<boolean | null>(null);
  const [llmAvailable, setLlmAvailable] = useState<boolean>(false);
  const [filters, setFilters] = useState<FilterState>({
    component: '',
    level: 'ALL',
    search: '',
  });

  // ---- 视图切换：日志分析（仪表盘）/ 组件管理 ----
  const [view, setView] = useState<'dashboard' | 'components'>('dashboard');

  // ---- 认证状态 ----
  const [currentUser, setCurrentUser] = useState<AuthUser | null>(null);
  const [authReady, setAuthReady] = useState(false);

  // ---- 会话状态 ----
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(true);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [sessionRestored, setSessionRestored] = useState(false);

  // ---- 预加载的会话消息（传递给 ChatPanel 避免冗余 fetch） ----
  // undefined = 尚未加载（ChatPanel 等待），[] = 空会话，[...] = 有历史消息
  const [preloadedMessages, setPreloadedMessages] = useState<SessionMessage[] | undefined>(undefined);

  // ---- 请求序列号（处理快速切换会话时的竞态） ----
  const selectRequestSeq = useRef(0);

  // ---- 上传解析流式状态 ----
  // parseStream !== null 即代表流式进行中（progress 阶段）；summary 到达后置 null。
  const [parseStream, setParseStream] = useState<UploadStreamState | null>(null);
  const parseAbortRef = useRef<AbortController | null>(null);

  /** 从服务端加载所有会话列表 */
  async function loadSessions() {
    try {
      const result = await listSessions();
      setSessions(result.sessions);
    } catch (e) {
      console.error('Failed to load sessions:', e);
    }
  }

  /**
   * 激活某个数据集：设置当前数据集 + 重置筛选 + 加载 summary。
   * 上传/切换/恢复三路径统一走这里。
   *
   * @param datasetId  数据集 ID
   * @param newSummary 已有 summary 时直接用（上传流式 summary 事件），否则 fetchSummary
   */
  async function activateDataset(datasetId: string, newSummary?: ParseSummary) {
    setActiveDatasetId(datasetId);
    setFilters({ component: '', level: 'ALL', search: '' });
    if (newSummary) {
      setSummary(newSummary);
      setError(null);
      return;
    }
    // 先清空上一个数据集的 summary，再异步拉取新的。
    // 否则 setActiveDatasetId 与 setSummary 之间隔着一次 await，会产生一个
    // “新数据集 + 旧 summary”的中间渲染：main-content 因 key 变化重挂载，
    // StatsPanel 会拿到上一个会话的陈旧 summary（其 componentErrors 可能为空），
    // 表现为“组件错误分布为空但实际有值”。清空后这一刻显示加载占位即可。
    setSummary(null);
    try {
      const res = await fetchSummary(datasetId);
      setSummary(res.summary);
      setError(null);
    } catch (e) {
      console.error('Failed to load dataset summary:', e);
      setSummary(null);
      setError('加载数据集失败，该数据集可能已被删除。');
    }
  }

  /**
   * 选中某个会话：加载其关联数据集 summary + 聊天历史。
   */
  async function handleSelectSession(sessionId: string) {
    if (sessionId === activeSessionId) return;

    // 取消未完成的上传流，避免其事件继续 setState 污染新会话视图
    parseAbortRef.current?.abort();
    setParseStream(null);

    const requestId = ++selectRequestSeq.current;

    setActiveSessionId(sessionId);
    localStorage.setItem(SESSION_STORAGE_KEY, sessionId);
    setPreloadedMessages(undefined); // 通知 ChatPanel：正在加载中

    try {
      const sessionDetail = await getSession(sessionId);
      // 竞态检查：忽略非最新请求的结果
      if (requestId !== selectRequestSeq.current) return;
      // 纯对话会话（dataset_id 为空）：回到「上传 + 对话」视图，不激活数据集
      if (sessionDetail.dataset_id) {
        await activateDataset(sessionDetail.dataset_id);
      } else {
        setActiveDatasetId(null);
        setSummary(null);
      }
      if (requestId !== selectRequestSeq.current) return;
      setPreloadedMessages(sessionDetail.messages);
    } catch (e) {
      if (requestId !== selectRequestSeq.current) return;
      console.error('Failed to load session data:', e);
      setError('会话数据加载失败，该会话可能已被删除。');
      setPreloadedMessages([]); // 解锁 ChatPanel（空消息）
    }
  }

  /**
   * 创建一个空数据集会话（纯对话）并设为活跃。
   *
   * 用于「新会话」按钮与首次落地（无可恢复会话时）：使新会话页立即显示
   * 「上传区 + 对话窗口」，用户可不上传直接提问。返回新建的 session_id。
   */
  async function startBlankSession(): Promise<string | null> {
    try {
      const session = await createSession(null);
      setActiveSessionId(session.session_id);
      setActiveDatasetId(null);
      setSummary(null);
      setPreloadedMessages([]); // 新会话无历史消息
      setError(null);
      localStorage.setItem(SESSION_STORAGE_KEY, session.session_id);

      const newSessionInfo: SessionInfo = {
        session_id: session.session_id,
        dataset_id: session.dataset_id,
        created_at: session.created_at,
        updated_at: session.created_at,
        message_count: 0,
        title: '',
      };
      // 去重后置顶（避免重复点击「新会话」造成列表重复）
      setSessions((prev) => [
        newSessionInfo,
        ...prev.filter((s) => s.session_id !== session.session_id),
      ]);
      return session.session_id;
    } catch (e) {
      console.error('Failed to create blank session:', e);
      setError('创建会话失败，请重试。');
      return null;
    }
  }

  /** 开始新会话：清空右侧 → 创建空数据集会话 → 显示「上传 + 对话」 */
  async function handleNewSession() {
    parseAbortRef.current?.abort();
    setParseStream(null);
    setSummary(null);
    setActiveDatasetId(null);
    setActiveSessionId(null);
    setError(null);
    setFilters({ component: '', level: 'ALL', search: '' });
    setPreloadedMessages(undefined);
    localStorage.removeItem(SESSION_STORAGE_KEY);
    await startBlankSession();
  }

  /**
   * 删除会话（乐观更新）。
   *
   * 点击删除后立即从 UI 移除会话，若后端删除失败则回滚并提示错误。
   */
  async function handleDeleteSession(sessionId: string) {
    // 保存当前状态用于回滚
    const prevSessions = sessions;
    const wasActive = activeSessionId === sessionId;
    const prevActiveId = activeSessionId;
    const prevDatasetId = activeDatasetId;
    const prevSummary = summary;
    const prevPreloadedMessages = preloadedMessages;

    // 乐观更新：立即从 UI 移除
    setSessions((prev) => prev.filter((s) => s.session_id !== sessionId));
    if (wasActive) {
      setActiveSessionId(null);
      setActiveDatasetId(null);
      setSummary(null);
      setError(null);
      setPreloadedMessages(undefined);
      localStorage.removeItem(SESSION_STORAGE_KEY);
    }

    try {
      await deleteSession(sessionId);
    } catch (e) {
      // 回滚：恢复删除前的状态
      setSessions(prevSessions);
      if (wasActive) {
        setActiveSessionId(prevActiveId);
        setActiveDatasetId(prevDatasetId);
        setSummary(prevSummary);
        setPreloadedMessages(prevPreloadedMessages);
        if (prevActiveId) {
          localStorage.setItem(SESSION_STORAGE_KEY, prevActiveId);
        }
      }
      console.error('Failed to delete session:', e);
      setError('删除会话失败，请重试。');
    }
  }

  /**
   * 为刚解析完成的数据集创建会话（上传 summary 阶段触发，不阻塞渲染）。
   */
  async function ensureSession(datasetId: string) {
    try {
      const session = await createSession(datasetId);
      setActiveSessionId(session.session_id);
      setPreloadedMessages([]); // 新会话无历史消息
      localStorage.setItem(SESSION_STORAGE_KEY, session.session_id);

      // 直接加入列表头部，无需重新 fetch
      const newSessionInfo: SessionInfo = {
        session_id: session.session_id,
        dataset_id: session.dataset_id,
        created_at: session.created_at,
        updated_at: session.created_at,
        message_count: 0,
        title: '',
      };
      setSessions((prev) => [newSessionInfo, ...prev]);
    } catch (e) {
      console.error('Failed to create session:', e);
      setError('创建会话失败，请重试。');
    }
  }

  /**
   * 上传并流式解析日志：消费 SSE 事件，渐进式渲染。
   *
   * 消费循环放在 App 而非 UploadZone —— summary 一到就会渲染仪表盘并卸载
   * UploadZone，循环必须留在父组件才能完整跑完。
   *
   * 事件：progress → summary（立即激活数据集 + 渲染 KPI/筛选/StatsPanel；
   * 日志条目由 LogTable 按需分页加载）→ done
   */
  async function handleUploadStream(file: File) {
    // 取消上一次未完成的流（用户重新上传/切换会话时触发）
    parseAbortRef.current?.abort();
    const controller = new AbortController();
    parseAbortRef.current = controller;

    setParseStream({ stage: 'extracting' });
    setError(null);

    try {
      for await (const ev of uploadAndParseStream(file, controller.signal)) {
        switch (ev.type) {
          case 'progress':
            setParseStream((s) => (s ? { ...s, stage: ev.stage } : s));
            break;

          case 'summary': {
            // summary 已就绪 = 后端已原子落盘 → 立即激活数据集（渲染 KPI/筛选/StatsPanel）。
            const dsId = ev.dataset_id;
            // 若当前是空数据集会话（先对话后上传）：把数据集绑定到当前会话，
            // 保留聊天历史、升级为带工具的会话；否则按原流程为该数据集新建会话。
            if (activeSessionId && !activeDatasetId) {
              try {
                await linkDatasetToSession(activeSessionId, dsId);
                setSessions((prev) =>
                  prev.map((s) =>
                    s.session_id === activeSessionId
                      ? { ...s, dataset_id: dsId }
                      : s
                  )
                );
              } catch (e) {
                console.error('Failed to link dataset to session:', e);
                setError('绑定数据集到会话失败，请重试。');
              }
            } else {
              void ensureSession(dsId);
            }
            void activateDataset(dsId, ev.summary);
            // 实质完成：数据集已就绪，LogTable 可立即拉首屏
            setParseStream(null);
            break;
          }

          case 'done':
            break;

          case 'error':
            throw new Error(ev.message);
        }
      }
    } catch (e) {
      // 用户主动取消（切换/新建会话、重新上传）：静默
      if ((e as Error).name === 'AbortError') return;
      const msg = e instanceof Error ? e.message : '解析失败，请重试';
      setError(msg);
      setSummary(null); // 回滚半成品
      setActiveDatasetId(null);
      setParseStream(null);
    } finally {
      parseAbortRef.current = null;
    }
  }

  /** 错误回调 */
  function handleError(msg: string) {
    setError(msg);
  }

  /**
   * 认证解析（最先运行）：若本地存有 token，校验它是否仍有效。
   * 有效则置 currentUser；失效则清除 token。完成后置 authReady=true。
   * 这一步必须先于业务初始化完成，因为它决定了渲染主应用还是登录界面。
   */
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const token = getAuthToken();
      if (!token) {
        if (!cancelled) setAuthReady(true);
        return;
      }
      try {
        const me = await fetchMe();
        if (!cancelled) {
          setCurrentUser({ user_id: me.user_id, username: me.username });
        }
      } catch {
        // token 失效：清理掉，让用户重新登录
        if (!cancelled) clearAuthToken();
      } finally {
        if (!cancelled) setAuthReady(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  /** 认证成功回调（LoginScreen 登录/注册成功后触发）。 */
  function handleAuthed(auth: AuthResponse) {
    persistAuthToken(auth.token);
    setCurrentUser({ user_id: auth.user_id, username: auth.username });
  }

  /**
   * 注册 401 处理器：任意 API 收到 401（token 失效）时，client 已清 token，
   * 这里再把 React 态重置为未登录，使界面退回登录页。挂载时注册一次。
   */
  useEffect(() => {
    setUnauthorizedHandler(() => {
      setCurrentUser(null);
      setActiveSessionId(null);
      setActiveDatasetId(null);
      setSummary(null);
      setPreloadedMessages(undefined);
      try {
        localStorage.removeItem(SESSION_STORAGE_KEY);
      } catch {
        // 忽略
      }
    });
    return () => setUnauthorizedHandler(null);
  }, []);

  /** 登出：吊销服务端 token（best-effort）+ 清除本地态。 */
  async function handleLogout() {
    await logout();
    clearAuthToken();
    setCurrentUser(null);
  }

  /**
   * 页面加载时：并行检查后端 + 加载会话列表，然后恢复上次活跃会话。
   */
  useEffect(() => {
    async function init() {
      // 1. 并行请求：健康检查 + 会话列表（互不依赖）
      const [health] = await Promise.all([
        checkHealth(),
        loadSessions(),
      ]);
      setBackendOnline(health.status === 'ok');
      setLlmAvailable(health.llm_available ?? false);
      setSessionsLoading(false);

      // 2. 尝试恢复上次活跃会话
      const savedSessionId = localStorage.getItem(SESSION_STORAGE_KEY);
      let restored = false;
      if (savedSessionId) {
        try {
          const session = await getSession(savedSessionId);
          setActiveSessionId(savedSessionId);
          setPreloadedMessages(session.messages);
          if (session.dataset_id) {
            await activateDataset(session.dataset_id);
          } else {
            // 纯对话会话：回到「上传 + 对话」视图，不激活数据集
            setActiveDatasetId(null);
            setSummary(null);
          }
          restored = true;
        } catch {
          // 会话或数据集已过期/删除，清除记录
          localStorage.removeItem(SESSION_STORAGE_KEY);
        }
      }

      // 3. 无可恢复会话：创建空数据集会话，使落地页即显示「上传 + 对话」
      if (!restored) {
        await startBlankSession();
      }

      setSessionRestored(true);
    }

    init();
  }, []);

  /**
   * 切换会话 / 恢复会话后，把页面滚回顶部。
   *
   * 聊天面板位于仪表盘下方（main-content 之下）。若不重置滚动位置，
   * 用户从上一个会话（常把页面停在底部的聊天区）切到新会话时，
   * 仍会停留在原滚动位置，看不到顶部的日志分析结果
   * （KPI / 日志表 / StatsPanel）。
   * 首次恢复会话时 activeSessionId 由 null → 值同样触发，无害。
   */
  useEffect(() => {
    window.scrollTo({ top: 0 });
  }, [activeSessionId]);

  /** 流式解析进行中（progress 阶段） */
  const streaming = parseStream !== null;

  /** 是否显示上传区域（无活跃数据集时显示） */
  const showUpload = !activeDatasetId;

  return (
    <div className="app">
      {/* 认证未就绪：加载中 */}
      {!authReady ? (
        <div className="dashboard-loading">
          <div className="spinner" />
          <p>加载中...</p>
        </div>
      ) : !currentUser ? (
        /* 未登录：渲染登录界面，且不渲染主应用 */
        <LoginScreen onAuthed={handleAuthed} />
      ) : (
        <>
      {/* 页面头部 */}
      <header className="app-header">
        <h1>BMC 日志分析系统</h1>
        <div className="status-indicator">
          <span
            className={`status-dot ${backendOnline ? 'online' : backendOnline === false ? 'offline' : ''}`}
          />
          <span>
            {backendOnline === null
              ? '检查中...'
              : backendOnline
                ? `后端已连接${llmAvailable ? ' · LLM 已就绪' : ' · 规则匹配模式'}`
                : '后端未连接'}
          </span>
          {activeSessionId && (
            <span className="session-badge" title={activeSessionId}>
              会话: {activeSessionId.slice(0, 8)}...
            </span>
          )}
          <span className="user-chip" title={`已登录：${currentUser.username}`}>
            <span className="user-avatar">{currentUser.username.slice(0, 1).toUpperCase()}</span>
            <span className="user-name">{currentUser.username}</span>
          </span>
          <button className="component-btn small logout-btn" onClick={handleLogout}>
            登出
          </button>
        </div>
      </header>

      {/* 顶部导航：日志分析 / 组件管理 */}
      <nav className="app-nav">
        <button
          className={`app-nav-tab ${view === 'dashboard' ? 'active' : ''}`}
          onClick={() => setView('dashboard')}
        >
          日志分析
        </button>
        <button
          className={`app-nav-tab ${view === 'components' ? 'active' : ''}`}
          onClick={() => setView('components')}
        >
          组件管理
        </button>
      </nav>

      {/* 组件管理视图：全宽渲染，隐藏侧边栏与仪表盘 */}
      {view === 'components' ? (
        <ComponentsPanel authed={!!currentUser} />
      ) : (
        /* 主体布局：侧边栏 + 主内容区 */
        <div className="app-layout">
          <SessionSidebar
            sessions={sessions}
            activeSessionId={activeSessionId}
            onSelectSession={handleSelectSession}
            onNewSession={handleNewSession}
            onDeleteSession={handleDeleteSession}
            loading={sessionsLoading}
          />

          <div className="main-area">
            {/* 上传区域（无活跃数据集时显示） */}
            {showUpload && (
              <UploadZone
                onFile={handleUploadStream}
                onError={handleError}
                streamState={parseStream}
              />
            )}

            {/* 全局错误提示 */}
            {error && (
              <div className="error-message" style={{ marginBottom: 24 }}>
                {error}
              </div>
            )}

            {/* 数据集 summary 加载中（切会话 / 恢复时，避免渲染陈旧数据） */}
            {activeDatasetId && !summary && !error && (
              <div className="dashboard-loading">
                <div className="spinner" />
                <p>正在加载会话数据...</p>
              </div>
            )}

            {/* 数据集就绪后展示仪表盘 */}
            {summary && activeDatasetId && (
              <>
                <KpiCards key={`kpi-${activeDatasetId}`} summary={summary} />

                {/* 筛选栏：全宽置于两列之上，使下方日志表与组件分布等高对齐 */}
                <FilterBar
                  summary={summary}
                  filters={filters}
                  onFiltersChange={setFilters}
                />

                <div className="main-content" key={`main-${activeDatasetId}`}>
                  <div className="left-panel">
                    <LogTable
                      datasetId={activeDatasetId}
                      filters={filters}
                      total={summary.totalLines}
                      loadingHint={streaming}
                    />
                  </div>
                  <div className="right-panel">
                    <StatsPanel summary={summary} />
                  </div>
                </div>

              </>
            )}

            {/* Agent 对话窗口：纯对话会话时位于上传区下方；有数据集时位于仪表盘下方 */}
            {activeSessionId && sessionRestored && (
              <div className="chat-section" key="chat-panel">
                <ChatPanel
                  key={activeSessionId}
                  sessionId={activeSessionId}
                  initialMessages={preloadedMessages}
                  hasDataset={!!activeDatasetId}
                />
              </div>
            )}
          </div>
        </div>
      )}
        </>
      )}
    </div>
  );
}
