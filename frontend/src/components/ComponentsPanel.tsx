/**
 * ComponentsPanel 组件 —— 组件注册表管理面板
 *
 * 功能：
 * - 列出所有已注册组件（名称 / Git 地址 / 分支 / 是否可用 / 操作）
 * - 是否可用：内联开关，点击切换 toggleComponent
 * - 编辑：行内表单，修改 git_url / branch / enabled，调用 updateComponent
 * - 删除：二次确认（✓/✗），调用 deleteComponent
 * - 添加组件：顶部按钮打开内联表单，调用 createComponent
 *
 * 样式遵循 WattVision 深色主题（详见 App.css 的 CSS 变量）。
 */

import { useEffect, useState } from 'react';
import type { ComponentInfo } from '../types/component';
import {
  listComponents,
  createComponent,
  updateComponent,
  toggleComponent,
  deleteComponent,
} from '../api/client';
import './ComponentsPanel.css';

/** 添加表单字段 */
interface AddForm {
  name: string;
  git_url: string;
  branch: string;
  enabled: boolean;
}

/** 编辑表单字段 */
interface EditForm {
  git_url: string;
  branch: string;
  enabled: boolean;
}

const EMPTY_ADD: AddForm = { name: '', git_url: '', branch: 'main', enabled: true };

export default function ComponentsPanel() {
  const [components, setComponents] = useState<ComponentInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  // ---- 添加表单 ----
  const [showAdd, setShowAdd] = useState(false);
  const [addForm, setAddForm] = useState<AddForm>(EMPTY_ADD);
  const [addError, setAddError] = useState<string | null>(null);

  // ---- 行内状态 ----
  const [editingName, setEditingName] = useState<string | null>(null);
  const [editForm, setEditForm] = useState<EditForm>({
    git_url: '',
    branch: '',
    enabled: false,
  });
  const [editError, setEditError] = useState<string | null>(null);

  // ---- 删除确认 + 行内开关错误 ----
  const [confirmName, setConfirmName] = useState<string | null>(null);
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});

  /** 初始加载组件列表 */
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const list = await listComponents();
        if (!cancelled) {
          setComponents(list);
          setLoadError(null);
        }
      } catch (e) {
        if (!cancelled) {
          setLoadError(e instanceof Error ? e.message : '加载组件失败');
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  /** 切换组件 enabled */
  async function handleToggle(comp: ComponentInfo) {
    // 乐观更新
    setComponents((prev) =>
      prev.map((c) => (c.name === comp.name ? { ...c, enabled: !c.enabled } : c)),
    );
    setRowErrors((prev) => {
      const next = { ...prev };
      delete next[comp.name];
      return next;
    });
    try {
      const updated = await toggleComponent(comp.name);
      setComponents((prev) => prev.map((c) => (c.name === comp.name ? updated : c)));
    } catch (e) {
      // 回滚
      setComponents((prev) =>
        prev.map((c) => (c.name === comp.name ? { ...c, enabled: comp.enabled } : c)),
      );
      setRowErrors((prev) => ({
        ...prev,
        [comp.name]: e instanceof Error ? e.message : '切换失败',
      }));
    }
  }

  /** 进入编辑模式 */
  function startEdit(comp: ComponentInfo) {
    setEditingName(comp.name);
    setEditForm({
      git_url: comp.git_url,
      branch: comp.branch,
      enabled: comp.enabled,
    });
    setEditError(null);
  }

  function cancelEdit() {
    setEditingName(null);
    setEditError(null);
  }

  /** 保存编辑 */
  async function saveEdit(name: string) {
    try {
      const updated = await updateComponent(name, editForm);
      setComponents((prev) => prev.map((c) => (c.name === name ? updated : c)));
      setEditingName(null);
      setEditError(null);
    } catch (e) {
      setEditError(e instanceof Error ? e.message : '保存失败');
    }
  }

  /** 确认删除 */
  async function handleDelete(name: string) {
    const prev = components;
    // 乐观移除
    setComponents((prev) => prev.filter((c) => c.name !== name));
    setConfirmName(null);
    try {
      await deleteComponent(name);
    } catch (e) {
      setComponents(prev);
      setRowErrors((prevErr) => ({
        ...prevErr,
        [name]: e instanceof Error ? e.message : '删除组件失败',
      }));
    }
  }

  /** 提交添加 */
  async function handleAdd() {
    setAddError(null);
    if (!addForm.name.trim()) {
      setAddError('请输入组件名称');
      return;
    }
    try {
      const created = await createComponent(addForm);
      setComponents((prev) => [created, ...prev]);
      setAddForm(EMPTY_ADD);
      setShowAdd(false);
    } catch (e) {
      setAddError(e instanceof Error ? e.message : '添加失败');
    }
  }

  function cancelAdd() {
    setShowAdd(false);
    setAddForm(EMPTY_ADD);
    setAddError(null);
  }

  return (
    <section className="components-panel">
      {/* ---- 头部 ---- */}
      <div className="components-panel-header">
        <h2 className="components-panel-title">组件管理</h2>
        {!showAdd && (
          <button className="add-component-btn" onClick={() => setShowAdd(true)}>
            <span className="add-component-icon">+</span>
            添加组件
          </button>
        )}
      </div>

      {/* ---- 添加表单 ---- */}
      {showAdd && (
        <div className="component-add-form">
          <div className="add-form-row">
            <label className="add-field">
              <span className="add-field-label">组件名称</span>
              <input
                className="component-input"
                type="text"
                value={addForm.name}
                placeholder="如 pcie_device"
                onChange={(e) => setAddForm({ ...addForm, name: e.target.value })}
              />
            </label>
            <label className="add-field add-field-grow">
              <span className="add-field-label">Git 地址</span>
              <input
                className="component-input component-input-mono"
                type="text"
                value={addForm.git_url}
                placeholder="https://github.com/org/repo.git"
                onChange={(e) => setAddForm({ ...addForm, git_url: e.target.value })}
              />
            </label>
            <label className="add-field">
              <span className="add-field-label">分支</span>
              <input
                className="component-input component-input-mono"
                type="text"
                value={addForm.branch}
                placeholder="main"
                onChange={(e) => setAddForm({ ...addForm, branch: e.target.value })}
              />
            </label>
            <label className="add-field add-field-check">
              <input
                type="checkbox"
                checked={addForm.enabled}
                onChange={(e) => setAddForm({ ...addForm, enabled: e.target.checked })}
              />
              <span>可用</span>
            </label>
          </div>
          {addError && <div className="add-form-error">{addError}</div>}
          <div className="add-form-actions">
            <button className="component-btn primary" onClick={handleAdd}>
              创建
            </button>
            <button className="component-btn" onClick={cancelAdd}>
              取消
            </button>
          </div>
        </div>
      )}

      {/* ---- 加载状态 ---- */}
      {loading ? (
        <div className="components-panel-state">
          <div className="spinner" />
          <p>加载中...</p>
        </div>
      ) : loadError ? (
        <div className="components-panel-state components-panel-error">
          <p>{loadError}</p>
        </div>
      ) : components.length === 0 ? (
        <div className="components-panel-state">
          <p>暂无注册组件</p>
          <p className="components-panel-hint">点击右上角“添加组件”开始注册</p>
        </div>
      ) : (
        <div className="components-table-wrap">
          <table className="components-table">
            <thead>
              <tr>
                <th>组件名称</th>
                <th>Git 地址</th>
                <th>分支</th>
                <th>是否可用</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {components.map((comp) => {
                const isEditing = editingName === comp.name;
                const isConfirming = confirmName === comp.name;
                const rowError = rowErrors[comp.name];

                if (isEditing) {
                  return (
                    <tr key={comp.name} className="components-row components-row-editing">
                      <td className="cell-name">{comp.name}</td>
                      <td>
                        <input
                          className="component-input component-input-mono"
                          type="text"
                          value={editForm.git_url}
                          onChange={(e) =>
                            setEditForm({ ...editForm, git_url: e.target.value })
                          }
                        />
                      </td>
                      <td>
                        <input
                          className="component-input component-input-mono"
                          type="text"
                          value={editForm.branch}
                          onChange={(e) =>
                            setEditForm({ ...editForm, branch: e.target.value })
                          }
                        />
                      </td>
                      <td>
                        <label className="cell-check">
                          <input
                            type="checkbox"
                            checked={editForm.enabled}
                            onChange={(e) =>
                              setEditForm({ ...editForm, enabled: e.target.checked })
                            }
                          />
                          <span>{editForm.enabled ? '可用' : '禁用'}</span>
                        </label>
                      </td>
                      <td className="cell-actions">
                        <button
                          className="component-btn primary small"
                          onClick={() => saveEdit(comp.name)}
                        >
                          保存
                        </button>
                        <button className="component-btn small" onClick={cancelEdit}>
                          取消
                        </button>
                        {editError && (
                          <span className="row-inline-error">{editError}</span>
                        )}
                      </td>
                    </tr>
                  );
                }

                return (
                  <tr key={comp.name} className="components-row">
                    <td className="cell-name">{comp.name}</td>
                    <td>
                      <span className="cell-mono">{comp.git_url}</span>
                    </td>
                    <td>
                      <span className="cell-mono">{comp.branch}</span>
                    </td>
                    <td>
                      <button
                        className={`toggle-badge ${comp.enabled ? 'on' : 'off'}`}
                        onClick={() => handleToggle(comp)}
                        title={comp.enabled ? '点击禁用' : '点击启用'}
                      >
                        <span className="toggle-knob" />
                        <span className="toggle-text">{comp.enabled ? '可用' : '禁用'}</span>
                      </button>
                      {rowError && <span className="row-inline-error">{rowError}</span>}
                    </td>
                    <td className="cell-actions">
                      {isConfirming ? (
                        <span className="delete-confirm">
                          <span className="confirm-text">确认删除?</span>
                          <span
                            className="confirm-yes"
                            onClick={() => handleDelete(comp.name)}
                            title="确认删除"
                          >
                            ✓
                          </span>
                          <span
                            className="confirm-no"
                            onClick={() => setConfirmName(null)}
                            title="取消"
                          >
                            ✕
                          </span>
                        </span>
                      ) : (
                        <>
                          <button
                            className="component-btn small"
                            onClick={() => startEdit(comp)}
                          >
                            编辑
                          </button>
                          <button
                            className="component-btn small danger"
                            onClick={() => setConfirmName(comp.name)}
                          >
                            删除
                          </button>
                        </>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
