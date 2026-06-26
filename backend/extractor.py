"""
Tar.gz 解压器

从 dump_info.tar.gz 中提取 app.log 和 framework.log 文件内容。
仅匹配路径中包含 LogDump/app.log 和 LogDump/framework.log 的成员。
"""

import tarfile
import os


def extract_logs(tar_path: str) -> tuple[str, str]:
    """
    解压 tar.gz 文件，提取 app.log 和 framework.log 的内容。

    Args:
        tar_path: tar.gz 文件的路径

    Returns:
        (app_log_content, framework_log_content) 两个字符串元组。
        若某文件不存在，对应内容为空字符串。
    """
    app_content = ""
    framework_content = ""

    # 使用 tarfile 打开 gzip 压缩的 tar 包
    with tarfile.open(tar_path, "r:gz") as tar:
        for member in tar.getmembers():
            # 仅处理普通文件，跳过目录
            if not member.isfile():
                continue

            # 规范化路径分隔符，统一使用 /
            normalized_name = member.name.replace("\\", "/")

            # 判断是否为 app.log
            if normalized_name.endswith("LogDump/app.log"):
                f = tar.extractfile(member)
                if f:
                    app_content = f.read().decode("utf-8", errors="ignore")
                    f.close()

            # 判断是否为 framework.log
            elif normalized_name.endswith("LogDump/framework.log"):
                f = tar.extractfile(member)
                if f:
                    framework_content = f.read().decode("utf-8", errors="ignore")
                    f.close()

    return app_content, framework_content
