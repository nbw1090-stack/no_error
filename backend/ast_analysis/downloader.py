"""
git 源码下载（subprocess）

仅用 git CLI，不引入 GitPython / dulwich 等依赖。

- remote_commit：git ls-remote 解析远端分支 HEAD sha（不克隆，用于增量比对）
- clone_component：git clone --depth 1 浅克隆 + rev-parse HEAD

阻塞调用：所有函数都是同步的，由调用方（service.py）经 asyncio.to_thread 包装。
"""

import subprocess


def remote_commit(git_url: str, branch: str, timeout: int = 30) -> str | None:
    """
    git ls-remote <git_url> <branch>，返回匹配 ref 的 sha（40 字符），失败返回 None。

    ls-remote 输出形如：
        <sha>\trefs/heads/main
        <sha>\trefs/tags/v1.0
    取第一行的第一个 token。
    """
    ref = branch or "HEAD"
    try:
        proc = subprocess.run(
            ["git", "ls-remote", git_url, ref],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    if not out:
        return None
    first_line = out.splitlines()[0]
    sha = first_line.split()[0]
    return sha or None


def clone_component(
    git_url: str, branch: str, dest_dir: str, timeout: int = 300
) -> str:
    """
    浅克隆指定分支到 dest_dir，返回 HEAD commit sha。

    branch 为空 → 克隆默认分支（省略 --branch）。
    非零退出 → 抛 RuntimeError(stderr)，由调用方捕获并作为组件错误上报。
    """
    cmd = ["git", "clone", "--depth", "1"]
    if branch:
        cmd += ["--branch", branch]
    cmd += [git_url, dest_dir]

    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0:
        raise RuntimeError(
            (proc.stderr or proc.stdout or "git clone failed").strip()
            or "git clone failed"
        )

    # 取 HEAD sha
    rev = subprocess.run(
        ["git", "-C", dest_dir, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if rev.returncode != 0:
        raise RuntimeError(
            (rev.stderr or "git rev-parse failed").strip()
            or "git rev-parse failed"
        )
    return rev.stdout.strip()
