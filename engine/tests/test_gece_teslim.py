"""Delivery (user decision 16.09): the system never pushes/merges on its own; the user asks from the `cadre` menu (5/6) or with `cadre push|merge`,
says 'yes' once, and the system does the work. Only the last COMPLETED run (QA+judge passed) is delivered. 'no' -> touches nothing.
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
import test_uctan_uca as e2e  # noqa: E402


class DeliveryTests(e2e.E2EFixture):
    def setUp(self):
        super().setUp()
        self.bare = Path(self.temp.name) / 'remote.git'
        subprocess.run(['git', 'init', '-q', '--bare', str(self.bare)], check=True)
        self.git('remote', 'add', 'origin', str(self.bare))
        self.git('push', '-q', 'origin', 'main')

    def cadre(self, *args, onay=None, onay_push=None):
        env = dict(self.env)
        if onay is not None:
            env['CADRE_ONAY'] = env['CADRE_ONAY_2'] = onay
        if onay_push is not None:
            env['CADRE_ONAY_PUSH'] = env['CADRE_ONAY_PUSH_2'] = onay_push
        answers = [onay] * 2 if onay is not None else []
        if onay_push is not None:
            answers += [onay_push] * 2
        if len(args) == 1 and len([r for r in self.store.list() if r['state'] == 'completed']) > 1:
            answers.insert(0, '')
        return subprocess.run(['bash', str(self.root / 'bin/cadre.sh'), *args], cwd=self.repo, env=env, text=True,
                              capture_output=True, timeout=60, input='\n'.join(answers) + ('\n' if answers else ''))

    def remote_branches(self):
        return subprocess.run(['git', '--git-dir', str(self.bare), 'branch', '--format=%(refname:short)'], capture_output=True, text=True, check=True).stdout.split()

    def test_push_and_merge_only_after_completion_only_with_consent(self):
        # without a completed run: denial
        r = self.cadre('push', onay='evet')
        self.assertEqual(r.returncode, 1)
        self.assertIn('TAMAMLANMIŞ', r.stderr)
        rid = self.submit()
        record = self.wait_terminal(rid)
        self.assertEqual(record['state'], 'completed')
        dal = subprocess.run(['git', '-C', record['worktree'], 'branch', '--show-current'], capture_output=True, text=True, check=True).stdout.strip()
        self.assertTrue(dal.startswith('cadre/'))
        # no -> touches nothing
        r = self.cadre('push', onay='hayir')
        self.assertEqual(r.returncode, 1)
        self.assertNotIn(dal, self.remote_branches())
        env = dict(self.env, CADRE_ONAY='evet')
        r = subprocess.run(['bash', str(self.root / 'bin/cadre.sh'), 'push'], cwd=self.repo, env=env, text=True, capture_output=True,
                           timeout=60, input='evet\n')
        self.assertEqual(r.returncode, 1, 'tek onay push etmez; ikinci onay ayri sorulur')
        self.assertIn('Çalışacak komut', r.stdout + r.stderr)
        self.assertNotIn(dal, self.remote_branches())
        # yes -> the cadre branch is on origin (the guard hook allows a cadre/* ff push)
        r = self.cadre('push', onay='evet')
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn(dal, self.remote_branches())
        self.assertIn('teslim_push', self.store.read(rid))
        # merge: the main repo is on main and clean; first no, then yes; the second question (main push) yes
        self.assertEqual(self.git('branch', '--show-current'), 'main')
        r = self.cadre('merge', onay='hayir')
        self.assertEqual(r.returncode, 1)
        self.assertFalse((self.repo / 'a.py').exists())
        r = self.cadre('merge', onay='evet', onay_push='evet')
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertTrue((self.repo / 'a.py').exists(), 'merge brought the product into main')
        log = self.git('log', '--oneline', '-3')
        self.assertIn('Merge', log)
        remote_main = subprocess.run(['git', '--git-dir', str(self.bare), 'rev-parse', 'main'], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(remote_main, self.git('rev-parse', 'main'))
        record = self.store.read(rid)
        self.assertIn('teslim_merge', record)
        self.assertIn('teslim_merge_push', record)

    def test_delivery_refuses_a_run_whose_tree_moved_after_acceptance(self):
        """B110-3: "completed" is the evidence AT ACCEPTANCE TIME. If the tree changed at delivery time (manual edit, half
        repair) the pushed product is not the reviewed product; the candidate list re-binds the current revision with approved()."""
        rid = self.submit()
        record = self.wait_terminal(rid)
        self.assertEqual(record['state'], 'completed')
        dal = subprocess.run(['git', '-C', record['worktree'], 'branch', '--show-current'],
                             capture_output=True, text=True, check=True).stdout.strip()
        (Path(record['worktree']) / 'sonradan.py').write_text('print("incelenmedi")\n')
        r = self.cadre('push', onay='evet')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('TAMAMLANMIŞ', r.stderr)
        self.assertNotIn(dal, self.remote_branches())
        (Path(record['worktree']) / 'sonradan.py').unlink()          # deliverable again once back to the old state
        r = self.cadre('push', onay='evet')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(dal, self.remote_branches())

    def onaylar_arasinda(self, args, arada):
        env = {k: v for k, v in self.env.items() if not k.startswith('CADRE_ONAY')}
        p = subprocess.Popen(['bash', str(self.root / 'bin/cadre.sh'), *args], cwd=self.repo, env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p.stdin.write(b'evet\n')
        p.stdin.flush()
        cikti, son = b'', time.time() + 60
        while 'Çalışacak komut'.encode() not in cikti and time.time() < son:
            if select.select([p.stdout], [], [], 1)[0]:
                parca = os.read(p.stdout.fileno(), 4096)
                if not parca:
                    break
                cikti += parca
        self.assertIn('Çalışacak komut'.encode(), cikti, 'ilk onaydan sonra komut gösterilir')
        arada()
        out, err = p.communicate(b'evet\n', timeout=60)
        return p.returncode, (cikti + out).decode(), err.decode()

    def arada_commit(self, wt):
        def yap():
            (Path(wt) / 'incelenmedi.py').write_text('x = 1\n')
            subprocess.run(['git', '-C', wt, 'add', 'incelenmedi.py'], check=True)
            subprocess.run(['git', '-C', wt, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'arada'], check=True)
        return yap

    def test_push_pins_the_reviewed_commit_across_the_two_confirmations(self):
        rid = self.submit()
        record = self.wait_terminal(rid)
        self.assertEqual(record['state'], 'completed')
        wt = record['worktree']
        dal = subprocess.run(['git', '-C', wt, 'branch', '--show-current'], capture_output=True, text=True, check=True).stdout.strip()
        kabul = record['final_result']['qa']['revision']['head']
        rc, out, err = self.onaylar_arasinda(['push'], self.arada_commit(wt))
        self.assertEqual(rc, 1, out + err)
        self.assertIn(kabul, out, 'onay metni incelenen commit kimliğini gösterir')
        self.assertIn('onaydan sonra ürün değişti', err)
        self.assertNotIn(dal, self.remote_branches(), 'incelenmemiş commit push edilmez')

    def test_merge_pins_the_reviewed_commit_across_the_two_confirmations(self):
        rid = self.submit()
        record = self.wait_terminal(rid)
        self.assertEqual(record['state'], 'completed')
        rc, out, err = self.onaylar_arasinda(['merge'], self.arada_commit(record['worktree']))
        self.assertEqual(rc, 1, out + err)
        self.assertIn('onaydan sonra ürün değişti', err)
        self.assertFalse((self.repo / 'a.py').exists(), 'incelenmemiş ürün merge edilmez')

    def test_merge_rechecks_the_main_repo_after_the_second_confirmation(self):
        rid = self.submit()
        self.assertEqual(self.wait_terminal(rid)['state'], 'completed')
        rc, out, err = self.onaylar_arasinda(['merge'], lambda: (self.repo / 'kirli.txt').write_text('x\n'))
        self.assertEqual(rc, 1, out + err)
        self.assertIn("commit'lenmemiş", err)
        self.assertFalse((self.repo / 'a.py').exists())

    def test_help_prints_usage_not_source_lines(self):
        """Comment lines are not used as program output (comment standard): `--help` prints its own text."""
        r = self.cadre('--help')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('cadre gorev', r.stdout)
        for kod in ('set -uo', 'CADRE_ENGINE_ROOT=', '. "$CADRE_ENGINE_ROOT'):
            self.assertNotIn(kod, r.stdout, 'yardim ciktisina kod sizmamali')

    def test_merge_refuses_dirty_or_wrong_branch_main_repo(self):
        rid = self.submit()
        self.assertEqual(self.wait_terminal(rid)['state'], 'completed')
        (self.repo / 'kirli.txt').write_text('x\n')
        r = self.cadre('merge', onay='evet')
        self.assertEqual(r.returncode, 1)
        self.assertIn("commit'lenmemiş", r.stderr)
        (self.repo / 'kirli.txt').unlink()
        self.git('checkout', '-q', '-b', 'baska')
        r = self.cadre('merge', onay='evet')
        self.assertEqual(r.returncode, 1)
        self.assertIn('dalında olmalı', r.stderr)
        self.assertFalse((self.repo / 'a.py').exists())

    def pty(self, sure, keys, *args):
        return subprocess.run([sys.executable, str(Path(__file__).with_name('pty_kos.py')), str(sure), keys, '--', 'bash', str(self.root / 'bin/cadre.sh'), *args],
                              cwd=self.repo, env=self.env, capture_output=True, text=True, timeout=sure + 30)

    def test_banner_numbered_selection_and_watcher_offer_karar_9(self):
        """Decision 9: two finished jobs -> the status screen warns 'waiting for approval'; `cadre push` pushes the job chosen from a numbered list
        (CADRE_SECIM), empty selection = the latest; the watcher offers [p]/[m]/[q] when it closes on a finished job, 'm' + yes + yes -> merge + main push;
        the merged job drops off the list."""
        rid1 = self.submit()
        self.assertEqual(self.wait_terminal(rid1)['state'], 'completed')
        rid2 = self.submit()
        self.assertEqual(self.wait_terminal(rid2, timeout=200)['state'], 'completed')
        dal = lambda rid: subprocess.run(['git', '-C', self.store.read(rid)['worktree'], 'branch', '--show-current'], capture_output=True, text=True, check=True).stdout.strip()
        r = self.cadre('durum')
        self.assertIn('2 bitmiş iş onay bekliyor', r.stdout)
        env = dict(self.env); env['CADRE_SECIM'] = '1'; env['CADRE_ONAY'] = 'evet'; env['CADRE_ONAY_2'] = 'evet'
        r = subprocess.run(['bash', str(self.root / 'bin/cadre.sh'), 'push'], cwd=self.repo, env=env, text=True, capture_output=True, timeout=60, input='evet\nevet\n')
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn('Bitmiş, teslim edilmemiş işler', r.stderr)
        self.assertIn(dal(rid1), self.remote_branches())
        self.assertNotIn(dal(rid2), self.remote_branches())
        self.assertIn('teslim_push', self.store.read(rid1))
        r = self.cadre('push', onay='evet')          # empty selection (stdin closed) -> the latest job
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn(dal(rid2), self.remote_branches())
        # watcher: finished run -> closes itself -> offer; m, yes (merge), yes (main push)
        r = self.pty(50, '9:m,12:evet\r,14:evet\r,16:evet\r,18:evet\r', 'izle')
        self.assertIn('[p]', r.stdout)
        self.assertIn('merge edildi', r.stdout, r.stdout[-1500:])
        self.assertTrue((self.repo / 'a.py').exists())
        remote_main = subprocess.run(['git', '--git-dir', str(self.bare), 'rev-parse', 'main'], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(remote_main, self.git('rev-parse', 'main'))
        self.assertIn('teslim_merge', self.store.read(rid2))
        r = self.cadre('durum')
        self.assertIn('1 bitmiş iş onay bekliyor', r.stdout)

    def test_watcher_offer_q_touches_nothing(self):
        rid = self.submit()
        self.assertEqual(self.wait_terminal(rid)['state'], 'completed')
        env = dict(self.env); env['CADRE_TESLIM_TUS'] = 'q'
        r = subprocess.run(['bash', str(self.root / 'bin/cadre.sh'), 'izle'], cwd=self.repo, env=env, text=True, capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('teslim yapılmadı', r.stdout)
        self.assertEqual(self.remote_branches(), ['main'])
        self.assertNotIn('teslim_push', self.store.read(rid))

    def test_menu_lists_delivery_options(self):
        r = subprocess.run([sys.executable, str(Path(__file__).with_name('pty_kos.py')), '9', '3:q', '--', 'bash', str(self.root / 'bin/cadre.sh')],
                           cwd=self.repo, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertIn('Ürünü push et', r.stdout)
        self.assertIn('Kanonik dala merge et', r.stdout)


if __name__ == '__main__':
    unittest.main()
