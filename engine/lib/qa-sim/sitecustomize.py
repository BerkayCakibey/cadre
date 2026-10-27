"""FIX-52 (N-QA01): the QA observer puts only this shim on PYTHONPATH; the engine module is loaded by file path, site-packages is not shadowed."""
import importlib.machinery
import importlib.util
import os
import sys

_SIM = os.path.dirname(os.path.abspath(__file__))
_RUNNER = os.path.join(os.path.dirname(_SIM), 'qa_evidence.py')


def _runner_load():
    spec = importlib.util.spec_from_file_location('qa_evidence', _RUNNER)
    module = importlib.util.module_from_spec(spec)
    sys.modules['qa_evidence'] = module
    spec.loader.exec_module(module)
    return module


def _next_sitecustomize():
    paths = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _SIM]
    spec = importlib.machinery.PathFinder.find_spec('sitecustomize', paths)
    if spec is not None and spec.loader is not None and spec.origin != __file__:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)


if os.environ.get('CADRE_QA_EVIDENCE') and os.environ.get('CADRE_QA_TOKEN'):
    _runner_load().install_unittest()
_next_sitecustomize()
