import asyncio
import functools
import logging
import os
import time
from collections.abc import Callable
from enum import Enum
from typing import Any, overload

import openai
import requests
from langchain.callbacks.base import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import ConfigurableField, Runnable
from langchain_openai.chat_models import ChatOpenAI
from langfuse.callback import CallbackHandler

logger = logging.getLogger(__name__)

# Transient OpenAI/LiteLLM error types that are safe to retry.
_RETRYABLE_OPENAI_ERRORS: tuple[type[Exception], ...] = (
    openai.RateLimitError,
    openai.InternalServerError,
    openai.APIConnectionError,
    openai.APITimeoutError,
)


def _is_retryable(exc: Exception) -> bool:
    """Return True if *exc* represents a transient LLM API failure."""
    if isinstance(exc, _RETRYABLE_OPENAI_ERRORS):
        return True
    # openai.APIStatusError covers 502/503 via status_code
    if isinstance(exc, openai.APIStatusError) and exc.status_code in (429, 500, 502, 503):
        return True
    # Connection-level errors surfaced by httpx / requests
    if isinstance(exc, ConnectionError | TimeoutError):
        return True
    return False


@overload
def retry_llm[**P, T](
    fn: Callable[P, T],
    *,
    max_retries: int = ...,
    base_delay: float = ...,
    max_delay: float = ...,
) -> Callable[P, T]: ...


@overload
def retry_llm[**P, T](
    fn: None = None,
    *,
    max_retries: int = ...,
    base_delay: float = ...,
    max_delay: float = ...,
) -> Callable[[Callable[P, T]], Callable[P, T]]: ...


def retry_llm[**P, T](
    fn: Callable[P, T] | None = None,
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
) -> Callable[P, T] | Callable[[Callable[P, T]], Callable[P, T]]:
    """Decorator that retries a function on transient LLM API errors.

    Supports both sync and async callables.  Uses exponential back-off with
    jitter (``min(base_delay * 2**attempt, max_delay)``).

    Can be used with or without arguments::

        @retry_llm
        def call_llm(): ...

        @retry_llm(max_retries=5, base_delay=2.0)
        def call_llm(): ...
    """

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
                last_exc: Exception | None = None
                for attempt in range(max_retries + 1):
                    try:
                        return await func(*args, **kwargs)  # type: ignore[no-any-return]
                    except Exception as exc:  # Broad catch intentional: retry decorator filters via _is_retryable
                        if not _is_retryable(exc) or attempt == max_retries:
                            raise
                        last_exc = exc
                        delay = min(base_delay * (2**attempt), max_delay)
                        logger.warning(
                            "retry_llm: %s attempt %d/%d failed (%s), retrying in %.1fs",
                            func.__qualname__,
                            attempt + 1,
                            max_retries,
                            exc,
                            delay,
                        )
                        await asyncio.sleep(delay)
                # Unreachable, but keeps mypy happy.
                raise last_exc  # type: ignore[misc]

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            last_exc: Exception | None = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:  # Broad catch intentional: retry decorator filters via _is_retryable
                    if not _is_retryable(exc) or attempt == max_retries:
                        raise
                    last_exc = exc
                    delay = min(base_delay * (2**attempt), max_delay)
                    logger.warning(
                        "retry_llm: %s attempt %d/%d failed (%s), retrying in %.1fs",
                        func.__qualname__,
                        attempt + 1,
                        max_retries,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
            # Unreachable, but keeps mypy happy.
            raise last_exc  # type: ignore[misc]

        return sync_wrapper

    if fn is not None:
        return decorator(fn)
    return decorator


class ButtercupLLM(Enum):
    """Enum for LLM models available in LiteLLM."""

    AZURE_GPT_4O = "azure-gpt-4o"
    AZURE_GPT_4O_MINI = "azure-gpt-4o-mini"
    AZURE_O3_MINI = "azure-o3-mini"
    AZURE_O1 = "azure-o1"
    OPENAI_GPT_4O = "openai-gpt-4o"
    OPENAI_GPT_4O_MINI = "openai-gpt-4o-mini"
    OPENAI_O3_MINI = "openai-o3-mini"
    OPENAI_O3 = "openai-o3"
    OPENAI_O1 = "openai-o1"
    OPENAI_GPT_4_1_NANO = "openai-gpt-4.1-nano"
    OPENAI_GPT_4_1_MINI = "openai-gpt-4.1-mini"
    OPENAI_GPT_4_1 = "openai-gpt-4.1"
    CLAUDE_3_5_SONNET = "claude-3.5-sonnet"
    CLAUDE_3_7_SONNET = "claude-3.7-sonnet"
    CLAUDE_4_SONNET = "claude-4-sonnet"
    GEMINI_PRO = "gemini-pro"
    GEMINI_2_5_FLASH = "gemini-2.5-flash"
    GEMINI_2_5_FLASH_EXP = "gemini-2.5-flash-exp"


@functools.cache
def is_langfuse_available() -> bool:
    """Check if LangFuse is available."""
    langfuse_host = os.getenv("LANGFUSE_HOST")
    if not langfuse_host:
        logger.info("LangFuse not configured")
        return False
    try:
        response = requests.post(f"{langfuse_host}/api/public/ingestion", timeout=2)
        return response.status_code == 401  # expect that we aren't authenticated
    except requests.RequestException:
        return False


@functools.cache
def langfuse_auth_check() -> bool:
    """Check if LangFuse is available.

    Uses the ingestion endpoint to check if the API key is valid.
    """
    langfuse_host = os.getenv("LANGFUSE_HOST")
    langfuse_public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key = os.getenv("LANGFUSE_SECRET_KEY")
    if langfuse_public_key is None or langfuse_secret_key is None:
        return False

    try:
        response = requests.post(
            f"{langfuse_host}/api/public/ingestion",
            timeout=2,
            auth=(langfuse_public_key, langfuse_secret_key),
        )
        return response.status_code == 400  # expect that we authenticate, but the request is invalid
    except requests.RequestException:
        return False


@functools.cache
def get_langfuse_callbacks() -> list[BaseCallbackHandler]:
    """Get Langchain callbacks for monitoring LLM calls with LangFuse, if available."""
    if is_langfuse_available():
        try:
            langfuse_handler = CallbackHandler(
                public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
                secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
                host=os.getenv("LANGFUSE_HOST"),
            )
            if langfuse_auth_check():
                logger.info("Tracing with LangFuse enabled")
                return [langfuse_handler]

            logger.warning("LangFuse authentication failed")
        except Exception:  # Broad catch intentional: LangFuse init can fail in many ways (network, auth, config)
            logger.error("Cannot connect to LangFuse")
    else:
        logger.info("LangFuse not available")

    return []


def create_default_llm(**kwargs: Any) -> Runnable:
    """Create an LLM object with the default configuration."""
    fallback_models = kwargs.pop("fallback_models", [])
    fallback_models = [create_default_llm(**{**kwargs, "model_name": m.value}) for m in fallback_models]
    return create_llm(
        model_name=kwargs.pop("model_name", ButtercupLLM.OPENAI_GPT_4_1.value),
        temperature=kwargs.pop("temperature", 0.1),
        timeout=420.0,
        max_retries=3,
        **kwargs,
    ).with_fallbacks(fallback_models)


def create_default_llm_with_temperature(**kwargs: Any) -> Runnable:
    """Create an LLM object with the default configuration and temperature."""
    fallback_models = kwargs.pop("fallback_models", [])
    fallback_models = [
        create_default_llm_with_temperature(**{**kwargs, "model_name": m.value}) for m in fallback_models
    ]
    return (
        create_llm(
            model_name=kwargs.pop("model_name", ButtercupLLM.OPENAI_GPT_4_1.value),
            temperature=kwargs.pop("temperature", 0.1),
            timeout=420.0,
            max_retries=3,
            **kwargs,
        )
        .configurable_fields(
            temperature=ConfigurableField(
                id="llm_temperature",
                name="LLM temperature",
                description="The temperature for the LLM model",
            ),
        )
        .with_fallbacks(fallback_models)
    )


def create_llm(**kwargs: Any) -> BaseChatModel:
    """Create an LLM object with the given configuration."""
    return ChatOpenAI(
        openai_api_base=os.environ["BUTTERCUP_LITELLM_HOSTNAME"],
        openai_api_key=os.environ["BUTTERCUP_LITELLM_KEY"],
        **kwargs,
    )
