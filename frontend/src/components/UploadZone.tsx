/**
 * UploadZone 组件 —— 拖拽上传区域
 * 支持拖拽和点击上传 tar.gz 压缩包，显示加载状态和错误信息
 */

import { useState, useRef, type DragEvent, type ChangeEvent } from 'react';
import { uploadAndParse } from '../api/client';
import type { ParseResult } from '../types/log';
import './UploadZone.css';

interface UploadZoneProps {
  onParseResult: (result: ParseResult) => void;
  onError: (error: string) => void;
}

export default function UploadZone({ onParseResult, onError }: UploadZoneProps) {
  const [isDragover, setIsDragover] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  /** 处理文件上传 */
  async function handleFile(file: File) {
    // 校验文件类型
    if (!file.name.endsWith('.tar.gz') && !file.name.endsWith('.tgz')) {
      const msg = '请上传 .tar.gz 格式的压缩包';
      setErrorMessage(msg);
      onError(msg);
      return;
    }

    setIsLoading(true);
    setErrorMessage(null);

    try {
      const result = await uploadAndParse(file);
      onParseResult(result);
    } catch (err) {
      const msg = err instanceof Error ? err.message : '解析失败，请重试';
      setErrorMessage(msg);
      onError(msg);
    } finally {
      setIsLoading(false);
    }
  }

  /** 点击触发文件选择 */
  function handleClick() {
    if (!isLoading) {
      fileInputRef.current?.click();
    }
  }

  /** 文件选择回调 */
  function handleFileChange(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (file) {
      handleFile(file);
    }
    // 重置 input 以允许重新选择同一文件
    e.target.value = '';
  }

  /** 拖拽事件 */
  function handleDragOver(e: DragEvent) {
    e.preventDefault();
    setIsDragover(true);
  }

  function handleDragLeave(e: DragEvent) {
    e.preventDefault();
    setIsDragover(false);
  }

  function handleDrop(e: DragEvent) {
    e.preventDefault();
    setIsDragover(false);

    const file = e.dataTransfer.files?.[0];
    if (file) {
      handleFile(file);
    }
  }

  const zoneClasses = [
    'upload-zone',
    isDragover ? 'dragover' : '',
    isLoading ? 'loading' : '',
    errorMessage ? 'has-error' : '',
  ].filter(Boolean).join(' ');

  return (
    <div
      className={zoneClasses}
      onClick={handleClick}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      {/* 隐藏的文件输入 */}
      <input
        ref={fileInputRef}
        type="file"
        accept=".tar.gz,.tgz"
        onChange={handleFileChange}
        style={{ display: 'none' }}
      />

      {isLoading ? (
        <>
          <div className="spinner" />
          <p className="upload-text">正在解析日志文件...</p>
        </>
      ) : (
        <>
          <div className="upload-icon">📁</div>
          <p className="upload-text">拖拽 dump_info.tar.gz 到此处上传</p>
          <p className="upload-hint">或点击此区域选择文件（仅支持 .tar.gz 格式）</p>
        </>
      )}

      {errorMessage && (
        <div className="error-message">{errorMessage}</div>
      )}
    </div>
  );
}
