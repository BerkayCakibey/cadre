"""4c-9: the exploration agent and the plan's sub-agent field were removed. A lane hands off to no sub-agent (research via websearch);
a plan that writes `alt_ajanlar` out of old habit is not rejected, the field is dropped."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import dispatch_util as du  # noqa: E402


def plan(alt=None):
    g = {'id': 'g1', 'tanim': 'bir sey yap', 'kabul': 'test gecer', 'dosyalar': ['src/a.py'],
         'kapsam_disi': [], 'bagimli': [], 'zorluk': 2, 'model': 'auto', 'paralel_grup': 0,
         'tahmin_dk': 10, 'tahmin_usd': 0.0}
    if alt is not None:
        g['alt_ajanlar'] = alt
    return {'is_id': 'deneme', 'gorevler': [g], 'birlestirme_sirasi': ['g1']}


def serit_istemi(p):
    r = subprocess.run([sys.executable, str(LIB / 'dispatch_util.py'), 'gorev_pf', 'g1'],
                       input=json.dumps(p), capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout


class AltAjanYokTests(unittest.TestCase):
    def test_an_old_sub_agent_field_is_dropped_and_the_lane_never_delegates(self):
        p = plan([{'rol': 'scout', 'neden': 'repo genis'}])
        du.validate_plan(p, root=None)
        self.assertNotIn('alt_ajanlar', p['gorevler'][0])
        istem = serit_istemi(p)
        self.assertIn('task() ile DEVRETME', istem)
        self.assertIn('websearch', istem)
        self.assertNotIn('scout', istem)

    def test_the_planner_prompt_no_longer_asks_for_sub_agents(self):
        istem = (LIB.parent / 'bin/cadre-dispatch.sh').read_text()
        for eski in ('ALT AJANLAR', 'alt_ajanlar', 'CADRE_IZINLI_ALT_AJANLAR', 'izinli-alt'):
            self.assertNotIn(eski, istem)
        self.assertIn('CADRE_ALT_AJANSIZ=1', istem, 'serit ve ek serit alt ajansiz baslar')

    def test_the_lane_prompt_carries_no_harness_internal_notes(self):
        """B217 (24.09 R6-medium/R6-hard): the internal note in the first line ('# SABIT-ONEK: yerel prompt-cache tahliyesini azaltir') was taken by the model as project
        context and leaked into the product documents as 'local prompt-cache triage'. The fixed prefix stays, its explanation does not enter the prompt."""
        istem = serit_istemi(plan([]))
        self.assertEqual(istem.splitlines()[0], '[QE-GOREV v1]', 'sabit onek (onbellek) aynen kalir')
        for ic in ('SABIT-ONEK', 'prompt-cache', 'tahliye'):
            self.assertNotIn(ic, istem)


if __name__ == '__main__':
    unittest.main()
