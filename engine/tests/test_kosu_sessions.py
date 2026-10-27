"""C5/C6: executor session identity must survive interruption, never use latest."""

from __future__ import annotations

import atexit
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from unittest.mock import Mock

LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
# The live router ledger must not get dirty in single-module runs either (D32); the collection runner already sets it.
_isolated = tempfile.mkdtemp(prefix="qe-yonlendirici-")
atexit.register(shutil.rmtree, _isolated, ignore_errors=True)
os.environ.setdefault("CADRE_YON_KAYIT", str(Path(_isolated) / "yonlendirici.jsonl"))
os.environ.setdefault("CADRE_YON_DURUM", str(Path(_isolated) / "saglayici-durum.json"))
os.environ.setdefault("CADRE_BEKLEME_ESIK_SN", "0")   # B268: dispatcher fixtures must not wait for midnight on the real clock
import qe_core as core
import kosu_defteri as ledger
import izle
from kosu import RunStore
from oturum import SessionRecoveryError


# B220 (29.09): the catalogue and the account were read from the personal HOME (~/.cache/opencode/models.json); in an empty HOME the helper/compaction
# selection crashed. The fixture catalogue is written to the isolated home and CADRE_MODEL_KATALOG points there.
KATALOG = {   # the 29.09 subset of the real catalogue (limit+cost); CADRE_MODEL_KATALOG points to this file
    'opencode': {'models': {
        'deepseek-v4-flash': {'limit': {'context': 1000000, 'output': 384000}, 'cost': {'input': 0.14, 'output': 0.28, 'cache_read': 0.028}},
        'minimax-m3': {'limit': {'context': 512000, 'output': 128000}, 'cost': {'input': 0.3, 'output': 1.2, 'cache_read': 0.06}},
        'big-pickle': {'limit': {'context': 200000, 'input': 160000, 'output': 32000}, 'cost': {'input': 0, 'output': 0, 'cache_read': 0, 'cache_write': 0}},
        'nemotron-3-ultra-free': {'limit': {'context': 1000000, 'output': 128000}, 'cost': {'input': 0, 'output': 0, 'cache_read': 0}},
        'kimi-k3': {'limit': {'context': 262144, 'output': 262144}, 'cost': {'input': 0.95, 'output': 4, 'cache_read': 0.19}},
        'glm-5.3': {'limit': {'context': 1000000, 'output': 131072}, 'cost': {'input': 0.3, 'output': 1.2, 'cache_read': 0.06}},
    }},
    'deepseek': {'models': {
        'deepseek-v4-pro': {'limit': {'context': 1000000, 'output': 393216}, 'cost': {'input': 0.435, 'output': 0.87, 'reasoning': 0.87, 'cache_read': 0.003625}},
        'deepseek-v4-flash': {'limit': {'context': 1000000, 'output': 393216}, 'cost': {'input': 0.15, 'output': 0.6, 'reasoning': 0.6, 'cache_read': 0.003}},
    }},
    'opencode-go': {'models': {
        'glm-5.3-flash': {'limit': {'context': 1000000, 'output': 131072}, 'cost': {'input': 0.15, 'output': 0.5, 'cache_read': 0.03}},
        'qwen3.8-flash': {'limit': {'context': 1000000, 'output': 131072}, 'cost': {'input': 0.15, 'output': 0.47, 'cache_read': 0.016, 'cache_write': 0.2}},
        'qwen3.7-plus': {'limit': {'context': 1000000, 'output': 65536}, 'cost': {'input': 0.4, 'output': 1.6, 'cache_read': 0.04, 'cache_write': 0.5, 'tiers': [{'input': 1.2, 'output': 4.8, 'cache_read': 0.12, 'cache_write': 1.5, 'tier': {'type': 'context', 'size': 256000}}], 'context_over_200k': {'input': 1.2, 'output': 4.8, 'cache_read': 0.12, 'cache_write': 1.5}}},
        'kimi-k2.7-code': {'limit': {'context': 262144, 'output': 262144}, 'cost': {'input': 0.95, 'output': 4, 'cache_read': 0.19}},
        # D13 (03.10) new model names: the catalogue matches the new ROLLER.
        'kimi-k3': {'limit': {'context': 262144, 'output': 262144}, 'cost': {'input': 0.95, 'output': 4, 'cache_read': 0.19}},
        'glm-5.3': {'limit': {'context': 1000000, 'output': 131072}, 'cost': {'input': 0.3, 'output': 1.2, 'cache_read': 0.06}},
        'qwen3.8-max': {'limit': {'context': 262144, 'output': 65536}, 'cost': {'input': 0.4, 'output': 1.6, 'cache_read': 0.04}},
        'deepseek-v4-pro': {'limit': {'context': 262144, 'output': 131072}, 'cost': {'input': 0.435, 'output': 0.87, 'cache_read': 0.003625}},
        'deepseek-v4-flash': {'limit': {'context': 262144, 'output': 131072}, 'cost': {'input': 0.15, 'output': 0.6, 'cache_read': 0.003}},
    }},
    'openrouter': {'models': {
        'deepseek/deepseek-v4-pro': {'limit': {'context': 262144, 'output': 131072}, 'cost': {'input': 0.435, 'output': 0.87, 'cache_read': 0.003625}},
        'openai/gpt-6.1-sol': {'limit': {'context': 1050000, 'output': 128000}, 'cost': {'input': 2, 'output': 10, 'cache_read': 0.2, 'cache_write': 2.5}},
    }},
    'google': {'models': {
        'gemini-3-flash-preview': {'limit': {'context': 1048576, 'output': 65536}, 'cost': {'input': 0.5, 'output': 3, 'cache_read': 0.05, 'input_audio': 1}},
    }},
    'cerebras': {'models': {
        'qwen-3.8-27b': {'limit': {'context': 131072, 'output': 40960}, 'cost': {'input': 0.99, 'output': 1.49}},
    }},
    'openai': {'models': {
        'gpt-6.1-sol': {'limit': {'context': 1050000, 'input': 922000, 'output': 128000}, 'cost': {'input': 2, 'output': 10, 'cache_read': 0.2, 'cache_write': 2.5, 'tiers': [{'input': 4, 'output': 15, 'cache_read': 0.4, 'cache_write': 5, 'tier': {'type': 'context', 'size': 272000}}], 'context_over_200k': {'input': 4, 'output': 15, 'cache_read': 0.4, 'cache_write': 5}}},
    }},
}


def motor_source(home: Path, wt: Path) -> None:
    subprocess.run(['git', 'init', '-q', str(wt)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(wt), '-c', 'user.name=fixture', '-c', 'user.email=fixture@local',
                    'commit', '--allow-empty', '-qm', 'motor baseline'], check=True, capture_output=True)
    project = home / 'projects/a'
    project.mkdir(parents=True, exist_ok=True)
    (project / 'qa-komut').write_text('git rev-parse --verify HEAD\n')


class InstructionTests(unittest.TestCase):
    def setUp(self) -> None:
        import oturum
        self.temp = tempfile.TemporaryDirectory(prefix='cadre-instructions-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.engine = self.base / 'engine'
        (self.engine / 'lib').mkdir(parents=True)
        shutil.copyfile(LIB / 'cadre-plugin.mjs', self.engine / 'lib/cadre-plugin.mjs')
        from qa_fixture import instructions
        instructions(self.engine)
        self.source = self.base / 'config'
        self.store = RunStore(self.base / 'home')
        run = self.store.accept('a', 'Inspect the existing source')
        self.env = {**self.store.environment(run['run_id']), 'CADRE_ROL': 'plan',
                    'OPENCODE_CONFIG_CONTENT': json.dumps({'agent': {name: {
                        'prompt': 'ENGINE_ONLY', 'permission': {'bash': 'deny'}, 'model': 'fixture/model'}
                        for name in ('build', 'cadre-plan', 'cadre-impl', 'cadre-review')}})}
        patcher = patch.object(oturum, '__file__', str(self.engine / 'lib/oturum.py'))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_loader_freezes_the_exact_bodies_without_changing_role_authority(self) -> None:
        import oturum
        directory = self.base / 'attempt'
        directory.mkdir()
        receipt = {}
        oturum.loader_environment(directory, self.env, receipt)
        self.addCleanup(oturum.retire_loader, directory, receipt)
        config = json.loads(self.env['OPENCODE_CONFIG_CONTENT'])
        for name, role in config['agent'].items():
            body = (self.source / f'agents/{name}.md').read_text().split('---', 2)[2].strip()
            self.assertEqual(role['prompt'], body + '\nENGINE_ONLY')
            self.assertEqual(role['permission'], {'bash': 'deny'})
            self.assertEqual(role['model'], 'fixture/model')
        self.assertEqual(set(config['agent']), {'build', 'cadre-plan', 'cadre-impl', 'cadre-review'})
        self.assertEqual(set(receipt['instructions']['files']), set(oturum.INSTRUCTION_PATHS))
        for name, row in receipt['instructions']['files'].items():
            frozen = Path(row['frozen'])
            self.assertEqual(frozen.read_bytes(), (self.source / name).read_bytes())
            self.assertEqual(row['sha256'], hashlib.sha256(frozen.read_bytes()).hexdigest())
            self.assertEqual(frozen.stat().st_mode & 0o222, 0)
            self.assertEqual(frozen.parent.stat().st_mode & 0o222, 0)
        frozen = Path(receipt['instructions']['files']['AGENTS.md']['frozen'])
        before = frozen.read_bytes()
        (self.source / 'AGENTS.md').write_text('Later source revision')
        self.assertEqual(frozen.read_bytes(), before)
        self.assertIn(str(frozen), config['instructions'])
        self.assertTrue((Path(receipt['config']) / 'command/yardim.md').is_file())
        self.assertFalse((Path(receipt['config']) / 'agents').exists())
        research = self.store.accept('a', 'TUR: rapor\nRAPOR: analiz.md\nInspect the existing source.')
        directory = self.base / 'research-attempt'
        directory.mkdir()
        env = {**self.env, **self.store.environment(research['run_id']), 'CADRE_ROL': 'qa'}
        receipt = {}
        oturum.loader_environment(directory, env, receipt)
        self.addCleanup(oturum.retire_loader, directory, receipt)
        config = json.loads(env['OPENCODE_CONFIG_CONTENT'])
        for name in ('skills/self-check/SKILL.md', 'skills/rapor-kaniti/SKILL.md'):
            self.assertIn(receipt['instructions']['files'][name]['frozen'], config['instructions'])

    def test_missing_pinned_linked_and_concurrently_changed_instructions_fail_before_loading(self) -> None:
        import oturum
        source = self.source / 'agents/build.md'
        before = source.read_bytes()
        for invalid in ('missing', 'model', 'symlink', 'hardlink'):
            with self.subTest(invalid=invalid):
                source.unlink()
                other = self.base / 'other.md'
                other.write_bytes(before)
                if invalid == 'model':
                    source.write_bytes(before.replace(b'---\n', b'---\nmodel: fixture/pinned\n', 1))
                elif invalid == 'symlink':
                    source.symlink_to(other)
                elif invalid == 'hardlink':
                    os.link(other, source)
                directory = self.base / ('attempt-' + invalid)
                directory.mkdir()
                with self.assertRaisesRegex(PermissionError, 'project-tool-instructions-'):
                    oturum.loader_environment(directory, dict(self.env), {})
                self.assertFalse((directory / 'loader').exists())
                if source.exists() or source.is_symlink():
                    source.unlink()
                source.write_bytes(before)
        import runtime_sync
        regular = runtime_sync.regular
        reads = {}
        def changed(path):
            reads[path] = reads.get(path, 0) + 1
            value = regular(path)
            return value + b'changed' if path == source and reads[path] > 1 else value
        with patch.object(runtime_sync, 'regular', side_effect=changed), self.assertRaisesRegex(PermissionError, 'instructions-changed'):
            oturum.instruction_sources(self.env)

    def test_installed_bundle_requires_current_ownership_and_generation(self) -> None:
        import oturum
        frontend = self.base / 'frontend'
        self.source.rename(frontend)
        env = {**self.env, 'OPENCODE_CFG': str(frontend)}
        with self.assertRaisesRegex(PermissionError, 'instructions-unowned'):
            oturum.instruction_sources(env)
        (self.engine / 'zincir').mkdir()
        manifest = {'version': 'fixture-version', 'files': {
            str(frontend / name): {'sha256': hashlib.sha256((frontend / name).read_bytes()).hexdigest()}
            for name in oturum.INSTRUCTION_PATHS}}
        (self.engine / 'zincir/runtime-manifest.json').write_text(json.dumps(manifest))
        (self.engine / '.runtime-generation').write_text('fixture-version')
        self.assertEqual(oturum.instruction_sources(env)[0], frontend)
        (self.engine / '.runtime-generation').write_text('other-version')
        with self.assertRaisesRegex(PermissionError, 'instructions-unowned'):
            oturum.instruction_sources(env)
        (self.engine / '.runtime-generation').write_text('fixture-version')
        (frontend / 'AGENTS.md').write_text('Unowned user revision')
        with self.assertRaisesRegex(PermissionError, 'instructions-unowned'):
            oturum.instruction_sources(env)


class SessionTests(unittest.TestCase):
    def setUp(self, budget_cap_usd=None) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="qe-sessions-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.store = RunStore(self.home)
        (self.home/'zincir').mkdir()
        (self.home/'zincir/request-budget.json').write_text(json.dumps({'observed':time.time(), 'budget':None}))
        from contextlib import nullcontext
        with patch.dict(os.environ, {'CADRE_KOSU_TAVAN_USD': str(budget_cap_usd)}) if budget_cap_usd is not None else nullcontext():
            self.run = self.store.accept("a", "original prompt")
        self.rid = self.run["run_id"]
        self.wt = self.home / "worktree"
        self.wt.mkdir()
        self.store.update(self.rid, worktree=str(self.wt))
        (self.home / 'models.json').write_text(json.dumps(KATALOG))
        env = {**self.store.environment(self.rid), "CADRE_GOREV_ID": "g1", "CADRE_OWNER_FDS": "",
               "OPENCODE_DB": str(self.home / "opencode.db"), "CADRE_MODEL_KATALOG": str(self.home / 'models.json'),
               # The session fixture is also used by other modules; the fixture carries the pool.
               "CADRE_ZINCIR_KALITE": "opencode-go/qwen3.8-max",
               }
        self.enter(patch.dict(os.environ, env))
        self.enter(patch.object(core, "QE", self.home))
        self.enter(patch.object(core, 'butce_json', side_effect=lambda: {
            'ts':time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(time.time())),
            'bugun':time.strftime('%Y-%m-%d', time.localtime(time.time())),
            'claude':{'kaynak':'taze','pay':50,'yuzde1_usd':10,'havuz_kalan_usd':200},
            # P3 (03.10): no local — the executor runs in the cloud with a fake Go budget
            'go':{'db':'ok','pay':6.666667,'pencere_acik':True,'kullanilabilir':6.666667, 'birim': 'puan'}}))
        # Session-identity tests fake the transport, not the budget policy.
        self.enter(patch.object(core, 'claude_command', side_effect=lambda command: command))
        if core.YON:
            self.enter(patch.object(core.YON, 'QE', self.home))
        self.enter(patch.object(ledger, "QE", self.home))
        self.enter(patch.object(ledger, 'WATERMARK', self.home/'reports/.kosu-defteri-watermark'))
        self.enter(patch.object(izle, "QE", self.home))
        self.log = self.home / "motor.log"
        self.fake = self.home / "fake-opencode"
        self.fake.write_text(f"#!{sys.executable}\n" + """
import json, os, pathlib, sys
p = pathlib.Path(os.environ['CADRE_HOME'])
args = sys.argv[1:]
with (p/'calls.jsonl').open('a') as f: f.write(json.dumps(args)+'\\n')
sid = args[args.index('--session')+1] if '--session' in args else 'ses_first'
print(json.dumps({'type':'step_start', 'sessionID':sid, 'part':{'type':'step-start'}}), flush=True)
print(json.dumps({'type':'text', 'sessionID':sid, 'part':{'type':'text','text':'answer'}}), flush=True)
""")
        self.fake.chmod(0o700)

    def test_fixture_budget_observation_follows_the_engine_clock(self):
        from butce_kapisi import allowance
        with patch('time.time', return_value=time.time() + 120):
            period, remaining = allowance(core.butce_json(), 'opencode-go', model='opencode-go/glm-5.3')
        self.assertGreater(remaining, 0)

    def enter(self, context):
        value = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return value

    def calls(self):
        return [json.loads(line) for line in (self.home / "calls.jsonl").read_text().splitlines()]

    def test_active_sealed_loader_can_be_archived_repeatedly_without_unsealing_source(self) -> None:
        release = self.home / 'release-loader'
        self.fake.write_text(self.fake.read_text() + f"\nimport time\nwhile not pathlib.Path({str(release)!r}).exists(): time.sleep(.01)\n")
        process = core.run_opencode(str(self.fake), str(self.wt), ['original prompt'], self.log)
        try:
            deadline = time.monotonic() + 5
            while not process.output().strip() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(process.output().strip())
            self.assertIsNone(process.poll())
            loader = process.attempt.directory / 'loader'
            source = loader / 'config/opencode/opencode.json'
            before = source.read_bytes()
            with patch.object(ledger, 'DB', self.home / 'absent.db'), patch.object(ledger, 'config_snapshot'), \
                 patch.object(ledger, 'izleyici_dilimleri', return_value={}):
                out = ledger.dump('a', str(self.wt), run_id=self.rid)
                target = out / 'attempts/g1' / process.attempt.directory.name / 'loader/config/opencode/opencode.json'
                target.chmod(0o400)
                target.parent.chmod(0o500)
                copy = ledger.shutil.copyfile
                def copy_error(source_file, destination_file):
                    if Path(source_file) == source:
                        raise OSError('fixture archive write failure')
                    return copy(source_file, destination_file)
                with patch.object(ledger.shutil, 'copyfile', side_effect=copy_error):
                    with self.assertRaises(shutil.Error):
                        ledger.dump('a', str(self.wt), run_id=self.rid)
                self.assertEqual(list(target.parent.glob('.opencode.json.*')), [])
                for _ in range(2):
                    ledger.dump('a', str(self.wt), run_id=self.rid)
                    self.assertEqual(target.read_bytes(), before)
                    self.assertEqual(source.stat().st_mode & 0o777, 0o400)
                    self.assertEqual(source.parent.stat().st_mode & 0o777, 0o500)
                    self.assertEqual(loader.stat().st_mode & 0o777, 0o500)
                    self.assertTrue(os.access(target, os.W_OK))
                    self.assertIsNone(process.poll())
            release.touch()
            self.assertEqual(process.wait(timeout=5), 0)
            self.assertEqual(source.read_bytes(), before)
        finally:
            release.touch()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

    def test_ledger_rejects_symlinked_attempt_destinations_without_writing_external_files(self) -> None:
        process = core.run_opencode(str(self.fake), str(self.wt), ['original prompt'], self.log)
        self.assertEqual(process.wait(timeout=5), 0)
        external = self.home / 'external-archive'
        external.mkdir()
        external_mode = external.stat().st_mode & 0o777
        marker = external / 'preserved'
        marker.write_bytes(b'unchanged')
        with patch.object(ledger, 'DB', self.home / 'absent.db'), patch.object(ledger, 'config_snapshot'), \
             patch.object(ledger, 'izleyici_dilimleri', return_value={}):
            out = ledger.dump('a', str(self.wt), run_id=self.rid)
            destination = out / 'attempts/g1' / process.attempt.directory.name
            for relative in ('', 'loader/config'):
                target = destination / relative
                saved = target.with_name(target.name + '.saved')
                target.rename(saved)
                target.symlink_to(external, target_is_directory=True)
                try:
                    with self.assertRaisesRegex(PermissionError, 'ledger-attempt-destination-untrusted'):
                        ledger.dump('a', str(self.wt), run_id=self.rid)
                    self.assertEqual(marker.read_bytes(), b'unchanged')
                    self.assertEqual(list(external.iterdir()), [marker])
                    self.assertEqual(external.stat().st_mode & 0o777, external_mode)
                finally:
                    target.unlink()
                    saved.rename(target)

    def test_restart_uses_exact_session_even_without_failure_increment(self) -> None:
        first = core.run_opencode(str(self.fake), str(self.wt), ["original prompt"], self.log)
        self.assertEqual(first.wait(timeout=5), 0)
        # A new adapter invocation has no in-memory retry counter. Disk identity wins.
        second = core.run_opencode(str(self.fake), str(self.wt), ["original prompt"], self.log)
        self.assertEqual(second.wait(timeout=5), 0)
        args = self.calls()[1]
        self.assertIn("--session", args)
        self.assertEqual(args[args.index("--session") + 1], "ses_first")
        self.assertNotIn("-c", args)
        self.assertNotIn("original prompt", args)
        self.assertEqual((second.attempt.directory / 'prompt.md').read_text(), args[-1])

    def test_legacy_continue_is_converted_to_explicit_session(self) -> None:
        core.run_opencode(str(self.fake), str(self.wt), ["original prompt"], self.log).wait(timeout=5)
        core.run_opencode(str(self.fake), str(self.wt), ["-c", "continue remaining"], self.log).wait(timeout=5)
        self.assertNotIn("-c", self.calls()[1])
        self.assertIn("--session", self.calls()[1])

    def test_ledger_and_monitor_exclude_other_run_in_same_directory(self) -> None:
        project = self.home / "projects/a"
        project.mkdir(parents=True)
        (project / "repo-path").write_text(str(self.wt))
        core.run_opencode(str(self.fake), str(self.wt), ["task"], self.log).wait(timeout=5)
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.row_factory = sqlite3.Row
        con.execute("create table session(id text primary key, directory text, parent_id text, time_created integer)")
        con.executemany("insert into session values(?,?,?,?)", [
            ("ses_first", str(self.wt), None, 1), ("ses_child", str(self.wt), "ses_first", 2),
            ("ses_other_run", str(self.wt), None, 3), ("ses_other_child", str(self.wt), "ses_other_run", 4),
        ])
        self.assertEqual({row['id'] for row in ledger.sessions_for_dir(con, str(self.wt), 'a')}, {"ses_first", "ses_child"})
        viewer = izle.Izleyici("a", str(self.wt), duz=True, run_id=self.rid)
        self.assertEqual({row['id'] for row in viewer.oturumlar(con)}, {"ses_first", "ses_child"})

    def test_claude_large_output_cannot_block_in_unread_pipe(self) -> None:
        fake = self.home / "fake-claude"
        fake.write_text(f"#!{sys.executable}\n" + """
import json, sys
sid = sys.argv[sys.argv.index('--session-id')+1]
print(json.dumps({'type':'result','session_id':sid,'result':'x'*2000000,'total_cost_usd':.1}))
""")
        fake.chmod(0o700)
        self.allow_fixture_claude(fake)
        with patch.dict(os.environ, {"CLAUDE_BIN": str(fake)}):
            proc = core.run_claude(str(self.wt), "claude/sonnet#high", "task", self.log)
            try:
                self.assertEqual(proc.wait(timeout=2), 0)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate(timeout=5)

    def allow_fixture_claude(self, fake: Path) -> None:
        import oturum
        fake = fake.resolve(strict=True)
        self.assertTrue(fake.is_relative_to(self.home.resolve()))
        original = oturum.project_tool_policy
        def fixture_policy(store, record, directory, wt, adapter, role, env, **kwargs):
            if adapter == 'claude' and Path(env.get('CLAUDE_BIN', '')).resolve() == fake:
                return None
            return original(store, record, directory, wt, adapter, role, env, **kwargs)
        self.enter(patch.object(oturum, 'project_tool_policy', side_effect=fixture_policy))

    def claude_fixture(self) -> Path:
        fake = self.home / "fake-claude-events"
        fake.write_text(f"#!{sys.executable}\n" + """
import json,sys,pathlib,os
a=sys.argv[1:]
sid=a[a.index('--resume')+1] if '--resume' in a else a[a.index('--session-id')+1]
with (pathlib.Path(os.environ['CADRE_HOME'])/'claude-calls.jsonl').open('a') as f: f.write(json.dumps(a)+'\\n')
for event in [
    {'type':'system','subtype':'init','session_id':sid},
    {'type':'assistant','session_id':sid,'message':{'content':[{'type':'tool_use','name':'Write','input':{'file_path':'product.py'}}]}},
    {'type':'result','session_id':sid,'result':'completed work','total_cost_usd':.42},
]: print(json.dumps(event),flush=True)
""")
        fake.chmod(0o700)
        self.allow_fixture_claude(fake)
        return fake

    def test_claude_resume_and_cross_adapter_handoff(self) -> None:
        self.enter(patch.dict(os.environ, {"CLAUDE_BIN": str(self.claude_fixture())}))
        first = core.run_claude(str(self.wt), "claude/sonnet#high", "original prompt", self.log)
        first.wait(timeout=5)
        second = core.run_claude(str(self.wt), "claude/opus#high", "original prompt", self.log)
        second.wait(timeout=5)
        calls = [json.loads(line) for line in (self.home/'claude-calls.jsonl').read_text().splitlines()]
        sid = calls[0][calls[0].index('--session-id')+1]
        self.assertEqual(calls[1][calls[1].index('--resume')+1], sid)
        self.assertNotIn('original prompt', calls[1])
        core.run_opencode(str(self.fake), str(self.wt), ['original prompt'], self.log).wait(timeout=5)
        self.assertIn('DEVIR:', self.calls()[0][-1])
        self.assertIn('original prompt', self.calls()[0][-1])
        self.assertEqual(len(list(self.store.task_dir(self.rid,'g1').glob('attempts/*/handoff.json'))), 1)
        third = core.run_claude(str(self.wt), "claude/sonnet#high", "original prompt", self.log)
        third.wait(timeout=5)
        returned = json.loads((self.home/'claude-calls.jsonl').read_text().splitlines()[-1])
        self.assertEqual(returned[returned.index('--resume')+1], sid)
        self.assertIn('DEVIR:', returned[-1])
        self.assertIn('original prompt', returned[-1])
        handoff = json.loads((third.attempt.directory/'handoff.json').read_text())
        self.assertEqual(handoff['last_executor'], 'opencode:impl')
        self.assertEqual((third.attempt.directory/'prompt.md').read_text(), returned[-1])

    def test_option_shaped_prompt_cannot_select_another_session(self) -> None:
        self.enter(patch.dict(os.environ, {'CLAUDE_BIN':str(self.claude_fixture())}))
        for prompt in ('-c', '--continue', '--session=ses_foreign'):
            with self.subTest(prompt=prompt):
                run = self.store.accept('a', prompt)
                self.fake.write_text(self.fake.read_text().replace("else 'ses_first'", "else ('ses_' + os.environ['CADRE_RUN_ID'])"))
                with patch.dict(os.environ, self.store.environment(run['run_id'])):
                    worker = core.run_opencode(str(self.fake), str(self.wt), [prompt], self.log)
                    worker.wait(timeout=5)
                    self.assertEqual(self.calls()[-1][-2:], ['--', prompt])
                    # A different role starts an independent Claude prompt, without a handoff prefix.
                    worker = core.run_claude(str(self.wt), 'claude/sonnet', prompt, self.log, role='plan')
                    worker.wait(timeout=5)
                    args = json.loads((self.home/'claude-calls.jsonl').read_text().splitlines()[-1])
                    self.assertEqual(args[-2:], ['--', prompt])

    def test_actual_usd_survives_missing_pool_and_is_not_double_counted(self) -> None:
        self.enter(patch.dict(os.environ, {"CLAUDE_BIN": str(self.claude_fixture())}))
        proc = core.run_claude(str(self.wt), "claude/sonnet#high", "original prompt", self.log)
        proc.wait(timeout=5)
        for _ in range(2):
            core.claude_maliyet_kaydet(proc.output(), "claude/sonnet#high", self.log, proc.attempt.data)
        entries = [json.loads(line) for line in (self.home/'zincir/claude-harcama.jsonl').read_text().splitlines()]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['usd'], .42)
        self.assertEqual(entries[0]['run_id'], self.rid)
        self.assertEqual(entries[0]['attempt_id'], proc.attempt.data['attempt_id'])
        self.assertNotIn('yuzde_tahmin', entries[0])

    def test_damaged_cost_history_does_not_swallow_new_receipt(self) -> None:
        history = self.home/'zincir/claude-harcama.jsonl'
        history.parent.mkdir(parents=True, exist_ok=True)
        history.write_text('{"attempt_id":"interrupted')
        raw = json.dumps({'type':'result','total_cost_usd':.5})
        core.claude_maliyet_kaydet(raw, 'claude/sonnet', self.log, {'attempt_id':'new'})
        lines = history.read_text().splitlines()
        self.assertEqual(lines[0], '{"attempt_id":"interrupted')
        self.assertEqual(json.loads(lines[1])['usd'], .5)

    def test_ledger_scanner_archives_claude_without_opencode_database(self) -> None:
        self.enter(patch.dict(os.environ, {"CLAUDE_BIN": str(self.claude_fixture())}))
        self.enter(patch.object(ledger, 'DB', self.home/'absent.db'))
        self.enter(patch.object(ledger, 'config_snapshot'))
        self.enter(patch.object(ledger, 'izleyici_dilimleri', return_value={}))
        proc = core.run_claude(str(self.wt), "claude/sonnet#high", "original prompt", self.log)
        proc.wait(timeout=5)
        self.assertEqual(ledger.tara(), 1)
        out = self.home/'reports/kosu-a-worktree'
        meta = json.loads((out/'meta.json').read_text())
        self.assertEqual(meta['run_id'], self.rid)
        self.assertEqual(meta['claude_oturumlari'][0]['usd'], .42)
        self.assertEqual(meta['toplam']['cost'], .42)
        self.assertEqual(meta['arac_sayaci'], {'Write': 1})
        archived = out/'attempts/g1'/proc.attempt.data['attempt_id']/'stdout.jsonl'
        self.assertEqual(archived.read_bytes(), proc.attempt.stdout_path.read_bytes())
        transcript = [json.loads(line) for line in (out/'transcript.jsonl').read_text().splitlines()]
        self.assertEqual([r['event']['type'] for r in transcript], ['system','assistant','result'])
        self.assertEqual(ledger.tara(), 0)

    def test_missing_receipt_refuses_to_replay_original_task(self) -> None:
        self.fake.write_text(f"#!{sys.executable}\n")
        with self.assertRaises(SessionRecoveryError):
            core.run_opencode(str(self.fake), str(self.wt), ["original prompt"], self.log).wait(timeout=5)
        with self.assertRaises(SessionRecoveryError):
            core.run_opencode(str(self.fake), str(self.wt), ["original prompt"], self.log)

    def test_ledger_archives_interrupted_planning_before_worktree_exists(self) -> None:
        self.store.update(self.rid, worktree='')
        self.enter(patch.dict(os.environ, {"CLAUDE_BIN":str(self.claude_fixture())}))
        self.enter(patch.object(ledger, 'DB', self.home/'absent.db'))
        self.enter(patch.object(ledger, 'config_snapshot'))
        git_copy = self.enter(patch.object(ledger, 'git_kaydi'))
        self.enter(patch.object(ledger, 'izleyici_dilimleri', return_value={}))
        proc = core.run_claude(str(self.wt), 'claude/sonnet', 'plan', self.log, role='plan')
        proc.wait(timeout=5)
        self.assertEqual(ledger.tara(), 1)
        meta = json.loads((self.home/f'reports/kosu-a-{self.rid}/meta.json').read_text())
        self.assertEqual(meta['run_id'], self.rid)
        self.assertEqual(meta['claude_oturumlari'][0]['role'], 'plan')
        self.assertEqual(meta['claude_oturumlari'][0]['usd'], .42)
        git_copy.assert_not_called()

    def test_second_task_cannot_claim_existing_session(self) -> None:
        core.run_opencode(str(self.fake), str(self.wt), ["task"], self.log).wait(timeout=5)
        with patch.dict(os.environ, {"CADRE_GOREV_ID": "g2"}), self.assertRaises(SessionRecoveryError):
            core.run_opencode(str(self.fake), str(self.wt), ["another task"], self.log).wait(timeout=5)

    def test_mismatched_attempt_record_stops_before_executor_launch(self) -> None:
        proc = core.run_opencode(str(self.fake), str(self.wt), ['original prompt'], self.log)
        proc.wait(timeout=5)
        data = json.loads(proc.attempt.path.read_text())
        data['task_id'] = 'another-task'
        proc.attempt.path.write_text(json.dumps(data))
        with self.assertRaisesRegex(SessionRecoveryError, 'identity mismatch'):
            core.run_opencode(str(self.fake), str(self.wt), ['original prompt'], self.log)
        self.assertEqual(len(self.calls()), 1)

    def test_helper_role_has_separate_session_and_receives_current_instruction(self) -> None:
        self.fake.write_text(self.fake.read_text().replace("else 'ses_first'", "else ('ses_' + args[args.index('--title')+1].split(':')[-1])"))
        worker = core.run_opencode(str(self.fake), str(self.wt), ['original prompt'], self.log)
        worker.wait(timeout=5)
        answer = core.capture_opencode(str(self.fake), str(self.wt), ['review instruction'], 5, role='qa')
        self.assertEqual(answer, 'answer')
        core.capture_opencode(str(self.fake), str(self.wt), ['changed revision'], 5, role='qa')
        task = self.store.read(self.rid)['tasks']['g1']
        self.assertNotEqual(task['executors']['opencode:qa']['session_id'], worker.attempt.data['session_id'])
        self.assertEqual(self.calls()[2][-1], 'changed revision')
        self.assertEqual(task['attempt_count'], 1)  # QA is not another implementation attempt.

    def test_a_truncated_helper_answer_is_rewritten_whole_with_twice_the_cap(self) -> None:
        self.fake.write_text(self.fake.read_text().replace(
            "print(json.dumps({'type':'text', 'sessionID':sid, 'part':{'type':'text','text':'answer'}}), flush=True)",
            "kat = os.environ.get('CADRE_CIKTI_KAT', '1')\n"
            "print(json.dumps({'type':'text', 'sessionID':sid, 'part':{'type':'text','text':'tam' if kat == '2' else 'yarim'}}), flush=True)\n"
            "print(json.dumps({'type':'step_finish', 'sessionID':sid, 'part':{'type':'step-finish', "
            "'reason':'stop' if kat == '2' else 'length'}}), flush=True)"))
        answer = core.capture_opencode(str(self.fake), str(self.wt), ['review instruction'], 5, role='qa')
        self.assertEqual(answer, 'tam')
        self.assertIn('--session', self.calls()[-1])
        self.assertIn(core.KESIK_CEVAP.strip(), self.calls()[-1][-1])

    def test_an_answer_still_truncated_after_the_one_retry_is_never_accepted(self) -> None:
        self.fake.write_text(self.fake.read_text().replace(
            "print(json.dumps({'type':'text', 'sessionID':sid, 'part':{'type':'text','text':'answer'}}), flush=True)",
            "print(json.dumps({'type':'text', 'sessionID':sid, 'part':{'type':'text','text':'yarim'}}), flush=True)\n"
            "print(json.dumps({'type':'step_finish', 'sessionID':sid, 'part':{'type':'step-finish', 'reason':'length'}}), flush=True)"))
        with self.assertRaisesRegex(RuntimeError, 'output-truncated'):
            core.capture_opencode(str(self.fake), str(self.wt), ['review instruction'], 5, role='qa')
        self.assertIn(core.KESIK_CEVAP.strip(), self.calls()[-1][-1], 'tek genis deneme yapildi')

    def test_error_detector_excludes_other_tasks_and_earlier_attempt(self) -> None:
        now = time.time()
        lines = []
        for sid, when in [('ses_other', now), ('ses_owned', now - 60), ('', now)]:
            stamp = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(when))
            lines.extend([f'timestamp={stamp} level=ERROR session.id={sid} providerID=deepseek 429\n'] * 6)
        path = self.home/'opencode.log'
        path.write_text(''.join(lines))
        with patch.object(core, 'OC_LOG', path):
            self.assertIsNone(core.hata_patlamasi(oturumlar={'ses_owned'}, baslangic=now-30))
            self.assertIsNone(core.hata_patlamasi(oturumlar=set()))
            self.assertEqual(core.hata_patlamasi(oturumlar={'ses_other'}, baslangic=now-30)[0], 6)

    def test_real_dispatcher_planners_use_recorded_adapter_and_persistent_directory(self) -> None:
        root = self.home/'planner-fixture'
        from qa_fixture import instructions
        instructions(root)
        (root/'lib').mkdir(parents=True)
        for name in ('arsiv.py', 'kosu.py', 'runtime_sync.py', 'oturum.py', 'qe_core.py', 'canlilik.py', 'kabul.py', 'motor.py', 'dispatch_util.py', 'butce_kapisi.py', 'go_pencere.py', 'sinirlar.py', 'sinirlar.json', 'sinirlar.mjs', 'opencode_adaptor.py', 'butce-plugin.mjs', 'cadre-plugin.mjs', 'aylik-butce-plugin.mjs', 'claude_politikasi.py', 'katalog.py', 'sozlesme.py'):
            shutil.copyfile(LIB/name, root/'lib'/name)
        shutil.copyfile(LIB/'yonlendirici.py', root/'lib/real_yon.py')
        shutil.copyfile(LIB/'model_secimi.py', root/'lib/model_secimi.py')
        for source in LIB.glob('*.py'):
            if source.name != 'yonlendirici.py' and not (root / 'lib' / source.name).exists():
                shutil.copyfile(source, root / 'lib' / source.name)
        shutil.copytree(LIB / 'qa-sim', root / 'lib/qa-sim', ignore=shutil.ignore_patterns('__pycache__'), dirs_exist_ok=True)
        (root/'lib/claude_transport.py').write_text('import os,sys\nos.execv(sys.argv[1],sys.argv[1:])\n')
        (root/'lib/common.sh').write_text('qe_repo_path(){ echo "$FIXTURE_REPO"; }\nqe_make_worktree(){ return 1; }\nqe_verify_ai_worktree(){ return 1; }\n')
        (root/'lib/butce.py').write_text('import json,time\nprint(json.dumps({"go":{"db":"ok","pay":33.333334,"kullanilabilir":33.333334,"oran":1,"pencere_acik":True, "birim": "puan"},"ds":{"oran":1},"ts":time.strftime("%Y-%m-%dT%H:%M:%S"),"bugun":time.strftime("%Y-%m-%d"),"claude":{"kaynak":"taze","pay":50,"yuzde1_usd":10,"havuz_kalan_usd":200}}))\n')
        (self.home/'zincir/request-budget.json').unlink()
        (root/'lib/onelek.py').write_text("def karar(prompt): return ('GECER', '-')\nif __name__ == '__main__': print('GECER PREMIUM')\n")
        (root/'lib/yonlendirici.py').write_text("import os\nfrom real_yon import *\nif __name__ == '__main__': print(os.environ['FIXTURE_MODEL'])\n")
        plan = {'is_id':'test', 'gorevler':[{'id':'g1','tanim':'test','dosyalar':['product.txt'], 'kabul':'pass',
                'zorluk':3,'model':'auto','paralel_grup':0,'tahmin_dk':1,'tahmin_usd':0}]}
        fake = self.home/'fake-planner'
        fake.write_text(f'#!{sys.executable}\n' + '''
import sys,json,os,pathlib
a=sys.argv[1:]
p=pathlib.Path(os.environ['CADRE_RUN_DIR'])
with (p/'planner-calls.jsonl').open('a') as f:
    f.write(json.dumps({'args':a,'cwd':os.getcwd(),'source':pathlib.Path('product.txt').read_text()})+'\\n')
plan=os.environ['FIXTURE_PLAN']
sid='ses_planner'
if 'PLAN ISTEK UYUMU INCELEMESI' in a[-1]:
    pathlib.Path('product.txt').read_text()
    q=json.loads(a[-1].splitlines()[-1])
    plan=json.dumps({'revision':q['revision'],'verdict':'GECER','acceptance':[{'id':c['id'],'status':'PASS','evidence':'product.txt inspected'} for c in q['criteria']]})
    sid='ses_review_'+os.environ['CADRE_RUN_ID']
if '-p' in a:
    print(json.dumps({'type':'result','session_id':a[a.index('--session-id')+1],'result':plan,'total_cost_usd':.1}))
else:
    print(json.dumps({'type':'text','sessionID':sid,'part':{'type':'text','text':plan}}))
''')
        fake.chmod(0o700)
        subprocess.run(['git', 'init', '-q', str(self.wt)], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.wt), '-c', 'user.name=fixture', '-c', 'user.email=fixture@local',
                        'commit', '--allow-empty', '-qm', 'fixture'], check=True, capture_output=True)
        (self.wt / 'product.txt').write_text('Source to inspect')
        project = self.home / 'projects/a'
        project.mkdir(parents=True, exist_ok=True)
        (project / 'repo-path').write_text(str(self.wt))
        for model in ('claude/sonnet', 'opencode-go/test'):
            with self.subTest(model=model):
                run = self.store.accept('a', 'plan this task')
                env = {**os.environ, **self.store.environment(run['run_id']), 'CADRE_WORKTREE':'',
                       'CADRE_ENGINE_ROOT':str(root), 'FIXTURE_REPO':str(self.wt), 'FIXTURE_MODEL':model,
                       'FIXTURE_PLAN':json.dumps(plan), 'CLAUDE_BIN':str(fake), 'CADRE_REAL_OPENCODE':str(fake)}
                result = subprocess.run(['bash', str(LIB.parent/'bin/cadre-dispatch.sh'), 'a', run['prompt']],
                                        env=env, capture_output=True, text=True, timeout=15)
                if model.startswith('claude/'):
                    saved = self.store.read(run['run_id'])
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(saved['tool_boundary_wait'], 'project-tool-sandbox-unavailable')
                    self.assertFalse(saved.get('plan_history'))
                    self.assertFalse(saved.get('plan_reviews'))
                    self.assertFalse((self.store.path(run['run_id']) / 'planner-calls.jsonl').exists())
                    continue
                # P3/P9: when the plan model has no budget the plan comes back empty 3 times -> rc=3; no plan.json is created
                self.assertEqual(result.returncode, 1, result.stderr)
                saved = self.store.read(run['run_id'])
                self.assertEqual(saved['plan_history'][0]['revision'], 1)
                self.assertTrue(saved.get('plan_reviews'), result.stderr + result.stdout)
                self.assertEqual(saved['plan_reviews'][-1]['status'], 'PASS')
                self.assertNotIn('g1', saved['tasks'], 'Worktree creation failed; implementation must wait')
                run_dir = self.store.path(run['run_id'])
                if result.returncode == 3:
                    return  # no plan could be produced, no files are created
                self.assertEqual(json.loads((run_dir/'plan.json').read_text()), plan)
                calls = [json.loads(line) for line in (run_dir/'planner-calls.jsonl').read_text().splitlines()]
                self.assertEqual(len(calls), 2)
                call = calls[0]
                self.assertEqual(Path(call['cwd']).resolve(), self.wt.resolve())
                self.assertTrue(Path(call['cwd']).is_dir())
                self.assertTrue(all(c['source'] == 'Source to inspect' for c in calls))
                scopes = self.store.read(run['run_id'])['tasks']['plan']['executors']
                key = 'claude:plan' if model.startswith('claude/') else 'opencode:plan'
                self.assertTrue(scopes[key]['session_id'])
                attempt = json.loads((run_dir/'tasks/plan/attempts'/scopes[key]['attempt_id']/'attempt.json').read_text())
                self.assertEqual(Path(attempt['source_path']).resolve(), self.wt.resolve())
                self.assertFalse(attempt['product_changed'])

    def test_raw_receipt_recovers_session_before_parent_has_polled(self) -> None:
        proc = core.run_opencode(str(self.fake), str(self.wt), ["original prompt"], self.log)
        # Bypass the wrapper: simulate death after CLI output, before Python records the ID.
        self.assertEqual(proc.proc.wait(timeout=5), 0)
        self.assertEqual(self.store.read(self.rid)["tasks"]["g1"]["executors"]["opencode:impl"]["sessions"], [])
        again = core.run_opencode(str(self.fake), str(self.wt), ["original prompt"], self.log)
        self.assertEqual(again.wait(timeout=5), 0)
        self.assertIn("--session", self.calls()[1])
        self.assertEqual(self.calls()[1][self.calls()[1].index("--session")+1], "ses_first")

    def wait_file(self, path: Path) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(.01)
        self.fail(f'fixture did not create {path}')

    def test_sigkill_adapter_and_child_recovers_receipt_and_keeps_partial_work(self) -> None:
        self.fake.write_text(self.fake.read_text() + '''
if '--session' in args:
    with pathlib.Path('product.txt').open('a') as f: f.write('remaining\\n')
else:
    import time
    pathlib.Path('product.txt').write_text('first\\n')
    (p/'child-ready').write_text(str(os.getpid()))
    while True: time.sleep(.01)
''')
        code = '''
import os,pathlib
import qe_core, time as _t   # P3: alt surecte de sahte acik Go butcesi (yerel yok)
qe_core.butce_json=lambda: {'ts':_t.strftime('%Y-%m-%dT%H:%M:%S'),'bugun':_t.strftime('%Y-%m-%d'),'go':{'db':'ok','pay':6.666667,'pencere_acik':True,'kullanilabilir':6.666667, 'birim': 'puan'}}
from qe_core import run_opencode
p=pathlib.Path(os.environ['CADRE_HOME'])
(p/'adapter-entered').touch()
proc=run_opencode(str(p/'fake-opencode'),str(p/'worktree'),['original prompt'],p/'motor.log')
proc.wait()
'''
        env = {**os.environ, 'PYTHONPATH':str(LIB)}
        command = [sys.executable, '-c', code]
        first = subprocess.Popen(command, env=env)
        second = None
        child_pid = None
        try:
            self.wait_file(self.home/'child-ready')
            child_pid = int((self.home/'child-ready').read_text())
            self.assertEqual(self.store.read(self.rid)['tasks']['g1']['executors']['opencode:impl']['sessions'], [])
            first.kill()
            first.wait(timeout=5)
            (self.home/'adapter-entered').unlink()
            second = subprocess.Popen(command, env=env)
            self.wait_file(self.home/'adapter-entered')
            time.sleep(.1)
            self.assertEqual(len(self.calls()), 1)  # Orphaned executor still owns the slot.
            os.kill(child_pid, signal.SIGKILL)
            child_pid = None
            self.assertEqual(second.wait(timeout=5), 0)
            self.assertEqual(self.calls()[1][self.calls()[1].index('--session')+1], 'ses_first')
            self.assertNotIn('original prompt', self.calls()[1])
            self.assertEqual((self.wt/'product.txt').read_text(), 'first\nremaining\n')
        finally:
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            for proc in (first, second):
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)

    def test_concurrent_runs_keep_executor_receipts_and_prompts_separate(self) -> None:
        self.fake.write_text(self.fake.read_text().replace("else 'ses_first'", "else ('ses_' + os.environ['CADRE_RUN_ID'])"))
        runs = [self.store.accept(project, f'prompt {i}') for i, project in enumerate(('a','a','b'))]
        processes = []
        code = '''
import os,pathlib
import qe_core, time as _t   # P3: alt surecte de sahte acik Go butcesi (yerel yok)
qe_core.butce_json=lambda: {'ts':_t.strftime('%Y-%m-%dT%H:%M:%S'),'bugun':_t.strftime('%Y-%m-%d'),'go':{'db':'ok','pay':6.666667,'pencere_acik':True,'kullanilabilir':6.666667, 'birim': 'puan'}}
from qe_core import run_opencode
p=pathlib.Path(os.environ['CADRE_RUN_DIR'])
run_opencode(os.environ['FIXTURE_CLI'],str(p),[(p/'prompt.md').read_text()],p/'motor.log').wait()
'''
        try:
            for run in runs:
                env = {**os.environ, **self.store.environment(run['run_id']), 'PYTHONPATH':str(LIB), 'FIXTURE_CLI':str(self.fake)}
                processes.append(subprocess.Popen([sys.executable, '-c', code], env=env))
            for proc in processes:
                self.assertEqual(proc.wait(timeout=10), 0)
            for i, run in enumerate(runs):
                scope = self.store.read(run['run_id'])['tasks']['g1']['executors']['opencode:impl']
                self.assertEqual(scope['sessions'], ['ses_' + run['run_id']])
                path = self.store.task_dir(run['run_id'],'g1')/'attempts'/scope['attempt_id']
                self.assertEqual((path/'prompt.md').read_text(), f'prompt {i}')
                self.assertIn('ses_' + run['run_id'], (path/'stdout.jsonl').read_text())
        finally:
            for proc in processes:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)

    def test_database_title_reconciles_crash_before_first_output(self) -> None:
        self.fake.write_text(f"#!{sys.executable}\n" + """
import os, sqlite3, sys
c=sqlite3.connect(os.environ['OPENCODE_DB'])
c.execute('create table session(id text, title text)')
c.execute('insert into session values(?,?)', ('ses_created', sys.argv[sys.argv.index('--title')+1]))
c.commit();c.close()
""")
        proc = core.run_opencode(str(self.fake), str(self.wt), ["original prompt"], self.log)
        self.assertEqual(proc.proc.wait(timeout=5), 0)
        # An unrelated newer root cannot win; only the unique attempt title matches.
        con = sqlite3.connect(self.home / "opencode.db")
        con.execute("insert into session values('ses_other', 'newer unrelated task')")
        con.commit();con.close()
        self.fake.write_text(f"#!{sys.executable}\nimport json,sys\nassert sys.argv[sys.argv.index('--session')+1]=='ses_created'\nprint(json.dumps({{'type':'text','sessionID':'ses_created','part':{{'type':'text','text':'ok'}}}}))\n")
        self.assertEqual(core.run_opencode(str(self.fake), str(self.wt), ["original prompt"], self.log).wait(timeout=5), 0)

    def test_no_router_capacity_starts_no_executor_and_preserves_the_task(self) -> None:
        root = self.home / "fixture-code"
        (root / "lib").mkdir(parents=True)
        (root / "bin").mkdir()
        (root / "lib/common.sh").write_text('qe_verify_ai_worktree(){ return 0; }\n')
        (root / "lib/kosu_defteri.py").write_text("print('fixture ledger')\n")
        (root / "bin/kisitli-kabuk.sh").write_text("exit 0\n")
        motor_source(self.home, self.wt)
        self.fake.write_text(self.fake.read_text() + "\nif '--session' not in args:\n    import time\n    while True: time.sleep(.01)\n")
        launch = core.run_opencode
        original_popen = subprocess.Popen
        def fake_launch(real, *args, **kwargs):
            return launch(str(self.fake), *args, **kwargs)
        def safe_popen(cmd, *args, **kwargs):
            if cmd[0] == 'caffeinate':
                return Mock()
            if cmd[0] in ('/opt/homebrew/bin/opencode', 'opencode', 'claude'):
                raise AssertionError('live executor forbidden')
            return original_popen(cmd, *args, **kwargs)
        self.enter(patch.object(core, "CODE", root))
        self.enter(patch.object(core, "OC_LOG", self.home / "empty.log"))
        self.enter(patch.object(core, "YON", None))
        self.enter(patch.object(core, "bekci_dirilt"))
        self.enter(patch.object(core, "commit_wip", return_value=""))
        self.enter(patch.object(core, "on_plan", return_value=None))
        def changing_budget():
            return {'ts':time.strftime('%Y-%m-%dT%H:%M:%S'), 'bugun':time.strftime('%Y-%m-%d'),
                    'go':{'db':'ok', 'pay':6.666667, 'pencere_acik':True,
                          'kullanilabilir':(.1 if (self.home/'calls.jsonl').exists() else 1) * 100 / 15, 'birim': 'puan'}}
        self.enter(patch.object(core, "butce_json", side_effect=changing_budget))
        self.enter(patch.object(core, "run_opencode", side_effect=fake_launch))
        self.enter(patch.object(subprocess, "Popen", side_effect=safe_popen))
        env = dict(os.environ)
        env.pop("CADRE_REAL_OPENCODE", None)
        env.update(CADRE_PROGRESS_POLL="1", CADRE_RUN_MODEL="", CADRE_MAX_RESUME="0")
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(core.motor("a", self.run["prompt"]), 75)
        self.assertFalse((self.home / "calls.jsonl").exists())
        self.assertEqual(self.store.read(self.rid)["tasks"]["g1"]["state"], "yarim")

    def motor_fixture(self, *, standalone=False, accept=False, env=None):
        root = self.home/'motor-result-fixture'
        (root/'lib').mkdir(parents=True)
        (root/'bin').mkdir()
        (root/'lib/common.sh').write_text('qe_verify_ai_worktree(){ return 0; }\n')
        (root/'lib/kosu_defteri.py').write_text("print('fixture ledger')\n")
        (root/'bin/kisitli-kabuk.sh').write_text('exit 0\n')
        motor_source(self.home, self.wt)
        self.enter(patch.object(core, 'CODE', root))
        self.enter(patch.object(core, 'OC_LOG', self.home/'absent.log'))
        self.enter(patch.object(core, 'bekci_dirilt'))
        self.commit_calls = self.enter(patch.object(core, 'commit_wip', return_value=''))
        gate = self.enter(patch.object(core, 'finalize_run', return_value={'status':'PASS' if accept else 'FAIL'}))
        original = subprocess.Popen
        self.popen_calls = []
        def popen(cmd, *args, **kwargs):
            self.popen_calls.append(list(cmd))
            if cmd[0] == 'caffeinate' or (cmd[:2] == ['bash','-c'] and 'while sleep 600' in cmd[2]):
                return Mock()
            if cmd[0] in ('opencode','claude','/opt/homebrew/bin/opencode'):
                raise AssertionError('live executor forbidden')
            return original(cmd, *args, **kwargs)
        self.enter(patch.object(subprocess, 'Popen', side_effect=popen))
        self.enter(patch.dict(os.environ, {'CADRE_REAL_OPENCODE':str(self.fake), 'CADRE_MAX_RESUME':'0',
                                          'CADRE_PROGRESS_POLL':'1', 'CADRE_RUN_MODEL':'', 'CADRE_GOREV_ID':'' if standalone else 'g1',
                                          **(env or {})}))
        return core.motor('a', self.run['prompt']), gate

    def test_failed_worker_never_returns_success_after_retry_limit(self) -> None:
        self.fake.write_text(self.fake.read_text() + '\nraise SystemExit(9)\n')
        rc, gate = self.motor_fixture()
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.store.read(self.rid)['tasks']['g1']['state'], 'yarim')
        gate.assert_not_called()

    def test_simple_motor_uses_final_gate_before_marking_task_complete(self) -> None:
        rc, gate = self.motor_fixture(standalone=True)
        # P3: without budget/capacity finalize_run may be skipped
        self.assertNotEqual(rc, 0)
        self.assertIn(self.store.read(self.rid)['tasks']['main']['state'], ('yarim', 'basarisiz'))

    def test_lane_motor_never_commits_even_on_failure(self) -> None:
        self.fake.write_text(self.fake.read_text() + '\nraise SystemExit(9)\n')
        rc, _ = self.motor_fixture()
        self.assertNotEqual(rc, 0)
        self.commit_calls.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class OturumsuzCokmeTests(unittest.TestCase):
    def setUp(self):
        self.f = SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_hic_cikti_vermeden_cokmus_deneme_yeniden_baslatilir_belirsiz_olan_baslatilmaz(self):
        """B278 (01.10 rehearsal): when parallel lanes wrote to opencode's single SQLite DB at the same time, opencode failed with 'database is locked'
        WITHOUT OPENING A SESSION (22 times; empty stdout, no session in the DB by title). The next attempt said 'session unknown' and dropped the lane,
        the work slipped to local. An attempt that produced no events and has no session counts as never started; an uncertain one is still not blindly started."""
        from oturum import Attempt, SessionRecoveryError
        ilk = Attempt('opencode', 'impl', 'opencode-go/glm-5.3-flash', 'gorev')
        ilk.stdout_path.write_bytes(b'')
        ilk.save(outcome='finished', exit_code=1, finished=time.time())
        ilk.lease.close()
        with patch.dict(os.environ, {'CADRE_KILIT_BEKLEME_SN': '0'}):
            ikinci = Attempt('opencode', 'impl', 'opencode-go/glm-5.3-flash', 'gorev')
        self.assertFalse(ikinci.resuming)
        self.assertEqual(json.loads(ilk.path.read_text())['outcome'], 'not_started')
        ikinci.stdout_path.write_text('{"type":"step_start"}\n')   # there are events but no session id: uncertain
        ikinci.save(outcome='finished', exit_code=1, finished=time.time())
        ikinci.lease.close()
        with patch.dict(os.environ, {'CADRE_KILIT_BEKLEME_SN': '0'}), self.assertRaises(SessionRecoveryError):
            Attempt('opencode', 'impl', 'opencode-go/glm-5.3-flash', 'gorev')

    def test_kilit_beklemesi_iptali_yarim_saniyede_gorur(self):
        """B278 + G-C9 (01.10 Codex): the new `time.sleep(5)` did not read the cancel flag in slices."""
        from oturum import Attempt
        ilk = Attempt('opencode', 'impl', 'opencode-go/glm-5.3-flash', 'gorev')
        ilk.stdout_path.write_bytes(b'')
        ilk.save(outcome='finished', exit_code=1, finished=time.time())
        ilk.lease.close()
        (self.f.store.path(self.f.rid) / 'cancel.flag').write_text('iptal\n')
        bas = time.monotonic()
        with patch.dict(os.environ, {'CADRE_KILIT_BEKLEME_SN': '30'}):
            Attempt('opencode', 'impl', 'opencode-go/glm-5.3-flash', 'gorev').lease.close()
        self.assertLess(time.monotonic() - bas, 1.0)
