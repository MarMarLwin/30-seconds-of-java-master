"""
Claude, through the official Anthropic Python SDK.

Credentials are resolved by the SDK: ANTHROPIC_API_KEY (environment or .env),
ANTHROPIC_AUTH_TOKEN, or an `ant auth login` profile.

Differences from the OpenAI-compatible providers, all recorded per result:
  - Current Claude models do not take a temperature. Depth of reasoning is set
    with `effort` instead (thinking is adaptive and always on for Opus 5.5).
  - Refusal fallbacks are on for the models that support them: if the model
    declines a request, the API re-runs it on Anthropic's recommended fallback
    model. `served_by` and `fallback_used` show when that happened, so those
    results can be excluded or reported separately.
"""

import logging
from typing import Any, Dict, Optional

from .base import ProviderFatalError, extract_code, new_result

try:
    import anthropic
except ImportError:
    raise ProviderFatalError("The claude provider needs the anthropic package: "
                             "pip install anthropic")

logger = logging.getLogger(__name__)

DEFAULT_MODEL = 'claude-opus-5-5'

# low | medium | high | xhigh | max. Opus 5.5 defaults to medium when unset, so
# it is always sent explicitly; high suits code changes that must stay correct.
DEFAULT_EFFORT = 'high'
EFFORT_LEVELS = ('low', 'medium', 'high', 'xhigh', 'max')

# Thinking tokens count against max_tokens. Snippets are short, so this leaves
# ample room while staying within non-streaming request limits.
MAX_TOKENS = 16000
REQUEST_TIMEOUT_SECONDS = 300
# The SDK retries connection errors, 408, 409, 429 and 5xx with backoff.
MAX_RETRIES = 5

# Models that accept server-side refusal fallbacks (fallbacks="default").
FALLBACK_MODELS = {'claude-opus-5-5', 'claude-opus-5', 'claude-fable-5-1', 'claude-sonnet-5-5'}
FALLBACK_BETA = 'server-side-fallback-2026-07-01'


class ClaudeRefactorer:
    def __init__(self, model: Optional[str], effort: Optional[str]):
        self.model = model or DEFAULT_MODEL
        self.effort = effort or DEFAULT_EFFORT
        if self.effort not in EFFORT_LEVELS:
            raise ProviderFatalError(f"Unknown effort {self.effort!r}; use one of "
                                     f"{', '.join(EFFORT_LEVELS)}")
        self.use_fallbacks = self.model in FALLBACK_MODELS
        self.settings: Dict[str, Any] = {
            'effort': self.effort,
            'fallbacks': 'default' if self.use_fallbacks else None,
        }
        self.client = anthropic.Anthropic(timeout=REQUEST_TIMEOUT_SECONDS,
                                          max_retries=MAX_RETRIES)

    def _create(self, system_prompt: str, user_prompt: str):
        params = dict(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=[{'role': 'user', 'content': user_prompt}],
            output_config={'effort': self.effort},
        )
        if self.use_fallbacks:
            return self.client.beta.messages.create(
                betas=[FALLBACK_BETA], fallbacks='default', **params)
        return self.client.messages.create(**params)

    def run(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        result = new_result(self)
        try:
            response = self._create(system_prompt, user_prompt)
        except anthropic.AuthenticationError as e:
            raise ProviderFatalError(
                "Claude rejected the credentials. Set ANTHROPIC_API_KEY (export it or add "
                f"it to .env) and check it hasn't been revoked. ({e.message})")
        except anthropic.PermissionDeniedError as e:
            raise ProviderFatalError(f"The credentials have no access to {self.model!r}. "
                                     f"Try another --model. ({e.message})")
        except anthropic.NotFoundError as e:
            raise ProviderFatalError(f"Model {self.model!r} was not found. Check --model. "
                                     f"({e.message})")
        except anthropic.BadRequestError as e:
            if 'credit balance' in e.message.lower():
                raise ProviderFatalError(f"The Anthropic account is out of credit. "
                                         f"({e.message})")
            result['error'] = f"BadRequestError: {e.message}"
            return result
        except anthropic.APIStatusError as e:
            # Rate limits and server errors that persisted through the SDK's retries.
            result['error'] = f"{type(e).__name__} ({e.status_code}): {e.message}"
            return result
        except anthropic.APIConnectionError as e:
            result['error'] = f"{type(e).__name__}: {e}"
            return result
        except TypeError as e:
            # Raised by the SDK before sending when it finds no credentials at all.
            if 'authentication' in str(e).lower() or 'api_key' in str(e).lower():
                raise ProviderFatalError("No Claude credentials found. Set ANTHROPIC_API_KEY "
                                         "(export it or add it to .env).")
            raise

        text = ''.join(block.text for block in response.content if block.type == 'text')
        result['refactored_code'] = extract_code(text)
        result['finish_reason'] = response.stop_reason
        result['served_by'] = response.model
        result['fallback_used'] = any(
            getattr(entry, 'type', None) == 'fallback_message'
            for entry in (getattr(response.usage, 'iterations', None) or []))

        usage = response.usage
        cache_write = usage.cache_creation_input_tokens or 0
        cache_read = usage.cache_read_input_tokens or 0
        # prompt_tokens counts all input, cached or not, to match the other
        # providers; completion_tokens includes thinking tokens.
        result['prompt_tokens'] = usage.input_tokens + cache_write + cache_read
        result['completion_tokens'] = usage.output_tokens
        result['total_tokens'] = result['prompt_tokens'] + usage.output_tokens
        result['usage'] = usage.to_dict()

        if response.stop_reason == 'refusal':
            details = getattr(response, 'stop_details', None)
            category = getattr(details, 'category', None) if details else None
            result['error'] = f"Claude declined the request (category: {category})"
        elif response.stop_reason == 'max_tokens':
            result['error'] = f"Response was cut off at max_tokens ({MAX_TOKENS})"
        elif not result['refactored_code']:
            result['error'] = 'Response contained no code'
        elif result['fallback_used']:
            logger.warning("  answered by fallback model %s instead of %s",
                           response.model, self.model)
        return result
