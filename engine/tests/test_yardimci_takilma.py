"""4c-9/10: no helper agent in a lane, the lane's own model does the compaction. When the guard cuts the worker on silence, the stuck one
is the MAIN model: the recorded session continues with the next (model, route) in the chain. The old helper ban environment (models excluded
after a stall, closing the helpers at the second stall) is no longer set up. Real engine, real attempt records, fake executor."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import test_kosu_sessions as fixtures  # noqa: E402
from test_gece_senaryolar import DS, GO_IMPL, butce, zincir_sahte  # noqa: E402
import qe_core as core  # noqa: E402


class TakilmaTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.enter(patch.object(core, 'project_memory', return_value=[]))
        self.f.enter(patch.object(core.YON, 'zincir', side_effect=zincir_sahte))
        self.f.enter(patch.object(core, 'butce_json', return_value=butce()))
        # Guard signals: model idle, subprocess not eating CPU -> silence = stall (no SSH/HTTP probe)
        self.f.enter(patch.object(core, 'arac_calisiyor', return_value=False))

    def test_a_stall_moves_the_session_to_the_next_route_and_compaction_follows_the_lane(self):
        home = self.f.home
        self.f.fake.write_text(f"#!{sys.executable}\n" + """
import json, os, pathlib, sys, time
p = pathlib.Path(os.environ['CADRE_HOME'])
n = len(list(p.glob('env-*.json'))) + 1
(p / f'env-{n}.json').write_text(json.dumps({k: os.environ.get(k) for k in ('CADRE_YASAK_MODELLER', 'CADRE_YARDIMCI_KAPALI')}))
args = sys.argv[1:]
sid = args[args.index('--session') + 1] if '--session' in args else 'ses_stall'
print(json.dumps({'type': 'step_start', 'sessionID': sid, 'part': {'type': 'step-start'}}), flush=True)
if n < 2:
    time.sleep(30)   # ana model asili: stdout sessiz, model bosta
print(json.dumps({'type': 'text', 'sessionID': sid, 'part': {'type': 'text', 'text': 'bitti'}}), flush=True)
""")
        rc, _gate = self.f.motor_fixture(accept=True, env={'CADRE_STALL_SEC': '1', 'CADRE_MAX_RESUME': '4'})
        self.assertEqual(rc, 0)
        attempts = sorted((json.loads(p.read_text()) for p in
                           (self.f.store.task_dir(self.f.rid, 'g1') / 'attempts').glob('*/attempt.json')),
                          key=lambda a: a['created'])
        self.assertEqual([a['model'] for a in attempts[:2]], [GO_IMPL, DS], 'takilan ana model; oturum sonraki yolla surer')
        for a in attempts:
            self.assertEqual(a['agents']['compaction'], a['model'], 'sikistirma seridin kendi modeli')
            self.assertNotIn('cadre-scout', a['agents'])
        for n in (1, 2):
            env = json.loads((home / f'env-{n}.json').read_text())
            self.assertEqual(env, {'CADRE_YASAK_MODELLER': None, 'CADRE_YARDIMCI_KAPALI': None})


if __name__ == '__main__':
    unittest.main()
