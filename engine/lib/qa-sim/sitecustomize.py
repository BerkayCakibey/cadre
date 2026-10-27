"""FIX-52 (N-QA01): the QA observer puts only this shim on PYTHONPATH; the engine module is loaded by file path, site-packages is not shadowed."""
import importlib.machinery
import importlib.util
import os
import sys

_SIM = os.path.dirname(os.path.abspath(__file__))
_MOTOR = os.path.join(os.path.dirname(_SIM), 'qa_kaniti.py')


def _motor_yukle():
    spec = importlib.util.spec_from_file_location('qa_kaniti', _MOTOR)
    module = importlib.util.module_from_spec(spec)
    sys.modules['qa_kaniti'] = module
    spec.loader.exec_module(module)
    return module


def _sonraki_sitecustomize():
    yollar = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _SIM]
    spec = importlib.machinery.PathFinder.find_spec('sitecustomize', yollar)
    if spec is not None and spec.loader is not None and spec.origin != __file__:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)


if os.environ.get('CADRE_QA_EVIDENCE') and os.environ.get('CADRE_QA_TOKEN'):
    _motor_yukle().install_unittest()
_sonraki_sitecustomize()
