"""
应用配置

所有配置项通过环境变量注入，支持 .env 文件。
用户可自行配置 LLM 提供商、模型、API Key 等。

配置优先级：环境变量 > .env 文件 > 默认值
"""

import os
from dataclasses import dataclass, field


# ============================================================
# .env 文件加载（在读取任何配置之前执行）
# ============================================================
def _load_dotenv(dotenv_path: str | None = None) -> None:
    """
    简易 .env 文件加载器（无需 python-dotenv 依赖）。

    支持格式：
        KEY=value
        KEY="value"
        KEY='value'
        # 注释行
        空行

    Args:
        dotenv_path: .env 文件路径，默认为 config.py 同级目录下的 .env
    """
    if dotenv_path is None:
        dotenv_path = os.path.join(os.path.dirname(__file__), ".env")

    if not os.path.exists(dotenv_path):
        return

    with open(dotenv_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            # 跳过空行和注释
            if not line or line.startswith("#"):
                continue

            # 解析 KEY=VALUE
            if "=" not in line:
                continue

            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()

            # 去除引号
            if len(value) >= 2:
                if (value.startswith('"') and value.endswith('"')) or \
                   (value.startswith("'") and value.endswith("'")):
                    value = value[1:-1]

            # 只设置尚未在环境变量中定义的键（环境变量优先级更高）
            if key and key not in os.environ:
                os.environ[key] = value


# 模块导入时自动加载 .env 文件
_load_dotenv()


# ============================================================
# 配置数据类
# ============================================================

@dataclass
class LLMConfig:
    """
    LLM 配置 —— 支持任意 OpenAI 兼容接口。

    配置方式（按优先级）：
    1. 环境变量：export LLM_API_KEY=sk-xxx
    2. .env 文件：在 backend/.env 中写入 LLM_API_KEY=sk-xxx
    3. 默认值

    示例：
        # 使用 OpenAI
        LLM_PROVIDER=openai
        LLM_API_KEY=sk-xxx
        LLM_MODEL=gpt-4o

        # 使用本地 Ollama
        LLM_PROVIDER=ollama
        LLM_API_BASE=http://localhost:11434/v1
        LLM_MODEL=qwen2.5
        LLM_API_KEY=ollama   # Ollama 不需要真实 key，但不能为空
    """

    provider: str = "openai"
    model: str = "gpt-4o"
    api_key: str = ""
    api_base: str = ""
    temperature: float = 0.3
    max_tokens: int = 4096

    @classmethod
    def from_env(cls) -> "LLMConfig":
        return cls(
            provider=os.getenv("LLM_PROVIDER", "openai"),
            model=os.getenv("LLM_MODEL", "gpt-4o"),
            api_key=os.getenv("LLM_API_KEY", os.getenv("OPENAI_API_KEY", "")),
            api_base=os.getenv("LLM_API_BASE", ""),
            temperature=float(os.getenv("LLM_TEMPERATURE", "0.3")),
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "4096")),
        )


@dataclass
class LangfuseConfig:
    """
    Langfuse 可观测性配置。

    配置方式（按优先级）：
    1. 环境变量
    2. .env 文件
    3. 默认值（enabled=False 时只打印日志，不上报）

    示例：
        LANGFUSE_ENABLED=true
        LANGFUSE_PUBLIC_KEY=pk-lf-xxx
        LANGFUSE_SECRET_KEY=sk-lf-xxx
        LANGFUSE_HOST=https://cloud.langfuse.com
    """

    enabled: bool = False
    public_key: str = ""
    secret_key: str = ""
    host: str = "https://cloud.langfuse.com"
    # Prompt Management：托管「数据分析模式」system prompt 的 Langfuse prompt 名 / 标签。
    # 拉取失败时 agent 自动回退到本地 build_system_prompt（保持优雅降级约定）。
    prompt_name: str = "bmc-system"
    prompt_label: str = "production"

    @classmethod
    def from_env(cls) -> "LangfuseConfig":
        return cls(
            enabled=os.getenv("LANGFUSE_ENABLED", "false").lower() == "true",
            public_key=os.getenv("LANGFUSE_PUBLIC_KEY", ""),
            secret_key=os.getenv("LANGFUSE_SECRET_KEY", ""),
            host=os.getenv("LANGFUSE_BASE_URL", os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")),
            prompt_name=os.getenv("LANGFUSE_PROMPT_NAME", "bmc-system"),
            prompt_label=os.getenv("LANGFUSE_PROMPT_LABEL", "production"),
        )


@dataclass
class AppConfig:
    """应用全局配置"""

    llm: LLMConfig = field(default_factory=LLMConfig.from_env)
    langfuse: LangfuseConfig = field(default_factory=LangfuseConfig.from_env)
    data_dir: str = field(
        default_factory=lambda: os.path.join(os.path.dirname(__file__), "data")
    )
    max_tool_iterations: int = 10
    max_history_messages: int = 40

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls(
            llm=LLMConfig.from_env(),
            langfuse=LangfuseConfig.from_env(),
            max_tool_iterations=int(os.getenv("AGENT_MAX_ITERATIONS", "10")),
            max_history_messages=int(os.getenv("AGENT_MAX_HISTORY", "40")),
        )
