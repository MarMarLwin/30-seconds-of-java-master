"""
Stage 4: build and test the code Stage 3 applied, and find the patches that
broke it.

For every strategy branch (or the working tree, after apply --strategy) the
repository's own build runs in a temporary git worktree, in three steps:

    compile   main and test sources        (Gradle testClasses, Maven test-compile, ...)
    test      the test suite               (only if compile passed)
    style     Checkstyle, when configured  (only if compile passed)

The base commit is built the same way first, so failures that were already
there are not blamed on the refactoring. Each compiler error, failing test and
style violation is then mapped back to the issue whose patch covers it (by the
patched_lines Stage 3 recorded), and every applied issue gets a status:

    broken           a compiler error inside its patch
    suspect          a compiler error elsewhere in the same file (e.g. a call
                     to a method the patch changed)
    not_verified     the build failed elsewhere, so this patch was not tested
    test_failed      a test of its file fails that passed on the base commit
    style_violation  a new style violation inside its patch
    ok               compiles, its tests pass and no new style violations

With revert_broken, the broken patches are left out and the branch is built
again (a few rounds at most, since one error can hide others), so every
branch SonarCloud analyses compiles.

Results go to stage4_verify_<model>.json and the build logs to stage4_logs/.
"""

import contextlib
import datetime
import json
import logging
import os
import re
import shlex
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..stage3.apply import ApplyError, rebuild_branch

logger = logging.getLogger(__name__)

STEPS = ('compile', 'test', 'style')
DEFAULT_TIMEOUT_SECONDS = 1800
MAX_REVERT_ROUNDS = 3


class VerifyError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Build commands
# --------------------------------------------------------------------------- #

@dataclass
class BuildCommands:
    tool: str
    steps: Dict[str, Optional[List[str]]]
    setup: Optional[List[str]] = None
    # Where the test step leaves JUnit XML reports, relative to the tree.
    test_reports: List[str] = field(default_factory=list)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8')
    except OSError:
        return ''


def detect_build(tree: Path) -> Optional[BuildCommands]:
    """The build commands of a Gradle, Maven, Flutter or Dart project."""
    gradle_files = [tree / n for n in ('build.gradle', 'build.gradle.kts',
                                       'settings.gradle', 'settings.gradle.kts')]
    if (tree / 'gradlew').is_file() or any(p.is_file() for p in gradle_files):
        gradle = ['./gradlew'] if (tree / 'gradlew').is_file() else ['gradle']
        gradle += ['--console=plain']
        uses_checkstyle = any('checkstyle' in _read(p) for p in gradle_files)
        return BuildCommands('gradle', {
            'compile': gradle + ['--continue', 'testClasses'],
            'test': gradle + ['--continue', 'test'],
            'style': gradle + ['--continue', 'checkstyleMain', 'checkstyleTest']
                     if uses_checkstyle else None,
        }, test_reports=['**/build/test-results/**/*.xml'])
    if (tree / 'pom.xml').is_file():
        maven = ['./mvnw'] if (tree / 'mvnw').is_file() else ['mvn']
        maven += ['-B']
        return BuildCommands('maven', {
            'compile': maven + ['test-compile'],
            'test': maven + ['test'],
            'style': maven + ['checkstyle:check']
                     if 'maven-checkstyle-plugin' in _read(tree / 'pom.xml') else None,
        }, test_reports=['**/target/surefire-reports/*.xml'])
    if (tree / 'pubspec.yaml').is_file():
        sdk = 'flutter' if re.search(r'sdk:\s*flutter', _read(tree / 'pubspec.yaml')) else 'dart'
        return BuildCommands(sdk, {
            'compile': [sdk, 'analyze', '--no-fatal-warnings']
                       + (['--no-fatal-infos'] if sdk == 'flutter' else []),
            'test': [sdk, 'test'],
            'style': None,
        }, setup=[sdk, 'pub', 'get'])
    return None


def build_commands(tree: Path, overrides: Optional[Dict[str, Optional[str]]] = None
                   ) -> BuildCommands:
    """Detected commands, with any step replaced by an override command line."""
    overrides = {k: v for k, v in (overrides or {}).items() if v}
    commands = detect_build(tree)
    if commands is None:
        if 'compile' not in overrides:
            raise VerifyError(f"No Gradle, Maven or Dart build found in {tree}; pass "
                              "--compile-cmd (and --test-cmd) to say how to build it")
        commands = BuildCommands('custom', {step: None for step in STEPS})
    for step, command in overrides.items():
        commands.steps[step] = shlex.split(command)
    return commands


# --------------------------------------------------------------------------- #
# Running and parsing
# --------------------------------------------------------------------------- #

# One error or violation per line, with an absolute or repository-relative path:
#   /x/A.java:55: error: cannot find symbol                         javac
#   [ERROR] /x/A.java:[55,12] cannot find symbol                    Maven
#   e: file:///x/A.kt:55:12 Unresolved reference                    kotlinc
#   [ant:checkstyle] [WARN] /x/A.java:55:3: Missing Javadoc. [Rule] Gradle Checkstyle
#   [ERROR] src/A.java:[55,3] (javadoc) Missing Javadoc.            Maven Checkstyle
#   error - lib/a.dart:55:12 - Undefined name 'x'. - undefined_identifier   dart analyze
_PATH = r'(?P<file>(?:/|[A-Za-z]:\\)?[^\s:\[\]]+\.\w+)'
_ERROR_RES = [
    re.compile(rf'^{_PATH}:(?P<line>\d+): error: (?P<message>.*)$'),
    re.compile(rf'^\[ERROR\] {_PATH}:\[(?P<line>\d+),\d+\] (?P<message>.*)$'),
    re.compile(rf'^e: (?:file://)?{_PATH}:(?P<line>\d+):\d+ (?P<message>.*)$'),
    re.compile(rf'^\s*error - {_PATH}:(?P<line>\d+):\d+ - (?P<message>.*)$'),
]
_STYLE_RES = [
    re.compile(rf'^\[ant:checkstyle\] \[(?:WARN|ERROR)\] {_PATH}:(?P<line>\d+)(?::\d+)?: '
               r'(?P<message>.*)$'),
    re.compile(rf'^\[(?:WARN|WARNING|ERROR)\] {_PATH}:\[(?P<line>\d+)(?:,\d+)?\] '
               r'(?P<message>.*)$'),
]


def _relative(file: str, tree: Path) -> str:
    """A path from the build output, relative to the tree when it is inside it."""
    root = os.path.realpath(tree)
    path = os.path.realpath(file if os.path.isabs(file) else os.path.join(root, file))
    return Path(os.path.relpath(path, root)).as_posix() if path.startswith(root + os.sep) \
        else file


def parse_problems(output: str, tree: Path, style: bool = False) -> List[Dict[str, Any]]:
    """The errors (or, with style, the style violations) in a build's output."""
    problems, seen = [], set()
    for line in output.splitlines():
        for regex in (_STYLE_RES if style else _ERROR_RES):
            match = regex.match(line.rstrip())
            if match:
                item = {'file': _relative(match.group('file'), tree),
                        'line': int(match.group('line')),
                        'message': match.group('message').strip()}
                key = (item['file'], item['line'], item['message'])
                if key not in seen:
                    seen.add(key)
                    problems.append(item)
                break
    return problems


def read_test_reports(tree: Path, patterns: List[str], since: float
                      ) -> Tuple[int, List[Dict[str, str]]]:
    """Test count and failing tests from the JUnit XML reports written after since."""
    total, failed = 0, []
    for pattern in patterns:
        for path in sorted(tree.glob(pattern)):
            if path.stat().st_mtime < since - 1:
                continue
            try:
                root = ET.parse(path).getroot()
            except ET.ParseError:
                continue
            for case in root.iter('testcase'):
                total += 1
                problem = case.find('failure')
                if problem is None:
                    problem = case.find('error')
                if problem is not None:
                    failed.append({'test': f"{case.get('classname')}.{case.get('name')}",
                                   'class': case.get('classname') or '',
                                   'message': (problem.get('message') or '')[:500]})
    return total, failed


def run_step(tree: Path, command: List[str], log_path: Path, timeout: int) -> Dict[str, Any]:
    started = time.time()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        done = subprocess.run(command, cwd=tree, capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)
        output, returncode, error = done.stdout + done.stderr, done.returncode, None
    except subprocess.TimeoutExpired as e:
        output = (e.stdout or b'').decode(errors='replace') if isinstance(e.stdout, bytes) \
            else (e.stdout or '')
        returncode, error = None, f'timed out after {timeout}s'
    except OSError as e:
        output, returncode, error = '', None, f'could not run {command[0]}: {e}'
    log_path.write_text(f"$ {shlex.join(command)}\n\n{output}", encoding='utf-8')
    step = {'ok': returncode == 0, 'returncode': returncode,
            'duration_s': round(time.time() - started, 1), 'log': log_path.name,
            '_output': output, '_started': started}
    if error:
        step['error'] = error
    return step


def run_build(tree: Path, commands: BuildCommands, log_dir: Path, label: str,
              run_tests: bool = True, run_style: bool = True,
              timeout: int = DEFAULT_TIMEOUT_SECONDS) -> Dict[str, Any]:
    """Run the build steps in tree; returns {step: result} (skipped steps left out)."""
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', label)
    if commands.setup:
        setup = run_step(tree, commands.setup, log_dir / f'{safe}_setup.log', timeout)
        if not setup['ok']:
            logger.warning("  setup failed (%s), see %s", shlex.join(commands.setup),
                           setup['log'])

    steps: Dict[str, Any] = {}
    compile_step = run_step(tree, commands.steps['compile'], log_dir / f'{safe}_compile.log',
                            timeout)
    compile_step['errors'] = parse_problems(compile_step['_output'], tree)
    steps['compile'] = compile_step
    logger.info("  compile %s (%ss, %d error(s))", 'ok' if compile_step['ok'] else 'FAILED',
                compile_step['duration_s'], len(compile_step['errors']))

    if compile_step['ok'] and run_tests and commands.steps.get('test'):
        test = run_step(tree, commands.steps['test'], log_dir / f'{safe}_test.log', timeout)
        test['tests'], test['failed'] = read_test_reports(tree, commands.test_reports,
                                                          test['_started'])
        steps['test'] = test
        logger.info("  test    %s (%ss, %d test(s), %d failed)", 'ok' if test['ok'] else 'FAILED',
                    test['duration_s'], test['tests'], len(test['failed']))
    if compile_step['ok'] and run_style and commands.steps.get('style'):
        style = run_step(tree, commands.steps['style'], log_dir / f'{safe}_style.log', timeout)
        style['violations'] = parse_problems(style['_output'], tree, style=True)
        steps['style'] = style
        logger.info("  style   %s (%d violation(s))", 'ok' if style['ok'] else 'FAILED',
                    len(style['violations']))
    for step in steps.values():
        step.pop('_output', None)
        step.pop('_started', None)
    return steps


# --------------------------------------------------------------------------- #
# Mapping problems to issues
# --------------------------------------------------------------------------- #

def _problem_key(problem: Dict[str, Any]) -> Tuple[str, str]:
    """Line numbers move with every patch, so compare problems by file and message."""
    return problem['file'], problem['message']


def _new(problems: List[Dict[str, Any]], baseline: List[Dict[str, Any]]
         ) -> List[Dict[str, Any]]:
    known = {_problem_key(p) for p in baseline}
    return [p for p in problems if _problem_key(p) not in known]


def _tests_file(test_class: str, file_path: str) -> bool:
    """True if a failing test class tests (or is) the class in file_path."""
    stem = Path(file_path).with_suffix('').as_posix()
    name = test_class.split('$', 1)[0].replace('.', '/')
    tested = re.sub(r'(?:Tests?|IT)$', '', name)
    return stem.endswith(name) or (tested != name and stem.endswith(tested))


def attribute(steps: Dict[str, Any], applied: List[Dict[str, Any]],
              baseline: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Give every applied issue a status from the build results."""
    base = baseline or {}
    errors = _new(steps['compile']['errors'], base.get('compile', {}).get('errors', []))
    compiled = steps['compile']['ok']
    base_failed = {t['test'] for t in base.get('test', {}).get('failed', [])}
    failing = [t for t in steps.get('test', {}).get('failed', []) if t['test'] not in base_failed]
    violations = _new(steps.get('style', {}).get('violations', []),
                      base.get('style', {}).get('violations', []))

    # Reports written before Stage 3 recorded patched_lines only have the
    # original range, which is close enough unless earlier patches in the
    # same file changed its length.
    applied = [{**i, 'patched_lines': i.get('patched_lines') or i.get('lines')}
               for i in applied]

    def inside(problem, issue):
        if not issue.get('patched_lines') or None in issue['patched_lines']:
            return False
        start, end = issue['patched_lines']
        return problem['file'] == issue['file_path'] and start <= problem['line'] <= end

    in_a_patch = {id(e) for e in errors if any(inside(e, i) for i in applied)}
    patched_files = {i['file_path'] for i in applied}
    issues = []
    for issue in applied:
        entry = {k: issue.get(k) for k in ('issue_key', 'rule', 'file_path', 'patched_lines')}
        mine = [e for e in errors if inside(e, issue)]
        same_file = [e for e in errors
                     if e['file'] == issue['file_path'] and id(e) not in in_a_patch]
        tests = [t for t in failing if _tests_file(t['class'], issue['file_path'] or '')]
        style = [v for v in violations if inside(v, issue)]
        if mine:
            entry['status'], entry['compile_errors'] = 'broken', mine
        elif same_file:
            entry['status'], entry['compile_errors'] = 'suspect', same_file
        elif not compiled:
            entry['status'] = 'not_verified'
        elif tests:
            entry['status'], entry['failing_tests'] = 'test_failed', tests
        elif style:
            entry['status'], entry['style_violations'] = 'style_violation', style
        else:
            entry['status'] = 'ok'
        issues.append(entry)

    counts: Dict[str, int] = {}
    for entry in issues:
        counts[entry['status']] = counts.get(entry['status'], 0) + 1
    return {
        'issues': issues, 'counts': counts,
        'new_compile_errors': len(errors),
        # Errors in files no patch touched: usually callers of a changed signature.
        'unattributed_errors': [e for e in errors if e['file'] not in patched_files],
        'new_test_failures': failing,
        'new_style_violations': len(violations),
    }


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(['git', *args], cwd=repo, capture_output=True, text=True)
    if done.returncode != 0:
        raise VerifyError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout.strip()


@contextlib.contextmanager
def checkout(repo: Path, commit: str) -> Iterator[Path]:
    """A temporary worktree at commit, removed afterwards."""
    with tempfile.TemporaryDirectory(prefix='pipeline-verify-') as tmp:
        tree = Path(tmp) / 'tree'
        _git(repo, 'worktree', 'add', '--quiet', '--detach', str(tree), commit)
        try:
            yield tree
        finally:
            subprocess.run(['git', 'worktree', 'remove', '--force', str(tree)], cwd=repo,
                           capture_output=True)
            subprocess.run(['git', 'worktree', 'prune'], cwd=repo, capture_output=True)


@dataclass
class VerifyOptions:
    overrides: Dict[str, Optional[str]] = field(default_factory=dict)
    run_tests: bool = True
    run_style: bool = True
    baseline: bool = True
    revert_broken: bool = False
    timeout: int = DEFAULT_TIMEOUT_SECONDS


def _applied(issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [i for i in issues if i.get('status') == 'applied']


def _verify_tree(tree: Path, label: str, applied: List[Dict[str, Any]],
                 baseline: Optional[Dict[str, Any]], log_dir: Path,
                 options: VerifyOptions) -> Dict[str, Any]:
    commands = build_commands(tree, options.overrides)
    logger.info("Verifying %s (%s)", label, commands.tool)
    steps = run_build(tree, commands, log_dir, label, options.run_tests, options.run_style,
                      options.timeout)
    return {'steps': steps, **attribute(steps, applied, baseline)}


def verify(repo: Path, applied_path: Path, results_path: Path, report_path: Path,
           log_dir: Path, options: Optional[VerifyOptions] = None) -> Dict[str, Any]:
    """Build and test what apply_results / apply_in_place wrote; returns the report."""
    options = options or VerifyOptions()
    if not applied_path.is_file():
        raise VerifyError(f"{applied_path} not found; run `python -m pipeline apply` first")
    applied_report = json.loads(applied_path.read_text(encoding='utf-8'))
    in_place = 'branches' not in applied_report
    if in_place and options.revert_broken:
        raise VerifyError("--revert-broken only works on strategy branches; for the working "
                          "tree, undo a broken patch with `git restore -p <file>`")
    base = applied_report.get('base_commit') or _git(repo, 'rev-parse', 'HEAD')
    model = applied_report.get('model', '')

    baseline = None
    if options.baseline:
        with checkout(repo, base) as tree:
            commands = build_commands(tree, options.overrides)
            logger.info("Verifying the base commit %s (%s)", base[:12], commands.tool)
            baseline = run_build(tree, commands, log_dir, f'{model}_base', options.run_tests,
                                 options.run_style, options.timeout)

    report: Dict[str, Any] = {
        'verified_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'model': model, 'applied_file': applied_path.name, 'base_commit': base,
        'commands': build_commands(repo, options.overrides).steps,
        'baseline': baseline, 'targets': {},
    }
    if in_place:
        strategy = applied_report['strategy']
        report['targets'][strategy] = {
            'target': 'working tree',
            **_verify_tree(repo, f'{model}_{strategy}_working_tree',
                           _applied(applied_report['issues']), baseline, log_dir, options)}
    else:
        for strategy, info in applied_report['branches'].items():
            report['targets'][strategy] = _verify_branch(
                repo, applied_report, applied_path, results_path, strategy, info, baseline,
                log_dir, options)

    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    logger.info("Verify report saved to %s", report_path)
    return report


def _verify_branch(repo: Path, applied_report: Dict[str, Any], applied_path: Path,
                   results_path: Path, strategy: str, info: Dict[str, Any],
                   baseline: Optional[Dict[str, Any]], log_dir: Path,
                   options: VerifyOptions) -> Dict[str, Any]:
    if not info.get('commit'):
        return {'branch': info['branch'], 'commit': None, 'skipped': 'no patches applied'}
    model, base = applied_report['model'], applied_report['base_commit']
    reverted: List[Dict[str, Any]] = []
    excluded: Dict[str, str] = {}
    for round_number in range(1, MAX_REVERT_ROUNDS + 2):
        with checkout(repo, info['commit']) as tree:
            label = f"{model}_{strategy}" + (f'_round{round_number}' if round_number > 1 else '')
            target = _verify_tree(tree, label, _applied(info['issues']), baseline, log_dir,
                                  options)
        broken = [i for i in target['issues'] if i['status'] == 'broken']
        if (not options.revert_broken or target['steps']['compile']['ok'] or not broken
                or round_number > MAX_REVERT_ROUNDS):
            break
        for issue in broken:
            excluded[issue['issue_key']] = 'reverted by Stage 4: does not compile'
            reverted.append({**issue, 'round': round_number})
        logger.info("  reverting %d broken patch(es) and rebuilding %s", len(broken),
                    info['branch'])
        try:
            info = rebuild_branch(repo, results_path, model, strategy, base, excluded)
        except ApplyError as e:
            raise VerifyError(f"Could not rebuild {info['branch']}: {e}")
        applied_report['branches'][strategy] = info
        applied_path.write_text(json.dumps(applied_report, indent=2, ensure_ascii=False),
                                encoding='utf-8')
    result = {'branch': info['branch'], 'commit': info['commit'], **target}
    if reverted:
        result['reverted'] = reverted
        result['counts']['reverted'] = len(reverted)
    return result


def summary_lines(report: Dict[str, Any]) -> List[str]:
    lines = []
    for strategy, target in report['targets'].items():
        if 'steps' not in target:
            lines.append(f"{strategy:17} {target.get('skipped', '')}")
            continue
        steps = target['steps']
        state = ' '.join(f"{name}={'ok' if steps[name]['ok'] else 'FAILED'}"
                         for name in STEPS if name in steps)
        counts = ' '.join(f"{k}={v}" for k, v in sorted(target['counts'].items()))
        lines.append(f"{strategy:17} {state}  issues: {counts}")
    return lines
