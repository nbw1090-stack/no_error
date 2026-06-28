"""
离线把本地 dump_info/LogDump 解析成一份**固定 id** 的数据集 JSON，让评测自带
数据、可复现——无需先在前端上传 dump_info.tar.gz 拿随机 dataset_id。

产物：backend/data/datasets/<dataset_id>.json（默认 dataset_id=eval_bmc_applog），
形状与 main.py 上传落盘完全一致（entries + summary + user_id），可直接被
run_eval._load_dataset 读取，也是 golden_cases.json 里 input.dataset_id 的取值。

为什么 user_id=3：source/3 下已构建 bios/network_adapter/pcie_device/sensor 的
AST 索引；agent 是否暴露源码工具以 session.user_id 为准（core._load_source_components），
故黄金用例统一用 user_id=3 才能让源码诊断链路真正生效。

运行（在 backend/ 下，venv 可选——本脚本不碰 tree-sitter）：
    cd backend && python -m eval.bootstrap_dataset
    cd backend && python -m eval.bootstrap_dataset --id eval_bmc_applog --user-id 3
"""

import argparse
import json
import logging
import os

from config import AppConfig
from parser import parse_logs

logger = logging.getLogger(__name__)

# 默认 dump 目录：repo_root/dump_info/LogDump（backend/eval/ 往上两级是 repo 根）
_DEFAULT_LOGDUMP = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "dump_info", "LogDump")
)
_DEFAULT_DATASET_ID = "eval_bmc_applog"
_DEFAULT_USER_ID = 3


def _atomic_write_json(path: str, data: dict) -> None:
    """原子写 JSON（复刻 main.atomic_write_json，避免导入 main 触发整个 app 初始化）。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read(path: str) -> str:
    """读日志文件；不存在则返回空串（parse_logs 会跳过空内容）。"""
    if not os.path.exists(path):
        logger.warning("日志文件不存在，按空处理：%s", path)
        return ""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def build_dataset(
    logdump_dir: str, dataset_id: str, user_id: int, data_dir: str
) -> str:
    """解析 logdump_dir 下的 app.log/framework.log，写出固定 id 的数据集 JSON。"""
    app_content = _read(os.path.join(logdump_dir, "app.log"))
    framework_content = _read(os.path.join(logdump_dir, "framework.log"))
    if not app_content and not framework_content:
        raise SystemExit(f"未在 {logdump_dir} 找到 app.log/framework.log")

    result = parse_logs(app_content, framework_content)

    datasets_dir = os.path.join(data_dir, "datasets")
    os.makedirs(datasets_dir, exist_ok=True)
    path = os.path.join(datasets_dir, f"{dataset_id}.json")
    _atomic_write_json(
        path,
        {
            "dataset_id": dataset_id,
            "entries": result["entries"],
            "summary": result["summary"],
            "user_id": user_id,
            "username": "eval",
        },
    )
    logger.info(
        "数据集已写入 %s（%d 条目，user_id=%s）",
        path,
        len(result["entries"]),
        user_id,
    )
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="把 dump_info/LogDump 解析成固定数据集")
    parser.add_argument("--logdump", default=_DEFAULT_LOGDUMP, help="LogDump 目录")
    parser.add_argument("--id", default=_DEFAULT_DATASET_ID, help="数据集固定 id")
    parser.add_argument(
        "--user-id", type=int, default=_DEFAULT_USER_ID, help="归属用户（决定源码工具可见性）"
    )
    args = parser.parse_args()

    config = AppConfig.from_env()
    build_dataset(args.logdump, args.id, args.user_id, config.data_dir)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    main()
