"""
LLM providers for Stage 3. Each one turns (system prompt, user prompt) into a
result record with the refactored code and token usage.

    gemini, openai   OpenAI-compatible APIs (openai_compat.py)
    claude           Anthropic API, billed to API credits (claude.py)
    claude-code      the Claude Code CLI, billed to a Claude subscription
                     such as Pro (claude_code.py)

Provider modules are imported only when used, so each needs only its own SDK.
"""

import logging
from typing import Optional

from .base import ProviderFatalError, Refactorer

logger = logging.getLogger(__name__)

PROVIDER_NAMES = ['gemini', 'openai', 'claude', 'claude-code']
DEFAULT_PROVIDER = 'gemini'


def default_model(name: str) -> str:
    """The model label a provider uses when no --model is given (names its files)."""
    return {'gemini': 'gemini-3.8-flash', 'openai': 'gpt-4o',
            'claude': 'claude-opus-5-5', 'claude-code': 'claude-code'}[name]


def create_refactorer(name: str, model: Optional[str] = None,
                      temperature: Optional[float] = None,
                      effort: Optional[str] = None) -> Refactorer:
    if name in ('claude', 'claude-code'):
        if temperature is not None:
            logger.warning("--temperature is ignored for Claude: current Claude models "
                           "do not take sampling parameters (use --effort)")
        if name == 'claude-code':
            from .claude_code import ClaudeCodeRefactorer
            return ClaudeCodeRefactorer(model, effort)
        from .claude import ClaudeRefactorer
        return ClaudeRefactorer(model, effort)
    if name in ('gemini', 'openai'):
        if effort is not None:
            logger.warning("--effort only applies to the Claude providers; ignored")
        from .openai_compat import OpenAICompatibleRefactorer
        return OpenAICompatibleRefactorer(name, model, temperature)
    raise ProviderFatalError(f"Unknown provider {name!r}; use one of "
                             f"{', '.join(PROVIDER_NAMES)}")


__all__ = ['PROVIDER_NAMES', 'DEFAULT_PROVIDER', 'ProviderFatalError', 'Refactorer',
           'create_refactorer', 'default_model']
