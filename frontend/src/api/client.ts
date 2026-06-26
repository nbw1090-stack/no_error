/**
 * API 客户端 —— 封装对后端的 fetch 调用
 */

import type {
  ParseResult,
  ChatRequest,
  ChatResponse,
  SessionInfo,
  SessionDetail,
} from '../types/log';

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

// ============================================================
// 数据集 API
// ============================================================

/**
 * 获取数据集详情（含日志条目和汇总统计）
 *
 * 用于会话切换时重新加载日志分析数据。
 */
export async function getDataset(datasetId: string): Promise<ParseResult> {
  const response = await fetch(`${API_BASE}/datasets/${datasetId}`);

  if (!response.ok) {
    throw new Error('数据集不存在');
  }

  const data = await response.json();
  return {
    success: true,
    dataset_id: data.dataset_id,
    summary: data.summary,
    entries: data.entries,
    errors: [],
  };
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
