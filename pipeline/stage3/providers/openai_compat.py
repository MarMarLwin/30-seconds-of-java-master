"""
Providers with an OpenAI-compatible chat completions API (Gemini, OpenAI),
called through the OpenAI SDK.
"""

import logging
import os
from typing import Any, Dict, Optional

from tenacity import (before_sleep_log, retry, retry_if_exception,
                      stop_after_attempt, wait_random_exponential)

from .base import ProviderFatalError, extract_code, new_result

try:
    import openai
    from openai import OpenAI
except ImportError:
    raise ProviderFatalError("This provider needs the openai package: pip install openai")

logger = logging.getLogger(__name__)

# request_params are passed to every chat.completions.create call.
PROVIDERS: Dict[str, Dict[str, Any]] = {
    'gemini': {
        'api_key_env': 'GEMINI_API_KEY',
        'base_url': 'https://generativelanguage.googleapis.com/v1beta/openai/',
        'default_model': 'gemini-3.8-flash',
        'request_params': {
            # Gemini 3 models always think, and thinking tokens count against
            # the output limit, so the limit is higher than OpenAI's and
            # thinking is kept low. gemini-3.8-flash rejects "minimal".
            'max_tokens': 8192,
            'reasoning_effort': 'low',
        },
    },
    'openai': {
        'api_key_env': 'OPENAI_API_KEY',
        'base_url': None,  # SDK default, or $OPENAI_BASE_URL if set
        'default_model': 'gpt-4o',
        'request_params': {'max_completion_tokens': 2048},
    },
}

# Temperature 0 keeps outputs as repeatable as possible, so differences between
# strategies come from the prompts rather than sampling noise.
DEFAULT_TEMPERATURE = 0.0
REQUEST_TIMEOUT_SECONDS = 120

# Retry policy for transient failures (rate limits, timeouts, 5xx errors).
MAX_ATTEMPTS = 6
RETRYABLE_ERRORS = (
    openai.RateLimitError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.InternalServerError,
)


def is_quota_error(error: BaseException) -> bool:
    """
    OpenAI reports an exhausted balance as a 429 RateLimitError, but unlike a
    real rate limit it won't clear by waiting.
    """
    return (isinstance(error, openai.RateLimitError)
            and getattr(error, 'code', None) == 'insufficient_quota')


def is_key_error(error: BaseException) -> bool:
    """Gemini rejects an invalid key with 400 "API key not valid" rather than 401."""
    return (isinstance(error, openai.BadRequestError)
            and 'api key' in str(error).lower())


def is_retryable(error: BaseException) -> bool:
    return isinstance(error, RETRYABLE_ERRORS) and not is_quota_error(error)


class OpenAICompatibleRefactorer:
    def __init__(self, provider_name: str, model: Optional[str],
                 temperature: Optional[float]):
        provider = PROVIDERS[provider_name]
        self.key_env = provider['api_key_env']
        if not os.environ.get(self.key_env):
            raise ProviderFatalError(f"{self.key_env} is not set. Export it or add it to a "
                                     ".env file in the current directory.")
        self.provider_name = provider_name
        self.model = model or provider['default_model']
        self.temperature = DEFAULT_TEMPERATURE if temperature is None else temperature
        self.settings: Dict[str, Any] = {}
        self.request_params = provider['request_params']
        # The SDK's own retries are disabled so tenacity is the single retry layer.
        self.client = OpenAI(
            api_key=os.environ[self.key_env],
            base_url=provider['base_url'],
            max_retries=0,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    @retry(
        retry=retry_if_exception(is_retryable),
        wait=wait_random_exponential(multiplier=2, max=60),
        stop=stop_after_attempt(MAX_ATTEMPTS),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _complete(self, system_prompt: str, user_prompt: str):
        return self.client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_prompt},
            ],
            **self.request_params,
        )

    def run(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        result = new_result(self)
        try:
            response = self._complete(system_prompt, user_prompt)
        except openai.AuthenticationError as e:
            raise ProviderFatalError(self._key_rejected(e))
        except openai.PermissionDeniedError as e:
            raise ProviderFatalError(f"The key is valid but has no access to model "
                                     f"{self.model!r}. Try another --model. ({e})")
        except openai.NotFoundError as e:
            raise ProviderFatalError(f"Model {self.model!r} was not found for provider "
                                     f"{self.provider_name}. Check --model. ({e})")
        except openai.APIError as e:
            if is_key_error(e):
                raise ProviderFatalError(self._key_rejected(e))
            if is_quota_error(e):
                raise ProviderFatalError("The account has no remaining credit "
                                         f"(insufficient_quota). Add billing. ({e})")
            result['error'] = f"{type(e).__name__}: {e}"
            return result

        choice = response.choices[0]
        result['refactored_code'] = extract_code(choice.message.content)
        if response.usage:
            result['prompt_tokens'] = response.usage.prompt_tokens
            result['completion_tokens'] = response.usage.completion_tokens
            result['total_tokens'] = response.usage.total_tokens
            # Includes provider-specific detail such as reasoning tokens.
            result['usage'] = response.usage.model_dump(exclude_none=True)
        result['finish_reason'] = choice.finish_reason
        if choice.finish_reason == 'length':
            result['error'] = "Response was cut off at the provider's output token limit"
        elif not result['refactored_code']:
            result['error'] = 'Response contained no code'
        return result

    def _key_rejected(self, error: Exception) -> str:
        return (f"The API rejected {self.key_env}. Check that it was copied completely "
                f"and hasn't been revoked. ({error})")
