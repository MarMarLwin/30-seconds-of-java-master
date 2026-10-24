"""
Stage 3: send each issue's zero-shot, few-shot and repository-aware prompts to
an LLM independently, extract the refactored code from each response, and
record token usage.

Providers live in providers/: Gemini (default) and OpenAI through their
OpenAI-compatible APIs, and Claude through the Anthropic SDK. API keys are
read from the environment (or a .env file in the current directory, see
pipeline/cli.py); never hard-code them.

Run with: python -m pipeline refactor --repo <path> [--provider claude] [--limit 1]

After the calls, the refactored code is written into one git branch per
strategy (see apply.py) unless --no-apply is given.

Results are saved after every issue. Re-running skips calls that already
succeeded with the same model, settings and prompt text, so an interrupted run
can be resumed without paying twice, and regenerating the Stage 2 prompts
re-runs only the calls whose prompts changed.
"""

import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .providers import DEFAULT_PROVIDER, ProviderFatalError, Refactorer, create_refactorer

logger = logging.getLogger(__name__)

# Strategies are sent in this order; any missing from the input are skipped.
STRATEGIES = ['zero_shot', 'few_shot', 'repository_aware']

# The same system instruction is used for every strategy so that only the user
# prompt differs between them.
SYSTEM_PROMPT_TEMPLATE = (
    "You are a senior software developer fixing a static analysis issue "
    "reported by SonarQube.\n"
    "Rule: {rule}\n"
    "Issue: {message}\n\n"
    "Requirements:\n"
    "- Fix only this issue. Do not change behavior or make unrelated edits.\n"
    "- Return the complete refactored version of the code you are asked to "
    "refactor, including its unchanged surrounding lines, with the original "
    "indentation preserved.\n"
    "- Respond with a single fenced code block and no explanation."
)


# --------------------------------------------------------------------------- #
# Input loading
# --------------------------------------------------------------------------- #

def load_json_list(path: Path) -> List[Dict[str, Any]]:
    try:
        with path.open('r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.error("Input file not found: %s", path)
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in %s: %s", path, e)
        sys.exit(1)
    if not isinstance(data, list):
        logger.error("Expected a JSON list in %s", path)
        sys.exit(1)
    return data


def issue_id(record: Dict[str, Any]) -> str:
    """Stable identifier for an issue, used to merge files and resume runs."""
    if record.get('issue_key'):
        return record['issue_key']
    return f"{record.get('file_path')}:{record.get('start_line')}:{record.get('rule')}"


def load_issues(input_paths: List[Path]) -> List[Dict[str, Any]]:
    """
    Merge Stage 2 files into one record per issue, combining their prompts.
    Issue order follows the first file.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    for path in input_paths:
        for record in load_json_list(path):
            if not isinstance(record, dict) or not isinstance(record.get('prompts'), dict):
                logger.warning("Skipping malformed record in %s", path)
                continue
            key = issue_id(record)
            if key in merged:
                merged[key]['prompts'].update(record['prompts'])
            else:
                merged[key] = {**record, 'prompts': dict(record['prompts'])}
    return list(merged.values())


def load_previous_results(output_path: Path) -> Dict[str, Dict[str, Any]]:
    """Load an earlier run's output, keyed by issue, so it can be resumed."""
    if not output_path.exists():
        return {}
    try:
        with output_path.open('r', encoding='utf-8') as f:
            return {issue_id(r): r for r in json.load(f)}
    except (json.JSONDecodeError, TypeError, AttributeError):
        logger.warning("Ignoring unreadable previous output %s", output_path)
        return {}


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #

def prompt_text(prompt: Any) -> str:
    """Stage 2 stores each prompt as {"text": ..., "context": [...]}; older
    files stored a bare string."""
    if isinstance(prompt, dict):
        return prompt.get('text') or ''
    return prompt or ''


def prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


def is_reusable(result: Optional[Dict[str, Any]], refactorer: Refactorer,
                prompt_sha: str) -> bool:
    """True if an earlier result succeeded with the same model, settings and prompt."""
    return (bool(result) and 'error' not in result
            and result.get('model') == refactorer.model
            and result.get('settings', {}) == refactorer.settings
            and result.get('prompt_sha256') == prompt_sha)


def is_complete(result_record: Optional[Dict[str, Any]], issue: Dict[str, Any],
                strategies: List[str], refactorer: Refactorer) -> bool:
    """True if every requested strategy already succeeded for this issue."""
    if not result_record:
        return False
    results = result_record.get('results', {})
    return all(is_reusable(results.get(s), refactorer,
                           prompt_hash(prompt_text(issue['prompts'][s])))
               for s in strategies)


def process_issue(issue: Dict[str, Any], refactorer: Refactorer,
                  previous: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Run every available strategy for one issue, reusing earlier successes."""
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        rule=issue.get('rule', ''),
        message=issue.get('message', ''),
    )
    previous_results = (previous or {}).get('results', {})
    results: Dict[str, Any] = {}

    for strategy in STRATEGIES:
        user_prompt = prompt_text(issue['prompts'].get(strategy))
        if not user_prompt:
            continue
        sha = prompt_hash(user_prompt)
        earlier = previous_results.get(strategy)
        if is_reusable(earlier, refactorer, sha):
            results[strategy] = earlier
            continue

        result = refactorer.run(system_prompt, user_prompt)
        result['prompt_sha256'] = sha
        results[strategy] = result
        if 'error' in result:
            logger.warning("  %-16s failed: %s", strategy, result['error'])
        else:
            logger.info("  %-16s prompt_tokens=%s completion_tokens=%s",
                        strategy, result['prompt_tokens'], result['completion_tokens'])

    output = {
        'file_path': issue.get('file_path'),
        'rule': issue.get('rule'),
        'original_snippet': issue.get('target_snippet'),
        'results': results,
    }
    # Carry location data forward so the patching stage knows where to write.
    for key in ('issue_key', 'message', 'start_line', 'end_line',
                'snippet_start_line', 'snippet_end_line', 'scope_kind'):
        if key in issue:
            output[key] = issue[key]
    return output


def save_results(results: List[Dict[str, Any]], output_path: Path) -> None:
    """Write atomically so an interrupted save can't corrupt earlier results."""
    tmp_path = output_path.with_suffix(output_path.suffix + '.tmp')
    with tmp_path.open('w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    tmp_path.replace(output_path)


def log_token_totals(results: List[Dict[str, Any]]) -> None:
    for strategy in STRATEGIES:
        calls = [r['results'][strategy] for r in results if strategy in r['results']]
        if not calls:
            continue
        prompt = sum(c.get('prompt_tokens') or 0 for c in calls)
        completion = sum(c.get('completion_tokens') or 0 for c in calls)
        failed = sum(1 for c in calls if 'error' in c)
        logger.info("Total %-16s calls=%d failed=%d prompt_tokens=%d completion_tokens=%d",
                    strategy, len(calls), failed, prompt, completion)


def run_refactoring(input_paths: List[Path], output_path_for: Callable[[str], Path],
                    provider_name: str = DEFAULT_PROVIDER, model: Optional[str] = None,
                    temperature: Optional[float] = None, limit: Optional[int] = None,
                    fresh: bool = False, effort: Optional[str] = None) -> str:
    """
    Run every prompt in input_paths and save the results to
    output_path_for(model), so each model's results live in their own file.
    Returns the model label the results file is named after.
    """
    try:
        refactorer = create_refactorer(provider_name, model, temperature, effort)
    except ProviderFatalError as e:
        logger.error("%s", e)
        sys.exit(1)
    output_path = output_path_for(refactorer.model)

    issues = load_issues(input_paths)
    if limit is not None:
        issues = issues[:limit]
    settings = ', '.join(f"{k}={v}" for k, v in refactorer.settings.items() if v)
    logger.info("Loaded %d issue(s); provider=%s model=%s%s", len(issues), provider_name,
                refactorer.model, f" ({settings})" if settings else '')

    previous = {} if fresh else load_previous_results(output_path)

    # Earlier results for issues outside this run (e.g. excluded by --limit)
    # are written back unchanged rather than dropped.
    current_keys = {issue_id(issue) for issue in issues}
    untouched = [r for k, r in previous.items() if k not in current_keys]

    results: List[Dict[str, Any]] = []
    try:
        for i, issue in enumerate(issues, 1):
            key = issue_id(issue)
            strategies = [s for s in STRATEGIES if prompt_text(issue['prompts'].get(s))]
            if is_complete(previous.get(key), issue, strategies, refactorer):
                logger.info("[%d/%d] %s (%s): already done, skipping",
                            i, len(issues), issue.get('file_path'), issue.get('rule'))
                results.append(previous[key])
                continue

            logger.info("[%d/%d] %s (%s)", i, len(issues),
                        issue.get('file_path'), issue.get('rule'))
            results.append(process_issue(issue, refactorer, previous.get(key)))
            # Save after every issue so paid-for results survive a crash.
            save_results(results + untouched, output_path)
    except ProviderFatalError as e:
        logger.error("%s", e)
        sys.exit(1)
    except KeyboardInterrupt:
        logger.warning("Interrupted; %d issue(s) saved. Re-run to resume.", len(results))
        sys.exit(130)
    finally:
        if results:
            save_results(results + untouched, output_path)

    if results:
        logger.info("Saved results for %d issue(s) to %s", len(results), output_path)
        log_token_totals(results)
    else:
        logger.info("No issues to process; %s was not changed", output_path)
    return refactorer.model
