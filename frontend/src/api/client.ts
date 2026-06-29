/**
 * API 客户端 —— 封装对后端的 fetch 调用
 *
 * 鉴权：除 /api/health 与 /api/auth/login|register 外，所有请求都附加
 * `Authorization: Bearer <token>`（authHeaders）。任一请求收到 401 时，
 * 经 setUnauthorizedHandler 注册的回调清空登录态并退回登录界面。
 */

import type {
  ParseResult,
  ParseSummary,
  ParseStreamEvent,
  PaginatedEntries,
  ChatRequest,
  ChatResponse,
  SessionInfo,
  SessionDetail,
  StreamEvent,
} from '../types/log';
import type { ComponentInfo } from '../types/component';
import type { AuthResponse, AuthUser } from '../types/auth';
import type { AstAnalyzeEvent, AstResult } from '../types/ast';
import type { WikiStatus, WikiSyncEvent } from '../types/wiki';

const API_BASE = '/api';

/** localStorage key for the persisted Bearer token. */
const AUTH_TOKEN_KEY = 'bmc_auth_token';

/**
 * 读取 localStorage 中持久化的认证 token。
 * @returns token 字符串，或未登录时返回 null。
 */
export function getAuthToken(): string | null {
  try {
    return localStorage.getItem(AUTH_TOKEN_KEY);
  } catch {
    return null;
  }
}

/** 写入 / 清除 token（内部使用）。 */
function setAuthToken(token: string): void {
  try {
    localStorage.setItem(AUTH_TOKEN_KEY, token);
  } catch {
    // 忽略 localStorage 不可用
  }
}

/** 清除 token（登出 / token 失效时）。 */
export function clearAuthToken(): void {
  try {
    localStorage.removeItem(AUTH_TOKEN_KEY);
  } catch {
    // 忽略
  }
}

/**
 * 构造请求头：当存在 token 时附加 `Authorization: Bearer <token>`。
 * @param extra 额外的 header（如 Content-Type）。
 */
function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = { ...(extra || {}) };
  const token = getAuthToken();
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }
  return headers;
}

// ============================================================
// 401 统一处理：token 失效 → 清空本地态并通知 App 退回登录
// ============================================================

let unauthorizedHandler: (() => void) | null = null;

/**
 * 注册「未授权」回调：App 挂载时调用，传入「清空 currentUser + 活跃会话态」。
 * 传 null 可注销。
 */
export function setUnauthorizedHandler(fn: (() => void) | null): void {
  unauthorizedHandler = fn;
}

/**
 * 响应为 401 时：清 token + 触发回调（让 UI 退回登录），并抛出可识别错误。
 */
function onUnauthorized(): never {
  clearAuthToken();
  if (unauthorizedHandler) {
    try {
      unauthorizedHandler();
    } catch {
      // 回调失败不影响抛错
    }
  }
  throw new Error('登录已过期，请重新登录');
}

/**
 * 上传 tar.gz 文件并获取解析结果
 */
export async function uploadAndParse(file: File): Promise<ParseResult> {
  const formData = new FormData();
  formData.append('file', file);

  const response = await fetch(`${API_BASE}/parse`, {
    method: 'POST',
    headers: authHeaders(),
    body: formData,
  });

  if (response.status === 401) onUnauthorized();
  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    throw new Error(errorData?.errors?.[0] || `服务器错误: ${response.status}`);
  }

  const data: ParseResult = await response.json();
  if (!data.success) {
    throw new Error(data.errors?.[0] || '解析失败');
  }

  return data;
}

/**
 * 检查后端服务健康状态
 */
export async function checkHealth(): Promise<{ status: string; llm_available: boolean }> {
  const response = await fetch(`${API_BASE}/health`);
  const data = await response.json();
  return data;
}

/**
 * 发送消息到 Agent 对话接口
 *
 * @param req  - 聊天请求体
 * @param signal - 可选的 AbortSignal，用于取消请求（用户点击停止按钮）
 */
export async function sendChatMessage(
  req: ChatRequest,
  signal?: AbortSignal,
): Promise<ChatResponse> {
  const response = await fetch(`${API_BASE}/chat`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(req),
    signal,
  });

  if (response.status === 401) onUnauthorized();
  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    throw new Error(errorData?.detail || `服务器错误: ${response.status}`);
  }

  return response.json();
}

/**
 * 通用 SSE reader：从 fetch Response 逐行读取 `data: ` 事件并 JSON.parse。
 *
 * 供聊天流式（sendChatMessageStream）和上传解析流式（uploadAndParseStream）复用。
 * 调用方负责先校验 response.ok 并抛出业务错误。
 */
async function* readSSE(response: Response): AsyncGenerator<unknown> {
  if (!response.body) {
    throw new Error('浏览器不支持 ReadableStream');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() || '';

    for (const line of lines) {
      if (line.startsWith('data: ')) {
        const data = line.slice(6).trim();
        if (!data) continue;
        try {
          yield JSON.parse(data);
        } catch {
          // 跳过无法解析的行
        }
      }
    }
  }
}

/**
 * 发送消息到 Agent 对话接口（SSE 流式版本）
 *
 * @param req    - 聊天请求体
 * @param signal - 可选的 AbortSignal，用于取消请求
 * @yields       - StreamEvent 事件（delta / tool_progress / done）
 */
export async function* sendChatMessageStream(
  req: ChatRequest,
  signal?: AbortSignal,
): AsyncGenerator<StreamEvent> {
  const response = await fetch(`${API_BASE}/chat/stream`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(req),
    signal,
  });

  if (response.status === 401) onUnauthorized();
  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    throw new Error(errorData?.detail || `服务器错误: ${response.status}`);
  }

  for await (const event of readSSE(response)) {
    yield event as StreamEvent;
  }
}

/**
 * 提交用户对一轮 AI 回复的反馈（点赞 / 踩），后端据此给对应 trace 打 Langfuse score。
 *
 * @param payload trace_id（来自 chat 响应 / SSE done）+ session_id + feedback
 */
export async function sendFeedback(payload: {
  trace_id: string;
  session_id: string;
  feedback: 'like' | 'dislike';
  comment?: string;
}): Promise<{ ok: boolean }> {
  const response = await fetch(`${API_BASE}/feedback`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(payload),
  });

  if (response.status === 401) onUnauthorized();
  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    throw new Error(errorData?.detail || `服务器错误: ${response.status}`);
  }

  return response.json();
}

/**
 * 上传 tar.gz 文件并以 SSE 流式接收解析进度。
 *
 * 事件序列：progress(extracting|parsing) → summary → done
 * 收到 summary 即代表后端已原子落盘，可安全创建会话。
 * 日志条目由前端经 fetchEntries 按需分页加载。
 *
 * @param file   - 待上传的 tar.gz 文件
 * @param signal - 可选的 AbortSignal，用于取消上传/解析
 * @yields        - ParseStreamEvent
 */
export async function* uploadAndParseStream(
  file: File,
  signal?: AbortSignal,
): AsyncGenerator<ParseStreamEvent> {
  const formData = new FormData();
  formData.append('file', file);

  const response = await fetch(`${API_BASE}/parse/stream`, {
    method: 'POST',
    headers: authHeaders(),
    body: formData,
    signal,
  });

  if (response.status === 401) onUnauthorized();
  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    throw new Error(errorData?.detail || `服务器错误: ${response.status}`);
  }

  for await (const event of readSSE(response)) {
    yield event as ParseStreamEvent;
  }
}

// ============================================================
// 数据集 API
// ============================================================

/**
 * 获取数据集的汇总统计（summary）。
 *
 * 日志条目改由 fetchEntries 按需分页加载，避免一次性传输全量 entries。
 */
export async function fetchSummary(
  datasetId: string,
): Promise<{ dataset_id: string; summary: ParseSummary }> {
  const response = await fetch(`${API_BASE}/datasets/${datasetId}`, {
    headers: authHeaders(),
  });

  if (response.status === 401) onUnauthorized();
  if (!response.ok) {
    throw new Error('数据集不存在');
  }

  return response.json();
}

/**
 * 分页获取数据集的日志条目（支持筛选）。
 *
 * LogTable 按需加载：首屏取前 limit 条，滚动到底加载下一批。
 */
export async function fetchEntries(
  datasetId: string,
  offset: number,
  limit: number,
  filters: { component: string; level: string; search: string },
): Promise<PaginatedEntries> {
  const params = new URLSearchParams({
    offset: String(offset),
    limit: String(limit),
  });
  if (filters.component) params.set('component', filters.component);
  if (filters.level && filters.level !== 'ALL') params.set('level', filters.level);
  if (filters.search) params.set('search', filters.search);

  const response = await fetch(
    `${API_BASE}/datasets/${datasetId}/entries?${params.toString()}`,
    { headers: authHeaders() },
  );

  if (response.status === 401) onUnauthorized();
  if (!response.ok) {
    throw new Error('加载日志条目失败');
  }

  return response.json();
}

// ============================================================
// 会话管理 API
// ============================================================

/**
 * 创建新的聊天会话（归属当前登录用户，由 token 决定）。
 *
 * datasetId 为空时创建「纯对话会话」（未上传日志，可直接提问）。
 */
export async function createSession(
  datasetId?: string | null
): Promise<SessionInfo> {
  const response = await fetch(`${API_BASE}/sessions`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ dataset_id: datasetId ?? null }),
  });

  if (response.status === 401) onUnauthorized();
  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    throw new Error(errorData?.detail || '创建会话失败');
  }

  return response.json();
}

/**
 * 把数据集绑定到已有会话（先对话后上传的升级场景）。
 */
export async function linkDatasetToSession(
  sessionId: string,
  datasetId: string
): Promise<{ session_id: string; dataset_id: string }> {
  const response = await fetch(`${API_BASE}/sessions/${sessionId}/dataset`, {
    method: 'PUT',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ dataset_id: datasetId }),
  });

  if (response.status === 401) onUnauthorized();
  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    throw new Error(errorData?.detail || '绑定数据集失败');
  }

  return response.json();
}

/**
 * 获取会话详情（含完整消息历史）
 */
export async function getSession(sessionId: string): Promise<SessionDetail> {
  const response = await fetch(`${API_BASE}/sessions/${sessionId}`, {
    headers: authHeaders(),
  });

  if (response.status === 401) onUnauthorized();
  if (!response.ok) {
    throw new Error('会话不存在');
  }

  return response.json();
}

/**
 * 列出当前用户的会话（后端按 token 过滤）
 */
export async function listSessions(): Promise<{ sessions: SessionInfo[] }> {
  const response = await fetch(`${API_BASE}/sessions`, {
    headers: authHeaders(),
  });

  if (response.status === 401) onUnauthorized();
  if (!response.ok) {
    throw new Error('获取会话列表失败');
  }

  return response.json();
}

/**
 * 删除会话
 */
export async function deleteSession(sessionId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/sessions/${sessionId}`, {
    method: 'DELETE',
    headers: authHeaders(),
  });

  if (response.status === 401) onUnauthorized();
  if (!response.ok) {
    throw new Error('删除会话失败');
  }
}

// ============================================================
// 组件注册表 API（按用户隔离；enabled = 是否已分析，由 AST 流程维护）
// ============================================================

/** 新建组件的请求体（不含 enabled —— 由 AST 流程维护） */
export type ComponentCreatePayload = Pick<ComponentInfo, 'name' | 'git_url' | 'branch'>;

/** 更新组件的请求体（仅 git_url / branch；enabled 由 AST 流程维护） */
export type ComponentUpdatePayload = Pick<ComponentInfo, 'git_url' | 'branch'>;

/**
 * 列出当前用户的注册组件（enabled 反映是否已分析）
 */
export async function listComponents(): Promise<ComponentInfo[]> {
  const response = await fetch(`${API_BASE}/components`, {
    headers: authHeaders(),
  });

  if (response.status === 401) onUnauthorized();
  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    throw new Error(errorData?.detail || '获取组件列表失败');
  }

  const data = await response.json();
  return data.components;
}

/**
 * 创建新组件
 */
export async function createComponent(
  comp: ComponentCreatePayload,
): Promise<ComponentInfo> {
  const response = await fetch(`${API_BASE}/components`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(comp),
  });

  if (response.status === 401) onUnauthorized();
  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    if (response.status === 409) {
      throw new Error('组件已存在');
    }
    throw new Error(errorData?.detail || '创建组件失败');
  }

  return response.json();
}

/**
 * 更新组件（git_url / branch；改源码会由后端重置 enabled=false）
 */
export async function updateComponent(
  name: string,
  comp: ComponentUpdatePayload,
): Promise<ComponentInfo> {
  const response = await fetch(`${API_BASE}/components/${encodeURIComponent(name)}`, {
    method: 'PUT',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(comp),
  });

  if (response.status === 401) onUnauthorized();
  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    if (response.status === 404) {
      throw new Error('组件不存在');
    }
    throw new Error(errorData?.detail || '更新组件失败');
  }

  return response.json();
}

/**
 * 删除当前用户对某组件的 AST 分析结果。
 *
 * 组件本身不从注册表删除；后端会把它标记为不可用（enabled=false），
 * 表示「暂未被 AST 分析」。返回更新后的组件信息（含 enabled=false）。
 *
 * @returns {deleted, component}；401 → 「登录已过期」，404 → 「组件不存在」。
 */
export async function deleteAstAnalysis(
  name: string,
): Promise<{ deleted: boolean; component: ComponentInfo }> {
  const response = await fetch(`${API_BASE}/ast/components/${encodeURIComponent(name)}`, {
    method: 'DELETE',
    headers: authHeaders(),
  });

  if (response.status === 401) onUnauthorized();
  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    if (response.status === 404) {
      throw new Error('组件不存在');
    }
    throw new Error(errorData?.detail || '删除 AST 分析失败');
  }

  return response.json();
}

/**
 * 删除组件
 */
export async function deleteComponent(name: string): Promise<void> {
  const response = await fetch(`${API_BASE}/components/${encodeURIComponent(name)}`, {
    method: 'DELETE',
    headers: authHeaders(),
  });

  if (response.status === 401) onUnauthorized();
  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    if (response.status === 404) {
      throw new Error('组件不存在');
    }
    throw new Error(errorData?.detail || '删除组件失败');
  }
}

// ============================================================
// 认证（Auth）API
// ============================================================

/**
 * 注册新账号。
 * @returns 含 token 的认证响应；409 → "用户名已存在"，400 → 后端 detail。
 */
export async function register(username: string, password: string): Promise<AuthResponse> {
  const response = await fetch(`${API_BASE}/auth/register`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  });

  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    if (response.status === 409) {
      throw new Error('用户名已存在');
    }
    throw new Error(errorData?.detail || '注册失败');
  }

  return response.json();
}

/**
 * 登录。@returns 认证响应；401 → "用户名或密码错误"。
 */
export async function login(username: string, password: string): Promise<AuthResponse> {
  const response = await fetch(`${API_BASE}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  });

  if (!response.ok) {
    if (response.status === 401) {
      throw new Error('用户名或密码错误');
    }
    const errorData = await response.json().catch(() => null);
    throw new Error(errorData?.detail || '登录失败');
  }

  return response.json();
}

/**
 * 用持久化的 token 校验当前登录用户。
 * @throws token 缺失或失效时抛出。
 */
export async function fetchMe(): Promise<AuthUser> {
  const response = await fetch(`${API_BASE}/auth/me`, {
    headers: authHeaders(),
  });

  if (!response.ok) {
    throw new Error('认证失效');
  }

  return response.json();
}

/**
 * 登出（best-effort）：吊销服务端 token，忽略网络/响应错误。
 */
export async function logout(): Promise<void> {
  try {
    await fetch(`${API_BASE}/auth/logout`, {
      method: 'POST',
      headers: authHeaders(),
    });
  } catch {
    // 登出失败不影响本地登出
  }
}

/**
 * 将 token 持久化到 localStorage（登录/注册成功后调用）。
 * 暴露为 public 是因为 LoginScreen/App 在认证成功回调里需要写 token。
 */
export function persistAuthToken(token: string): void {
  setAuthToken(token);
}

// ============================================================
// AST 语法分析 API
// ============================================================

/**
 * 获取当前用户已存储的 AST 分析结果。
 * @returns 分析结果；尚未分析时（HTTP 404）返回 null。
 */
export async function getAstResult(): Promise<AstResult | null> {
  const response = await fetch(`${API_BASE}/ast/result`, {
    headers: authHeaders(),
  });

  if (response.status === 401) onUnauthorized();
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    throw new Error(errorData?.detail || '获取 AST 结果失败');
  }

  return response.json();
}

/**
 * 提交组件进行 AST 语法分析（SSE 流式）。
 *
 * 复用 readSSE 解析 `data:` 行。事件联合见 AstAnalyzeEvent。
 *
 * @param componentNames 待分析的组件名列表（非空，否则后端在流式前返回 400）。
 * @param signal         可选的 AbortSignal，用于停止按钮中断流。
 * @yields               AstAnalyzeEvent。
 */
export async function* analyzeAstStream(
  componentNames: string[],
  signal?: AbortSignal,
): AsyncGenerator<AstAnalyzeEvent> {
  const response = await fetch(`${API_BASE}/ast/analyze`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ component_names: componentNames }),
    signal,
  });

  if (response.status === 401) onUnauthorized();
  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    throw new Error(errorData?.detail || `服务器错误: ${response.status}`);
  }

  for await (const event of readSSE(response)) {
    yield event as AstAnalyzeEvent;
  }
}

// ============================================================
// Wiki 知识库 API（全局共享的 openUBMC 文档检索库）
// ============================================================

/**
 * 获取全局 wiki 知识库索引状态（来源/commit/页数/片段数/同步时间）。
 * 未建索引时返回 { indexed: false, meta: null }。
 */
export async function getWikiStatus(): Promise<WikiStatus> {
  const response = await fetch(`${API_BASE}/wiki/status`, {
    headers: authHeaders(),
  });

  if (response.status === 401) onUnauthorized();
  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    throw new Error(errorData?.detail || '获取 wiki 状态失败');
  }

  return response.json();
}

/**
 * 触发 wiki 全量同步（SSE 流式）：从 openUBMC 文档站克隆 → 解析 → 重建索引。
 *
 * 复用 readSSE 解析 `data:` 行。事件联合见 WikiSyncEvent。结果是全局共享索引。
 *
 * @param signal 可选的 AbortSignal，用于停止按钮中断流。
 * @yields       WikiSyncEvent。
 */
export async function* syncWikiStream(
  signal?: AbortSignal,
): AsyncGenerator<WikiSyncEvent> {
  const response = await fetch(`${API_BASE}/wiki/sync`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({}),
    signal,
  });

  if (response.status === 401) onUnauthorized();
  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    throw new Error(errorData?.detail || `服务器错误: ${response.status}`);
  }

  for await (const event of readSSE(response)) {
    yield event as WikiSyncEvent;
  }
}
