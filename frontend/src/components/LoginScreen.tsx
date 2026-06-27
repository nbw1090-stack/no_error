/**
 * LoginScreen —— 登录 / 注册界面
 *
 * 在用户未认证时由 App 渲染（auth gate）。两个 Tab：登录 / 注册。
 * 认证成功后调用 onAuthed，由 App 持久化 token 并切换主视图。
 *
 * 复用 WattVision 深色主题 token 与 ComponentsPanel.css 中的通用类
 * （.component-input / .component-btn.primary）。
 */

import { useState } from 'react';
import type { FormEvent } from 'react';
import type { AuthResponse } from '../types/auth';
import { login, register } from '../api/client';
import './LoginScreen.css';

interface LoginScreenProps {
  /** 认证成功回调（App 据此持久化 token + 设置 currentUser）。 */
  onAuthed: (auth: AuthResponse) => void;
}

type Mode = 'login' | 'register';

const MIN_LEN = 3;

export default function LoginScreen({ onAuthed }: LoginScreenProps) {
  const [mode, setMode] = useState<Mode>('login');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  function switchMode(next: Mode) {
    if (next === mode) return;
    setMode(next);
    setError(null);
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);

    if (username.trim().length < MIN_LEN || password.length < MIN_LEN) {
      setError(`用户名和密码至少 ${MIN_LEN} 个字符`);
      return;
    }

    setSubmitting(true);
    try {
      const auth =
        mode === 'login'
          ? await login(username.trim(), password)
          : await register(username.trim(), password);
      onAuthed(auth);
    } catch (err) {
      setError(err instanceof Error ? err.message : '操作失败，请重试');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="login-screen">
      <div className="login-card">
        <h1 className="login-title">BMC 日志分析系统</h1>
        <p className="login-subtitle">请登录后使用组件 AST 语法分析</p>

        <div className="login-tabs">
          <button
            type="button"
            className={`login-tab ${mode === 'login' ? 'active' : ''}`}
            onClick={() => switchMode('login')}
          >
            登录
          </button>
          <button
            type="button"
            className={`login-tab ${mode === 'register' ? 'active' : ''}`}
            onClick={() => switchMode('register')}
          >
            注册
          </button>
        </div>

        <form className="login-form" onSubmit={handleSubmit}>
          <label className="login-field">
            <span className="login-field-label">用户名</span>
            <input
              className="component-input"
              type="text"
              autoComplete="username"
              value={username}
              placeholder={`至少 ${MIN_LEN} 个字符`}
              onChange={(e) => setUsername(e.target.value)}
              disabled={submitting}
            />
          </label>

          <label className="login-field">
            <span className="login-field-label">密码</span>
            <input
              className="component-input"
              type="password"
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
              value={password}
              placeholder={`至少 ${MIN_LEN} 个字符`}
              onChange={(e) => setPassword(e.target.value)}
              disabled={submitting}
            />
          </label>

          {error && <div className="login-error">{error}</div>}

          <button
            type="submit"
            className="component-btn primary login-submit"
            disabled={submitting}
          >
            {submitting ? '处理中...' : mode === 'login' ? '登录' : '注册'}
          </button>
        </form>
      </div>
    </div>
  );
}
