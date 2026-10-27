"""G-C5 packaging tolerance (23.09 live T1v4, B174): three cloud judges fell in a row with "malformed review", the local judge timed out
after 15 min, acceptance FAIL `review-not-passed`. The raw outputs (fixtures/judge-20260923/) show that big-pickle and Cerebras
had produced VALID JSON with the right product identity and a PASS verdict — but inside prose: braces in the text like `{package_version()}`
broke the "outermost {…}" tolerance (B39) ("Expecting property name", "Extra data"). Contract: JSON candidates
are searched with `raw_decode` from every `{` position and in ```json fences; the meaning check (identity, verdict, scope, placeholder) stays the SAME.
glm's invented criterion ids are still rejected. No model/network."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import outcome  # noqa: E402

FIX = Path(__file__).resolve().parent / 'fixtures' / 'judge-20260923'
DIGEST = '1ac22fd46a8262c44dff3425958192510553527ba8b45125b643f28026540951'
EXPECTED = {'digest': DIGEST}


def read(entry_name: str) -> str:
    return (FIX / entry_name).read_text(encoding='utf-8')


class JudgeEnvelopeTest(unittest.TestCase):
    def test_big_pickle_fenced_json_inside_plain_text_is_accepted(self):
        r = outcome.parse_review(read('judge-opencode_big-pickle.txt'), EXPECTED, ['main'])
        self.assertEqual((r['status'], r['verdict']), ('PASS', 'PASS'), r.get('reason'))
        self.assertEqual([k['id'] for k in r['acceptance']], ['main'])

    def test_cerebras_unfenced_json_inside_text_is_accepted(self):
        r = outcome.parse_review(read('judge-cerebras_qwen-3.8-27b.txt'), EXPECTED, ['main'])
        self.assertEqual((r['status'], r['verdict']), ('PASS', 'PASS'), r.get('reason'))

    def test_glm_made_up_criterion_ids_are_still_rejected(self):
        r = outcome.parse_review(read('judge-opencode-go_glm-5.3-flash.txt'), EXPECTED, ['main'])
        self.assertEqual(r['status'], 'ERROR')
        self.assertIn('acceptance', r['reason'])  # coverage incomplete / unknown criterion — the meaning check did not loosen

    def test_first_json_with_a_wrong_id_is_skipped_the_right_one_is_taken(self):
        text = ('Earlier draft: {"revision": "deadbeef", "verdict": "REVISE", "acceptance": []}\n'
                 'Then the actual decision:\n```json\n{"revision": "' + DIGEST + '", "verdict": "PASS", '
                 '"acceptance": [{"id": "main", "status": "PASS", "evidence": "tests/unit/test_cli_version.py:1-36 two tests pass"}]}\n```\n')
        r = outcome.parse_review(text, EXPECTED, ['main'])
        self.assertEqual((r['status'], r['verdict']), ('PASS', 'PASS'), r.get('reason'))

    def test_line_contract_still_works_without_any_json(self):
        text = f'REVISION: {DIGEST}\nDECISION: PASS\nCRITERION main: PASS - tests/unit/test_cli_version.py:1-36 two tests pass\n'
        for valid in (text, text.lower()):
            self.assertEqual(outcome.parse_review(valid, EXPECTED, ['main'])['status'], 'PASS')
        for original, replacement in (('REVISION', 'REV\u0131SION'), ('DECISION', 'DEC\u0130SION'),
                                      ('CRITERION', 'CRITER\u0131ON'), ('PASS', 'PA\u017fS')):
            with self.subTest(keyword=original, replacement=ascii(replacement)):
                self.assertEqual(outcome.parse_review(text.replace(original, replacement), EXPECTED, ['main'])['status'], 'ERROR')




class WriterOutputTest(unittest.TestCase):
    def test_single_report_object_is_read_from_intermediate_text_and_fence_an_ambiguous_or_incomplete_answer_is_rejected(self):
        import json
        answer = {'report': '# Findings\nx', 'evidence': [], 'assumptions': [], 'decisions': []}
        text = 'Source read {note}.\n```json\n' + json.dumps(answer) + '\n```\n' + json.dumps(answer)
        self.assertEqual(outcome.single_json_object(text, 'report'), answer)
        for bad in (json.dumps(answer) + '\n' + json.dumps({**answer, 'report': 'other'}), 'report absent {"x": 1}', ''):
            with self.subTest(bad=bad[:30]), self.assertRaisesRegex(ValueError, 'review-output-invalid'):
                outcome.single_json_object(bad, 'report')


if __name__ == '__main__':
    unittest.main()
