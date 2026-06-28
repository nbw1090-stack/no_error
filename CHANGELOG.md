# Changelog

本项目所有版本变更记录于此。版本号与 [VERSION](VERSION) 文件**严格绑定**：每次提交
都必须同步更新 `VERSION`（末位字段 +1）与本文件，并在对应版本下用一句话写明本次改动。
版本格式 `主.次.补`，当前约定每次提交 **补丁位 +1**（如 0.0.0 → 0.0.1）。

每个版本的效果存档放在 [result/](result/)（如 `result/v0.0.0.md`）。

---

## 0.0.0 — 2026-06-28

首版基线：BMC 日志分析 ReAct agent + 评测评估框架。

- 评测框架落地：13 条黄金用例（10 源码可定位 + 3 负例）、13 项指标（10 确定性 + 3 LLM 裁判），
  确定性指标可离线跑 CI，裁判缺失自动降级。
- 离线与在线评测**双模式均接入 Langfuse**：在线 `run_experiment` → Datasets→Runs 做版本对比；
  离线 `--offline` 出本地记分卡并回灌 Traces（按 `session=eval-<run_name>` 分组）。
- 工具轨迹采集（`eval/trajectory.py`，从持久化 session 重建，不改 agent 热路径）、
  固定数据集 bootstrap、黄金用例上传脚本。
- 修复：`run_experiment` 在运行中事件循环内驱动 task 导致的 `asyncio.run` 崩溃（新增 `eval/aio.py`
  线程化 runner）；Langfuse 拒收 `value=None` score（`na_to_none` 包装，不适用指标返回 None 跳过）。
- 修复：评测临时会话改用 `user_id=3` 创建，否则源码工具永不暴露、诊断退化为纯日志问答。
- 基线结果存档于 `result/v0.0.0.md`：source_tool_used 1.00，但 rootcause_correctness≈0.34、
  faithfulness≈0.36 为弱项，下一版重点改进。
