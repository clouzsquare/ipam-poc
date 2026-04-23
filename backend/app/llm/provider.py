"""LLM Provider 추상화 계층.

현재 지원:
- Google Gemini (gemini)
- Ollama (ollama)

새로운 LLM 추가 시:
1. LLMProvider 추상 클래스를 상속
2. register_provider()로 등록
3. config/llm_config.json 또는 환경변수에서 선택
"""

from abc import ABC, abstractmethod
from typing import List, Union, Optional
import os
import json

from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage
from langchain_core.outputs import ChatResult, ChatGeneration
from langchain_core.callbacks import CallbackManagerForLLMRun

# ─────────────────────────────────────────────────────────────
# Provider Registry
# ─────────────────────────────────────────────────────────────
_PROVIDER_REGISTRY: dict = {}


def register_provider(name: str, provider_class: type):
    """LLM Provider 등록"""
    _PROVIDER_REGISTRY[name] = provider_class


def get_provider(name: Optional[str] = None) -> "LLMProvider":
    """설정 기반 LLM Provider 인스턴스 반환"""
    if name is None:
        name = _resolve_provider_name()

    if name not in _PROVIDER_REGISTRY:
        raise ValueError(f"Unknown LLM provider: {name}. Available: {list(_PROVIDER_REGISTRY.keys())}")

    return _PROVIDER_REGISTRY[name]()


def _resolve_provider_name() -> str:
    """환경변수 또는 설정 파일에서 provider 이름 결정"""
    # 1. LLM_PROVIDER 환경변수 우선
    provider = os.getenv("LLM_PROVIDER")
    if provider:
        return provider

    # 2. config/llm_config.json 확인
    config_path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "llm_config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                cfg = json.load(f)
                if cfg.get("provider"):
                    return cfg["provider"]
        except Exception:
            pass

    # 3. 기본값
    return "gemini"


# ─────────────────────────────────────────────────────────────
# Abstract Base
# ─────────────────────────────────────────────────────────────
class LLMProvider(ABC):
    """LLM Provider 추상 클래스"""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """provider 식별자 (registry key로 사용)"""

    @abstractmethod
    def invoke(
        self,
        messages: List[BaseMessage],
        **kwargs,
    ) -> ChatResult:
        """메시지 목록으로 LLM 호출"""

    @abstractmethod
    def get_config_fields(self) -> dict:
        """필요한 환경변수/설정 목록 반환"""

    def as_langchain_chat_model(self):
        """LangChain ChatModel 형태로 반환 (호환성)"""
        from langchain.chat_models.base import SimpleChatModel
        from pydantic import ValidationError

        class WrappedChatModel(SimpleChatModel):
            @property
            def _llm_type(self) -> str:
                return self._provider.provider_name

            @property
            def _default_params(self) -> dict:
                return {}

            def _generate(self, messages, **kwargs):
                kwargs.pop("run_manager", None)
                result = self._provider.invoke(messages, **kwargs)
                if hasattr(result, "content") and isinstance(result.content, str):
                    content = result.content
                elif hasattr(result, "messages") and result.messages:
                    content = result.messages[0].content
                else:
                    content = str(result)
                from langchain_core.messages import AIMessage
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

            def _call(
                self,
                messages: List[BaseMessage],
                **kwargs,
            ):
                kwargs.pop("run_manager", None)
                result = self._provider.invoke(messages, **kwargs)
                if hasattr(result, "messages") and result.messages:
                    return result.messages[0].content
                return str(result)

        wrapper = WrappedChatModel()
        wrapper._provider = self
        return wrapper


# ─────────────────────────────────────────────────────────────
# Google Gemini Provider
# ─────────────────────────────────────────────────────────────
class GeminiProvider(LLMProvider):
    """Google Gemini provider"""

    @property
    def provider_name(self) -> str:
        return "gemini"

    def __init__(self, model: str = "gemini-2.5-flash", temperature: float = 0):
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("GOOGLE_API_KEY environment variable is required for Gemini")

        from langchain_google_genai import ChatGoogleGenerativeAI
        self._llm = ChatGoogleGenerativeAI(
            model=model,
            temperature=temperature,
            google_api_key=api_key,
        )

    def invoke(
        self,
        messages: List[BaseMessage],
        **kwargs,
    ) -> ChatResult:
        return self._llm.invoke(messages, **kwargs)

    def get_config_fields(self) -> dict:
        return {
            "LLM_PROVIDER": "gemini",
            "GOOGLE_API_KEY": "Google AI API key",
        }


# ─────────────────────────────────────────────────────────────
# Ollama Provider
# ─────────────────────────────────────────────────────────────
class OllamaProvider(LLMProvider):
    """Ollama provider (local LLM)"""

    @property
    def provider_name(self) -> str:
        return "ollama"

    def __init__(
        self,
        model: str = "gemma4:e2b",
        base_url: str = "http://localhost:11434",
        temperature: float = 0,
    ):
        try:
            from langchain_ollama import ChatOllama
        except ImportError:
            raise ImportError(
                "langchain-ollama is required for Ollama provider. "
                "Install with: pip install langchain-ollama"
            )

        self._llm = ChatOllama(
            model=model,
            base_url=base_url,
            temperature=temperature,
        )

    def invoke(
        self,
        messages: List[BaseMessage],
        **kwargs,
    ) -> ChatResult:
        return self._llm.invoke(messages, **kwargs)

    def get_config_fields(self) -> dict:
        return {
            "LLM_PROVIDER": "ollama",
            "OLLAMA_BASE_URL": "http://localhost:11434 (default)",
            "OLLAMA_MODEL": "gemma4:e2b (default)",
        }


# ─────────────────────────────────────────────────────────────
# Provider Registration
# ─────────────────────────────────────────────────────────────
register_provider("gemini", GeminiProvider)
register_provider("ollama", OllamaProvider)