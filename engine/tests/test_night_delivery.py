"""Delivery (user decision 16.09): the system never pushes/merges on its own; the user asks from the `cadre` menu (5/6) or with `cadre push|merge`,
gives two separate explicit approvals, and the system does the work. Only the last COMPLETED run (QA+judge passed) is delivered. 'no' -> touches nothing.
Real cadre.sh, real git (a fake bare remote), the real guard hook (pre-push: only cadre/* ff push)."""

from __future__ import annotations

import os
from pathlib import Path
import select
import subprocess
import sys
import time
import unittest

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import test_end_to_end as e2e  # noqa: E402


class DeliveryTests(e2e.E2EFixture):
    def setUp(self):
        super().setUp()
        self.bare = Path(self.temp.name) / 'remote.git'
        subprocess.run(['git', 'init', '-q', '--bare', str(self.bare)], check=True)
        self.git('remote', 'add', 'origin', str(self.bare))
        self.git('push', '-q', 'origin', 'main')

    def cadre(self, *args, approval=None, approval_push=None):
        env = dict(self.env)
        if approval is not None:
            env['CADRE_APPROVAL'] = env['CADRE_APPROVAL_2'] = approval
        if approval_push is not None:
            env['CADRE_APPROVAL_PUSH'] = env['CADRE_APPROVAL_PUSH_2'] = approval_push
        answers = [approval] * 2 if approval is not None else []
        if approval_push is not None:
            answers += [approval_push] * 2
        if len(args) == 1 and len([r for r in self.store.list() if r['state'] == 'completed']) > 1:
            answers.insert(0, '')
        return subprocess.run(['bash', str(self.root / 'bin/cadre.sh'), *args], cwd=self.repo, env=env, text=True,
                              capture_output=True, timeout=60, input='\n'.join(answers) + ('\n' if answers else ''))

    def remote_branches(self):
        return subprocess.run(['git', '--git-dir', str(self.bare), 'branch', '--format=%(refname:short)'], capture_output=True, text=True, check=True).stdout.split()

    def test_push_and_merge_only_after_completion_only_with_consent(self):
        # without a completed run: denial
        r = self.cadre('push', approval='yes')
        self.assertEqual(r.returncode, 1)
        self.assertIn('COMPLETED', r.stderr)
        rid = self.submit()
        record = self.wait_terminal(rid)
        self.assertEqual(record['state'], 'completed')
        branch = subprocess.run(['git', '-C', record['worktree'], 'branch', '--show-current'], capture_output=True, text=True, check=True).stdout.strip()
        self.assertTrue(branch.startswith('cadre/'))
        # no -> touches nothing
        r = self.cadre('push', approval='no')
        self.assertEqual(r.returncode, 1)
        self.assertNotIn(branch, self.remote_branches())
        env = dict(self.env, CADRE_APPROVAL='yes')
        r = subprocess.run(['bash', str(self.root / 'bin/cadre.sh'), 'push'], cwd=self.repo, env=env, text=True, capture_output=True,
                           timeout=60, input='yes\n')
        self.assertEqual(r.returncode, 1, 'One approval does not push; request the second approval separately')
        self.assertIn('Command to run', r.stdout + r.stderr)
        self.assertNotIn(branch, self.remote_branches())
        # yes -> the cadre branch is on origin (the guard hook allows a cadre/* ff push)
        r = self.cadre('push', approval='yes')
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn(branch, self.remote_branches())
        self.assertIn('delivery_push', self.store.read(rid))
        # merge: the main repo is on main and clean; first no, then yes; the second question (main push) yes
        self.assertEqual(self.git('branch', '--show-current'), 'main')
        r = self.cadre('merge', approval='no')
        self.assertEqual(r.returncode, 1)
        self.assertFalse((self.repo / 'a.py').exists())
        r = self.cadre('merge', approval='yes', approval_push='yes')
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertTrue((self.repo / 'a.py').exists(), 'merge brought the product into main')
        log = self.git('log', '--oneline', '-3')
        self.assertIn('Merge', log)
        remote_main = subprocess.run(['git', '--git-dir', str(self.bare), 'rev-parse', 'main'], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(remote_main, self.git('rev-parse', 'main'))
        record = self.store.read(rid)
        self.assertIn('delivery_merge', record)
        self.assertIn('delivery_merge_push', record)

    def test_delivery_refuses_a_run_whose_tree_moved_after_acceptance(self):
        """B110-3: "completed" is the evidence AT ACCEPTANCE TIME. If the tree changed at delivery time (manual edit, half
        repair) the pushed product is not the reviewed product; the candidate list re-binds the current revision with approved()."""
        rid = self.submit()
        record = self.wait_terminal(rid)
        self.assertEqual(record['state'], 'completed')
        branch = subprocess.run(['git', '-C', record['worktree'], 'branch', '--show-current'],
                             capture_output=True, text=True, check=True).stdout.strip()
        (Path(record['worktree']) / 'later.py').write_text('print("not reviewed")\n')
        r = self.cadre('push', approval='yes')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('COMPLETED', r.stderr)
        self.assertNotIn(branch, self.remote_branches())
        (Path(record['worktree']) / 'later.py').unlink()          # deliverable again once back to the old state
        r = self.cadre('push', approval='yes')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(branch, self.remote_branches())

    def approvals_between(self, args, between):
        env = {k: v for k, v in self.env.items() if not k.startswith('CADRE_APPROVAL')}
        p = subprocess.Popen(['bash', str(self.root / 'bin/cadre.sh'), *args], cwd=self.repo, env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p.stdin.write(b'yes\n')
        p.stdin.flush()
        output, last = b'', time.time() + 60
        while 'Command to run'.encode() not in output and time.time() < last:
            if select.select([p.stdout], [], [], 1)[0]:
                part = os.read(p.stdout.fileno(), 4096)
                if not part:
                    break
                output += part
        self.assertIn('Command to run'.encode(), output, 'the command is shown after the first approval')
        between()
        out, err = p.communicate(b'yes\n', timeout=60)
        return p.returncode, (output + out).decode(), err.decode()

    def between_commit(self, wt):
        def make_change():
            (Path(wt) / 'not_reviewed.py').write_text('x = 1\n')
            subprocess.run(['git', '-C', wt, 'add', 'not_reviewed.py'], check=True)
            subprocess.run(['git', '-C', wt, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'between'], check=True)
        return make_change

    def test_push_pins_the_reviewed_commit_across_the_two_confirmations(self):
        rid = self.submit()
        record = self.wait_terminal(rid)
        self.assertEqual(record['state'], 'completed')
        wt = record['worktree']
        branch = subprocess.run(['git', '-C', wt, 'branch', '--show-current'], capture_output=True, text=True, check=True).stdout.strip()
        acceptance = record['final_result']['qa']['revision']['head']
        rc, out, err = self.approvals_between(['push'], self.between_commit(wt))
        self.assertEqual(rc, 1, out + err)
        self.assertIn(acceptance, out, 'the approval text shows the reviewed commit id')
        self.assertIn('product changed after the approval', err)
        self.assertNotIn(branch, self.remote_branches(), 'an unreviewed commit is not pushed')

    def test_merge_pins_the_reviewed_commit_across_the_two_confirmations(self):
        rid = self.submit()
        record = self.wait_terminal(rid)
        self.assertEqual(record['state'], 'completed')
        rc, out, err = self.approvals_between(['merge'], self.between_commit(record['worktree']))
        self.assertEqual(rc, 1, out + err)
        self.assertIn('product changed after the approval', err)
        self.assertFalse((self.repo / 'a.py').exists(), 'an unreviewed product is not merged')

    def test_merge_rechecks_the_main_repo_after_the_second_confirmation(self):
        rid = self.submit()
        self.assertEqual(self.wait_terminal(rid)['state'], 'completed')
        rc, out, err = self.approvals_between(['merge'], lambda: (self.repo / 'dirty.txt').write_text('x\n'))
        self.assertEqual(rc, 1, out + err)
        self.assertIn("uncommitted", err)
        self.assertFalse((self.repo / 'a.py').exists())

    def test_help_prints_usage_not_source_lines(self):
        """Comment lines are not used as program output (comment standard): `--help` prints its own text."""
        r = self.cadre('--help')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('cadre task', r.stdout)
        for code in ('set -uo', 'CADRE_ENGINE_ROOT=', '. "$CADRE_ENGINE_ROOT'):
            self.assertNotIn(code, r.stdout, 'Help output must not leak code')

    def test_merge_refuses_dirty_or_wrong_branch_main_repo(self):
        rid = self.submit()
        self.assertEqual(self.wait_terminal(rid)['state'], 'completed')
        (self.repo / 'dirty.txt').write_text('x\n')
        r = self.cadre('merge', approval='yes')
        self.assertEqual(r.returncode, 1)
        self.assertIn("uncommitted", r.stderr)
        (self.repo / 'dirty.txt').unlink()
        self.git('checkout', '-q', '-b', 'other')
        r = self.cadre('merge', approval='yes')
        self.assertEqual(r.returncode, 1)
        self.assertIn('must be on its recorded main branch', r.stderr)
        self.assertFalse((self.repo / 'a.py').exists())

    def pty(self, duration, keys, *args):
        return subprocess.run([sys.executable, str(Path(__file__).with_name('pty_run.py')), str(duration), keys, '--', 'bash', str(self.root / 'bin/cadre.sh'), *args],
                              cwd=self.repo, env=self.env, capture_output=True, text=True, timeout=duration + 30)

    def test_banner_numbered_selection_and_watcher_offer_decision_9(self):
        """Decision 9: two finished jobs -> the status screen warns 'waiting for approval'; `cadre push` pushes the job chosen from a numbered list
        (CADRE_CHOICE), empty selection = the latest; the watcher offers [p]/[m]/[q] when it closes on a finished job, 'm' + yes + yes -> merge + main push;
        the merged job drops off the list."""
        rid1 = self.submit()
        self.assertEqual(self.wait_terminal(rid1)['state'], 'completed')
        rid2 = self.submit()
        self.assertEqual(self.wait_terminal(rid2, timeout=200)['state'], 'completed')
        branch = lambda rid: subprocess.run(['git', '-C', self.store.read(rid)['worktree'], 'branch', '--show-current'], capture_output=True, text=True, check=True).stdout.strip()
        r = self.cadre('status')
        self.assertIn('Finished jobs awaiting approval: 2', r.stdout)
        env = dict(self.env); env['CADRE_CHOICE'] = '1'; env['CADRE_APPROVAL'] = 'yes'; env['CADRE_APPROVAL_2'] = 'yes'
        r = subprocess.run(['bash', str(self.root / 'bin/cadre.sh'), 'push'], cwd=self.repo, env=env, text=True, capture_output=True, timeout=60, input='yes\nyes\n')
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn('Finished jobs not yet delivered', r.stderr)
        self.assertIn(branch(rid1), self.remote_branches())
        self.assertNotIn(branch(rid2), self.remote_branches())
        self.assertIn('delivery_push', self.store.read(rid1))
        r = self.cadre('push', approval='yes')          # empty selection (stdin closed) -> the latest job
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn(branch(rid2), self.remote_branches())
        # watcher: finished run -> closes itself -> offer; m, yes (merge), yes (main push)
        r = self.pty(50, '9:m,12:yes\r,14:yes\r,16:yes\r,18:yes\r', 'watch')
        self.assertIn('[p]', r.stdout)
        self.assertIn('merged', r.stdout, r.stdout[-1500:])
        self.assertTrue((self.repo / 'a.py').exists())
        remote_main = subprocess.run(['git', '--git-dir', str(self.bare), 'rev-parse', 'main'], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(remote_main, self.git('rev-parse', 'main'))
        self.assertIn('delivery_merge', self.store.read(rid2))
        r = self.cadre('status')
        self.assertIn('Finished jobs awaiting approval: 1', r.stdout)

    def test_watcher_offer_q_touches_nothing(self):
        rid = self.submit()
        self.assertEqual(self.wait_terminal(rid)['state'], 'completed')
        env = dict(self.env); env['CADRE_DELIVERY_KEY'] = 'q'
        r = subprocess.run(['bash', str(self.root / 'bin/cadre.sh'), 'watch'], cwd=self.repo, env=env, text=True, capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('not delivered', r.stdout)
        self.assertEqual(self.remote_branches(), ['main'])
        self.assertNotIn('delivery_push', self.store.read(rid))

    def test_menu_lists_delivery_options(self):
        r = subprocess.run([sys.executable, str(Path(__file__).with_name('pty_run.py')), '9', '3:q', '--', 'bash', str(self.root / 'bin/cadre.sh')],
                           cwd=self.repo, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertIn('Push the product', r.stdout)
        self.assertIn('Merge into the canonical branch', r.stdout)


if __name__ == '__main__':
    unittest.main()
