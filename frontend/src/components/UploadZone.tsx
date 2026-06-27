/**
 * UploadZone 组件 —— 拖拽上传区域
 *
 * 支持拖拽和点击上传 tar.gz 压缩包。
 *
 * 流式解析职责划分：本组件只负责文件校验与透传，真正的 SSE 消费在父组件（App）进行——
 * 因为 summary 一到父组件就会渲染仪表盘并卸载本组件，消费循环不能放在这里。
 * 父组件通过 streamState 把当前阶段/进度传回来用于展示。
 */

import { useState, useRef, type DragEvent, type ChangeEvent } from 'react';
import './UploadZone.css';

/** 流式解析状态（由父组件传入） */
export interface UploadStreamState {
  stage: 'extracting' | 'parsing' | null;
}

interface UploadZoneProps {
  /** 文件校验通过后，透传给父组件消费 SSE 流 */
  onFile: (file: File) => void;
  onError: (error: string) => void;
  /** 流式解析状态，存在则展示阶段/进度 */
  streamState?: UploadStreamState | null;
}

/** 根据 stage 生成进度文案 */
function stageText(state: UploadStreamState): string {
  switch (state.stage) {
    case 'extracting':
      return '正在解压日志文件...';
    case 'parsing':
      return '正在解析日志...';
    default:
      return '正在处理...';
  }
}

export default function UploadZone({ onFile, onError, streamState }: UploadZoneProps) {
  const [isDragover, setIsDragover] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const streaming = !!streamState?.stage;

  /** 处理文件上传：校验后透传给父组件 */
  function handleFile(file: File) {
    // 校验文件类型
    if (!file.name.endsWith('.tar.gz') && !file.name.endsWith('.tgz')) {
      const msg = '请上传 .tar.gz 格式的压缩包';
      setErrorMessage(msg);
      onError(msg);
      return;
    }

    setErrorMessage(null);
    onFile(file);
  }

  /** 点击触发文件选择 */
  function handleClick() {
    if (!streaming) {
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
    if (streaming) return;
    const file = e.dataTransfer.files?.[0];
    if (file) {
      handleFile(file);
    }
  }

  const zoneClasses = [
    'upload-zone',
    isDragover ? 'dragover' : '',
    streaming ? 'loading' : '',
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

      {streaming && streamState ? (
        <>
          <div className="spinner" />
          <p className="upload-text">{stageText(streamState)}</p>
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
