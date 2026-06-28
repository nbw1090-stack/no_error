"""
把本地 golden_cases.json 推到 Langfuse 成为一个 Dataset（每条一个 item），
之后 run_eval --dataset <name> 即可在 Langfuse Datasets→Runs 做回归对比。

幂等性：Langfuse 按 (dataset, item id) 累积；本脚本用 input.question 的稳定指纹
作为 item id，重复运行只更新同一条而非堆叠重复项。

运行（在 backend/ 下，Langfuse 已配置）：
    cd backend && python -m eval.upload_dataset --dataset bmc-diagnosis-golden
"""

import argparse
import hashlib
import json
import logging
import os

from config import AppConfig
from observability import init_observability, get_client

logger = logging.getLogger(__name__)

_DEFAULT_CASES = os.path.join(os.path.dirname(__file__), "datasets", "golden_cases.json")


def _item_id(case: dict) -> str:
    """用问题文本的短哈希作稳定 item id，保证重复上传幂等。"""
    q = (case.get("input") or {}).get("question", "")
    return "case-" + hashlib.sha1(q.encode("utf-8")).hexdigest()[:12]


def main(dataset_name: str, cases_path: str) -> None:
    with open(cases_path, "r", encoding="utf-8") as f:
        cases = json.load(f)

    config = AppConfig.from_env()
    init_observability(config.langfuse)
    langfuse = get_client()._langfuse
    if langfuse is None:
        raise SystemExit("Langfuse 未启用/未初始化，请检查 LANGFUSE_* 配置")

    # 数据集不存在则创建（已存在时 create_dataset 在多数版本是幂等 upsert）
    try:
        langfuse.create_dataset(name=dataset_name)
    except Exception as e:  # noqa: BLE001 — 已存在等情况忽略，继续灌 item
        logger.info("create_dataset 跳过（可能已存在）：%s", e)

    for case in cases:
        langfuse.create_dataset_item(
            dataset_name=dataset_name,
            id=_item_id(case),
            input=case["input"],
            expected_output=case.get("expected_output"),
            metadata=case.get("metadata"),
        )
    langfuse.flush()
    logger.info("已上传 %d 条用例到 Langfuse dataset '%s'", len(cases), dataset_name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="上传黄金用例到 Langfuse dataset")
    parser.add_argument("--dataset", default="bmc-diagnosis-golden", help="Langfuse dataset 名称")
    parser.add_argument("--cases", default=_DEFAULT_CASES, help="本地 golden_cases.json 路径")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    main(args.dataset, args.cases)
