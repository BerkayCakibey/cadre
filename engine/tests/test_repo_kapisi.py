"""V3/TEMIZ1: the config and security contract in the repo instead of the installed copy."""
from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'engine/lib'))


def config_oku() -> dict:
    return json.loads(re.sub(r'^\s*//.*$', '', (REPO / 'config/opencode.jsonc').read_text(), flags=re.M))


class RepoKapisiTests(unittest.TestCase):
    def test_model_defaults_belong_to_runtime_router(self):
        cfg = config_oku()
        self.assertNotIn('model', cfg)
        self.assertNotIn('small_model', cfg)
        for agent in cfg['agent'].values():
            self.assertNotIn('model', agent)

    def test_context_envelope_keeps_tools_and_reasoning_room(self):
        cfg = config_oku()
        self.assertEqual(cfg['compaction'], {'auto': True, 'prune': False,
                                           'preserve_recent_tokens': 16384, 'reserved': 8192})
        self.assertGreaterEqual(cfg['tool_output']['max_bytes'], 49152)
        self.assertGreaterEqual(cfg['tool_output']['max_lines'], 5000)
        self.assertNotIn('lmstudio', cfg['provider'], '06.10: yerel model kaldirildi')

    def test_global_and_build_permissions_protect_destructive_commands_and_secrets(self):
        cfg = config_oku()
        for policy in (cfg['permission'], cfg['agent']['build']['permission']):
            def action(kind, value):
                result = None
                for pattern, setting in policy[kind].items():
                    if fnmatch.fnmatchcase(value, pattern):
                        result = setting
                return result
            for command in ('git push --force origin main', 'git reset --hard HEAD',
                            'git clean -fd', 'rm -rf /tmp/important', 'sudo command'):
                self.assertEqual(action('bash', command), 'deny', command)
            for path in ('project/.env', 'project/.env.prod', 'secrets/server.pem', 'secrets/id_rsa'):
                self.assertEqual(action('read', path), 'deny', path)
            self.assertEqual(action('external_directory', '/outside/project'), 'deny')
            self.assertEqual(action('read', 'project/.env.example'), 'allow')
        build = cfg['agent']['build']['permission']['bash']
        self.assertNotIn('*', build, 'FIX-03: manuel build oturumunda genel kabuk kapali degil')
        for command in ('ssh host uptime', 'scp file host:', 'rsync -a src dst', 'kubectl get pods', 'terraform plan',
                        'aws s3 ls', 'gcloud auth list', 'bash scripts/deploy_release.sh'):
            self.assertEqual(next((v for k, v in reversed(build.items()) if fnmatch.fnmatchcase(command, k)), None), 'ask', command)
        for command in ('git status', 'ls -la', 'python3 -m pytest -q'):
            self.assertIsNone(next((v for k, v in build.items() if fnmatch.fnmatchcase(command, k)), None), command)

    def test_regression_entry_warns_when_the_lint_type_python_is_missing(self):
        import regresyon
        self.assertIn('CADRE_TEST_PYTEST_PYTHON', regresyon.ortam_uyarisi({}))
        self.assertEqual(regresyon.ortam_uyarisi({'CADRE_TEST_PYTEST_PYTHON': '/venv/bin/python'}), '')

    def test_local_gate_uses_repo_scratch_and_bounded_parallelism_by_default(self):
        """N-OBS05: the local gate's default does not write to the live data home or start heavy work on every CPU."""
        from unittest.mock import patch
        import paralel_kos
        import regresyon
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(regresyon.arsiv_yolu(), REPO / '.tmp/test-arsivi')
        with patch.dict(os.environ, {'CADRE_TEST_ARSIVI': '/fixture/evidence'}, clear=True):
            self.assertEqual(regresyon.arsiv_yolu(), Path('/fixture/evidence'))
        for cpus, expected in ((None, 4), (1, 1), (2, 2), (10, 4), (32, 4)):
            self.assertEqual(paralel_kos.isci_sayisi({}, cpus), expected)
        self.assertEqual(paralel_kos.isci_sayisi({'CADRE_TEST_ISCI': '6'}, 10), 6)
        for invalid in ('0', '-2', 'abc'):
            with self.assertRaises(ValueError):
                paralel_kos.isci_sayisi({'CADRE_TEST_ISCI': invalid}, 10)

    def test_agent_roles_and_tools_cannot_recurse_or_write_during_review(self):
        agents = REPO / 'config/agents'
        for name in ('build', 'cadre-plan', 'cadre-review', 'cadre-impl'):   # 4c-9: no exploration agent
            source = (agents / (name + '.md')).read_text()
            front = source.split('---')[1]
            self.assertNotRegex(front, r'(?m)^model:')
            self.assertRegex(front, r'(?m)^mode: (all|subagent)$')
        impl = (agents / 'cadre-impl.md').read_text().split('---')[1]
        self.assertIn('task: false', impl)
        self.assertIn('mode: subagent', impl)
        self.assertIn('edit:\n    "*": deny', (agents / 'cadre-plan.md').read_text().split('---')[1])
        self.assertIn('edit: deny', (agents / 'cadre-review.md').read_text().split('---')[1])
        self.assertFalse((agents / 'cadre-scout.md').exists(), '4c-9: kesif ajani kalkti')

    def test_config_composition_is_fail_closed_and_mcp_is_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'projects/example'
            project.mkdir(parents=True)
            for name in ('PROJECT.md', 'POLICY.md', 'STATE.md'):
                (project / name).write_text('fixture')
            mcp = project / 'mcp.json'
            mcp.write_text(json.dumps({'extra': {'enabled': False}}))
            env = {**os.environ, 'CADRE_HOME': str(root)}
            script = '. "$1"; qe_config_content example default'
            def run():
                return subprocess.run(['bash', '-c', script, 'fixture', str(REPO / 'engine/lib/common.sh')],
                                      env=env, capture_output=True, text=True, timeout=10)
            result = run()
            self.assertEqual(result.returncode, 0, result.stderr)
            cfg = json.loads(result.stdout)
            self.assertEqual(len(cfg['instructions']), 3)
            self.assertFalse(cfg['mcp']['extra']['enabled'])
            env['CADRE_MCP_ENABLE'] = 'extra'
            self.assertTrue(json.loads(run().stdout)['mcp']['extra']['enabled'])
            mcp.write_text('not json')
            self.assertNotEqual(run().returncode, 0)


if __name__ == '__main__':
    unittest.main()
