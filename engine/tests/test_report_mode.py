"""G-R1..G-R6 report/analysis mode (DESIGN-4). A report is not prose but a set of evidenced
claims: the acceptance record recognizes a report job and only the report + evidence appendix may change (G-R1), the evidence appendix is required (G-R2), report
QA verifies the claims mechanically (G-R3/G-R4), the judge probes with a sample (G-R5), the result is written by a strong model, not a local one (G-R6).
No model/network; evidence commands run in the real sandbox."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import report_mode  # noqa: E402
import qe_core as core  # noqa: E402
from run_store import RunStore, operation_mode, execute_run  # noqa: E402
from outcome import parse_review, revision  # noqa: E402


def setUpModule():
    environment = patch.dict(os.environ, {'CADRE_CHAIN_QUALITY': 'opencode-go/qwen3.8-max'})
    environment.start()
    unittest.addModuleCleanup(environment.stop)


EXTRA1_OUTPUT = "OUTPUT: docs/reviews/2026-09-24/codex-audit.md (write only this file). Use plain English."
EXTRA2_OUTPUT = "## OUTPUT\n`docs/reviews/2026-09-24/tool-output-audit.md` (write only this file). Use plain English."
REPORT_PROMPT = "ROUND: report\nREPORT: report_mode.md\nAudit app.py and report the findings."
APPLICATION = "def sum(a, b):\n    return a - b\n"   # deliberate bug: subtraction instead of addition


def git(wt, *args):
    return subprocess.run(['git', '-C', str(wt), '-c', 'user.name=fixture', '-c', 'user.email=fixture@local', *args],
                          check=True, capture_output=True, text=True, timeout=10).stdout


def repo(root: Path) -> tuple[Path, str]:
    wt = root / 'wt'
    wt.mkdir(parents=True)
    git(wt, 'init', '-q')
    (wt / 'app.py').write_text(APPLICATION)
    git(wt, 'add', '.')
    git(wt, 'commit', '-qm', 'base')
    return wt, git(wt, 'rev-parse', 'HEAD').strip()


def evidence_write(wt: Path, report_text: str, evidence, path='report_mode.md'):
    (wt / path).write_text(report_text)
    (wt / path.replace('.md', '.evidence.jsonl')).write_text(''.join(json.dumps(k, ensure_ascii=False) + '\n' for k in evidence))


def K(i, round, source, expected='', claim='claim'):
    return {'id': f'K{i}', 'claim': claim, 'round': round, 'source': source, 'expected': expected}


VALID_REPORT = "# Summary\n`sum` subtracts two numbers instead of adding them; it returns 0 for 2 and 2 [K1][K2].\n"
VALID_EVIDENCE = [K(1, 'file', 'app.py:2', 'return a - b'),
                 K(2, 'bug', "python3 -c 'from app import sum; assert sum(2, 2) == 4'", 'AssertionError')]


class DeclarationTest(unittest.TestCase):
    def test_open_round_line_and_report_path(self):
        b = report_mode.declaration("ROUND: report\nREPORT: docs/audit.md\nSECTIONS: Summary; Funnel; Bugs\nmeasure the repository")
        self.assertEqual(b, {'path': 'docs/audit.md', 'evidence': 'docs/audit.evidence.jsonl', 'sections': ['Summary', 'Funnel', 'Bugs']})

    def test_only_write_this_file_pattern_is_a_report_job(self):
        for prompt, path in ((EXTRA1_OUTPUT, 'docs/reviews/2026-09-24/codex-audit.md'),
                           (EXTRA2_OUTPUT, 'docs/reviews/2026-09-24/tool-output-audit.md')):
            with self.subTest(path=path):
                self.assertEqual(report_mode.declaration(prompt)['path'], path)

    def test_code_task_is_not_a_report(self):
        for prompt in ('Fix product.txt and add tests', 'Update README.md', 'ROUND: code\nREPORT: x.md'):
            with self.subTest(prompt=prompt):
                self.assertIsNone(report_mode.declaration(prompt))

    def test_unsafe_report_path_is_not_accepted(self):
        self.assertEqual(report_mode.declaration("ROUND: report\nREPORT: ../outside.md")['path'], 'REPORT.md', 'an explicit declaration falls back from an unsafe path')
        self.assertIsNone(report_mode.declaration("`/etc/x.md` (write only this file)"), 'an unsafe implicit path does not enable report mode')

    def test_acceptance_record_carries_the_report_mode_and_the_paths(self):
        with tempfile.TemporaryDirectory() as home_dir:
            record = RunStore(Path(home_dir)).accept('a', "ROUND: report\nREPORT: r.md\nmeasure")
        self.assertEqual((record['mode'], record['report']['path'], record['report']['evidence']), ('report', 'r.md', 'r.evidence.jsonl'))

    def test_read_only_review_mode_does_not_change(self):
        self.assertEqual(operation_mode('Review this module and only write a report; do not write code'), 'review')
        self.assertEqual(operation_mode('Review this module and only write a report; do not write code\nROUND: report\nREPORT: x.md'), 'report')

    def test_short_report_request_enters_dispatch(self):
        """G-R6: the pre-filter does not leave a short report request to the plain engine (the local model alone)."""
        from prefilter import decision
        self.assertEqual(decision("ROUND: report\nREPORT: r.md\nmeasure", True)[0], 'PASS')
        self.assertEqual(decision("ROUND: report\nREPORT: r.md\nmeasure", False)[0], 'SKIP')


class EvidenceAppendixTest(unittest.TestCase):
    def test_valid_lines_are_read(self):
        records, findings = report_mode.evidence_read('{"id":"K1","claim":"a","round":"file","source":"app.py:2","expected":"x"}\n\n')
        self.assertEqual(([k['id'] for k in records], findings), (['K1'], []))

    def test_broken_lines_produce_a_finding(self):
        text = '\n'.join([
            'not json',
            '{"id":"K1","claim":"a","round":"file","source":"app.py:2"}',
            '{"id":"K2","claim":"a","round":"estimate","source":"x","expected":"y"}',
            '{"id":"X3","claim":"a","round":"url","source":"https://a"}',
            '{"id":"K4","claim":"a","round":"url","source":"https://a"}',
            '{"id":"K4","claim":"b","round":"url","source":"https://b"}',
        ])
        _k, findings = report_mode.evidence_read(text)
        self.assertEqual(len(findings), 5, findings)
        self.assertTrue(all(b.startswith('evidence-broken:') for b in findings), findings)

    def test_lane_prefixed_id_valid_outside_the_format_broken(self):
        """B224 (25.09 live R12-report): two analysts writing to the same appendix used the prefix the planner gave (K-tz1, K-backup11); the prefixed
        form is valid, an id outside the form (K-1, Ktz1, tz1) stays broken."""
        line = '{{"id":"{}","claim":"a","round":"file","source":"app.py:2","expected":"x"}}'
        records, findings = report_mode.evidence_read('\n'.join(line.format(i) for i in ('K-tz1', 'K-backup11', 'K3', 'K-line-1')))   # B236: prefix-dash-number is valid too
        self.assertEqual(([k['id'] for k in records], findings), (['K-tz1', 'K-backup11', 'K3', 'K-line-1'], []))
        for bad in ('K-1', 'Ktz1', 'tz1'):
            with self.subTest(id=bad):
                self.assertTrue(report_mode.evidence_read(line.format(bad))[1])


class ReviewTextTest(unittest.TestCase):
    def report(self):
        return {'Findings': [{'text': 'The source contains 4 checks.', 'evidence_ids': ['K1']}],
                'Uncertainties': [{'text': 'The install result is unverified.', 'evidence_ids': []}],
                'Method': [{'text': 'Line 2 was read from the source.', 'evidence_ids': ['K1']}]}

    def test_citations_are_rendered_on_each_author_linked_sentence_before_its_punctuation(self):
        report = self.report()
        report['Findings'].append({'text': 'Range 10-12 exists in the record', 'evidence_ids': ['K1']})
        evidence = [K(1, 'file', 'app.py:2', 'return a - b')]
        rendered = report_mode.review_text(report, evidence)
        self.assertIn('- The source contains 4 checks [K1].', rendered)
        self.assertIn('- Range 10-12 exists in the record [K1].', rendered)
        self.assertIn('- Line 2 was read from the source [K1].', rendered)
        self.assertEqual(report_mode.unevidenced_numbers(rendered), [])
        self.assertEqual(len(evidence), 1, 'reusing a source does not manufacture extra evidence')

    def test_missing_unknown_duplicate_and_inline_references_are_rejected_instead_of_invented(self):
        for refs in ([], ['K99'], ['K1', 'K1'], 'K1', [True], [{}]):
            with self.subTest(refs=refs):
                report = self.report()
                report['Findings'][0]['evidence_ids'] = refs
                with self.assertRaises(ValueError):
                    report_mode.review_text(report, [K(1, 'file', 'app.py:2', 'return a - b')])
        report = self.report()
        report['Findings'][0]['text'] += ' [K99]'
        with self.assertRaisesRegex(ValueError, 'the engine adds headings, lists and citations'):
            report_mode.review_text(report, [K(1, 'file', 'app.py:2', 'return a - b')])

    def test_free_markdown_missing_sections_and_multiple_sentences_do_not_escape_the_contract(self):
        for report in ('# Findings\n4 check.', {}, {**self.report(), 'extra': []}):
            with self.subTest(report=report), self.assertRaises(ValueError):
                report_mode.review_text(report, [])
        for text in ('', '4 check. Other 7 check.', '4 check.\nOther check.', '4 check.\u2028# 7 check',
                     '# 4 check', '| 4 |', '```', '<!-- 4'):
            with self.subTest(text=text):
                report = self.report()
                report['Findings'][0]['text'] = text
                with self.assertRaises(ValueError):
                    report_mode.review_text(report, [K(1, 'file', 'app.py:2', 'return a - b')])

    def test_numeric_claim_without_a_reference_in_method_remains_red(self):
        report = self.report()
        report['Method'][0]['evidence_ids'] = []
        rendered = report_mode.review_text(report, [K(1, 'file', 'app.py:2', 'return a - b')])
        self.assertEqual([text for _, text in report_mode.unevidenced_numbers(rendered)], ['- Line 2 was read from the source.'])

    def test_section_case_preserves_output_and_ambiguous_duplicate_sections_are_rejected(self):
        evidence = [K(1, 'file', 'app.py:2', 'return a - b')]
        original = self.report()
        translated = {**original, 'method': original['Method']}
        translated.pop('Method')
        self.assertEqual(report_mode.review_text(translated, evidence), report_mode.review_text(original, evidence))
        with self.assertRaisesRegex(ValueError, 'same section'):
            report_mode.review_text({**original, 'method': original['Method']}, evidence)

    def test_abbreviation_dot_is_not_a_sentence_end_but_a_real_second_sentence_still_is(self):
        evidence = [K(1, 'file', 'app.py:2', 'return a - b')]
        for text in ('The environment is set up, e.g. ROLECATCH_DISTRIBUTION becomes local', 'Values remain fixed, e.g. `FEEDLOOM_DB` is temporary',
                     'Packages remain fixed, etc. Others are absent', 'Exporter checks hashes, e.g. Certifi is pinned'):
            with self.subTest(text=text):
                report = self.report()
                report['Findings'][0]['text'] = text
                self.assertIn(text, report_mode.review_text(report, evidence))
        report = self.report()
        report['Findings'][0]['text'] = 'The environment is set up. Then cwd changes'
        with self.assertRaisesRegex(ValueError, 'single sentence'):
            report_mode.review_text(report, evidence)

    def test_empty_uncertainties_are_allowed_but_findings_and_method_are_not(self):
        evidence = [K(1, 'file', 'app.py:2', 'return a - b')]
        rendered = report_mode.review_text({**self.report(), 'Uncertainties': []}, evidence)
        self.assertIn('# Uncertainties\n\n# Method', rendered)
        for section in ('Findings', 'Method'):
            with self.subTest(section=section), self.assertRaisesRegex(ValueError, section + ': at least one'):
                report_mode.review_text({**self.report(), section: []}, evidence)

    def test_all_format_errors_are_listed_instead_of_stopping_at_the_first(self):
        report = self.report()
        report['Findings'] = [{'text': 'First sentence. Second sentence', 'evidence_ids': ['K1']},
                              {'text': 'Unknown evidence', 'evidence_ids': ['K9']},
                              {'text': 'Finding without evidence', 'evidence_ids': []}]
        report['Method'] = []
        with self.assertRaises(report_mode.ReportFormatError) as error:
            report_mode.review_text(report, [K(1, 'file', 'app.py:2', 'return a - b')])
        self.assertEqual([h.split(':', 1)[0] for h in error.exception.errors],
                         ['Findings[1]', 'Findings[2]', 'Findings[3]', 'Method'])


class UnevidencedNumberTest(unittest.TestCase):
    def test_unevidenced_number_line_is_caught(self):
        lines = report_mode.unevidenced_numbers("# Heading 1\n\nIn the last 21 days 431 listings were rejected.\nSource count 55 [K3].\n")
        self.assertEqual([n for n, _ in lines], [3])

    def test_digits_that_are_not_numbers_do_not_count(self):
        text = "\n".join([
            "## 2. Funnel table",
            "1. Remove dead listings first.",
            "Audit performed at 24.09.2026 14:30; en-v30 rule, R1–R5 notes, G-R3 and B203.",
            "`make check` and `pytest -k test_2` output; eligibility.py:120 line; https://x.io/a1?b=2",
            "```", "total = 431", "```",
            "Priority 3: review the source list.",
            "| Stage | Input | Present |", "|---|---|---|", "| seniority | 120 | 80 [K1] |",
            "This ratio is unverified: approximately 30%.",
        ])
        self.assertEqual(report_mode.unevidenced_numbers(text), [])

    def test_citation_of_a_trimmed_sentence_is_on_the_next_line(self):
        """B227 (25.09 live R12-report): most of the 28 unevidenced-number findings were sentences whose citation fell to the next line
        in a wrapped paragraph. The unit is the sentence: a sentence without a citation is still caught, a sentence whose citation is on the next line is evidenced."""
        text = ("- The wrapper returns **rc=1** on EACCES and\n  this code is undefined [K-backup8]. It returns 0 for a missing source\n"
                 "  and does not report degraded (3) [K-backup9].\n\nThe first sentence has 5 errors. The second has 7 [K1].\n")
        self.assertEqual([n for n, _ in report_mode.unevidenced_numbers(text)], [5])

    def test_sentence_starting_with_a_backtick_is_audited_separately(self):
        self.assertEqual([n for n, _ in report_mode.unevidenced_numbers("The previous sentence yielded 2 results [K1].\n`make check` failed 3 times.\n")], [2])

    def test_range_citation_and_ticket_id(self):
        text = "Of 11 reserve findings, 6 are bugs [K-backup1..K-backup11].\nFirst five [K1–K5].\nScope OPS-001, BACKUP-STALE-001 and UTF-8.\n"
        self.assertEqual(report_mode.unevidenced_numbers(text), [])

    def test_hash_proposal_id_is_not_a_number(self):
        self.assertEqual(report_mode.unevidenced_numbers("- The #21/#22 claim was checked against the actual script steps.\n"), [])
        self.assertEqual([n for n, _ in report_mode.unevidenced_numbers("The #21 claim was verified in 3 steps.\n")], [1])

    def test_a_parenthesized_id_list_is_also_a_citation(self):
        """B233 (25.09 live R13-v2): the summary line '10 bugs (K4, K6), 3 risks (K5)' was counted as unevidenced; an id list in parentheses is a citation too."""
        self.assertEqual(report_mode.unevidenced_numbers("- **Total findings:** 10 bug (K4, K6, K-yd3), 3 design choices (K5), 3 test gaps (K-tz1..K-tz3).\n"), [])
        self.assertEqual(sorted(report_mode.ID.findall(" ".join(report_mode.REF.findall("10 bug (K4, K6) and [K9]")))), ['K4', 'K6', 'K9'])

    def test_a_bare_id_is_also_a_citation(self):
        """B235 (25.09 live R13-v3): the summary table '| Bug suspicion (6) | K-dn5 (trailing slash), K-dn6+K-dn27 (utm) |' was counted as unevidenced;
        an evidence id in a sentence or table row is a citation, square brackets are only one way of writing it."""
        text = ("| Class | Evidence |\n|---|---|\n| Suspected bugs (6) | K-dn5 (trailing slash), K-dn6+K-dn27 (utm) |\n\n"
                 "A total of 3 findings are supported by K1 and K2.\nThis line has 5 numbers but no id.\n")
        self.assertEqual([n for n, _ in report_mode.unevidenced_numbers(text)], [6])
        self.assertEqual(sorted(report_mode.ID.findall(" ".join(report_mode.REF.findall("K-dn5 (x), K-dn6+K-dn27 and [K9]")))), ['K-dn27', 'K-dn5', 'K-dn6', 'K9'])

    def test_prefixed_evidence_citation_proves_the_number(self):
        self.assertEqual(report_mode.unevidenced_numbers("Source count 55 [K-tz3].\nTwo findings 7 and 9 [K-tz1, K-backup2].\n"), [])

    def test_unevidenced_number_in_a_table_row_is_caught(self):
        self.assertEqual([n for n, _ in report_mode.unevidenced_numbers("| Stage | Input |\n|---|---|\n| language | 57 |\n")], [3])


class IdIntervalTest(unittest.TestCase):
    def test_id_range_is_not_a_number_claim_a_real_number_is_still_red(self):
        for text in ('3. RC-OSS-005 remains PROCEED. Full OSS-001\u2026004 acceptance is outside this slice.',
                      '- Full OSS-001..004 acceptance is outside this slice.', '- FIX-91...97 were applied locally.'):
            self.assertEqual(report_mode.unevidenced_numbers(text), [], text)
        for text in ('- OSS-001\u2026004 has 4 missing tests.', '- The script runs 12 steps.'):
            self.assertEqual(len(report_mode.unevidenced_numbers(text)), 1, text)


class EvidenceSkillTest(unittest.TestCase):
    """25.09 live R12/R13: models wrote the evidence appendix wrong again and again (a finding class in the type field, multiple sources, prose 'expected').
    The skill file describes the same vocabulary as the code; it is bound here so the skill has to change when the code changes."""
    def test_skill_describes_the_evidence_vocabulary_the_same_as_the_code(self):
        path = Path(__file__).resolve().parents[2] / 'config/skills/report-evidence/SKILL.md'
        self.assertTrue(path.is_file(), path)
        text = path.read_text()
        for round in report_mode.EVIDENCE_KINDS:
            self.assertIn(f'`{round}`', text, round)
        for sample in ('K1', 'K-tz1', 'K-line-1', 'uv run', 'path:line', 'unverified', 'cannot cite itself'):
            self.assertIn(sample, text, sample)


class CommandSandboxTest(unittest.TestCase):
    """B232 (25.09 live R13-v2): `uv run` could not write to `~/.cache/uv` in the sandbox, so 5/5 command evidences failed with 'rc=2'.
    The sandbox environment redirects the uv cache to a temporary directory (writable) and runs without network."""
    def test_command_output_is_cut_at_the_cap_and_does_not_stream_unbounded_to_disk(self):
        """D18 (01.10 Codex): the 1 MB limit only cut the returned text; the output was first written unbounded to a temporary file."""
        t = Path(tempfile.mkdtemp(prefix='scratch-'))
        self.addCleanup(shutil.rmtree, t, True)
        wt = t / 'wt'; wt.mkdir()
        rc, output = report_mode.command_execute('head -c 3000000 /dev/zero | tr "\\0" x; echo', wt, t / 'temporary', 60)
        self.assertEqual((rc, len(output)), (0, report_mode.COMMAND_OUTPUT_CAP))
        self.assertLess(sum(f.stat().st_size for f in (t / 'temporary').rglob('*') if f.is_file()), 100_000)

    def test_uv_cache_is_in_the_temporary_directory_and_writable(self):
        t = Path(tempfile.mkdtemp(prefix='scratch-'))
        self.addCleanup(shutil.rmtree, t, True)
        wt = t / 'wt'; wt.mkdir()
        rc, output = report_mode.command_execute('echo "$UV_CACHE_DIR|$UV_OFFLINE"; mkdir -p "$UV_CACHE_DIR" && touch "$UV_CACHE_DIR/attempt" && echo written', wt, t / 'temporary', 30)
        self.assertEqual(rc, 0, output)
        self.assertIn(f'{(t / "temporary").resolve()}/', output.split('|')[0], 'the uv cache is under the temporary directory')
        self.assertIn('|1', output, 'UV_OFFLINE=1')
        self.assertIn('written', output)

    def test_background_stdout_does_not_hold_up_a_finished_command(self):
        with tempfile.TemporaryDirectory(prefix='scratch-pipe-') as d:
            t = Path(d)
            started = time.monotonic()
            rc, output = report_mode.command_execute('sleep 6 & echo "$!"; printf "done\\n"', t, t / 'temporary', 1)
            elapsed = time.monotonic() - started
            self.assertEqual(rc, 0, output)
            self.assertIn('done', output)
            self.assertLess(elapsed, 2, 'a completed command’s background child must not delay stdout closure')
            self.assert_process_stopped(int(output.splitlines()[0]))

    def assert_process_stopped(self, pid):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(.02)
        os.kill(pid, signal.SIGKILL)
        self.fail('an evidence-command child remained alive after exit')

    def test_command_timeout_still_stops_the_process_group(self):
        with tempfile.TemporaryDirectory(prefix='scratch-duration-') as d:
            t = Path(d)
            started = time.monotonic()
            rc, output = report_mode.command_execute('sleep 6 & echo "$!" > child.pid; wait', t, t / 'temporary', .3)
            self.assertIsNone(rc)
            self.assertIn('time limit exceeded', output)
            self.assertLess(time.monotonic() - started, 2)
            self.assert_process_stopped(int((t / 'child.pid').read_text()))

    def test_cancellation_still_propagates_and_stops_the_process_group(self):
        with tempfile.TemporaryDirectory(prefix='scratch-cancel-') as d:
            t = Path(d)
            started = time.monotonic()
            with patch.object(core, 'cancel_requested', side_effect=lambda p: (t / 'child.pid').exists()):
                with self.assertRaises(core.CancelledRun):
                    report_mode.command_execute('sleep 6 & echo "$!" > child.pid; wait', t, t / 'temporary', 10,
                                   wait=lambda p, duration: core.wait_process(p, duration, 'fixture'))
            self.assertLess(time.monotonic() - started, 2)
            self.assert_process_stopped(int((t / 'child.pid').read_text()))


class ReportQaTest(unittest.TestCase):
    """G-R3/G-R4: deterministic report QA in a real git repository and the real sandbox."""
    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix='report-qa-'))
        self.addCleanup(shutil.rmtree, self.t, True)
        self.wt, self.sha = repo(self.t)
        self.declaration = report_mode.declaration(REPORT_PROMPT)

    def qa(self, declaration=None):
        return report_mode.report_qa(str(self.wt), declaration or self.declaration, self.sha, str(self.t / 'temporary'), seed='constant')

    def kinds(self, outcome):
        return sorted({b.split(':', 1)[0] for b in outcome['findings']})

    def test_evidenced_report_passes(self):
        evidence_write(self.wt, VALID_REPORT, VALID_EVIDENCE)
        outcome = self.qa()
        self.assertEqual((outcome['status'], outcome['findings']), ('PASS', []))

    def test_review_file_evidence_outside_the_accepted_read_scope_is_rejected_before_it_is_read(self):
        other = self.wt / 'other.py'
        other.write_text('outside = 42\n')
        git(self.wt, 'add', 'other.py')
        git(self.wt, 'commit', '-qm', 'extra public source')
        self.sha = git(self.wt, 'rev-parse', 'HEAD').strip()
        evidence = [VALID_EVIDENCE[0], K(2, 'file', 'other.py:1', 'outside = 42')]
        evidence_write(self.wt, '# Summary\nSource differs [K1][K2].\n', evidence)
        original = report_mode._file_evidence
        with patch.object(report_mode, '_file_evidence', wraps=original) as check:
            result = report_mode.report_qa(self.wt, self.declaration, self.sha, self.t / 'scope-check',
                                    source_paths=[self.wt / 'app.py'])
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(self.kinds(result), ['file-scope'])
        self.assertEqual([call.args[1]['id'] for call in check.call_args_list], ['K1'])
        self.assertEqual(self.qa()['status'], 'PASS', 'unrestricted report jobs keep their existing behavior')
        broad = report_mode.report_qa(self.wt, self.declaration, self.sha, self.t / 'dir-check', source_paths=[self.wt])
        self.assertEqual(broad['status'], 'PASS', 'an explicitly selected directory includes its files')

    def test_external_source_and_query_evidence_requires_a_date_the_report_writes_the_data_date(self):
        """CR-30 / N-REP01: data of unknown freshness cannot be presented as definite fact; if a url/query is undated, the report has no data date
        line or the date is in the future, red; dated evidence + a 'Data date' line pass."""
        url = {**K(3, 'url', 'https://example.org/data', claim='outside source')}
        query = K(4, 'query', "python3 -c 'print(42)'", '42')
        evidence_write(self.wt, VALID_REPORT + "The external source returns 42 records [K3, K4].\n", VALID_EVIDENCE + [url, query])
        outcome = self.qa()
        self.assertEqual(outcome['status'], 'FAIL')
        self.assertEqual(sorted(b.split(':', 1)[0] for b in outcome['findings']), ['source-date', 'source-date', 'without-data-date'])
        evidence_write(self.wt, VALID_REPORT + "The external source returns 42 records [K3, K4].\n\nData date: 2026-10-14\n",
                  VALID_EVIDENCE + [{**url, 'date': '2999-01-01'}, {**query, 'date': '14.10.2026'}])
        outcome = self.qa()
        self.assertEqual(sorted(b.split(':', 1)[0] for b in outcome['findings']), ['source-date', 'source-date'])
        self.assertTrue(any('future' in b for b in outcome['findings']) and any('YYYY-MM-DD' in b for b in outcome['findings']))
        evidence_write(self.wt, VALID_REPORT + "The external source returns 42 records [K3, K4].\n\nData date: 2026-10-14\n",
                  VALID_EVIDENCE + [{**url, 'date': '2026-10-14'}, {**query, 'date': '2026-10-14'}])
        outcome = self.qa()
        self.assertEqual((outcome['status'], outcome['findings']), ('PASS', []))

    def test_different_expected_for_the_same_source_is_contradicting_evidence(self):
        """CR-30 / N-REP01: contradictory data is explained in the report; the same source cannot silently pass with two different expected values."""
        evidence_write(self.wt, VALID_REPORT + "The query reports 42 and 43 [K5][K6].\n\nData date: 2026-10-14\n",
                  VALID_EVIDENCE + [{**K(5, 'query', "python3 -c 'print(42)'", '42'), 'date': '2026-10-14'},
                                   {**K(6, 'query', "python3 -c 'print(42)'", '43'), 'date': '2026-10-14'}])
        outcome = self.qa()
        self.assertEqual(outcome['status'], 'FAIL')
        self.assertTrue(any(b.startswith('contradicting-evidence: K5, K6') for b in outcome['findings']), outcome['findings'])
        self.assertNotIn('contradicting-evidence', ' '.join(b.split(':')[0] for b in self.qa(self.declaration)['findings'] if 'K1' in b))
        evidence_write(self.wt, VALID_REPORT + "The query returns 42 [K7]; the same query prints one line [K8].\n\nData date: 2026-10-14\n",
                  VALID_EVIDENCE + [{**K(7, 'query', "python3 -c 'print(42)'", '42', claim='value 42'), 'date': '2026-10-14'},
                                   {**K(8, 'query', "python3 -c 'print(42)'", 'single line', claim='single line output'), 'date': '2026-10-14'}])
        findings = self.qa()['findings']
        self.assertFalse(any(b.startswith('contradicting-evidence') for b in findings), ('FIX-20: different claims from the same source do not contradict', findings))

    def test_red_if_there_is_no_report_or_evidence_appendix(self):
        self.assertEqual(self.kinds(self.qa()), ['no-report', 'without-evidence'])
        (self.wt / 'report_mode.md').write_text(VALID_REPORT)
        self.assertEqual(self.kinds(self.qa()), ['unknown-evidence', 'without-evidence'])

    def test_unevidenced_number_and_unknown_id_are_red(self):
        evidence_write(self.wt, VALID_REPORT + "Total 431 listings rejected.\nSource 55 [K9].\n", VALID_EVIDENCE)
        outcome = self.qa()
        self.assertEqual((outcome['status'], self.kinds(outcome)), ('FAIL', ['unknown-evidence', 'unsupported-number']))
        self.assertTrue(any('report_mode.md:3' in b for b in outcome['findings']), outcome['findings'])

    def test_prefixed_command_evidence_does_not_crash_in_sampling(self):
        """B229 (25.09 live R13): B224 made the prefixed id valid but the command sample sorted ids with int(id[1:]);
        'K-u1' crashed QA with a ValueError on every attempt, and the correction round could not repair it."""
        evidence_write(self.wt, "# Summary\n`sum` returns 0 for 2 and 2 [K-u1].\n",
                  [{'id': 'K-u1', 'claim': 'sum', 'round': 'command', 'source': "python3 -c 'from app import sum; print(sum(2, 2))'", 'expected': '0'},
                   {'id': 'K2', 'claim': 'code', 'round': 'file', 'source': 'app.py:2', 'expected': 'return a - b'}])
        outcome = self.qa()
        self.assertEqual((outcome['status'], outcome['findings']), ('PASS', []))

    def test_report_or_evidence_appendix_cannot_prove_itself(self):
        """B228 (25.09 live R12-report): the correction round based the numbers on the evidence appendix itself (K22) and on the report heading (K23);
        since the file and the quote existed QA passed it. Self-citing evidence proves no claim."""
        evidence_write(self.wt, VALID_REPORT + "Total 3 evidence present [K3].\n",
                  VALID_EVIDENCE + [K(3, 'file', 'report_mode.evidence.jsonl', '"id"'), K(4, 'file', 'report_mode.md:1', 'Summary')])
        outcome = self.qa()
        self.assertEqual(outcome['status'], 'FAIL')
        self.assertEqual(sorted(b.split(':', 2)[1].strip() for b in outcome['findings'] if b.startswith('own-citation')), ['K3', 'K4'])

    def test_self_citation_cannot_be_evaded_with_an_absolute_path_or_a_link(self):
        """B261 (29.09 external audit): self-citation only caught the relative path; the same report's absolute path and its symlink
        passed as 'file' evidence ("999 tests passed" could be proven with its own report)."""
        absolute = str((self.wt / 'report_mode.md').resolve())
        (self.wt / 'copy.md').symlink_to('report_mode.md')
        evidence_write(self.wt, VALID_REPORT + "999 tests passed [K3][K4].\n",
                  VALID_EVIDENCE + [K(3, 'file', absolute + ':3', '999 tests passed'), K(4, 'file', 'copy.md:3', '999 tests passed')])
        outcome = self.qa()
        self.assertEqual(outcome['status'], 'FAIL')
        self.assertEqual(sorted(b.split(':', 2)[1].strip() for b in outcome['findings'] if b.startswith('own-citation')), ['K3', 'K4'])

    def test_file_evidence_cannot_be_outside_the_tree_or_an_untracked_file(self):
        """B267 (29.09 bug hunt, B261 class): a file the writer wrote itself became evidence — a file OUTSIDE the working tree,
        a file git ignores (.git/info/exclude) and a cache directory (__pycache__) did not enter the outside-the-report check."""
        outside = self.t / 'outside.txt'
        outside.write_text("999 test passed\n")
        (self.wt / 'notes.log').write_text("999 test passed\n")
        (self.wt / '.git/info').mkdir(parents=True, exist_ok=True)
        (self.wt / '.git/info/exclude').write_text("notes.log\n")
        (self.wt / '__pycache__').mkdir()
        (self.wt / '__pycache__/outcome.txt').write_text("999 test passed\n")
        evidence_write(self.wt, VALID_REPORT + "999 tests passed [K3][K4][K5].\n",
                  VALID_EVIDENCE + [K(3, 'file', str(outside) + ':1', '999 test passed'), K(4, 'file', 'notes.log:1', '999 test passed'),
                                   K(5, 'file', '__pycache__/outcome.txt:1', '999 test passed')])
        outcome = self.qa()
        self.assertEqual((outcome['status'], self.kinds(outcome)), ('FAIL', ['file-source']), outcome['findings'])
        self.assertEqual(sorted(b.split(':', 2)[1].strip() for b in outcome['findings']), ['K3', 'K4', 'K5'])

    def test_unaudited_claim_is_red_when_the_bug_cap_is_exceeded(self):
        """B262 (29.09 external audit): bug claims after the cap were silently not run; in 11 recorded reports a fake record that
        was never run got PASS. Exceeding the cap is a finding and counts the unchecked ids."""
        self.assertEqual(report_mode.BUG_CAP, 10)
        real = [K(i, 'bug', "python3 -c 'from app import sum; assert sum(2, 2) == 4'", 'AssertionError') for i in (3, 4)]
        fake = K(5, 'bug', 'echo AssertionError', 'AssertionError')
        evidence_write(self.wt, VALID_REPORT + "Bugs [K3][K4][K5].\n", [*VALID_EVIDENCE, *real, fake])
        with patch.dict(os.environ, {'CADRE_REPORT_BUG_CAP': '3'}):
            outcome = self.qa()
        self.assertEqual((outcome['status'], self.kinds(outcome)), ('FAIL', ['bug-cap']), outcome['findings'])
        self.assertTrue(any('K5' in b and 'K4' not in b for b in outcome['findings']), outcome['findings'])

    def test_prefixed_unknown_id_does_not_crash(self):
        """A leak of the B224 addition: unknown ids were sorted with int(x[1:]); 'K-tz9' would crash QA with a ValueError."""
        evidence_write(self.wt, VALID_REPORT + "Source 55 [K-tz9, K12].\n", VALID_EVIDENCE)
        outcome = self.qa()
        self.assertEqual(outcome['status'], 'FAIL')
        self.assertTrue(any(b.startswith('unknown-evidence: K12, K-tz9') for b in outcome['findings']), outcome['findings'])

    def test_nonexistent_file_line_and_a_non_matching_quote_are_red(self):
        evidence_write(self.wt, VALID_REPORT + "Additional evidence [K3][K4][K5].\n",
                  [*VALID_EVIDENCE, K(3, 'file', 'app.py:99', 'return'), K(4, 'file', 'absent.py:1', 'x'),
                   K(5, 'file', 'app.py:2', 'return a + b')])
        outcome = self.qa()
        self.assertEqual(sorted(b.split(':')[1].strip() for b in outcome['findings']), ['K3', 'K4', 'K5'], outcome['findings'])
        self.assertEqual(self.kinds(outcome), ['file-source'])

    def test_evidence_limit_in_the_request_is_parsed_mechanically(self):
        self.assertEqual(report_mode.evidence_limit('Use at most 5 direct file:line citations.', 'at most 5 file:line evidence references; source remains unchanged'), 5)
        self.assertEqual(report_mode.evidence_limit('at most 3 file:line evidence references', 'At most 7 file:line citations'), 3)
        self.assertIsNone(report_mode.evidence_limit('Provide brief Findings and an evidence appendix.', ''))
        self.assertIsNone(report_mode.evidence_limit('Run at most 5 commands.'))

    def test_evidence_limit_at_five_passes_five_six_is_rejected_an_unlimited_request_is_unchanged(self):
        def write(count):
            evidence = [K(i, 'file', 'app.py:2', 'return a - b') for i in range(1, count + 1)]
            evidence_write(self.wt, "# Summary\n`sum` performs subtraction " + ''.join(f'[K{i}]' for i in range(1, count + 1)) + ".\n", evidence)
        qa = lambda limit: report_mode.report_qa(str(self.wt), self.declaration, self.sha, str(self.t / 'temporary'), seed='constant', evidence_limit=limit)
        write(5)
        self.assertEqual(qa(5)['findings'], [], 'five evidence references pass at the limit')
        write(6)
        outcome = qa(5)
        self.assertEqual((outcome['status'], self.kinds(outcome)), ('FAIL', ['evidence-limit']), outcome['findings'])
        self.assertIn('at most 5 file:line references', outcome['findings'][0])
        write(10)
        self.assertEqual(qa(None)['findings'], [], 'behavior is preserved when no limit is specified')

    def test_shifting_line_number_stays_red_and_names_the_real_line(self):
        evidence_write(self.wt, VALID_REPORT + "Difference [K3].\n", [*VALID_EVIDENCE, K(3, 'file', 'app.py:1', 'return a - b')])
        outcome = self.qa()
        self.assertEqual((outcome['status'], self.kinds(outcome)), ('FAIL', ['file-source']), outcome['findings'])
        finding = next(b for b in outcome['findings'] if b.startswith('file-source: K3:'))
        self.assertIn('quote found on file line 2', finding)
        evidence_write(self.wt, VALID_REPORT + "Absent [K3].\n", [*VALID_EVIDENCE, K(3, 'file', 'app.py:1', 'a line that does not exist')])
        finding = next(b for b in self.qa()['findings'] if b.startswith('file-source: K3:'))
        self.assertNotIn('file line', finding)

    def test_review_writer_prompt_asks_for_real_line_numbers_and_names_no_tool(self):
        import acceptance
        self.assertIn('nl -ba', acceptance.WRITER_EVIDENCE_RULE)
        self.assertIn('cat -n', acceptance.WRITER_EVIDENCE_RULE)
        self.assertIn('including Method and Uncertainties', acceptance.WRITER_EVIDENCE_RULE)
        self.assertIn('rg --files', acceptance.WRITER_EVIDENCE_RULE)
        source = Path(acceptance.__file__).read_text()
        self.assertIn('+ WRITER_EVIDENCE_RULE +', source)
        self.assertNotIn('in the report-evidence schema', source)

    def test_lineless_file_source_is_searched_in_the_whole_file(self):
        evidence_write(self.wt, VALID_REPORT + "Definition [K3].\n", [*VALID_EVIDENCE, K(3, 'file', 'app.py', 'def sum(a, b):')])
        self.assertEqual(self.qa()['findings'], [])

    def test_sensitive_path_is_not_read(self):
        evidence_write(self.wt, VALID_REPORT + "Key [K3].\n", [*VALID_EVIDENCE, K(3, 'file', '~/.ssh/id_rsa:1', 'BEGIN')])
        outcome = self.qa()
        self.assertEqual(outcome['status'], 'FAIL')
        self.assertTrue(any('K3' in b and 'sensitive' in b for b in outcome['findings']), outcome['findings'])

    def test_non_matching_command_is_red_matching_one_passes(self):
        evidence_write(self.wt, VALID_REPORT + "One return present [K3].\n", [*VALID_EVIDENCE, K(3, 'command', 'grep -c return app.py', '1')])
        self.assertEqual(self.qa()['findings'], [])
        evidence_write(self.wt, VALID_REPORT + "Seven returns present [K3].\n", [*VALID_EVIDENCE, K(3, 'command', 'grep -c return app.py', '7')])
        self.assertEqual(self.kinds(self.qa()), ['command-source'])

    def test_forbidden_executed_evidence_is_rejected_before_any_command_runs(self):
        command = K(3, 'command', 'touch SIDE_EFFECT; echo 1', '1')
        bug = K(4, 'bug', "python3 -c 'raise SystemExit(1)'", 'SystemExit')
        evidence_write(self.wt, VALID_REPORT + "One return present [K3][K4].\n", [*VALID_EVIDENCE, command, bug])
        forbidden = report_mode.evidence_kind_bans('At most 5 file:line evidence references are used, no command evidence')
        with patch.object(report_mode, 'command_execute', side_effect=AssertionError('forbidden evidence must not run')):
            outcome = report_mode.report_qa(str(self.wt), self.declaration, self.sha, str(self.t / 'temporary'), seed='constant', forbidden_kinds=forbidden)
        self.assertEqual(outcome['status'], 'FAIL')
        self.assertIn('evidence-kind-forbidden', self.kinds(outcome))
        self.assertIn('K3, K4', next(b for b in outcome['findings'] if b.startswith('evidence-kind-forbidden')))
        self.assertFalse((self.wt / 'SIDE_EFFECT').exists())
        self.assertEqual(report_mode.evidence_kind_bans('command evidence only if needed; reading tools are allowed'), frozenset())
        self.assertEqual(report_mode.evidence_kind_bans('No command evidence, new code, tests, scripts, installation or network requests.'),
                         report_mode.EXECUTED_EVIDENCE_KINDS)

    def test_bug_claim_requires_a_red_test(self):
        """G-R4: if the bug is real the reproduction command ends RED on the current code; if green the bug claim falls."""
        green = K(3, 'bug', "python3 -c 'from app import sum; assert sum(2, 2) == 0'", 'AssertionError')
        fake = K(4, 'bug', 'echo AssertionError', 'AssertionError')   # a "reproduction" that prints the expected text but ends green
        evidence_write(self.wt, VALID_REPORT + "Second bug [K3][K4].\n", [*VALID_EVIDENCE, green, fake])
        outcome = self.qa()
        self.assertEqual(self.kinds(outcome), ['bug-again'])
        self.assertTrue(any('K3' in b for b in outcome['findings']))
        self.assertTrue(any('K4' in b and 'rc=0' in b for b in outcome['findings']), outcome['findings'])

    def test_non_report_file_change_is_red(self):
        """G-R1 outside-the-report; with B267 a file changed during the job (app.py, K1's source) cannot be evidence either (file-source)."""
        evidence_write(self.wt, VALID_REPORT, VALID_EVIDENCE)
        (self.wt / 'app.py').write_text(APPLICATION + "# changed while writing the report\n")
        (self.wt / 'not.txt').write_text('draft\n')
        outcome = self.qa()
        self.assertEqual((outcome['status'], self.kinds(outcome)), ('FAIL', ['file-source', 'report-outside']))
        self.assertTrue(any(b.startswith('file-source: K1:') and 'changed during the task' in b for b in outcome['findings']), outcome['findings'])
        outside = next(b for b in outcome['findings'] if b.startswith('report-outside'))
        self.assertIn('app.py', outside)
        self.assertIn('not.txt', outside)

    def test_in_review_mode_a_bound_dirty_source_is_evidence_a_later_changed_one_is_not(self):
        from run_store import plan_product
        (self.wt / 'app.py').write_text(APPLICATION + "CARRIED = 'uncommitted'\n")
        start = plan_product(self.wt)
        declaration = {'path': 'REVIEW.md', 'evidence': 'REVIEW.evidence.jsonl', 'sections': ['Findings']}
        texts = ("# Findings\nThe carried line is in the bound source [K1].\n",
                    json.dumps(K(1, 'file', 'app.py:3', "CARRIED = 'uncommitted'"), ensure_ascii=False))
        execute = lambda: report_mode.report_qa(str(self.wt), declaration, self.sha, str(self.t / 'temporary'), seed='constant', texts=texts,
                                     start=start, source_read_only=True)
        self.assertEqual((execute()['status'], execute()['findings']), ('PASS', []), 'the bound dirty state does not count as a change during the task')
        (self.wt / 'app.py').write_text(APPLICATION + "CARRIED = 'uncommitted'\n# changed during the task\n")
        outcome = execute()
        self.assertEqual((outcome['status'], self.kinds(outcome)), ('FAIL', ['file-source', 'report-outside']), outcome['findings'])
        self.assertTrue(any(b.startswith('file-source: K1:') and 'changed during the task' in b for b in outcome['findings']), outcome['findings'])

    def test_red_if_the_requested_section_is_missing(self):
        evidence_write(self.wt, VALID_REPORT, VALID_EVIDENCE)
        outcome = self.qa({**self.declaration, 'sections': ['Summary', 'Bugs']})
        self.assertEqual([b for b in outcome['findings']], ['section-missing: Bugs'])

    def test_evidence_command_cannot_write_outside_the_worktree(self):
        target = self.t / 'outside.txt'
        evidence_write(self.wt, VALID_REPORT + "Attempt [K3].\n", [*VALID_EVIDENCE, K(3, 'command', f'echo x > {target}; echo complete', 'complete')])
        self.qa()
        self.assertFalse(target.exists(), 'the evidence command wrote outside the read-only sandbox')

    def test_command_sample_is_bounded_and_stable(self):
        evidence = [*VALID_EVIDENCE] + [K(i, 'command', 'echo absent', f'expected-{i}') for i in range(3, 7)]
        evidence_write(self.wt, VALID_REPORT + "Four commands [K3][K4][K5][K6].\n", evidence)
        with patch.dict(os.environ, {'CADRE_REPORT_COMMAND_SAMPLE': '2'}):
            a, b = self.qa(), self.qa()
        self.assertEqual(len(a['findings']), 2, a['findings'])
        self.assertEqual(a['findings'], b['findings'], 'the same product sees the same sample')


class PollTest(unittest.TestCase):
    """G-R5: the judge picks a stratified sample from the evidence appendix and verifies it from its source; the harness applies the verdict mechanically."""
    REV = {'head': 'h', 'digest': 'd' * 64}

    def review(self, poll, verdict='PASS'):
        return json.dumps({'revision': self.REV['digest'], 'verdict': verdict, 'poll': poll,
                           'acceptance': [{'id': 'main', 'status': 'PASS', 'evidence': 'report read and evidence probed'}]})

    def apply(self, raw, ids=('K1', 'K2', 'K3', 'K4', 'K5', 'K6')):
        return report_mode.poll_apply(raw, parse_review(raw, self.REV, ['main']), list(ids))

    def y(self, i, s='verified'):
        return {'id': i, 'outcome': s, 'evidence': f'{i} read from its source'}

    def test_five_verified_polls_do_not_break_acceptance(self):
        self.assertEqual(self.apply(self.review([self.y(f'K{i}') for i in range(1, 6)]))['status'], 'PASS')

    def test_a_single_wrong_one_produces_revise(self):
        r = self.apply(self.review([self.y('K1', 'wrong')] + [self.y(f'K{i}') for i in range(2, 6)]))
        self.assertEqual((r['status'], r['verdict']), ('FAIL', 'REVISE'))
        self.assertIn('K1', next(a['evidence'] for a in r['acceptance'] if a['id'] == 'report_poll'))

    def test_revise_if_the_unverifiable_ratio_exceeds_the_threshold(self):
        endpoint = [self.y(f'K{i}', 'unverifiable') for i in range(1, 4)] + [self.y('K4'), self.y('K5')]
        two = [self.y(f'K{i}', 'unverifiable') for i in range(1, 3)] + [self.y(f'K{i}') for i in range(3, 6)]
        self.assertEqual(self.apply(self.review(endpoint))['status'], 'FAIL')
        self.assertEqual(self.apply(self.review(two))['status'], 'PASS')

    def test_missing_or_made_up_poll_is_an_invalid_review(self):
        four = [self.y(f'K{i}') for i in range(1, 5)]
        for entry_name, poll in (('absent', []), ('least', four), ('made_up id', [self.y('K99')] + four),
                            ('invalid outcome', [{'id': 'K5', 'outcome': 'maybe', 'evidence': 'x'}] + four),
                            ('repeat', [self.y('K1')] + four)):
            with self.subTest(entry_name=entry_name):
                self.assertEqual(self.apply(self.review(poll))['status'], 'ERROR')

    def test_in_a_low_evidence_report_the_threshold_is_the_evidence_count(self):
        self.assertEqual(self.apply(self.review([self.y('K1'), self.y('K2')]), ids=('K1', 'K2'))['status'], 'PASS')

    def test_line_format_poll(self):
        raw = (f"REVISION: {self.REV['digest']}\nDECISION: PASS\nCRITERION main: PASS - report read\n"
               + "".join(f"POLL K{i}: verified - app.py:{i} read\n" for i in range(1, 6)))
        self.assertEqual(self.apply(raw)['status'], 'PASS')
        self.assertEqual(self.apply(raw.lower())['status'], 'PASS')
        for original, replacement in (('K1', '\u212a1'), ('verified', 'ver\u0131fied')):
            with self.subTest(keyword=original, replacement=ascii(replacement)):
                invalid = raw.replace(original, replacement)
                self.assertEqual(self.apply(invalid)['status'], 'ERROR')
                self.assertNotIn('K1', [p['id'] for p in report_mode._poll_find(invalid, self.REV['digest'])])

    def test_line_format_prefixed_id(self):
        ids = ('K-tz1', 'K-tz2', 'K-backup1', 'K-backup2', 'K-backup3')
        raw = (f"REVISION: {self.REV['digest']}\nDECISION: PASS\nCRITERION main: PASS - report read\n"
               + "".join(f"POLL {i}: verified - source read\n" for i in ids))
        self.assertEqual(self.apply(raw, ids=ids)['status'], 'PASS')

    def test_no_poll_is_sought_in_a_non_report_prompt(self):
        raw = self.review([])
        r = parse_review(raw, self.REV, ['main'])
        self.assertEqual(report_mode.poll_apply(raw, r, None), r)


ATTEMPT11_REPORT = {
    'Findings': [
        {'text': 'release_smoke.py calls collect_source with Adapter and Transport test doubles', 'evidence_ids': ['K1']},
        {'text': 'release_smoke.py runs evaluation through process_observed_items', 'evidence_ids': ['K2']},
        {'text': 'release_smoke.py verifies dashboard /items, /items/{id} and /static/app.js rendering with TestClient',
         'evidence_ids': ['K3']},
        {'text': "release_smoke.py backs up the canonical and dashboard SQLite stores", 'evidence_ids': ['K4']},
        {'text': "The card’s claim that it tests more than migrations is correct; the script also includes collect, evaluate, dashboard, "
                 "static, backup/restore, reopen and NoBrowser/DNS steps", 'evidence_ids': ['K1', 'K2', 'K3', 'K4']},
        {'text': 'test_release_smoke_client.py only tests export_client from export_smoke_client for exporting packages from a lock file '
                 'and does not assert the smoke steps in release_smoke.py', 'evidence_ids': ['K5']},
        {'text': 'release_smoke.py contains no cross-version update or rollback logic; backup and restore operate on same-version '
                 'databases', 'evidence_ids': ['K4']},
        {'text': "The RC-OSS-005 criterion requiring the existing lock to resolve in a clean environment is not verified in release_smoke.py; "
                 'this step is tested only through test_release_smoke_client.py and export_smoke_client.py',
         'evidence_ids': ['K5']}],
    'Uncertainties': [
        {'text': 'Real clean install, update and rollback cannot be verified in this read-only slice; RC-OSS-005 remains PROCEED', 'evidence_ids': []},
        {'text': 'The internals of export_smoke_client.py were not read; lock resolution logic was not examined', 'evidence_ids': []}],
    'Method': [{'text': 'Read-only code analysis; only the three specified files were read, no other sources were read', 'evidence_ids': []}]}
ATTEMPT11_EVIDENCE = [K(1, 'file', 'scripts/release_smoke.py:200-211', 'result = collect_source('),
                  K(2, 'file', 'scripts/release_smoke.py:232-239', 'outcome = process_observed_items(database, profile=profile)'),
                  K(3, 'file', 'scripts/release_smoke.py:254-269', 'with TestClient(create_app()) as test_client:'),
                  K(4, 'file', 'scripts/release_smoke.py:270-277', 'live.backup(saved)'),
                  K(5, 'file', 'tests/unit/test_release_smoke_client.py:11-23', 'result = export_client(ROOT / "uv.lock")')]
ATTEMPT11_PLAN = {'tasks': [
    {'id': 'g1', 'acceptance': 'A list with at least one file:line reference for every step. Environment isolation, DNS/browser blocking, '
                          'collect/evaluate/dashboard/static, migration checksum, backup/restore and reopen steps are evidenced. '
                          'The absence of update or rollback logic in the script is evidenced.'},
    {'id': 'g2', 'acceptance': 'List the behavior actually asserted by each test: package export from a lock file, hash presence, '
                          'no playwright, and ValueError for missing/duplicate/no-wheel/bad-hash cases. Establish that the test '
                          'does not assert the smoke steps within it.'}]}
ATTEMPT11_ACCEPTANCE = ('Only the specified public script/test/card slice; at most 5 references for release_smoke steps and test assertion coverage '
                  'file:line evidence; source remains unchanged; no install/rollback PASS, release acceptance or card closure claims')
ATTEMPT11_CARD = ('#### [ ] RC-OSS-005 — Clean install/update/rollback quality acceptance\n'
                 '**Acceptance:** fixture collect/evaluate/dashboard; update and planned rollback with the same state; migration checksum; '
                 'the existing lock resolves in a clean environment.\n')


def attempt11_source(root: Path) -> Path:
    wt = root / 'wt'
    (wt / 'scripts').mkdir(parents=True)
    (wt / 'tests/unit').mkdir(parents=True)
    smoke = {n: f'x_{n} = {n}' for n in range(1, 306)}
    smoke.update({
        1: '"""Exercise a distribution with synthetic state and no browser or network."""',
        27: 'class NoBrowser(importlib.abc.MetaPathFinder):',
        29: '        _require(not name.startswith("playwright"), "Browser import attempted")',
        39: 'def _no_dns(*args):',
        40: '    raise OSError("DNS disabled during release smoke")',
        53: 'os.environ["FEEDLOOM_STATE_DIR"] = str(state)',
        54: 'os.chdir(workdir)',
        137: '_require(initialize(database) == ("0001",), "Fresh initialization returned wrong migrations")',
        138: '_require(initialize(database) == (), "Second initialization was not empty")',
        141: 'class Adapter:',
        174: 'class Transport:',
        200: 'result = collect_source(',
        201: '    database, adapter=Adapter(), transport=Transport(),',
        202: ')',
        232: 'outcome = process_observed_items(database, profile=profile)',
        233: '_require(not outcome.failures, "Installed evaluation failed")',
        254: 'with TestClient(create_app()) as test_client:',
        255: '    _require(test_client.get("/items").status_code == 200, "Dashboard failed")',
        256: '    _require(test_client.get(f"/items/{identifier}").status_code == 200, "Dashboard detail failed")',
        257: '    _require(test_client.get("/static/app.js").status_code == 200, "Static asset missing")',
        270: 'for name in ("canonical", "dashboard"):',
        271: '    with sqlite3.connect(state / name) as live, sqlite3.connect(backup / name) as saved:',
        272: '        live.backup(saved)',
        278: 'reopened = open_database(state)',
        279: '_require(reopened.version == version, "Same-version reopen failed")',
        283: 'restored = workdir / "restored"',
        284: 'restore_copy(backup, restored)',
    })
    (wt / 'scripts/release_smoke.py').write_text(''.join(smoke[n] + '\n' for n in range(1, 306)))
    test = {n: f'y_{n} = {n}' for n in range(1, 51)}
    test.update({
        11: 'export_client = cast(',
        13: '    runpy.run_path(str(ROOT / "scripts/export_smoke_client.py"))["export_client"],',
        17: 'def test_smoke_client_is_exported_from_lock_with_hashes() -> None:',
        18: '    result = export_client(ROOT / "uv.lock")',
        19: '    assert all(" --hash=sha256:" in line for line in result.splitlines())',
        20: '    assert "playwright" not in result',
        26: '@pytest.mark.parametrize("broken", ["missing", "duplicate", "no-wheel", "bad-hash"])',
        28: '    with pytest.raises(ValueError):',
    })
    (wt / 'tests/unit/test_release_smoke_client.py').write_text(''.join(test[n] + '\n' for n in range(1, 51)))
    git(wt, 'init', '-q')
    git(wt, 'add', '.')
    git(wt, 'commit', '-qm', 'base')
    return wt


class EvidenceCatalogTest(unittest.TestCase):
    """N-REP01: the writer picks the allowed range; the engine binds the source and the quote verbatim."""

    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix='evidence-catalog-'))
        self.addCleanup(shutil.rmtree, self.t, True)
        self.wt = self.t / 'source'
        self.wt.mkdir()

    def test_source_blocks_cover_the_whole_allowed_files_and_only_the_selected_card(self):
        script, unit, card = (self.wt / name for name in ('script.py', 'unit.py', 'BACKLOG.md'))
        script.write_text(''.join(f'variable_{n} = "source line {n}"\n' for n in range(1, 306)))
        unit.write_text(''.join(f'assert value_{n}\n' for n in range(1, 51)))
        card.write_text('OUTSIDE BEFORE\n' + 'Selected card\n' * 34 + 'OUTSIDE AFTER\n')
        catalog = report_mode.review_evidence_catalog(self.wt, [script, unit, card], {card: [(2, 35)]})
        self.assertEqual(len(catalog['evidence']), 4)
        self.assertEqual(catalog['missing_paths'], [])
        self.assertNotIn('OUTSIDE', json.dumps(catalog))
        blocks = [k for k in catalog['evidence'] if k['source'].startswith('script.py:')]
        self.assertEqual([k['source'] for k in blocks], ['script.py:1-200', 'script.py:201-305'])
        self.assertEqual('\n'.join(k['text'] for k in blocks), script.read_text().rstrip('\n'))
        for entry in catalog['evidence']:
            self.assertIn(entry['expected'], entry['text'])
            self.assertLessEqual(len(entry['text']), report_mode.REVIEW_EVIDENCE_TEXT_CAP)

    def test_references_cannot_override_paths_quotes_or_use_unknown_or_duplicate_ids(self):
        source = self.wt / 'app.py'
        source.write_text('assert ready\nassert ready\n')
        catalog = report_mode.review_evidence_catalog(self.wt, [source])
        reference = {'id': 'K1', 'claim': 'ready assertion'}
        resolved = report_mode.review_evidence_selection({'evidence_refs': [reference]}, catalog)
        self.assertEqual(resolved, [{'id': 'K1', 'claim': 'ready assertion', 'round': 'file',
                                     'source': 'app.py:1-2', 'expected': 'assert ready'}])
        for bad in ([{**reference, 'source': 'outside.py:1'}], [{**reference, 'expected': 'fabricated'}],
                    [{**reference, 'id': 'K999'}], [reference, reference], [], [None]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                report_mode.review_evidence_selection({'evidence_refs': bad}, catalog)
        with self.assertRaises(ValueError):
            report_mode.review_evidence_selection({'evidence_refs': [reference], 'evidence': []}, catalog)
        old = [{**resolved[0], 'source': 'app.py:999'}]
        self.assertEqual(report_mode.review_evidence_selection({'evidence': old}, catalog), old,
                         'historical hand-written evidence or a wrong line is not corrected automatically')

    def test_catalog_bounds_content_and_rejects_sensitive_and_escaping_paths(self):
        source = self.wt / 'large.py'
        source.write_text(('value = ' + 'x' * 990 + '\n') * 100)
        catalog = report_mode.review_evidence_catalog(self.wt, [source])
        self.assertLessEqual(sum(len(k['text']) for k in catalog['evidence']), report_mode.REVIEW_CATALOG_TEXT_CAP)
        self.assertEqual(catalog['missing_paths'], ['large.py'])
        outside = self.t / 'outside.py'
        outside.write_text('not permitted')
        sensitive = self.wt / '.env'
        sensitive.write_text('synthetic fixture only')
        alias = self.wt / 'alias.py'
        alias.symlink_to(outside)
        for path in (outside, sensitive, alias):
            with self.subTest(path=path.name), self.assertRaises(ValueError):
                report_mode.review_evidence_catalog(self.wt, [path])

    def test_file_replaced_by_a_symlink_after_validation_is_not_read_into_the_catalog(self):
        source = (self.wt / 'selected.py').resolve()
        source.write_text('allowed source\n')
        outside = (self.t / 'outside.py').resolve()
        outside.write_text('OUTSIDE_SYNTHETIC_FIXTURE\n')
        real_stat, swapped = Path.stat, False

        def stat_and_swap(path, *args, **kwargs):
            nonlocal swapped
            value = real_stat(path, *args, **kwargs)
            if path == source and not swapped:
                swapped = True
                source.unlink()
                source.symlink_to(outside)
            return value

        with patch.object(Path, 'stat', stat_and_swap), self.assertRaisesRegex(ValueError, 'review-source-packet-unavailable'):
            report_mode.review_evidence_catalog(self.wt, [source])
        self.assertTrue(swapped, 'the real file changed after verification')

    def test_judge_sees_terms_after_four_thousand_characters_and_clipping_is_explicit(self):
        source = self.wt / 'script.py'
        source.write_text('# padding source xxxxxxxxxxxx\n' * 199 + 'evaluate = True\n')
        evidence = [{'id': 'K1', 'round': 'file', 'source': 'script.py:1-200', 'expected': '# padding source'}]
        items = [{'id': 'Findings-1', 'text': 'evaluate', 'evidence_ids': ['K1']}]
        packet = report_mode.claim_package(self.wt, items, evidence, [source.read_text()])[0]
        self.assertGreater(len(packet['evidence_texts']['K1']), 4000)
        self.assertIn('evaluate', packet['evidence_texts']['K1'])
        self.assertEqual(packet['evidence_non_terms'], [])
        self.assertEqual(packet['trimmed_evidence'], [])
        source.write_text(('# padding source ' + 'x' * 40 + '\n') * 199 + 'evaluate = True\n')
        packet = report_mode.claim_package(self.wt, items, evidence, [source.read_text()])[0]
        self.assertEqual(packet['trimmed_evidence'], ['K1'])
        self.assertEqual(packet['evidence_non_terms'], [], 'trimmed text does not prove absence')


class SemanticAcceptanceTest(unittest.TestCase):
    """N-REP01/CR-30: quote existence, claim support and request scope are separate decisions (the trial 11 false-PASS topology)."""
    REV = {'head': 'h', 'digest': 'd' * 64}
    CRITERIA = ['request', 'evidence', 'scope', 'task:g1', 'task:g2']

    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix='semantic-acceptance-'))
        self.addCleanup(shutil.rmtree, self.t, True)
        self.wt = attempt11_source(self.t)
        self.elements = report_mode.review_elements(ATTEMPT11_REPORT)
        scope = [(self.wt / p).read_text() for p in ('scripts/release_smoke.py', 'tests/unit/test_release_smoke_client.py')]
        self.claims = report_mode.claim_package(self.wt, self.elements, ATTEMPT11_EVIDENCE, scope, [ATTEMPT11_CARD])
        self.requirements = report_mode.review_requirements(ATTEMPT11_ACCEPTANCE, ATTEMPT11_PLAN)

    def terms(self, id):
        return next(i['evidence_non_terms'] for i in self.claims if i['id'] == id)

    def decision(self, claims=None, requirements=None, verdict='PASS', **extra):
        body = {'revision': self.REV['digest'], 'verdict': verdict,
                 'acceptance': [{'id': c, 'status': 'PASS', 'evidence': 'report and source read separately'} for c in self.CRITERIA],
                 'poll': [{'id': f'K{i}', 'outcome': 'verified', 'evidence': f'K{i} quote exists in the source'} for i in range(1, 6)], **extra}
        if claims is not None:
            body['claims'] = claims
        if requirements is not None:
            body['requirements'] = requirements
        raw = json.dumps(body, ensure_ascii=False)
        review = report_mode.poll_apply(raw, parse_review(raw, self.REV, self.CRITERIA), [f'K{i}' for i in range(1, 6)])
        return report_mode.semantic_decision_apply(raw, review, self.elements, self.claims, self.requirements)

    def approval(self, term_status='context'):
        return [{'id': i['id'], 'support': 'full', 'rationale': 'the evidence range supports the sentence',
                 'terms': [{'term': t, 'status': term_status} for t in i['evidence_non_terms']]} for i in self.claims]

    def scope(self, element='Findings-1', status='satisfied'):
        return [{'id': g['id'], 'status': status, 'report': [element] if status == 'satisfied' else [], 'rationale': 'the report element satisfies it'}
                for g in self.requirements]

    def test_engine_marks_real_case_terms_that_the_cited_ranges_do_not_contain(self):
        self.assertEqual([i['id'] for i in self.claims], [f'Findings-{n}' for n in range(1, 9)])
        self.assertEqual({t.casefold() for t in self.terms('Findings-5')},
                         {'migrations', 'restore', 'reopen', 'nobrowser', 'dns'})
        self.assertIn('restore', [t.casefold() for t in self.terms('Findings-7')])
        self.assertEqual(self.terms('Findings-8'), ['RC-OSS-005'])
        for clean in ('Findings-1', 'Findings-2', 'Findings-3', 'Findings-4', 'Findings-6'):
            self.assertEqual(self.terms(clean), [], clean)
        k4 = next(i for i in self.claims if i['id'] == 'Findings-7')['evidence_texts']['K4']
        self.assertIn('live.backup(saved)', k4)
        self.assertNotIn('restore', k4, 'the judge sees the real cited range read by the engine, not the quoted excerpt')

    def test_requirements_come_from_request_acceptance_and_each_plan_acceptance_clause(self):
        ids = [g['id'] for g in self.requirements]
        self.assertEqual(ids[:4], ['R1', 'R2', 'R3', 'R4'])
        self.assertEqual([g['id'] for g in self.requirements if g['criterion'] == 'task:g1'], ['g1.1', 'g1.2', 'g1.3'])
        isolation = next(g for g in self.requirements if g['id'] == 'g1.2')
        self.assertIn('Environment isolation', isolation['text'])
        self.assertIn('ValueError', next(g['text'] for g in self.requirements if g['id'] == 'g2.1'))

    def test_real_legacy_judge_pass_without_separate_semantic_decisions_is_not_acceptance(self):
        outcome = self.decision()
        self.assertEqual(outcome['status'], 'ERROR')
        self.assertIn('semantic decision', outcome['reason'])

    def test_judge_cannot_say_a_flagged_term_is_inside_the_cited_range(self):
        outcome = self.decision(self.approval('in-range'), self.scope())
        self.assertEqual(outcome['status'], 'ERROR')
        missing = [dict(i, terms=[]) for i in self.approval()]
        outcome = self.decision(missing, self.scope())
        self.assertEqual(outcome['status'], 'PASS', 'a term decision is optional; the claim support field is the decision')
        self.assertTrue(all(i['support'] == 'full' for i in outcome['claims']), 'a missing term decision does not automatically downgrade the claim')

    def test_honest_real_case_decisions_fail_with_claim_and_coverage_findings_for_the_repair(self):
        claims = self.approval()
        for decision in claims:
            if decision['id'] in ('Findings-5', 'Findings-7', 'Findings-8'):
                decision['support'] = 'absent' if decision['id'] != 'Findings-7' else 'partial'
                decision['rationale'] = 'the cited range does not contain these steps'
                for term in decision['terms']:
                    if term['term'].casefold() in ('restore', 'reopen', 'nobrowser', 'dns', 'migration'):
                        term['status'] = 'unsupported'
        scope = self.scope()
        for g in scope:
            if g['id'] in ('g1.2', 'g2.1'):
                g.update(status='missing', report=[], rationale='the report omits environment isolation and invalid-lock assertions')
        outcome = self.decision(claims, scope)
        self.assertEqual((outcome['status'], outcome['verdict']), ('FAIL', 'REVISE'))
        items = {a['id']: a for a in outcome['acceptance']}
        self.assertEqual((items['claim_support']['status'], items['request_scope']['status']), ('FAIL', 'FAIL'))
        for part in ('Findings-5', 'Findings-7', 'Findings-8', 'NoBrowser'):
            self.assertIn(part, items['claim_support']['evidence'])
        for part in ('g1.2', 'g2.1', 'Environment isolation'):
            self.assertIn(part, items['request_scope']['evidence'])

    def test_requirement_cannot_be_satisfied_by_an_unsupported_claim_or_by_the_judges_own_text(self):
        claims = self.approval()
        next(i for i in claims if i['id'] == 'Findings-5')['support'] = 'partial'
        outcome = self.decision(claims, self.scope(element='Findings-5'))
        self.assertEqual(outcome['status'], 'FAIL')
        self.assertIn('Findings-5', next(a['evidence'] for a in outcome['acceptance'] if a['id'] == 'request_scope'))
        for entry_name, scope in (('empty report', [dict(g, report=[]) for g in self.scope()]),
                           ('nonexistent element', self.scope(element='Findings-99')),
                           ('judge explanation', [dict(g, report=['judge: environment isolation 27-40']) for g in self.scope()]),
                           ('missing requirement', self.scope()[:-1]),
                           ('plan item outside the report', [dict(g, status='report_outside', report=[]) for g in self.scope()])):
            with self.subTest(entry_name=entry_name):
                self.assertEqual(self.decision(self.approval(), scope)['status'], 'ERROR')

    def test_process_only_request_clause_may_be_marked_outside_the_report_with_a_reason(self):
        scope = self.scope()
        scope[2].update(status='report_outside', report=[], rationale='unchanged source is checked by the engine identity')
        self.assertEqual(scope[2]['id'], 'R3')
        self.assertEqual(self.decision(self.approval(), scope)['status'], 'PASS')

    def test_only_engine_classified_process_clauses_may_be_marked_outside_the_report(self):
        self.requirements = report_mode.review_requirements(
            'Explain environment isolation and migration steps in the delivered report; budget/duration constant; independent final review; '
            'source remains unchanged; at most 5 file:line evidence references for release_smoke steps and test assertion coverage; '
            'no install/rollback PASS, release acceptance or card closure claims', {'tasks': []})
        self.assertEqual([g['round'] for g in self.requirements], ['content', 'process', 'process', 'process', 'content', 'content'])
        for id in ('R1', 'R5', 'R6'):
            with self.subTest(content=id):
                scope = self.scope()
                next(g for g in scope if g['id'] == id).update(status='report_outside', report=[], rationale='treated as a process rule')
                outcome = self.decision(self.approval(), scope)
                self.assertEqual(outcome['status'], 'ERROR')
                self.assertIn(id, outcome['reason'])
        scope = self.scope()
        for g in scope:
            if g['id'] in ('R2', 'R3', 'R4'):
                g.update(status='report_outside', report=[], rationale='the engine checks budget, source identity and an independent session')
        self.assertEqual(self.decision(self.approval(), scope)['status'], 'PASS')
        extra = self.scope() + [{'id': 'extra-1', 'text': 'Budget remains constant', 'status': 'report_outside', 'report': [], 'rationale': 'added by judge'}]
        self.assertEqual(self.decision(self.approval(), extra)['status'], 'ERROR', 'an item added by the judge cannot create an outside-report exception')

    def test_plan_task_clauses_use_the_same_process_classifier(self):
        requirements = report_mode.review_requirements('source remains unchanged; no PASS claims', {'tasks': [
            {'id': 'g1', 'acceptance': 'source will be unchanged; Do not write PASS claims; independent final review'}]})
        self.assertEqual([(g['id'], g['round']) for g in requirements],
                         [('R1', 'process'), ('R2', 'content'), ('g1.1', 'process'), ('g1.2', 'content'), ('g1.3', 'process')])

    def test_judge_rule_requires_report_items_for_a_met_requirement(self):
        self.assertIn('The report list cannot be empty for satisfied', report_mode.SEMANTIC_JUDGE_RULE)
        self.assertIn('a report element explicitly stating that absence', report_mode.SEMANTIC_JUDGE_RULE)

    def test_negative_decision_overrides_a_valid_verdict_and_consistent_decisions_pass(self):
        claims = self.approval()
        claims[0]['support'] = 'partial'
        self.assertEqual(self.decision(claims, self.scope(element='Uncertainties-1'))['status'], 'FAIL')
        outcome = self.decision(self.approval(), self.scope())
        self.assertEqual(outcome['status'], 'PASS')
        self.assertEqual({a['id']: a['status'] for a in outcome['acceptance'] if a['id'] in ('claim_support', 'request_scope')},
                         {'claim_support': 'PASS', 'request_scope': 'PASS'})


ATTEMPT22_ACCEPTANCE = (
    'Only the specified public script, unit test and selected RC-OSS-005 card slice are reviewed read-only; Source remains unchanged; '
    'Explain concretely in the report how the code prepares the environment using temporary state and settings files; '
    'Explain concretely in the report how the working directory is changed and restored; '
    'Write the test’s positive output assertions and expected values concretely in Findings; a Method sentence saying only that assertions were inspected '
    'does not satisfy this; write the negative fixture cases and expected errors concretely in Findings; '
    'Independent final review; Budget/duration constant')
ATTEMPT22_REPORT = {
    'Findings': [
        {'text': 'The script creates a temporary directory with TemporaryDirectory and binds environment variables such as FEEDLOOM_DB and ROLECATCH_DISTRIBUTION '
                 'to it; it clears any residual ROLECATCH_SOURCE_POLICY.', 'evidence_ids': ['K2']},
        {'text': 'The test’s negative fixture cases use missing, duplicate, no-wheel and bad-hash lock contents, where '
                 'export_client raises ValueError.', 'evidence_ids': ['K4']},
        {'text': 'The test’s negative fixture cases are parametrized with missing, duplicate, no-wheel and bad-hash problems '
                 'and expect ValueError in every case', 'evidence_ids': ['K4']},
        {'text': 'The script binds FEEDLOOM_DB and ROLECATCH_REQUEST_STATE paths to the temporary directory, while ROLECATCH_DISTRIBUTION '
                 'receives the constant value "local".', 'evidence_ids': ['K2']}],
    'Uncertainties': [],
    'Method': [{'text': 'Only the two allowed files were reviewed read-only.', 'evidence_ids': []}]}
ATTEMPT22_EVIDENCE = [
    K(2, 'file', 'scripts/release_smoke.py:1-200', '"""Exercise a distribution with disposable synthetic state and no browser/network.'),
    K(3, 'file', 'scripts/release_smoke.py:201-305', 'y = 201'),
    K(4, 'file', 'tests/unit/test_release_smoke_client.py:1-50', 'from __future__ import annotations')]
ATTEMPT22_SMOKE = {
    1: '"""Exercise a distribution with disposable synthetic state and no browser/network.',
    51: '    with TemporaryDirectory(prefix="rolecatch-release-smoke-") as directory:',
    52: '        state = Path(directory)',
    53: '        os.environ.update(',
    54: '            FEEDLOOM_DB=str(state / "canonical.sqlite3"),',
    55: '            FEEDLOOM_DASHBOARD_DB=str(state / "dashboard.sqlite3"),',
    56: '            FEEDLOOM_CV_PROFILE=str(state / "missing-cv.json"),',
    57: '            FEEDLOOM_SETTINGS=str(state / "settings.json"),',
    58: '            FEEDLOOM_WARM_ON_START="0",',
    59: '            FEEDLOOM_ALLOW_REGISTRATION="0",',
    60: '            ROLECATCH_DISTRIBUTION="local",',
    61: '            ROLECATCH_REQUEST_STATE=str(state / "request.sqlite3"),',
    62: '        )',
    63: '        os.environ.pop("ROLECATCH_SOURCE_POLICY", None)',
    147: '            return PageRequest("https://fixture.example.org/api/items")',
    186: '        kind="fixture",',
    201: 'y = 201'}
ATTEMPT22_TEST = {
    1: 'from __future__ import annotations',
    26: '@pytest.mark.parametrize("problem", ["missing", "duplicate", "no-wheel", "bad-hash"])',
    27: 'def test_smoke_client_refuses_incomplete_or_ambiguous_lock(',
    28: '    tmp_path: Path, problem: str',
    47: '    lock = tmp_path / "uv.lock"',
    49: '    with pytest.raises(ValueError):',
    50: '        export_client(lock)'}


class Attempt20_22Test(unittest.TestCase):
    REV = {'head': 'h', 'digest': 'e' * 64}
    CRITERIA = ['request', 'evidence', 'scope']

    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix='attempt22-'))
        self.addCleanup(shutil.rmtree, self.t, True)
        self.wt = self.t / 'wt'
        (self.wt / 'scripts').mkdir(parents=True)
        (self.wt / 'tests/unit').mkdir(parents=True)
        (self.wt / 'scripts/release_smoke.py').write_text(
            ''.join(ATTEMPT22_SMOKE.get(n, f'x_{n} = {n}') + '\n' for n in range(1, 306)))
        (self.wt / 'tests/unit/test_release_smoke_client.py').write_text(
            ''.join(ATTEMPT22_TEST.get(n, f'y_{n} = {n}') + '\n' for n in range(1, 51)))
        names = ['scripts/release_smoke.py', 'tests/unit/test_release_smoke_client.py']
        self.elements = report_mode.review_elements(ATTEMPT22_REPORT)
        self.claims = report_mode.claim_package(self.wt, self.elements, ATTEMPT22_EVIDENCE, [(self.wt / a).read_text() for a in names],
                                           scope_names=names, request=ATTEMPT22_ACCEPTANCE)
        self.requirements = report_mode.review_requirements(ATTEMPT22_ACCEPTANCE, {'tasks': []})

    def package(self, id):
        return next(i for i in self.claims if i['id'] == id)

    def requirement(self, part):
        return next(g['id'] for g in self.requirements if part in g['text'])

    def decision(self, claims, requirements, verdict='PASS'):
        raw = json.dumps({'revision': self.REV['digest'], 'verdict': verdict,
                          'acceptance': [{'id': c, 'status': 'PASS', 'evidence': 'report and source read separately'} for c in self.CRITERIA],
                          'poll': [{'id': k['id'], 'outcome': 'verified', 'evidence': 'the quote exists in the source'} for k in ATTEMPT22_EVIDENCE],
                          'claims': claims, 'requirements': requirements}, ensure_ascii=False)
        review = report_mode.poll_apply(raw, parse_review(raw, self.REV, self.CRITERIA), [k['id'] for k in ATTEMPT22_EVIDENCE], at_least=3)
        return raw, report_mode.semantic_decision_apply(raw, review, self.elements, self.claims, self.requirements)

    def approval(self, status='equivalent', **changed):
        return [{'id': i['id'], 'support': changed.get(i['id'], 'full'), 'rationale': 'the cited range shows the behavior stated in the sentence',
                 'terms': [{'term': t, 'status': status} for t in i['evidence_non_terms']]} for i in self.claims]

    def scope(self, element='Findings-4'):
        return [{'id': g['id'], 'status': 'satisfied' if g['round'] == 'content' else 'report_outside',
                 'report': [element] if g['round'] == 'content' else [], 'rationale': 'the report element satisfies it'} for g in self.requirements]

    def test_flagged_request_word_shows_where_the_scope_uses_it_and_that_it_comes_from_the_request(self):
        for id in ('Findings-2', 'Findings-3'):
            package = self.package(id)
            self.assertEqual(package['evidence_non_terms'], ['fixture'], id)
            warning = package['term_warnings'][0]
            self.assertEqual((warning['term'], warning['request_term']), ('fixture', True))
            self.assertEqual(warning['scope_lines'],
                             ['scripts/release_smoke.py:147: return PageRequest("https://fixture.example.org/api/items")',
                              'scripts/release_smoke.py:186: kind="fixture",'])
        self.assertIn('alone does not reduce support', report_mode.SEMANTIC_JUDGE_RULE)
        self.assertIn('a matching word does not establish', report_mode.SEMANTIC_JUDGE_RULE)

    def test_word_warning_alone_does_not_reject_but_a_real_unsupported_claim_does(self):
        raw, outcome = self.decision(self.approval('equivalent'), self.scope())
        self.assertEqual(outcome['status'], 'PASS', outcome.get('reason') or outcome.get('acceptance'))
        self.assertEqual(self.decision(self.approval('context'), self.scope())[1]['status'], 'PASS')
        _, outcome = self.decision(self.approval('unsupported'), self.scope())
        self.assertEqual((outcome['status'], outcome['verdict']), ('FAIL', 'REVISE'))
        self.assertIn('fixture', next(a['evidence'] for a in outcome['acceptance'] if a['id'] == 'claim_support'))

    def test_identifier_present_in_the_cited_range_is_not_proof_of_the_claimed_value(self):
        package = self.package('Findings-1')
        self.assertEqual(package['evidence_non_terms'], [], 'terms occur in range; no word warning')
        self.assertEqual(package['code_lines']['ROLECATCH_DISTRIBUTION'], ['K2:60: ROLECATCH_DISTRIBUTION="local",'])
        self.assertEqual(package['code_lines']['FEEDLOOM_DB'], ['K2:54: FEEDLOOM_DB=str(state / "canonical.sqlite3"),'])
        self.assertEqual(package['code_lines']['ROLECATCH_SOURCE_POLICY'], ['K2:63: os.environ.pop("ROLECATCH_SOURCE_POLICY", None)'])
        self.assertEqual(self.package('Findings-4')['code_lines']['ROLECATCH_DISTRIBUTION'],
                         ['K2:60: ROLECATCH_DISTRIBUTION="local",'])
        environment = self.requirement('temporary state')
        scope = self.scope()
        next(g for g in scope if g['id'] == environment)['report'] = ['Findings-1']
        _, outcome = self.decision(self.approval(**{'Findings-1': 'partial'}), scope)
        self.assertEqual(outcome['status'], 'FAIL')
        items = {a['id']: a['evidence'] for a in outcome['acceptance'] if a['status'] == 'FAIL'}
        self.assertIn('Findings-1 support=partial', items['claim_support'])
        self.assertIn(f'{environment} relies on unsupported elements: Findings-1', items['request_scope'])
        _, correct = self.decision(self.approval(**{'Findings-1': 'partial'}), self.scope('Findings-4'))
        self.assertEqual(correct['status'], 'FAIL', 'a correct sentence does not excuse a wrong sentence that remains in the report')
        report_ = {**ATTEMPT22_REPORT, 'Findings': ATTEMPT22_REPORT['Findings'][1:]}
        self.elements = report_mode.review_elements(report_)
        names = ['scripts/release_smoke.py', 'tests/unit/test_release_smoke_client.py']
        self.claims = report_mode.claim_package(self.wt, self.elements, ATTEMPT22_EVIDENCE, [(self.wt / a).read_text() for a in names],
                                           scope_names=names, request=ATTEMPT22_ACCEPTANCE)
        self.assertEqual(self.decision(self.approval(), self.scope('Findings-3'))[1]['status'], 'PASS', 'a correct description passes')

    def test_extra_decisions_for_unflagged_code_terms_are_not_a_protocol_error_but_unsupported_ones_still_fail(self):
        claims = self.approval()
        claims[0]['terms'] = [{'term': 'ROLECATCH_DISTRIBUTION', 'status': 'negative'}, {'term': 'FEEDLOOM_DB', 'status': 'equivalent'}]
        _, outcome = self.decision(claims, self.scope())
        self.assertEqual(outcome['status'], 'PASS', outcome.get('reason'))
        self.assertEqual(next(i for i in outcome['claims'] if i['id'] == 'Findings-1')['terms'], [])
        claims[0]['terms'][0]['status'] = 'unsupported'
        _, outcome = self.decision(claims, self.scope())
        self.assertEqual(outcome['status'], 'FAIL')
        self.assertIn('ROLECATCH_DISTRIBUTION', next(a['evidence'] for a in outcome['acceptance'] if a['id'] == 'claim_support'))
        repeat = self.approval()
        repeat[1]['terms'] = repeat[1]['terms'] * 2
        self.assertEqual(self.decision(repeat, self.scope())[1]['status'], 'ERROR', 'two decisions for one term fail')

    def test_incomplete_judge_answer_never_passes_but_bound_concrete_findings_are_kept_for_one_repair(self):
        assertions = self.requirement('positive output assertions')
        claims = [i for i in self.approval() if i['id'] != 'Findings-2']
        scope = self.scope()
        next(g for g in scope if g['id'] == assertions).update(status='missing', report=[],
                                                               rationale='the report omits the expected values of positive assertions')
        claims[0].update(support='partial', rationale='ROLECATCH_DISTRIBUTION is assigned "local" rather than the temporary directory')
        raw, outcome = self.decision(claims, scope, verdict='REVISE')
        self.assertEqual(outcome['status'], 'ERROR', 'an answer missing a claim decision is neither acceptance nor rejection')
        findings = report_mode.concrete_corrections(raw, self.REV['digest'], self.claims, self.requirements, self.CRITERIA)
        self.assertEqual([(b['round'], b['id']) for b in findings], [('claim', 'Findings-1'), ('requirement', assertions)])
        self.assertIn('"local"', findings[0]['rationale'])
        body = json.loads(raw)
        for entry_name, broken in (('other revision', {**body, 'revision': 'f' * 64}),
                          ('passing verdict', {**body, 'verdict': 'PASS'}),
                          ('unknown id', {**body, 'claims': [dict(claims[0], id='Findings-99')], 'requirements': []}),
                          ('placeholder rationale', {**body, 'claims': [dict(claims[0], rationale='...')], 'requirements': []})):
            with self.subTest(entry_name=entry_name):
                self.assertEqual(report_mode.concrete_corrections(json.dumps(broken), self.REV['digest'], self.claims,
                                                         self.requirements, self.CRITERIA), [])
        contradiction = json.dumps(body) + '\n' + json.dumps({**body, 'requirements': []})
        self.assertEqual(report_mode.concrete_corrections(contradiction, self.REV['digest'], self.claims, self.requirements,
                                                 self.CRITERIA), [], 'conflicting bodies are not sent to repair')


TRIAL23 = Path(__file__).resolve().parent / 'fixtures/trial23-20261018'


class Trial23Test(unittest.TestCase):
    def setUp(self):
        self.package = json.loads((TRIAL23 / 'bundle.json').read_text())
        self.raw = (TRIAL23 / 'judge1.txt').read_text()
        self.rev = {'digest': self.package['revision']}
        self.elements = report_mode.review_elements(self.package['report_input'])
        self.requirements = report_mode.review_requirements(self.package['acceptance'], self.package['plan'])

    def apply(self, raw):
        review = parse_review(raw, self.rev, self.package['criteria'], planning=True)
        review = report_mode.poll_apply(raw, review, [k['id'] for k in self.package['evidence']])
        return report_mode.semantic_decision_apply(raw, review, self.elements, self.package['claims'], self.requirements,
                                           self.package['evidence'])

    def body(self):
        import re
        block = re.findall(r'```json\s*(\{.*?\})\s*```', self.raw, re.S)[-1]
        return json.loads(block)

    def test_real_answer_is_parsed_without_a_protocol_error_and_evidence_count_is_closed_by_the_engine(self):
        mechanical = [g['id'] for g in self.requirements if g['round'] == 'mechanical']
        self.assertEqual(mechanical, ['R17', 'g3.5'])
        outcome = self.apply(self.raw)
        self.assertNotEqual(outcome['status'], 'ERROR', outcome.get('reason'))
        self.assertEqual({i['id'] for i in outcome['claims']}, {o['id'] for o in self.elements if o['evidence_ids']})
        decisions = {g['id']: g for g in outcome['requirements']}
        self.assertEqual({k: (decisions[k]['status'], decisions[k]['report']) for k in mechanical},
                         {k: ('satisfied', []) for k in mechanical})
        self.assertTrue(decisions['R17']['rationale'].startswith('engine evidence appendix check'))
        self.assertEqual({k: decisions[k]['status'] for k in self.package['operator']['unsatisfied']},
                         dict.fromkeys(self.package['operator']['unsatisfied'], 'satisfied'),
                         'the format is valid; the operator rejected these content decisions by the judge')

    def test_operator_content_verdict_on_the_same_report_is_a_rejection_for_the_one_repair(self):
        body = self.body()
        body['verdict'] = 'REVISE'
        for decision in body['requirements']:
            if decision['id'] in self.package['operator']['unsatisfied']:
                decision.update(status='missing', report=[], rationale='concrete values are absent from the report: variable names, "local", len(lines) == 3, order')
        for decision in body['claims']:
            if decision['id'] == 'Findings-8':
                decision.update(support='partial', rationale='checksum implementation was not read; the visible check is initialize() == ()')
        outcome = self.apply(json.dumps(body, ensure_ascii=False))
        self.assertEqual((outcome['status'], outcome['verdict']), ('FAIL', 'REVISE'))
        evidence = {a['id']: a['evidence'] for a in outcome['acceptance'] if a['status'] == 'FAIL'}
        self.assertIn('Findings-8 support=partial', evidence['claim_support'])
        for id in self.package['operator']['unsatisfied']:
            self.assertIn(f'{id} missing', evidence['request_scope'])

    def test_word_warnings_do_not_catch_the_real_overclaim_and_missing_term_decisions_do_not_decide_support(self):
        package = {i['id']: i for i in self.package['claims']}
        self.assertEqual(package['Findings-8']['evidence_non_terms'], [], 'the checksum overclaim does not produce a word warning')
        body = self.body()
        for decision in body['claims']:
            decision['terms'] = []
        outcome = self.apply(json.dumps(body, ensure_ascii=False))
        self.assertNotEqual(outcome['status'], 'ERROR', outcome.get('reason'))
        self.assertEqual({i['id']: i['support'] for i in outcome['claims']}, {k['id']: k['support'] for k in body['claims']})

    def test_incomplete_real_answer_never_passes(self):
        body = self.body()
        body['claims'] = body['claims'][:-1]
        self.assertEqual(self.apply(json.dumps(body, ensure_ascii=False))['status'], 'ERROR')
        body = self.body()
        body['requirements'] = [g for g in body['requirements'] if g['id'] != 'R3']
        self.assertEqual(self.apply(json.dumps(body, ensure_ascii=False))['status'], 'ERROR')

    def test_only_identical_plan_clauses_are_deduplicated_and_count_rule_allows_expected_assert_values(self):
        plan = {'tasks': [{'id': 'g1', 'acceptance': 'Source remains unchanged; RC-OSS-005 is stated as PROCEED'},
                             {'id': 'g2', 'acceptance': 'source remains unchanged.; At most 5 file:line evidence references are used'}]}
        ids = [g['id'] for g in report_mode.review_requirements('Source remains unchanged; RC-OSS-005 remains PROCEED', plan)]
        self.assertEqual(ids, ['R1', 'R2', 'g1.2', 'g2.2'])
        self.assertEqual([g['id'] for g in self.requirements if g['criterion'] != 'request'][:3], ['g1.1', 'g1.2', 'g1.3'])
        self.assertIn('g3.6', [g['id'] for g in self.requirements], 'a similar but different plan condition is preserved')
        self.assertIn('len(lines) == 3', report_mode.SEMANTIC_JUDGE_RULE)
        import acceptance
        self.assertIn('len(lines) == 3', acceptance.WRITER_EVIDENCE_RULE)
        self.assertNotIn('number or line information', acceptance.WRITER_EVIDENCE_RULE)

    def test_mechanical_clause_classifier_is_narrow(self):
        self.assertTrue(report_mode.mechanical_item('At most 5 file:line evidence references are used, no command evidence'))
        self.assertTrue(report_mode.mechanical_item('at most 5 file:line references are used'))
        self.assertFalse(report_mode.mechanical_item('At most 5 file:line evidence references are used and every finding has evidence'))
        self.assertFalse(report_mode.mechanical_item('Do not write test totals or counts'))
        excess = [{'id': f'K{n}', 'round': 'file'} for n in range(6)] + [{'id': 'K9', 'round': 'command'}]
        decision = report_mode.mechanical_decision({'id': 'R17', 'text': 'At most 5 file:line evidence references are used, no command evidence'}, excess)
        self.assertEqual(decision['status'], 'missing')
        self.assertIn('6>5', decision['rationale'])
        self.assertIn('K9', decision['rationale'])


class SourceSectionQaTest(unittest.TestCase):
    """N-IN04: evidence given for a selected source section cannot spill outside the section's lines."""

    def test_evidence_on_a_section_restricted_file_must_stay_inside_the_selected_lines(self):
        from run_store import plan_product
        t = Path(tempfile.mkdtemp(prefix='section-qa-'))
        self.addCleanup(shutil.rmtree, t, True)
        wt, sha = repo(t)
        (wt / 'BACKLOG.md').write_text('### [x] OLD — Existing\nold body\n### [ ] NEXT — Pending\nnext body\n')
        git(wt, 'add', '.')
        git(wt, 'commit', '-qm', 'backlog')
        declaration = {'path': 'REVIEW.md', 'evidence': 'REVIEW.evidence.jsonl', 'sections': []}
        for source, expected, error in (('BACKLOG.md:3-4', 'next body', False), ('BACKLOG.md:4', 'next body', False),
                                       ('BACKLOG.md:1-2', 'old body', True), ('BACKLOG.md:2-4', 'next body', True),
                                       ('BACKLOG.md', 'next body', True)):
            with self.subTest(source=source):
                evidence = json.dumps(K(1, 'file', source, expected))
                outcome = report_mode.report_qa(wt, declaration, sha, t / 'temporary', texts=('# Findings\n- x [K1].\n', evidence),
                                       start=plan_product(wt), source_read_only=True,
                                       source_paths=[wt / 'BACKLOG.md', wt / 'app.py'],
                                       source_sections={wt / 'BACKLOG.md': [(3, 4)]})
                found = [b for b in outcome['findings'] if b.startswith('section-scope')]
                self.assertEqual(bool(found), error, outcome['findings'])


class PlanAuditTest(unittest.TestCase):
    """G-R1: in a report job the plan may only touch the report + evidence appendix; a violation is a schema error (correction to the planner)."""
    def plan(self, *file_lists):
        return {'job_id': 'x', 'tasks': [{'id': f'g{i}', 'definition': 't', 'acceptance': 'k', 'model': 'auto', 'difficulty': 2,
                                            'parallel_group': 0, 'estimate_min': 5, 'files': list(d)}
                                           for i, d in enumerate(file_lists, 1)]}

    def extract(self, plan, declaration):
        t = Path(tempfile.mkdtemp(prefix='report-plan-'))
        self.addCleanup(shutil.rmtree, t, True)
        (t / 'run.json').write_text(json.dumps({'report': declaration}))
        return subprocess.run([sys.executable, str(LIB / 'dispatch_util.py'), 'extract'], input=json.dumps(plan), text=True,
                              capture_output=True, env={**os.environ, 'CADRE_RUN_DIR': str(t)}, timeout=20)

    def test_plan_listing_a_non_report_file_is_rejected(self):
        b = report_mode.declaration(REPORT_PROMPT)
        self.assertIsNone(report_mode.plan_audit(self.plan(['report_mode.evidence.jsonl'], ['report_mode.md', 'report_mode.evidence.jsonl']), b))
        self.assertIn('app.py', report_mode.plan_audit(self.plan(['report_mode.md', 'app.py']), b))
        self.assertIn('task writes the report', report_mode.plan_audit(self.plan(['report_mode.evidence.jsonl']), b))
        output = self.extract(self.plan(['report_mode.md', 'app.py']), b)
        self.assertEqual(output.returncode, 2)
        self.assertIn('SCHEMA: report mode', output.stderr)
        self.assertEqual(self.extract(self.plan(['report_mode.md', 'app.py']), None).returncode, 0, 'no rule for a non-report request')

    def test_lane_prompt_carries_the_evidence_rule_in_a_report_job(self):
        plan = self.plan(['report_mode.md', 'report_mode.evidence.jsonl'])
        prompt = {}
        for entry_name, declaration in (('report', report_mode.declaration(REPORT_PROMPT)), ('code', None)):
            t = Path(tempfile.mkdtemp(prefix='report-pf-'))
            self.addCleanup(shutil.rmtree, t, True)
            (t / 'run.json').write_text(json.dumps({'report': declaration}))
            prompt[entry_name] = subprocess.run([sys.executable, str(LIB / 'dispatch_util.py'), 'task_pf', 'g1'], input=json.dumps(plan),
                                       text=True, capture_output=True, env={**os.environ, 'CADRE_RUN_DIR': str(t)}, timeout=20).stdout
        self.assertIn('REPORT MODE', prompt['report'])
        self.assertIn('report_mode.evidence.jsonl', prompt['report'])
        self.assertNotIn('REPORT MODE', prompt['code'])

    def test_plan_and_lane_prompts_carry_the_evidence_rules(self):
        b = report_mode.declaration(REPORT_PROMPT)
        self.assertIn('report_mode.evidence.jsonl', report_mode.plan_prompt(b))
        self.assertIn('[K', report_mode.lane_rules(b))
        self.assertTrue(report_mode.is_writer(self.plan(['report_mode.evidence.jsonl'], ['report_mode.md', 'report_mode.evidence.jsonl']), 'g2', b))
        self.assertFalse(report_mode.is_writer(self.plan(['report_mode.evidence.jsonl'], ['report_mode.md', 'report_mode.evidence.jsonl']), 'g1', b))


class ReportAcceptanceGateTest(unittest.TestCase):
    """G-R3/G-R5/G-R6 from the production path: qa_gate and finalize_run."""
    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix='report-acceptance-'))
        self.addCleanup(shutil.rmtree, self.t, True)
        self.wt, self.base = repo(self.t)
        self.store = RunStore(self.t)
        (self.t / 'projects/a').mkdir(parents=True)
        (self.t / 'projects/a/qa-command').write_text('exit 1')   # if project QA ran in a report job it would be red
        self.run = self.store.accept('a', REPORT_PROMPT, source_sha=self.base)
        self.rid = self.run['run_id']
        self.store.update(self.rid, worktree=str(self.wt))
        writing = self.store.task_dir(self.rid, 'impl') / 'attempts/fixture/attempt.json'
        writing.parent.mkdir(parents=True)
        writing.write_text(json.dumps({'role': 'impl', 'model': 'fixture/writer', 'pid': 123}))
        for ctx in (patch.dict(os.environ, {**self.store.environment(self.rid), 'CADRE_TASK_ID': '', 'CADRE_OWNER_FDS': ''}),
                    patch.object(core, 'QE', self.t), patch.object(core, 'worktree_base_sha', return_value=self.base),
                    patch.object(core, 'budget_json', return_value=None)):
            ctx.__enter__()
            self.addCleanup(ctx.__exit__, None, None, None)
        self.repair = Mock(return_value=Mock(poll=lambda: 0, wait=lambda **k: 0, returncode=0))
        ctx = patch.object(core, 'run_opencode', self.repair)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.log = self.t / 'runner.log'

    def test_in_report_mode_the_qa_gate_runs_report_qa_not_project_qa(self):
        evidence_write(self.wt, VALID_REPORT, VALID_EVIDENCE)
        outcome = core.qa_gate('a', str(self.wt), self.log, '/fake/cli')
        self.assertEqual((outcome['status'], outcome.get('kind')), ('PASS', 'report'))
        self.assertEqual(outcome['revision'], revision(self.wt))

    def test_no_local_repair_on_a_report_qa_red(self):
        evidence_write(self.wt, VALID_REPORT + "Total 431 listings.\n", VALID_EVIDENCE)
        outcome = core.qa_gate('a', str(self.wt), self.log, '/fake/cli')
        self.assertEqual(outcome['status'], 'FAIL')
        self.repair.assert_not_called()
        self.assertIn('unsupported-number', Path(outcome['attempts'][-1]['output']).read_text())

    def test_judge_prompt_carries_the_poll_protocol_and_a_wrong_one_becomes_revise(self):
        evidence_write(self.wt, VALID_REPORT, VALID_EVIDENCE)
        prompts = []

        def judge(real, wt, args, timeout, env=None, role='quality'):
            prompts.append(args[-1])
            data = json.loads(args[-1].splitlines()[-1])
            return json.dumps({'revision': data['revision'], 'verdict': 'PASS',
                               'acceptance': [{'id': c['id'], 'status': 'PASS', 'evidence': 'report read'} for c in data['criteria']],
                               'poll': [{'id': 'K1', 'outcome': 'verified', 'evidence': 'app.py:2 read'},
                                           {'id': 'K2', 'outcome': 'wrong', 'evidence': 'command succeeded'}]})
        with patch.object(core, 'capture_opencode', side_effect=judge):
            outcome = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')
        self.assertIn('REPORT PROBE', prompts[0])
        self.assertEqual(json.loads(prompts[0].splitlines()[-1])['report']['evidence_ids'], ['K1', 'K2'])
        self.assertEqual((outcome['status'], outcome['reason'], outcome['review']['verdict']), ('FAIL', 'review-not-passed', 'REVISE'))


class ReportCorrectionPromptTest(unittest.TestCase):
    def test_correction_prompt_carries_the_report_findings_and_the_evidence_rule(self):
        t = Path(tempfile.mkdtemp(prefix='report-plain-'))
        self.addCleanup(shutil.rmtree, t, True)
        (t / 'qa.log').write_text('REPORT QA FAIL: 1 finding\nunsupported-number: report_mode.md:2: 431 listings rejected\n')
        (t / 'prompt.md').write_text(REPORT_PROMPT)
        record = {'report': report_mode.declaration(REPORT_PROMPT), 'prompt': str(t / 'prompt.md'),
                 'final_result': {'status': 'FAIL', 'reason': 'qa-not-passed', 'qa': {'attempts': [{'output': str(t / 'qa.log')}]}}}
        text = core.correction_prompt(record, 1)
        self.assertIn('REPORT QA', text)
        self.assertIn('unsupported-number: report_mode.md:2', text)
        self.assertIn('report_mode.evidence.jsonl', text)
        self.assertNotIn('project test command', text)


class ReportExecutionTest(unittest.TestCase):
    def test_report_job_does_not_fall_to_the_plain_runner_without_a_strong_model(self):
        """G-R6: when the dispatcher skips (no plan/budget) a report job is not handed to the local single-lane path."""
        t = Path(tempfile.mkdtemp(prefix='report-rc3-'))
        self.addCleanup(shutil.rmtree, t, True)
        root = t / 'code'
        (root / 'bin').mkdir(parents=True)
        (root / 'bin/cadre-dispatch.sh').write_text('echo dispatch >> "$CADRE_RUN_DIR/calls"\nexit 3\n')
        (root / 'bin/job-runner.sh').write_text('echo runner >> "$CADRE_RUN_DIR/calls"\nexit 0\n')
        store = RunStore(t)
        run = store.accept('a', REPORT_PROMPT)
        env = {**os.environ, 'CADRE_HOME': str(t), 'CADRE_ENGINE_ROOT': str(root)}
        rc = subprocess.run([sys.executable, str(LIB / 'run_store.py'), 'run', run['run_id']], env=env, timeout=20).returncode
        record = store.read(run['run_id'])
        self.assertEqual((store.path(run['run_id']) / 'calls').read_text().split(), ['dispatch'])
        self.assertNotEqual(rc, 0)
        self.assertEqual(record['state'], 'retryable')
        self.assertTrue(record['reason'].startswith('budget-wait'), record['reason'])
        self.assertTrue(record.get('retry_after'))


REPORT_WORKER = """
import json, os, pathlib, sys
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT'] + '/lib')
from run_store import RunStore
gid = os.environ['CADRE_TASK_ID']; wt = pathlib.Path(os.environ['CADRE_WORKTREE'])
with open(os.environ['FIXTURE_MODELS'], 'a') as f:
    f.write(gid + '=' + (os.environ.get('CADRE_RUN_MODEL') or 'auto') + '\\n')
with open(os.environ['FIXTURE_MODELS'] + '.extra-lane', 'a') as f:   # B226: no allocation transfer while the extra lane runs
    f.write(gid + ':' + ('present' if (RunStore().path(os.environ['CADRE_RUN_ID']) / 'extra-lane.flag').exists() else 'absent') + '\\n')
with open(os.environ['FIXTURE_MODELS'] + '.before', 'a') as f:   # G-R7 independence: report state at lane start
    f.write(gid + ':' + ('present' if (wt / 'report_mode.md').exists() else 'absent') + '\\n')
evidence = [{'id': 'K1', 'claim': 'subtraction', 'round': 'file', 'source': 'app.py:2', 'expected': 'return a - b'}]
(wt / 'report_mode.evidence.jsonl').write_text(''.join(json.dumps(k) + '\\n' for k in evidence))
line = 'sum performs subtraction [K1].' if gid.startswith('correction') or gid.endswith('reconciliation') or os.environ.get('FIXTURE_WRITER_FALL') else 'sum performs subtraction; 431 listings affected.'
(wt / 'report_mode.md').write_text('# Summary\\n' + line + '\\n')
if os.environ.get('FIXTURE_WRITER_FALL') and gid == 'g1':   # B238: product on disk, but writer lane fails (loop/guard interruption)
    raise SystemExit(1)
if os.environ.get('FIXTURE_RECONCILIATION_FALL') and gid.endswith('reconciliation'):   # B256: the reconciler fails on budget rejection or outage and emits no output
    (wt / 'report_mode.md').unlink(); raise SystemExit(1)
RunStore().task_update(os.environ['CADRE_RUN_ID'], gid, state='produced')
"""

FAKE_JUDGE = """
import os, sys, json
calls = os.path.join(os.path.dirname(sys.argv[0]), 'reviewer-calls')
if not os.environ['CADRE_TASK_ID'].startswith('plan-review-'): open(calls, 'a').write('x')
q = json.loads(sys.argv[-1].splitlines()[-1])
answer = {'verdict': 'PASS', 'revision': q['revision'],
          'acceptance': [{'id': i['id'], 'status': 'PASS', 'evidence': 'report read and evidence probed'} for i in q['criteria']]}
r = q.get('report') or {}
answer['poll'] = [{'id': k, 'outcome': 'verified', 'evidence': k + ' read from its source'} for k in (r.get('evidence_ids') or [])[:r.get('at_least', 0)]]
print(json.dumps({'type': 'text', 'sessionID': 'ses_' + os.environ['CADRE_TASK_ID'], 'part': {'type': 'text', 'text': json.dumps(answer)}}))
"""


class ReportEndToEndTest(unittest.TestCase):
    """Real dispatcher + coordinator; a fake worker deliberately writes an invented number (the design's evidence plan)."""
    STRONG = 'deepseek/deepseek-v4-pro'

    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix='report-e2e-'))
        self.addCleanup(shutil.rmtree, self.t, True)
        self.wt, self.base = repo(self.t)
        self.store = RunStore(self.t)
        (self.t / 'projects/a').mkdir(parents=True)
        (self.t / 'projects/a/qa-command').write_text('exit 1')
        self.rid = self.store.accept('a', REPORT_PROMPT, source_sha=self.base)['run_id']
        self.store.update(self.rid, worktree=str(self.wt))
        writing = self.store.task_dir(self.rid, 'impl') / 'attempts/fixture/attempt.json'
        writing.parent.mkdir(parents=True)
        writing.write_text(json.dumps({'role': 'impl', 'model': 'fixture/writer', 'pid': 123}))

    def execute_run(self, strong, plan_extra=None, env_extra=None):
        root = self.t / 'dispatcher'
        from qa_fixture import instructions
        instructions(root)
        (root / 'lib').mkdir(parents=True)
        (root / 'bin').mkdir()
        for name in ('worktree_archive.py', 'run_store.py', 'outcome.py', 'session.py', 'qe_core.py', 'liveness.py', 'acceptance.py', 'runner.py', 'dispatch_util.py', 'budget_gate.py', 'go_window.py', 'limits.py', 'limits.json', 'limits.mjs', 'opencode_adaptor.py', 'budget-plugin.mjs', 'cadre-plugin.mjs', 'monthly-budget-plugin.mjs',
                     'model_router.py', 'model_selection.py', 'model_catalog.py', 'contract.py', 'level.py', 'prefilter.py', 'task_split.py', 'report_mode.py',
                     'standard-task-form.txt'):
            shutil.copyfile(LIB / name, root / 'lib' / name)
        for source in LIB.glob('*.py'):
            if not (root / 'lib' / source.name).exists():
                shutil.copyfile(source, root / 'lib' / source.name)
        shutil.copytree(LIB / 'qa-sim', root / 'lib/qa-sim', ignore=shutil.ignore_patterns('__pycache__'), dirs_exist_ok=True)
        router = (root / 'lib/model_router.py').read_text()   # only the fixture copy: imitating a strong plan link
        router = router.replace('if __name__ == "__main__":\n    a = sys.argv[1:]\n',
                          'if __name__ == "__main__":\n    a = sys.argv[1:]\n'
                          '    _z = [m for m in os.environ.get("FIXTURE_STRONG", "").split(",") if m]\n'
                          '    if a[:1] == ["first"] and a[1:2] and a[1] in ("plan", "writer") and "FIXTURE_STRONG" in os.environ:\n'
                          '        print(_z[0] if _z else ""); sys.exit(0)\n'
                          '    if a[:1] == ["next"] and a[1:2] and a[1] in ("plan", "writer") and _z:\n'
                          '        print(_z[_z.index(a[2]) + 1] if a[2] in _z[:-1] else ""); sys.exit(0)\n', 1)
        (root / 'lib/model_router.py').write_text(router)
        import test_end_to_end
        from test_run_sessions import CATALOG
        (root / 'lib/provider_budget.py').write_text(test_end_to_end.FAKE_BUDGET)
        (self.t / 'models.json').write_text(json.dumps(CATALOG))
        shutil.copyfile(LIB.parent / 'bin/cadre-dispatch.sh', root / 'bin/cadre-dispatch.sh')
        (root / 'lib/common.sh').write_text('qe_verify_ai_worktree(){ return 0; }\nqe_make_worktree(){ return 99; }\nqe_commit_wip(){ echo 0; }\n')
        (root / 'worker.py').write_text(REPORT_WORKER)
        (root / 'bin/job-runner.sh').write_text(f'exec "{sys.executable}" "$CADRE_ENGINE_ROOT/worker.py"\n')
        plan = {'job_id': 'fixture', 'tasks': [{'id': 'g1', 'definition': 'write the report', 'model': 'auto', 'parallel_group': 0,
                                                  'files': ['report_mode.md', 'report_mode.evidence.jsonl'], 'acceptance': 'evidenced report',
                                                  'difficulty': 2, 'estimate_min': 1}]}
        plan.update(plan_extra or {})
        (self.store.path(self.rid) / 'plan.json').write_text(json.dumps(plan))
        self.store.update(self.rid, phase='dispatch')
        judge = self.t / 'fake-reviewer'
        judge.write_text(f'#!{sys.executable}\n' + FAKE_JUDGE)
        judge.chmod(0o700)
        env = {'CADRE_ENGINE_ROOT': str(root), 'CADRE_REAL_OPENCODE': str(judge), 'OPENCODE_DB': str(self.t / 'absent.db'),
               'FIXTURE_MODELS': str(self.t / 'models'), 'FIXTURE_STRONG': strong, 'CADRE_WAIT_THRESHOLD_SEC': '0', 'CADRE_HOME': str(self.t),
               'CADRE_MODEL_CATALOG': str(self.t / 'models.json'), **(env_extra or {})}   # P3: no local, the cloud has a fake budget+catalogue
        with patch.dict(os.environ, env):
            rc = execute_run(self.store, self.rid)
        models = (self.t / 'models').read_text().split() if (self.t / 'models').exists() else []
        return rc, self.store.read(self.rid), models

    def test_made_up_number_is_caught_by_qa_and_the_correction_round_closes_it_with_a_strong_model(self):
        rc, record, models = self.execute_run(self.STRONG)
        self.assertEqual((rc, record['state']), (0, 'completed'), record.get('final_result'))
        self.assertEqual(models, [f'g1={self.STRONG}', f'correction-1={self.STRONG}'], 'G-R6: the writer and correction round use a strong model')
        self.assertEqual([d['signature'][:3] for d in record.get('correction') or []], ['qa:'])
        self.assertEqual(record['final_result']['qa'].get('kind'), 'report')
        self.assertTrue(record['final_result']['review'].get('poll'), 'the judge probe is recorded')

    def test_hard_report_has_two_independent_analysts_and_reconciliation(self):
        """G-R7: in a hard+ report the second analyst writes without seeing the first one's draft; a third model reconciles the divergences."""
        (self.store.path(self.rid) / 'extra-lane.flag').write_text('previous dispatcher died midway through G-R7\n')   # stale marker
        rc, record, models = self.execute_run('m/a,m/b,m/c', plan_extra={'scope_points': 50})
        self.assertEqual((rc, record['state']), (0, 'completed'), record.get('final_result'))
        self.assertEqual(models, ['g1=m/a', 'g1-analyst-b=m/b', 'g1-reconciliation=m/c'])
        self.assertEqual((self.t / 'models.extra-lane').read_text().split(), ['g1:absent', 'g1-analyst-b:present', 'g1-reconciliation:present'])
        self.assertFalse((self.store.path(self.rid) / 'extra-lane.flag').exists(), 'the marker is removed at the end of G-R7')
        self.assertIn('g1-analyst-b:absent', (self.t / 'models.before').read_text(), 'the second analyst saw the first draft')
        for entry_name in ('analyst-a', 'analyst-b'):
            self.assertTrue((self.wt / '.cadre-plan' / entry_name / 'report_mode.md').is_file(), entry_name)
        prompt = (self.store.path(self.rid) / 'tasks/g1-reconciliation/prompt.md').read_text()
        self.assertIn('.cadre-plan/analyst-a/report_mode.md', prompt)
        self.assertIn('.cadre-plan/analyst-b/report_mode.evidence.jsonl', prompt)
        self.assertEqual(record['final_result']['qa']['status'], 'PASS')
        analyst = record['report']['analyst']
        self.assertEqual((analyst['mode'], analyst['first'], analyst['second'], analyst['reconciler']), ('reconciliation', 'm/a', 'm/b', 'm/c'))
        self.assertIn('Analyst note (G-R7): two independent analyst', (self.wt / 'report_mode.md').read_text())

    def test_reconciliation_marks_the_lanes_whose_thought_was_discarded(self):
        """B256 (26.09 live EXTRA1): when the reconciler failed the dispatcher went back to the first analyst's report; the second analyst's and the reconciler's
        output was NOT in the product but their models counted as 'writers' and exhausted the judge pool. A discarded lane gets `rejected.flag`."""
        rc, record, models = self.execute_run('m/a,m/b,m/c', plan_extra={'scope_points': 50}, env_extra={'FIXTURE_RECONCILIATION_FALL': '1'})
        self.assertEqual((rc, record['state']), (0, 'completed'), record.get('final_result'))
        self.assertIn('reconciliation produced no output', (self.store.path(self.rid) / 'dispatch.log').read_text())
        for gid in ('g1-analyst-b', 'g1-reconciliation'):
            self.assertTrue((self.store.task_dir(self.rid, gid) / 'rejected.flag').is_file(), f'{gid}: a lane whose output is discarded is marked')
        self.assertFalse((self.store.task_dir(self.rid, 'g1') / 'rejected.flag').exists(), 'the first analyst remained in the product')
        self.assertEqual((record['report']['analyst']['mode'], record['report']['analyst']['reason']), ('single', 'reconciliation produced no output'))
        self.assertEqual((self.wt / 'report_mode.md').read_text().count('Analyst note (G-R7): single analyst'), 1, 'one line before QA even if the correction round deletes the note')

    def test_reconciliation_produced_on_re_approval_does_not_run_again(self):
        """B258 (26.09 live EXTRA1): when a report job came back for re-approval (`--proceed`/retry) the dispatcher ran the G-R7 lanes again from scratch
        even though their outputs were `produced` (~$0.3 + 5 min every time). If the reconciler is already `produced` (or discarded) G-R7 is skipped."""
        for gid in ('g1-analyst-b', 'g1-reconciliation'):
            self.store.task_update(self.rid, gid, state='produced')
        rc, record, models = self.execute_run('m/a,m/b,m/c', plan_extra={'scope_points': 50})
        self.assertEqual((rc, record['state']), (0, 'completed'), record.get('final_result'))
        self.assertEqual(models[0], 'g1=m/a')
        self.assertFalse([m for m in models if 'analyst-b' in m or 'reconciliation' in m], f'G-R7 lanes did not run again: {models}')
        self.assertIn('B258', (self.store.path(self.rid) / 'dispatch.log').read_text())

    def test_continues_with_a_single_analyst_if_there_is_no_second_one(self):
        """N-REP03 (CR-30): without a strong second model a single analyst is written explicitly in the run record and in the report itself."""
        rc, record, models = self.execute_run('m/a', plan_extra={'scope_points': 50})
        self.assertEqual(models[0], 'g1=m/a')
        self.assertNotIn('g1-analyst-b', ' '.join(models))
        self.assertIn('G-R7', (self.store.path(self.rid) / 'dispatch.log').read_text())
        self.assertEqual((rc, record['state']), (0, 'completed'), record.get('final_result'))
        analyst = record['report']['analyst']
        self.assertEqual((analyst['mode'], analyst['first'], analyst['second'], analyst['reason']), ('single', 'm/a', None, 'no second strong analyst available'))
        self.assertIn('Analyst note (G-R7): single analyst; no second independent analyst ran (no second strong analyst available).', (self.wt / 'report_mode.md').read_text())

    def test_if_the_product_on_disk_passes_the_gate_it_is_recovered_even_if_the_writer_falls(self):
        """B238 (26.09 live R13-v4): after writing the report the Go writer read its own report 9 times and looped, the guard cut it,
        the lane became 'failed' and a 12 KB report + a 19 KB evidence appendix were thrown away. When the chain is exhausted, if the product on disk passes
        report QA the lane counts as produced; otherwise the old behaviour (failed)."""
        rc, record, models = self.execute_run(self.STRONG, env_extra={'FIXTURE_WRITER_FALL': '1'})
        self.assertEqual((rc, record['state']), (0, 'completed'), record.get('final_result'))
        self.assertIn('RECOVERED', (self.store.path(self.rid) / 'dispatch.log').read_text())
        self.assertEqual(models[0], f'g1={self.STRONG}')
        self.assertNotIn('auto', ' '.join(models), 'recovery uses a strong model rather than the router')

    def test_report_writer_does_not_run_without_a_strong_model(self):
        rc, record, models = self.execute_run('')
        self.assertNotEqual(rc, 0)
        self.assertEqual(models, [], 'G-R6: the report writer did not run without a strong model')
        self.assertIn('report writer', (self.store.path(self.rid) / 'dispatch.log').read_text())


if __name__ == '__main__':
    unittest.main()
