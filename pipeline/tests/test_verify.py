"""Stage 4: parsing build output, blaming patches, and the verify loop."""

import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from pipeline.stage3.apply import apply_results, branch_name
from pipeline.stage4.verify import (VerifyOptions, attribute, build_commands, detect_build,
                                    parse_problems, read_test_reports, verify)

TREE = Path('/work/repo')

JAVAC = """\
> Task :compileJava FAILED
/work/repo/src/main/java/network/HttpPostSnippet.java:55: error: cannot find symbol
  public static HttpResponse<String> httpPost(String address, Map<String, String> arguments)
                                                              ^
  symbol:   class Map
src/test/java/algorithm/VerhoeffSnippetTest.java:67: error: cannot find symbol
/work/repo/src/main/java/A.java:3: warning: [deprecation] x
"""


def applied(key, file, start, end):
    return {'issue_key': key, 'rule': 'java:S1319', 'file_path': file, 'status': 'applied',
            'patched_lines': [start, end]}


class ParseTest(unittest.TestCase):
    def test_javac(self):
        self.assertEqual(parse_problems(JAVAC, TREE), [
            {'file': 'src/main/java/network/HttpPostSnippet.java', 'line': 55,
             'message': 'cannot find symbol'},
            {'file': 'src/test/java/algorithm/VerhoeffSnippetTest.java', 'line': 67,
             'message': 'cannot find symbol'}])

    def test_maven_kotlin_dart(self):
        output = ("[ERROR] /work/repo/src/A.java:[12,5] cannot find symbol\n"
                  "e: file:///work/repo/src/B.kt:7:3 Unresolved reference: x\n"
                  "  error - lib/a.dart:4:9 - Undefined name 'y'. - undefined_identifier\n")
        self.assertEqual([(p['file'], p['line']) for p in parse_problems(output, TREE)],
                         [('src/A.java', 12), ('src/B.kt', 7), ('lib/a.dart', 4)])

    def test_checkstyle(self):
        output = ("[ant:checkstyle] [WARN] /work/repo/src/A.java:10:3: Missing a Javadoc "
                  "comment. [MissingJavadocMethod]\n")
        self.assertEqual(parse_problems(output, TREE, style=True),
                         [{'file': 'src/A.java', 'line': 10,
                           'message': 'Missing a Javadoc comment. [MissingJavadocMethod]'}])
        self.assertEqual(parse_problems(output, TREE), [])

    def test_junit_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp) / 'build' / 'test-results' / 'test'
            reports.mkdir(parents=True)
            (reports / 'TEST-a.xml').write_text(
                '<testsuite><testcase classname="algorithm.VerhoeffSnippetTest" name="gen()">'
                '<failure message="expected 9"/></testcase>'
                '<testcase classname="algorithm.VerhoeffSnippetTest" name="ok()"/></testsuite>')
            total, failed = read_test_reports(Path(tmp), ['**/build/test-results/**/*.xml'], 0)
        self.assertEqual(total, 2)
        self.assertEqual(failed[0]['test'], 'algorithm.VerhoeffSnippetTest.gen()')


class AttributeTest(unittest.TestCase):
    def steps(self, errors=(), failed=(), ok=None):
        steps = {'compile': {'ok': not errors if ok is None else ok, 'errors': list(errors)}}
        if not errors:
            steps['test'] = {'ok': not failed, 'failed': list(failed)}
        return steps

    def test_error_inside_patch_is_broken(self):
        errors = parse_problems(JAVAC, TREE)
        issues = [applied('P1', 'src/main/java/network/HttpPostSnippet.java', 40, 73),
                  applied('V1', 'src/test/java/algorithm/VerhoeffSnippetTest.java', 70, 80),
                  applied('O1', 'src/main/java/Other.java', 1, 9)]
        result = attribute(self.steps(errors), issues, None)
        status = {i['issue_key']: i['status'] for i in result['issues']}
        self.assertEqual(status, {'P1': 'broken', 'V1': 'suspect', 'O1': 'not_verified'})
        self.assertEqual(result['unattributed_errors'], [])

    def test_old_reports_fall_back_to_the_original_lines(self):
        errors = parse_problems(JAVAC, TREE)
        issue = {**applied('P1', 'src/main/java/network/HttpPostSnippet.java', 0, 0),
                 'patched_lines': None, 'lines': [40, 73]}
        result = attribute(self.steps(errors), [issue], None)
        self.assertEqual(result['issues'][0]['status'], 'broken')

    def test_baseline_errors_and_failures_are_not_blamed(self):
        error = {'file': 'src/A.java', 'line': 5, 'message': 'boom'}
        baseline = {'compile': {'ok': False, 'errors': [{**error, 'line': 3}]}}
        result = attribute(self.steps([error], ok=False), [applied('A', 'src/A.java', 4, 6)],
                           baseline)
        self.assertEqual(result['issues'][0]['status'], 'not_verified')
        self.assertEqual(result['new_compile_errors'], 0)

    def test_failing_test_is_mapped_to_the_class_it_tests(self):
        failed = [{'test': 'algorithm.VerhoeffSnippetTest.gen()',
                   'class': 'algorithm.VerhoeffSnippetTest', 'message': ''}]
        issues = [applied('S', 'src/main/java/algorithm/VerhoeffSnippet.java', 1, 5),
                  applied('T', 'src/test/java/algorithm/VerhoeffSnippetTest.java', 1, 5),
                  applied('U', 'src/main/java/algorithm/Other.java', 1, 5)]
        status = {i['issue_key']: i['status']
                  for i in attribute(self.steps(failed=failed), issues, None)['issues']}
        self.assertEqual(status, {'S': 'test_failed', 'T': 'test_failed', 'U': 'ok'})
        baseline = {'compile': {'ok': True, 'errors': []}, 'test': {'failed': failed}}
        result = attribute(self.steps(failed=failed), issues, baseline)
        self.assertEqual(result['counts'], {'ok': 3})


class DetectBuildTest(unittest.TestCase):
    def test_gradle_with_checkstyle(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'gradlew').write_text('')
            (Path(tmp) / 'build.gradle').write_text("apply plugin: 'checkstyle'")
            commands = detect_build(Path(tmp))
        self.assertEqual(commands.steps['compile'][-1], 'testClasses')
        self.assertEqual(commands.steps['style'][-2:], ['checkstyleMain', 'checkstyleTest'])

    def test_override_and_missing_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(detect_build(Path(tmp)))
            commands = build_commands(Path(tmp), {'compile': 'make "all files"', 'test': None})
        self.assertEqual(commands.steps['compile'], ['make', 'all files'])


SOURCE = "class A {\n  int a() { return 1; }\n  int b() { return 2; }\n}\n"
# A fake compiler: every line containing BROKEN is a compile error.
FAKE_COMPILER = textwrap.dedent("""\
    import pathlib, sys
    bad = [(p, n) for p in sorted(pathlib.Path('.').rglob('*.java'))
           for n, line in enumerate(p.read_text().splitlines(), 1) if 'BROKEN' in line]
    for p, n in bad:
        print(f"{p.resolve()}:{n}: error: cannot find symbol")
    sys.exit(1 if bad else 0)
""")


def git(repo, *args):
    return subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


@unittest.skipUnless(shutil.which('git'), 'git is not installed')
class VerifyBranchesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / 'repo'
        self.repo.mkdir()
        (self.repo / 'A.java').write_text(SOURCE)
        git(self.repo, 'init', '-q', '-b', 'main')
        git(self.repo, 'config', 'user.email', 't@example.com')
        git(self.repo, 'config', 'user.name', 'Test')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'base')
        compiler = root / 'compile.py'
        compiler.write_text(FAKE_COMPILER)
        self.options = VerifyOptions(overrides={'compile': f'{sys.executable} {compiler}'},
                                     run_tests=False)
        self.results = root / 'results.json'
        self.applied = root / 'applied.json'
        self.report = root / 'verify.json'
        self.logs = root / 'logs'

        def record(key, line, code):
            return {'issue_key': key, 'rule': 'java:S1', 'file_path': 'A.java',
                    'snippet_start_line': line, 'snippet_end_line': line,
                    'original_snippet': SOURCE.splitlines()[line - 1],
                    'results': {'zero_shot': {'refactored_code': code}}}
        self.results.write_text(json.dumps([
            record('GOOD', 2, '  int a() { return 10; }'),
            record('BAD', 3, '  int b() { return BROKEN; }')]))
        apply_results(self.repo, self.results, 'm', self.applied)

    def tearDown(self):
        self.tmp.cleanup()

    def run_verify(self, **options):
        for name, value in options.items():
            setattr(self.options, name, value)
        return verify(self.repo, self.applied, self.results, self.report, self.logs,
                      self.options)

    def test_broken_patch_is_found(self):
        report = self.run_verify()
        target = report['targets']['zero_shot']
        self.assertTrue(report['baseline']['compile']['ok'])
        self.assertFalse(target['steps']['compile']['ok'])
        status = {i['issue_key']: i['status'] for i in target['issues']}
        self.assertEqual(status, {'GOOD': 'not_verified', 'BAD': 'broken'})
        self.assertTrue((self.logs / 'm_zero_shot_compile.log').is_file())
        self.assertEqual(git(self.repo, 'worktree', 'list').count('\n'), 0)

    def test_revert_broken_rebuilds_the_branch(self):
        report = self.run_verify(revert_broken=True)
        target = report['targets']['zero_shot']
        self.assertTrue(target['steps']['compile']['ok'])
        self.assertEqual([i['issue_key'] for i in target['reverted']], ['BAD'])
        self.assertEqual({i['issue_key']: i['status'] for i in target['issues']},
                         {'GOOD': 'ok'})
        content = git(self.repo, 'show', f"{branch_name('m', 'zero_shot')}:A.java")
        self.assertIn('return 10;', content)
        self.assertNotIn('BROKEN', content)
        applied_report = json.loads(self.applied.read_text())
        self.assertEqual(applied_report['branches']['zero_shot']['commit'], target['commit'])


if __name__ == '__main__':
    unittest.main()
