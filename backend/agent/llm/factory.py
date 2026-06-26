"""
LLM 适配器工厂

根据配置创建对应的 LLM 适配器实例。
"""

from config import LLMConfig
from agent.llm.base import BaseLLMAdapter
from agent.llm.openai_adapter import OpenAIAdapter


def create_llm(config: LLMConfig) -> BaseLLMAdapter:
    """
    根据配置创建 LLM 适配器。

    当前支持所有 OpenAI 兼容接口（通过 api_base 切换），
    包括：OpenAI、Ollama、vLLM、LM Studio、Groq 等。

    未来可扩展 Anthropic、Google 等原生适配器。
    """
    if config.provider in ("openai", "ollama", "custom"):
        return OpenAIAdapter(config)

    raise ValueError(f"Unsupported LLM provider: {config.provider}")
