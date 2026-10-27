"""qe_verify_ai_worktree: shared repository identity with the real common.sh and real Git repositories."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

COMMON = Path(__file__).resolve().parents[1] / 'lib/common.sh'
PROJECT = 'p'


class WorktreeIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / 'home'
        self.hooks = self.home / 'hooks'
        self.hooks.mkdir(parents=True)
        for name in ('pre-push', 'pre-commit', 'commit-msg'):
            hook = self.hooks / name
            hook.write_text('#!/bin/sh\nexit 0\n')
            hook.chmod(0o755)
        (self.home / 'projects' / PROJECT).mkdir(parents=True)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('CADRE_', 'GIT_'))}
        self.env.update(CADRE_HOME=str(self.home), HOME=str(self.root), GIT_CONFIG_NOSYSTEM='1',
                        GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid',
                        GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.invalid')
        self.source = self.repo('source')
        self.settings(self.source)

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(['git', *args], cwd=cwd, env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def repo(self, name: str, *init: str) -> Path:
        path = self.root / name
        self.git('init', '-q', '-b', 'main', *init, str(path))
        (path / 'product.txt').write_text(name + '\n')
        self.git('-C', str(path), 'add', 'product.txt')
        self.git('-C', str(path), 'commit', '-q', '-m', 'base')
        return path

    def settings(self, repo_path: Path, base: str = 'main') -> None:
        project = self.home / 'projects' / PROJECT
        (project / 'repo-path').write_text(f'{repo_path}\n')
        (project / 'base-branch').write_text(f'{base}\n')

    def shell(self, command: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(['bash', '-c', '. "$1"; shift; ' + command, 'qe', str(COMMON), *args],
                              env=self.env, capture_output=True, text=True)

    def make(self) -> Path:
        made = self.shell('qe_make_worktree "$1" "$2"', PROJECT, 'kimlik testi')
        self.assertEqual(made.returncode, 0, made.stderr)
        return Path(made.stdout.strip())

    def verify(self, worktree: Path) -> subprocess.CompletedProcess[str]:
        return self.shell('qe_verify_ai_worktree "$1" "$2"', str(worktree), PROJECT)

    def foreign(self, repo: Path, name: str) -> Path:
        worktree = self.home / 'worktrees' / PROJECT / name
        worktree.parent.mkdir(parents=True, exist_ok=True)
        self.git('-C', str(repo), 'worktree', 'add', '-q', '-b', f'cadre/{name}', str(worktree), 'main')
        self.git('-C', str(repo), 'config', 'extensions.worktreeConfig', 'true')
        self.git('-C', str(worktree), 'config', '--worktree', 'core.hooksPath', str(self.hooks))
        return worktree

    def assertRefused(self, result: subprocess.CompletedProcess[str], reason: str) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(reason, result.stderr)

    def test_normal_source_repo_worktree_passes(self) -> None:
        worktree = self.make()
        self.assertEqual(self.verify(worktree).returncode, 0, self.verify(worktree).stderr)

    def test_linked_source_worktree_passes_with_shared_common_dir(self) -> None:
        linked = self.root / 'linked-source'
        self.git('-C', str(self.source), 'worktree', 'add', '-q', '--detach', str(linked), 'main')
        self.settings(linked)
        worktree = self.make()
        self.assertEqual(Path(self.git('-C', str(worktree), 'rev-parse', '--path-format=absolute', '--git-common-dir')),
                         self.source / '.git')
        result = self.verify(worktree)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_worktree_of_another_repo_is_refused(self) -> None:
        other = self.repo('other')
        self.assertRefused(self.verify(self.foreign(other, 'other')), 'does not belong to')

    def test_similar_path_prefixes_are_refused(self) -> None:
        sibling = self.repo('source-copy')
        self.assertRefused(self.verify(self.foreign(sibling, 'sibling')), 'does not belong to')
        shadow = self.repo('shadow', '--separate-git-dir', str(self.source / '.git-shadow'))
        self.assertTrue(str(Path(self.git('-C', str(shadow), 'rev-parse', '--path-format=absolute', '--git-common-dir')))
                        .startswith(str(self.source / '.git')))
        self.assertRefused(self.verify(self.foreign(shadow, 'shadow')), 'does not belong to')

    def test_subdirectories_are_not_repository_identities(self) -> None:
        worktree = self.make()
        (worktree / 'sub').mkdir()
        self.assertRefused(self.verify(worktree / 'sub'), 'not a git work tree root')
        (self.source / 'nested').mkdir()
        self.settings(self.source / 'nested')
        self.assertRefused(self.verify(worktree), 'repo-path is not a git work tree root')

    def test_inherited_git_dir_does_not_change_identity(self) -> None:
        other = self.repo('other')
        foreign = self.foreign(other, 'leak')
        self.env['GIT_DIR'] = str(self.source / '.git')
        identity = self.shell('qe_git_common_dir "$1"', str(foreign))
        self.assertEqual((identity.returncode, identity.stdout.strip()), (0, str(other / '.git')), identity.stderr)
        self.assertRefused(self.verify(foreign), 'REFUSING TO EDIT')

    def test_wrong_branch_is_refused(self) -> None:
        worktree = self.make()
        self.git('-C', str(worktree), 'switch', '-q', '-c', 'feature/x')
        self.assertRefused(self.verify(worktree), "does not match cadre/*")

    def test_base_branch_name_is_refused(self) -> None:
        worktree = self.make()
        branch = self.git('-C', str(worktree), 'branch', '--show-current')
        self.settings(self.source, base=branch)
        self.assertRefused(self.verify(worktree), 'branch equals base branch')

    def test_outside_central_area_is_refused(self) -> None:
        outside = self.root / 'outside'
        self.git('-C', str(self.source), 'worktree', 'add', '-q', '-b', 'cadre/outside', str(outside), 'main')
        self.assertRefused(self.verify(outside), 'not under the central worktree area')

    def test_missing_or_non_executable_guard_hooks_are_refused(self) -> None:
        worktree = self.make()
        (self.hooks / 'pre-commit').chmod(0o644)
        self.assertRefused(self.verify(worktree), "guard hook 'pre-commit' missing or not executable")
        (self.hooks / 'pre-commit').chmod(0o755)
        self.git('-C', str(worktree), 'config', '--worktree', '--unset', 'core.hooksPath')
        self.assertRefused(self.verify(worktree), 'cadre guard hooks not installed')


class LinkedSourceDispatchTests(unittest.TestCase):
    def test_real_cli_reaches_implementation_from_a_linked_source_worktree(self) -> None:
        import json
        import sys
        from test_uctan_uca import E2EFixture
        fixture = E2EFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        linked = (fixture.repo.parent / 'linked-source').resolve()
        fixture.git('worktree', 'add', '-q', '--detach', str(linked), 'main')
        (fixture.home / 'projects/e2e/repo-path').write_text(str(linked))
        head = fixture.git('rev-parse', 'HEAD')
        command = [sys.executable, str(fixture.root / 'lib/kosu.py')]

        def cli(*args: str, data: object = None, timeout: int = 30) -> subprocess.CompletedProcess[str]:
            return subprocess.run([*command, *args], input=json.dumps(data) if data is not None else None,
                                  text=True, capture_output=True, env=fixture.env, timeout=timeout)

        raw = dict(submission_id='linked-code', project='e2e', target_path=str(linked), target_kind='existing', input_mode='goal',
                   prompt=fixture.PROMPT, output='Working a.py and b.py', acceptance='Both files compile and contain TAMAM',
                   preference='economic', difficulty_override='easy', duration_minutes='60', budget_usd='2', backlog_path='', source_token='')
        submitted = cli('request', data=raw)
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        rid = json.loads(submitted.stdout)['run_id']
        proposal = cli('request-proposal', rid)
        self.assertEqual(proposal.returncode, 0, proposal.stderr)
        proposal = json.loads(proposal.stdout)
        first = cli('request-approve', rid, data=proposal)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = cli('request-approve', rid, json.loads(first.stdout)['confirmation'], data=proposal)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(cli('request-plan', rid).returncode, 0)
        executed = cli('run', rid, timeout=100)
        saved = fixture.store.read(rid)
        self.assertEqual(executed.returncode, 0, saved)
        self.assertEqual((saved['state'], saved['final_result']['status']), ('completed', 'PASS'))
        self.assertTrue(any(c['role'].startswith('worker') for c in fixture.calls()))
        worktree = Path(saved['worktree'])
        self.assertEqual(Path(subprocess.check_output(['git', '-C', str(worktree), 'rev-parse', '--path-format=absolute',
                                                       '--git-common-dir'], text=True).strip()).resolve(),
                         (fixture.repo / '.git').resolve())
        self.assertTrue(subprocess.check_output(['git', '-C', str(worktree), 'branch', '--show-current'], text=True).startswith('cadre/'))
        self.assertIn('TAMAM', (worktree / 'a.py').read_text())
        self.assertFalse((linked / 'a.py').exists())
        self.assertEqual(subprocess.check_output(['git', '-C', str(linked), 'status', '--porcelain'], text=True), '')
        self.assertEqual(fixture.git('rev-parse', 'HEAD'), head)


class GuardRefusalRetryTests(unittest.TestCase):
    def test_permanent_guard_refusal_is_recorded_and_not_revived_automatically(self) -> None:
        import json
        import sys
        from unittest.mock import patch
        from test_uctan_uca import E2EFixture
        fixture = E2EFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        command = [sys.executable, str(fixture.root / 'lib/kosu.py')]

        def cli(*args: str, data: object = None, timeout: int = 30) -> subprocess.CompletedProcess[str]:
            return subprocess.run([*command, *args], input=json.dumps(data) if data is not None else None,
                                  text=True, capture_output=True, env=fixture.env, timeout=timeout)

        raw = dict(submission_id='guard-refusal', project='e2e', target_path=str(fixture.repo), target_kind='existing', input_mode='goal',
                   prompt=fixture.PROMPT, output='Working a.py and b.py', acceptance='Both files compile and contain TAMAM',
                   preference='economic', difficulty_override='easy', duration_minutes='60', budget_usd='2', backlog_path='', source_token='')
        rid = json.loads(cli('request', data=raw).stdout)['run_id']
        proposal = json.loads(cli('request-proposal', rid).stdout)
        first = json.loads(cli('request-approve', rid, data=proposal).stdout)
        self.assertEqual(cli('request-approve', rid, first['confirmation'], data=proposal).returncode, 0)
        self.assertEqual(cli('request-plan', rid).returncode, 0)
        (fixture.home / 'hooks' / 'pre-commit').chmod(0o600)
        executed = cli('run', rid, timeout=100)
        saved = fixture.store.read(rid)
        self.assertNotEqual(executed.returncode, 0)
        self.assertEqual(saved['state'], 'failed', saved.get('reason'))
        self.assertTrue(saved['reason'].startswith('worktree-guard: '), saved['reason'])
        self.assertIn("guard hook 'pre-commit' missing or not executable", saved['reason'])
        self.assertFalse(any(c['role'].startswith('worker') for c in fixture.calls()), 'koruma reddinde yazar baslamaz')
        import kosu
        with patch.dict(os.environ, {'CADRE_HOME': str(fixture.home)}), patch.object(kosu, '_baslat') as start:
            kosu.revive(kosu.RunStore(fixture.home), fixture.root)
        start.assert_not_called()

if __name__ == '__main__':
    unittest.main()
