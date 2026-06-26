/**
 * API 客户端 —— 封装对后端的 fetch 调用
 */

import type { ParseResult } from '../types/log';

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
export async function checkHealth(): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE}/health`);
    const data = await response.json();
    return data?.status === 'ok';
  } catch {
    return false;
  }
}
