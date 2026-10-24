"""Turns Stage 1 snippet records into Stage 2 prompt records."""

import datetime
import hashlib
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import IssueRecord, approx_tokens
from .rules import RuleCatalog
from .strategies import ContextStrategy, StrategySkipped
from .template import render_prompt

logger = logging.getLogger(__name__)


def load_issue_records(path: Path) -> List[IssueRecord]:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        logger.error("Input file not found: %s", path)
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in %s: %s", path, e)
        sys.exit(1)
    if not isinstance(data, list):
        logger.error("Expected a JSON list of snippet records in %s", path)
        sys.exit(1)

    records = []
    for entry in data:
        try:
            records.append(IssueRecord.from_dict(entry))
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("Skipping malformed snippet record (%s): %r",
                           e, entry.get('issue_key') if isinstance(entry, dict) else entry)
    return records


def git_state(repo_root: Path) -> Dict[str, Any]:
    """HEAD commit and uncommitted files, so a run can be traced to its code."""
    def git(*args: str) -> Optional[str]:
        try:
            done = subprocess.run(['git', *args], cwd=repo_root, capture_output=True,
                                  text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return done.stdout if done.returncode == 0 else None

    commit = (git('rev-parse', 'HEAD') or '').strip() or None
    status = git('status', '--porcelain') or ''
    dirty = sorted(line[3:] for line in status.splitlines() if line.strip())
    return {'git_commit': commit, 'dirty_files': dirty}


def prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


class PromptGenerator:
    def __init__(self, strategies: List[ContextStrategy], rules: RuleCatalog):
        self.strategies = strategies
        self.rules = rules

    def generate(self, issue: IssueRecord) -> Dict[str, Any]:
        rule = self.rules.get(issue.rule)
        prompts: Dict[str, Any] = {}
        skipped: Dict[str, str] = {}

        for strategy in self.strategies:
            try:
                chunks = strategy.build(issue, rule)
            except StrategySkipped as e:
                skipped[strategy.name] = str(e)
                logger.warning("  %-16s skipped: %s", strategy.name, e)
                continue
            text = render_prompt(issue, rule, strategy.name, chunks)
            prompts[strategy.name] = {
                'text': text,
                'sha256': prompt_hash(text),
                'approx_tokens': approx_tokens(text),
                'context_tokens': sum(c.approx_tokens for c in chunks),
                'context': [c.to_dict() for c in chunks],
            }
            logger.info("  %-16s %4d tokens, %d context chunk(s)", strategy.name,
                        prompts[strategy.name]['approx_tokens'], len(chunks))

        return {
            'issue_key': issue.issue_key,
            'rule': issue.rule,
            'message': issue.message,
            'file_path': issue.file_path,
            'start_line': issue.start_line,
            'end_line': issue.end_line,
            'snippet_start_line': issue.snippet_start_line,
            'snippet_end_line': issue.snippet_end_line,
            'scope_kind': issue.scope_kind,
            'target_snippet': issue.target_snippet,
            'rule_info': {'name': rule.name, 'type': rule.type, 'impacts': rule.impacts,
                          'description_source': rule.description_source},
            'prompts': prompts,
            'skipped_strategies': skipped,
        }


def build_manifest(records: List[Dict[str, Any]], issues: List[IssueRecord],
                   repo_root: Path, settings: Dict[str, Any]) -> Dict[str, Any]:
    state = git_state(repo_root)
    target_files = sorted({i.file_path for i in issues})
    dirty_targets = [f for f in target_files if f in state['dirty_files']]
    if dirty_targets:
        logger.warning("Target file(s) have uncommitted changes, so prompts may not match "
                       "the scanned commit: %s", ', '.join(dirty_targets))

    per_strategy: Dict[str, Dict[str, int]] = {}
    for record in records:
        for name, prompt in record['prompts'].items():
            stats = per_strategy.setdefault(name, {'prompts': 0, 'approx_tokens': 0,
                                                   'context_chunks': 0})
            stats['prompts'] += 1
            stats['approx_tokens'] += prompt['approx_tokens']
            stats['context_chunks'] += len(prompt['context'])

    return {
        'generated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        **state,
        'dirty_target_files': dirty_targets,
        'settings': settings,
        'issues': len(records),
        'strategies': per_strategy,
        'rule_description_sources': {r['rule']: r['rule_info']['description_source']
                                     for r in records},
        'skipped': {r['issue_key']: r['skipped_strategies']
                    for r in records if r['skipped_strategies']},
    }


def write_json(data: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)
