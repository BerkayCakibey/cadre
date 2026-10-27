from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import unittest


def review_output(text: str) -> str:
    class Output(str):
        @property
        def attempt(self):
            import qe_core as core
            from oturum import project_tool_policy
            if not hasattr(self, '_attempt'):
                attempt = core.Attempt('opencode', 'kalite', 'fixture/reviewer', 'unit review metadata')
                try:
                    record = attempt.store.read(attempt.run_id)
                    policy = project_tool_policy(attempt.store, record, attempt.directory,
                                                 record['worktree'], 'opencode', 'kalite', dict(os.environ))
                    assert policy is not None
                    attempt.tool_policy = policy
                    attempt.save(session_id='ses_' + core.uuid.uuid4().hex, outcome='finished', exit_code=0)
                    self._attempt = attempt
                finally:
                    attempt.lease.close()
            return self._attempt
    return Output(text)


def instructions(engine: Path) -> None:
    from oturum import INSTRUCTION_PATHS
    source = Path(__file__).resolve().parents[2] / 'config'
    for name in INSTRUCTION_PATHS:
        target = engine.resolve().parent / 'config' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)


def command(shell: str = 'test -f product.txt', runner: Path | None = None) -> str:
    return shlex.join([sys.executable, str(runner or Path(__file__).resolve()), shell])


def run() -> int:
    suite = unittest.TestSuite()
    for path in sorted(Path.cwd().glob('test_*.py')):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        if spec is None or spec.loader is None:
            raise RuntimeError('fixture-test-loader-unavailable')
        module = importlib.util.module_from_spec(spec)
        sys.modules[path.stem] = module
        spec.loader.exec_module(module)
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))
        for name, value in vars(module).items():
            if name.startswith('test_') and callable(value) and not isinstance(value, type):
                suite.addTest(unittest.FunctionTestCase(value))
    def acceptance() -> None:
        result = subprocess.run(['bash', '-c', sys.argv[1]], capture_output=True, text=True)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr + f'\nacceptance rc={result.returncode}')
    suite.addTest(unittest.FunctionTestCase(acceptance))
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(run())
