"""
LLM Wiki 模块（全局共享，不按用户隔离）—— Karpathy「LLM Wiki」模式

不把 openUBMC 原始文档切片检索，而是让 LLM 把精选文档**提炼**成一套结构化、互相链接的
markdown 知识库（落盘为 markdown 文件 + 索引页），agent 直接读整页，结合 BMC 架构、源码、
日志作答。

- store：磁盘 markdown 存储（index.md / pages/<slug>.md / manifest.json）+ 检索兜底
- compile：LLM 提炼流水线（规划精选源 → 逐页提炼 → 互链 → 写索引总览）
- service：SSE 编译编排（克隆 → 有界并发提炼 → 索引 → manifest，增量可续编）
- routes：/api/wiki/sync（SSE）+ /status + /index + /page + /search

模块导出：
- init_wiki_dir / set_wiki_dir：wiki 目录初始化与路径重定向（测试用）
- set_llm：注入 LLM adapter（编译需要）
- router：FastAPI APIRouter（前缀 /api/wiki）
"""

from wiki.store import init_wiki_dir, set_wiki_dir
from wiki.routes import router, set_llm

__all__ = ["init_wiki_dir", "set_wiki_dir", "set_llm", "router"]
