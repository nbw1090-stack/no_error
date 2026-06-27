"""create_llm 工厂单元测试"""

import pytest

from config import LLMConfig
from agent.llm.factory import create_llm
from agent.llm.openai_adapter import OpenAIAdapter


def test_provider_openai_returns_openai_adapter():
    adapter = create_llm(LLMConfig(provider="openai", api_key="sk-test"))
    assert isinstance(adapter, OpenAIAdapter)


def test_provider_ollama_returns_openai_adapter():
    adapter = create_llm(LLMConfig(provider="ollama", api_key="x", api_base="http://localhost:11434/v1"))
    assert isinstance(adapter, OpenAIAdapter)


def test_provider_custom_returns_openai_adapter():
    adapter = create_llm(LLMConfig(provider="custom", api_key="x"))
    assert isinstance(adapter, OpenAIAdapter)


def test_provider_unknown_raises_valueerror():
    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        create_llm(LLMConfig(provider="anthropic", api_key="x"))
