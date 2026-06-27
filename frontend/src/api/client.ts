/**
 * API 客户端 —— 封装对后端的 fetch 调用
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

const API_BASE = '/api';

/**
 * 上传 tar.gz 文件并获取解析结果
 */
export async function uploadAndParse(file: File): Promise<ParseResult> {
  const formData = new FormData();
  formData.append('file', file);

  const response = await fetch(`${API_BASE}/parse`, {
    method: 'POST',
    body: formData,
  });

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
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
    signal,
  });

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
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
    signal,
  });

  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    throw new Error(errorData?.detail || `服务器错误: ${response.status}`);
  }

  for await (const event of readSSE(response)) {
    yield event as StreamEvent;
  }
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
    body: formData,
    signal,
  });

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
  const response = await fetch(`${API_BASE}/datasets/${datasetId}`);

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
  );

  if (!response.ok) {
    throw new Error('加载日志条目失败');
  }

  return response.json();
}

// ============================================================
// 会话管理 API
// ============================================================

/**
 * 创建新的聊天会话
 */
export async function createSession(datasetId: string): Promise<SessionInfo> {
  const response = await fetch(`${API_BASE}/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ dataset_id: datasetId }),
  });

  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    throw new Error(errorData?.detail || '创建会话失败');
  }

  return response.json();
}

/**
 * 获取会话详情（含完整消息历史）
 */
export async function getSession(sessionId: string): Promise<SessionDetail> {
  const response = await fetch(`${API_BASE}/sessions/${sessionId}`);

  if (!response.ok) {
    throw new Error('会话不存在');
  }

  return response.json();
}

/**
 * 列出所有会话
 */
export async function listSessions(): Promise<{ sessions: SessionInfo[] }> {
  const response = await fetch(`${API_BASE}/sessions`);

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
  });

  if (!response.ok) {
    throw new Error('删除会话失败');
  }
}

// ============================================================
// 组件注册表 API
// ============================================================

/**
 * 列出所有已注册组件
 */
export async function listComponents(): Promise<ComponentInfo[]> {
  const response = await fetch(`${API_BASE}/components`);

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
  comp: Omit<ComponentInfo, never>,
): Promise<ComponentInfo> {
  const response = await fetch(`${API_BASE}/components`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(comp),
  });

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
 * 更新组件（git_url / branch / enabled）
 */
export async function updateComponent(
  name: string,
  comp: Pick<ComponentInfo, 'git_url' | 'branch' | 'enabled'>,
): Promise<ComponentInfo> {
  const response = await fetch(`${API_BASE}/components/${encodeURIComponent(name)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(comp),
  });

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
 * 切换组件的 enabled 状态
 */
export async function toggleComponent(name: string): Promise<ComponentInfo> {
  const response = await fetch(`${API_BASE}/components/${encodeURIComponent(name)}/toggle`, {
    method: 'PATCH',
  });

  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    if (response.status === 404) {
      throw new Error('组件不存在');
    }
    throw new Error(errorData?.detail || '切换组件状态失败');
  }

  return response.json();
}

/**
 * 删除组件
 */
export async function deleteComponent(name: string): Promise<void> {
  const response = await fetch(`${API_BASE}/components/${encodeURIComponent(name)}`, {
    method: 'DELETE',
  });

  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    if (response.status === 404) {
      throw new Error('组件不存在');
    }
    throw new Error(errorData?.detail || '删除组件失败');
  }
}
