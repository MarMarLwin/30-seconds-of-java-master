"""
Write the Stage 3 refactorings back into the source code, in one of two ways:

  apply_in_place()  one chosen strategy, straight into the current working
                    files (uncommitted, review with `git diff`)
  apply_results()   every strategy, each on its own git branch, so each can
                    be built, tested and re-scanned on its own:

    refactor/<model>/zero-shot
    refactor/<model>/few-shot
    refactor/<model>/repository-aware

Each branch starts from the current commit (HEAD) and gets one commit with
that strategy's patches. The work happens in a temporary git worktree, so the
current checkout and any uncommitted changes are never touched.

A patch replaces lines snippet_start_line..snippet_end_line of its file and is
skipped when:
  - the call failed or returned no code,
  - those lines no longer match the snippet that was sent to the model (the
    file changed since extraction, or the change is not committed),
  - it overlaps a patch already applied on the branch. Larger patches are
    applied first, so an enclosing refactoring (e.g. a whole method for
    Cognitive Complexity) wins over the smaller ones inside it.

What happened to every patch is written to a stage3_applied_<model>*.json
report.
"""

import datetime
import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

STRATEGIES = ['zero_shot', 'few_shot', 'repository_aware']
BRANCH_PREFIX = 'refactor'
# Marks commits made by this module, so a branch is only ever replaced when
# everything on it came from the pipeline.
TRAILER_KEY = 'Generated-By'
TRAILER_VALUE = 'refactoring-pipeline'


class ApplyError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Git helpers
# --------------------------------------------------------------------------- #

def _git(repo: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(['git', *args], cwd=repo, capture_output=True, text=True)
    if check and done.returncode != 0:
        raise ApplyError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def branch_name(model: str, strategy: str) -> str:
    safe_model = re.sub(r'[^A-Za-z0-9._-]+', '-', model).strip('-.') or 'model'
    return f"{BRANCH_PREFIX}/{safe_model}/{strategy.replace('_', '-')}"


def _check_branch_replaceable(repo: Path, branch: str, base: str) -> None:
    """Refuse to replace a branch that holds commits not made by the pipeline."""
    if not _git(repo, 'rev-parse', '--verify', '--quiet', f'refs/heads/{branch}', check=False):
        return
    worktrees = _git(repo, 'worktree', 'list', '--porcelain')
    if f'branch refs/heads/{branch}' in worktrees.splitlines():
        raise ApplyError(f"{branch} is checked out; switch away from it first")
    # One line per commit on the branch but not in base: "<sha> <trailer value>".
    log = _git(repo, 'log', f'{base}..{branch}',
               f'--format=%H %(trailers:key={TRAILER_KEY},valueonly,separator=%x2C)')
    foreign = [line.split()[0] for line in log.splitlines()
               if line.strip() and line.partition(' ')[2].strip() != TRAILER_VALUE]
    if foreign:
        raise ApplyError(f"{branch} has {len(foreign)} commit(s) not made by the pipeline; "
                         "rename or delete the branch before applying again")


# --------------------------------------------------------------------------- #
# Patching
# --------------------------------------------------------------------------- #

def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(' \t'))


def _min_indent(lines: List[str]) -> int:
    return min((_indent(l) for l in lines if l.strip()), default=0)


def fit_indentation(original: List[str], refactored: List[str]) -> Tuple[List[str], bool]:
    """
    Models sometimes return the snippet dedented. If every line of the
    refactored code is indented less than the original's least-indented line,
    shift it back by the difference.
    """
    shift = _min_indent(original) - _min_indent(refactored)
    if shift <= 0:
        return refactored, False
    first = next(l for l in original if l.strip())
    pad = ('\t' if first.startswith('\t') else ' ') * shift
    return [pad + l if l.strip() else l for l in refactored], True


def _plan_patches(results: List[Dict[str, Any]], strategy: str
                  ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split one strategy's results into patches to try and outcomes for the rest."""
    patches, outcomes = [], []
    for record in results:
        outcome = {'issue_key': record.get('issue_key'), 'rule': record.get('rule'),
                   'file_path': record.get('file_path'),
                   'lines': [record.get('snippet_start_line'), record.get('snippet_end_line')]}
        result = (record.get('results') or {}).get(strategy)
        if not result:
            outcomes.append({**outcome, 'status': 'skipped', 'reason': 'no result'})
        elif result.get('error') or not result.get('refactored_code'):
            outcomes.append({**outcome, 'status': 'skipped',
                             'reason': f"refactoring failed: {result.get('error', 'no code')}"})
        elif None in outcome['lines'] or not record.get('original_snippet'):
            outcomes.append({**outcome, 'status': 'skipped', 'reason': 'no line range'})
        else:
            patches.append({'record': record, 'code': result['refactored_code'],
                            'outcome': outcome})
    # Larger patches first, so an enclosing refactoring wins over those inside it.
    patches.sort(key=lambda p: p['outcome']['lines'][1] - p['outcome']['lines'][0],
                 reverse=True)
    return patches, outcomes


def _apply_patches(worktree: Path, patches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    applied: Dict[str, List[Tuple[int, int, str]]] = {}   # file -> ranges taken
    accepted: List[Dict[str, Any]] = []
    outcomes = []
    for patch in patches:
        outcome = patch['outcome']
        start, end = outcome['lines']
        clash = next((key for s, e, key in applied.get(outcome['file_path'], [])
                      if start <= e and end >= s), None)
        if clash:
            outcomes.append({**outcome, 'status': 'conflict', 'conflicts_with': clash,
                             'reason': 'overlaps a larger patch applied on this branch'})
            continue
        path = worktree / outcome['file_path']
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except OSError as e:
            outcomes.append({**outcome, 'status': 'skipped', 'reason': f'cannot read file: {e}'})
            continue
        if '\n'.join(lines[start - 1:end]) != patch['record']['original_snippet']:
            new_lines, _ = fit_indentation(patch['record']['original_snippet'].splitlines(),
                                           patch['code'].splitlines())
            if lines[start - 1:start - 1 + len(new_lines)] == new_lines:
                outcomes.append({**outcome, 'status': 'already_applied'})
            else:
                outcomes.append({**outcome, 'status': 'skipped',
                                 'reason': 'file content at these lines differs from the '
                                           'snippet sent to the model (changed or not '
                                           'committed)'})
            continue
        applied.setdefault(outcome['file_path'], []).append((start, end, outcome['issue_key']))
        accepted.append(patch)

    # Write bottom-up per file so earlier line numbers stay valid.
    by_file: Dict[str, List[Dict[str, Any]]] = {}
    for patch in accepted:
        by_file.setdefault(patch['outcome']['file_path'], []).append(patch)
    for file_path, file_patches in by_file.items():
        path = worktree / file_path
        text = path.read_text(encoding='utf-8')
        lines = text.splitlines()
        for patch in sorted(file_patches, key=lambda p: p['outcome']['lines'][0], reverse=True):
            start, end = patch['outcome']['lines']
            new_lines, reindented = fit_indentation(lines[start - 1:end],
                                                    patch['code'].splitlines())
            lines[start - 1:end] = new_lines
            outcomes.append({**patch['outcome'], 'status': 'applied',
                             'new_line_count': len(new_lines), 'reindented': reindented})
        path.write_text('\n'.join(lines) + ('\n' if text.endswith('\n') else ''),
                        encoding='utf-8')
    return outcomes


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def apply_results(repo: Path, results_path: Path, model: str,
                  report_path: Path) -> Dict[str, Any]:
    """Create one branch per strategy with its patches; returns the report."""
    if not results_path.is_file():
        raise ApplyError(f"{results_path} not found; run `python -m pipeline refactor` first")
    if _git(repo, 'rev-parse', '--is-inside-work-tree', check=False) != 'true':
        raise ApplyError(f"{repo} is not a git repository")
    results = json.loads(results_path.read_text(encoding='utf-8'))
    base = _git(repo, 'rev-parse', 'HEAD')
    base_branch = _git(repo, 'branch', '--show-current', check=False) or base[:12]

    dirty = {line[3:] for line in _git(repo, 'status', '--porcelain').splitlines()}
    dirty_targets = sorted({r.get('file_path') for r in results} & dirty)
    if dirty_targets:
        logger.warning("Uncommitted changes in %s are not on the branches (they start "
                       "from %s); patches for changed lines will be skipped",
                       ', '.join(dirty_targets), base[:12])

    report: Dict[str, Any] = {
        'applied_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'model': model, 'base_branch': base_branch, 'base_commit': base,
        'results_file': results_path.name, 'branches': {},
    }
    for strategy in STRATEGIES:
        if not any(strategy in (r.get('results') or {}) for r in results):
            continue
        branch = branch_name(model, strategy)
        report['branches'][strategy] = _build_branch(repo, base, branch, model, strategy,
                                                     results)

    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    logger.info("Apply report saved to %s", report_path)
    return report


def apply_in_place(repo: Path, results_path: Path, model: str, strategy: str,
                   report_path: Path) -> Dict[str, Any]:
    """Write one strategy's patches into the current working files, uncommitted."""
    if strategy not in STRATEGIES:
        raise ApplyError(f"Unknown strategy {strategy!r}; use one of {', '.join(STRATEGIES)}")
    if not results_path.is_file():
        raise ApplyError(f"{results_path} not found; run `python -m pipeline refactor` first")
    results = json.loads(results_path.read_text(encoding='utf-8'))
    if not any(strategy in (r.get('results') or {}) for r in results):
        raise ApplyError(f"{results_path.name} has no {strategy} results")

    patches, outcomes = _plan_patches(results, strategy)
    outcomes += _apply_patches(repo, patches)
    counts = {s: sum(1 for o in outcomes if o['status'] == s)
              for s in ('applied', 'already_applied', 'conflict', 'skipped')}
    _log_outcomes('working tree', counts, outcomes)

    report = {
        'applied_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'model': model, 'strategy': strategy, 'target': 'working tree',
        'results_file': results_path.name, **counts,
        'issues': sorted(outcomes, key=lambda o: (o['file_path'] or '', o['lines'][0] or 0)),
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    logger.info("Apply report saved to %s", report_path)
    return report


def _log_outcomes(target: str, counts: Dict[str, int], outcomes: List[Dict[str, Any]]) -> None:
    logger.info("  %-40s %s", target, ' '.join(f"{k}={v}" for k, v in counts.items()))
    for o in outcomes:
        if o['status'] == 'applied':
            logger.info("      applied %s:%s %s", o['file_path'], o['lines'][0], o['rule'])
        else:
            logger.info("      %s %s:%s %s %s", o['status'], o['file_path'], o['lines'][0],
                        o['rule'], o.get('reason', ''))


def _build_branch(repo: Path, base: str, branch: str, model: str, strategy: str,
                  results: List[Dict[str, Any]]) -> Dict[str, Any]:
    _check_branch_replaceable(repo, branch, base)
    patches, outcomes = _plan_patches(results, strategy)

    with tempfile.TemporaryDirectory(prefix='pipeline-apply-') as tmp:
        worktree = Path(tmp) / 'tree'
        _git(repo, 'worktree', 'add', '--quiet', '-B', branch, str(worktree), base)
        try:
            outcomes += _apply_patches(worktree, patches)
            applied = [o for o in outcomes if o['status'] == 'applied']
            commit = None
            if applied:
                _git(worktree, 'add', '-A')
                message = (f"Apply {strategy.replace('_', '-')} refactorings ({model})\n\n"
                           f"{len(applied)} of {len(outcomes)} SonarQube issue(s) patched by "
                           f"the {strategy} strategy.\n"
                           + ''.join(f"- {o['rule']} {o['file_path']}:{o['lines'][0]}\n"
                                     for o in applied)
                           + f"\nBase: {base}\nRefactoring-Model: {model}\n"
                             f"{TRAILER_KEY}: {TRAILER_VALUE}\n")
                done = subprocess.run(['git', 'commit', '--quiet', '--no-verify', '-F', '-'],
                                      cwd=worktree, input=message, capture_output=True,
                                      text=True)
                if done.returncode != 0:
                    raise ApplyError(f"git commit failed on {branch}: {done.stderr.strip()}")
                commit = _git(worktree, 'rev-parse', 'HEAD')
        finally:
            _git(repo, 'worktree', 'remove', '--force', str(worktree), check=False)
            _git(repo, 'worktree', 'prune', check=False)

    counts = {s: sum(1 for o in outcomes if o['status'] == s)
              for s in ('applied', 'conflict', 'skipped')}
    _log_outcomes(branch, counts, outcomes)
    return {'branch': branch, 'commit': commit, **counts,
            'issues': sorted(outcomes, key=lambda o: (o['file_path'] or '', o['lines'][0] or 0))}
