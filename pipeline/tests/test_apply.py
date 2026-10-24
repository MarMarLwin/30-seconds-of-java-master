"""Apply step against a throwaway git repository."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pipeline.stage3.apply import (ApplyError, apply_in_place, apply_results, branch_name,
                                   fit_indentation)

SOURCE = """\
class A {
  Widget build() {
    final a = x ? 1 : y ? 2 : 3;
    final b = p ? 1 : q ? 2 : 3;
    return Text('$a$b');
  }
}

void other() {
  try { run(); } catch (_) { print(_); }
}
"""


def git(repo, *args, stdin=None):
    return subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True,
                          text=True, input=stdin).stdout.strip()


def lines(start, end):
    return '\n'.join(SOURCE.splitlines()[start - 1:end])


def record(key, rule, start, end, **results):
    return {'issue_key': key, 'rule': rule, 'file_path': 'lib/a.dart',
            'snippet_start_line': start, 'snippet_end_line': end,
            'original_snippet': lines(start, end),
            'results': {s: r for s, r in results.items()}}


@unittest.skipUnless(shutil.which('git'), 'git is not installed')
class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / 'repo'
        (self.repo / 'lib').mkdir(parents=True)
        (self.repo / 'lib' / 'a.dart').write_text(SOURCE)
        git(self.repo, 'init', '-q', '-b', 'main')
        git(self.repo, 'config', 'user.email', 't@example.com')
        git(self.repo, 'config', 'user.name', 'Test')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'base')
        self.results = Path(self.tmp.name) / 'results.json'
        self.report = Path(self.tmp.name) / 'report.json'

    def tearDown(self):
        self.tmp.cleanup()

    def write_results(self, records):
        self.results.write_text(json.dumps(records))

    def apply(self):
        return apply_results(self.repo, self.results, 'test-model', self.report)

    def show(self, strategy):
        return git(self.repo, 'show', f"{branch_name('test-model', strategy)}:lib/a.dart")

    def test_branches_get_their_own_patches(self):
        self.write_results([
            record('T1', 'dart:S3358', 3, 3,
                   zero_shot={'refactored_code': '    final a = pickA();'},
                   few_shot={'error': 'cut off', 'refactored_code': None}),
            record('W1', 'dart:S7055', 10, 10,
                   zero_shot={'refactored_code': '  try { run(); } catch (e) { print(e); }'},
                   few_shot={'refactored_code': '  try { run(); } catch (err) { print(err); }'}),
        ])
        report = self.apply()

        zero = self.show('zero_shot')
        self.assertIn('final a = pickA();', zero)
        self.assertIn('catch (e)', zero)
        few = self.show('few_shot')
        self.assertIn('final a = x ? 1 : y ? 2 : 3;', few)     # failed call left as is
        self.assertIn('catch (err)', few)

        self.assertEqual(report['branches']['zero_shot']['applied'], 2)
        self.assertEqual(report['branches']['few_shot']['skipped'], 1)
        self.assertEqual(git(self.repo, 'branch', '--show-current'), 'main')
        self.assertEqual(git(self.repo, 'status', '--porcelain'), '')
        self.assertEqual((self.repo / 'lib' / 'a.dart').read_text(), SOURCE)
        message = git(self.repo, 'log', '-1', '--format=%B', branch_name('test-model', 'zero_shot'))
        self.assertIn('Generated-By: refactoring-pipeline', message)

    def test_enclosing_patch_wins_over_patches_inside_it(self):
        self.write_results([
            record('T1', 'dart:S3358', 3, 3, zero_shot={'refactored_code': '    final a = 1;'}),
            record('C1', 'dart:S3776', 2, 6, zero_shot={'refactored_code':
                   '  Widget build() {\n    return Text(label());\n  }'}),
        ])
        report = self.apply()
        issues = {i['issue_key']: i for i in report['branches']['zero_shot']['issues']}
        self.assertEqual(issues['C1']['status'], 'applied')
        self.assertEqual(issues['T1']['status'], 'conflict')
        self.assertEqual(issues['T1']['conflicts_with'], 'C1')
        content = self.show('zero_shot')
        self.assertIn('return Text(label());', content)
        self.assertIn('void other()', content)

    def test_changed_lines_are_skipped(self):
        stale = record('T1', 'dart:S3358', 3, 3, zero_shot={'refactored_code': '    x;'})
        stale['original_snippet'] = '    something else'
        self.write_results([stale])
        report = self.apply()
        self.assertEqual(report['branches']['zero_shot']['issues'][0]['status'], 'skipped')
        self.assertIsNone(report['branches']['zero_shot']['commit'])

    def test_dedented_output_is_reindented(self):
        self.write_results([record('T1', 'dart:S3358', 3, 4, zero_shot={
            'refactored_code': 'final a = pickA();\nfinal b = pickB();'})])
        self.apply()
        self.assertIn('\n    final a = pickA();\n    final b = pickB();\n',
                      self.show('zero_shot'))

    def test_reapply_replaces_pipeline_branch_but_not_user_commits(self):
        self.write_results([record('T1', 'dart:S3358', 3, 3,
                                   zero_shot={'refactored_code': '    final a = 1;'})])
        self.apply()
        self.write_results([record('T1', 'dart:S3358', 3, 3,
                                   zero_shot={'refactored_code': '    final a = 2;'})])
        self.apply()
        self.assertIn('final a = 2;', self.show('zero_shot'))

        branch = branch_name('test-model', 'zero_shot')
        git(self.repo, 'checkout', '-q', branch)
        (self.repo / 'notes.txt').write_text('mine')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'my own work')
        git(self.repo, 'checkout', '-q', 'main')
        with self.assertRaises(ApplyError):
            self.apply()


    def test_in_place_writes_working_tree_once(self):
        self.write_results([
            record('T1', 'dart:S3358', 3, 3, repository_aware={'refactored_code': '    final a = pickA();'}),
            record('W1', 'dart:S7055', 10, 10, zero_shot={'refactored_code': '  // other strategy'}),
        ])
        report = apply_in_place(self.repo, self.results, 'test-model', 'repository_aware',
                                self.report)
        self.assertEqual(report['applied'], 1)
        content = (self.repo / 'lib' / 'a.dart').read_text()
        self.assertIn('    final a = pickA();\n', content)
        self.assertNotIn('other strategy', content)
        self.assertEqual(git(self.repo, 'status', '--porcelain'), 'M lib/a.dart')
        self.assertEqual(git(self.repo, 'branch', '--list', 'refactor/*'), '')

        again = apply_in_place(self.repo, self.results, 'test-model', 'repository_aware',
                               self.report)
        self.assertEqual((again['applied'], again['already_applied']), (0, 1))
        self.assertEqual((self.repo / 'lib' / 'a.dart').read_text(), content)


class IndentationTest(unittest.TestCase):
    def test_fit_indentation(self):
        self.assertEqual(fit_indentation(['    a'], ['a', '  b']), (['    a', '      b'], True))
        self.assertEqual(fit_indentation(['  a'], ['  a2']), (['  a2'], False))


if __name__ == '__main__':
    unittest.main()
