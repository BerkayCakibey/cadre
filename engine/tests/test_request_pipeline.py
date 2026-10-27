from __future__ import annotations

import json
import shutil
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))

from run_store import RunStore, atomic_json, execute_run, generate_request_plan, request_permission, request_proposal, review_plan


class RequestPipelineTests(unittest.TestCase):
    def duration_fill(self):
        record = self.store.read(self.rid)
        record['request_clock'] = {'start': record['created'], 'active_sec': record['request']['duration_minutes'] * 60, 'session': None}
        atomic_json(self.store.path(self.rid) / 'run.json', record)

    def setUp(self):
        from test_run import PlanReviewTests
        self.fixture = PlanReviewTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.home, self.repo = self.fixture.home, self.fixture.source.repo
        self.store = RunStore(self.home)
        project = self.home / 'projects/lab'
        project.mkdir(parents=True)
        (project / 'repo-path').write_text(str(self.repo))
        self.backlog = '### [x] OLD — Existing item\nREADME.md must retain its content.\n### [ ] NEXT — Pending item\nUpdate README.md.\n'
        (self.repo / 'BACKLOG.md').write_text(self.backlog)
        self.raw = dict(submission_id='pipeline', project='lab', target_path=str(self.repo), target_kind='existing',
                        input_mode='plan_only', prompt='Read source, preserve README and plan the requested update.', output='Reviewed plan',
                        acceptance='README contents remain intact', preference='balanced', difficulty_override=None,
                        duration_minutes='60', budget_usd='2', backlog_path='', source_token='')
        self.rid = self.store.request(self.raw)['run_id']
        import qe_core as core
        self.enter(patch.object(core.ROUTER, 'chain', return_value=(['opencode-go/glm-5.3'], ['fixture approved route'])))
        self.enter(patch.dict(os.environ, {**self.store.environment(self.rid), 'CADRE_REAL_OPENCODE': str(self.fixture.fixture.fake)}))
        self.fake = self.fixture.fixture.fake
        self.fake.write_text('#!' + sys.executable + '\n' + r'''
import hashlib,json,os,pathlib,sys,time
home=pathlib.Path(os.environ['CADRE_HOME']); q=json.loads(sys.argv[-1].splitlines()[-1])
for line in sys.argv[-1].splitlines():
    if line.startswith('{'):
        item=json.loads(line)
        if item.get('kind') in ('review-output','review-acceptance'): q={**q,**item}
mode=(home/'pipeline-mode').read_text() if (home/'pipeline-mode').exists() else ''
adv=home/'hour-advance.json'
if adv.exists():
    rule=json.loads(adv.read_text())
    plan_call='schema' in q
    target=('revision' if plan_call and q.get('change') else 'plan' if plan_call else
           'review' if ('criteria' in q and not q.get('kind')) else None)
    if target and rule.get(target):
        ofs=home/'hour-offset'
        ofs.write_text(str((float(ofs.read_text() or 0) if ofs.exists() else 0.0)+float(rule[target])))
        if rule.get('single'): adv.unlink()
    if target=='revision' and rule.get('revision-collapse') and not (home/'revision-crashed').exists():
        (home/'revision-crashed').write_text('1'); sys.exit(1)
with (home/'pipeline-calls.jsonl').open('a') as out: out.write(json.dumps(q)+'\n')
(home/'pipeline-prompt.txt').write_text(sys.argv[-1])
(home/'pipeline-config.json').write_text(os.environ['OPENCODE_CONFIG_CONTENT'])
if pathlib.Path('README.md').exists(): pathlib.Path('README.md').read_text()
else: list(pathlib.Path('.').iterdir())
if q['backlog'].get('full_path'):
    data=pathlib.Path(q['backlog']['full_path']).read_bytes()
    (home/('backlog-read-'+os.environ['CADRE_TASK_ID']+'.json')).write_text(json.dumps({'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)}))
if mode=='write': pathlib.Path('forbidden.txt').write_text('readonly violation')
if mode=='wait':
    (home/'pipeline-ready').touch()
    time.sleep(60)
if q.get('kind')=='review-output':
    (home/'pipeline-author-prompt.txt').write_text(sys.argv[-1])
    expected=pathlib.Path('README.md').read_text().splitlines()[0]
    answer={'report':{'Findings':[{'text':'README source preserved.','evidence_ids':['K1']}],
                      'Uncertainties':[{'text':'A local fixture does not establish semantic model acceptance.','evidence_ids':[]}],
                      'Method':[{'text':'Source file reviewed read-only.','evidence_ids':['K1']}]},
            'evidence':[{'id':'K1','claim':'README content','round':'file','source':'README.md:1','expected':expected}]}
    if mode=='product-bad-report' or (mode=='product-bad-report-once' and 'EVIDENCE AUDIT FAIL' not in sys.argv[-1]):
        answer['report']['Findings'][0]['evidence_ids']=['K99']
    if mode=='product-final-reject' or (mode in ('product-final-reject-once','product-claim-unsupported-once','product-final-incomplete-revise-once') and 'INDEPENDENT FINAL REVIEW FAIL' not in sys.argv[-1]):
        answer['report']['Findings'][0]['text']='Source verifies clean installation and cross-version rollback.'
    if mode=='product-bad-evidence': answer['evidence'][0]['expected']='invented content'
    if mode=='product-side-effect': answer['evidence'].append({'id':'K2','claim':'command','round':'command','source':'touch SIDE_EFFECT; echo complete','expected':'complete'})
    if mode=='product-bad-evidence-once' and 'EVIDENCE AUDIT FAIL' not in sys.argv[-1]: answer['evidence'][0]['expected']='invented content'
    if mode in ('product-catalog','product-catalog-invalid-once'):
        chosen=next(k for k in q['evidence_catalog']['evidence'] if k['source'].startswith('README.md:'))
        for section in answer['report'].values():
            for item in section:
                if item['evidence_ids']: item['evidence_ids']=[chosen['id']]
        del answer['evidence']
        answer['evidence_refs']=[{'id':chosen['id'],'claim':'README source packet'}]
        if mode=='product-catalog-invalid-once' and 'EVIDENCE AUDIT FAIL' not in sys.argv[-1]: answer['evidence_refs'][0]['id']='K999'
    if mode=='product-author-critical': answer['decisions']=[{'id':'author-scope','kind':'scope','question':'Which requested outcome applies?','reason':'Source requirements conflict'}]
    if mode=='product-write': pathlib.Path('forbidden.txt').write_text('readonly violation')
    if mode=='product-budget':
        p=home/'runs'/os.environ['CADRE_RUN_ID']/'run.json'; r=json.loads(p.read_text()); r['budget_cap_usd']=0; p.write_text(json.dumps(r))
    if mode=='product-wait':
        (home/'pipeline-ready').touch(); time.sleep(60)
elif q.get('kind')=='review-acceptance':
    answer={'revision':q['revision'],'verdict':'PASS','acceptance':[{'id':c['id'],'status':'PASS','evidence':'Report and original source inspected separately'} for c in q['criteria']],
            'poll':[{'id':e['id'],'outcome':'verified','evidence':'README first line matches source'} for e in q.get('evidence') or [{'id':'K1'}]],
            'claims':[{'id':i['id'],'support':'full','rationale':'Cited range text supports the sentence',
                         'terms':[{'term':t,'status':'context'} for t in i['evidence_non_terms']]} for i in q.get('claims',[])],
            'requirements':[{'id':g['id'],'status':'satisfied','report':[q['report_elements'][0]['id']],'rationale':'The delivered report item covers it'} for g in q.get('requirements',[])]}
    if mode=='product-final-legacy': del answer['claims'], answer['requirements']
    if mode=='product-output-constraints':
        assert q['qa']['status']=='PASS'
        assert q['qa']['revision']['digest']==q['revision']
        assert q['qa']['counter']['evidence']==len(q['evidence'])
        for item in answer['requirements']: item['report']=[o['id'] for o in q['report_elements']]
    if mode=='product-claim-unsupported-once' and 'Source verifies clean installation' in q['report']:
        answer['claims'][0].update(support='absent',rationale='README line does not show clean installation or rollback')
    if mode=='product-final-bad-id': answer['poll'][0]['id']+='-verification'
    if mode=='product-final-protocol-once' and 'JUDGE PROTOCOL REPAIR' not in sys.argv[-1]: answer['claims']=answer['claims'][:-1]
    if mode=='product-final-incomplete-always': answer['claims']=answer['claims'][:-1]
    if mode=='product-final-protocol-then-reject':
        if 'JUDGE PROTOCOL REPAIR' in sys.argv[-1]: answer['acceptance'][0].update(status='FAIL',evidence='README quote does not support the claim'); answer['verdict']='REVISE'
        else: answer['claims']=answer['claims'][:-1]
    if mode=='product-final-incomplete-revise-once' and 'Source verifies clean installation' in q['report']:
        answer['claims'][0].update(support='absent',rationale='README line does not show clean installation or rollback')
        answer['claims']=answer['claims'][:1]; answer['verdict']='REVISE'
    if mode=='product-final-reject': answer['acceptance'][0]['status']='FAIL'; answer['verdict']='REVISE'
    if mode=='product-final-reject-once' and 'Source verifies clean installation' in q['report']:
        answer['acceptance'][0].update(status='FAIL',evidence='The matching README quote does not prove clean installation or cross-version rollback'); answer['verdict']='REVISE'
    if mode=='product-final-critical': answer['decisions']=[{'id':'final-purpose','kind':'purpose','question':'Which original purpose applies?','reason':'The original purpose remains ambiguous'}]
elif 'schema' in q or q.get('original_request_path'):
    answer=q['schema'] if 'schema' in q else json.loads((home/'legacy-plan.json').read_text())
    answer['job_kind']=(q.get('request') or {}).get('job_type',q.get('job_type','code'))
    if mode=='wrong-type': answer['job_kind']='research' if answer['job_kind']=='code' else 'code'
    answer['source_items']=[{'id':i['id'],'source_sha256':i.get('sha256',''),'tasks':['g1'],'status':'pending','evidence':'README.md inspected; checked source is not automatically trusted'} for i in q['backlog']['items']]
    if mode=='omit': answer['source_items']=answer['source_items'][:-1]
    if mode=='full-budget-estimate':
        for g in answer['tasks']: g['estimate_usd']=float(q['request']['budget_usd'])
    if mode=='schema-copy': answer['source_items']=q['schema']['source_items']
    if mode=='bad-hash': answer['source_items'][-1]['source_sha256']='0'*64
    if mode=='bad-hash-once' and not q.get('previous_rejection'): answer['source_items'][-1]['source_sha256']='0'*64
    if mode=='empty-evidence': answer['source_items'][-1]['evidence']='  '
    if mode=='nontext-evidence': answer['source_items'][-1]['evidence']=True
    if mode=='verified-dispatched': answer['source_items'][0]['status']='verified'
    if mode=='verified': answer['source_items'][0].update(status='verified',tasks=[])
    if mode=='all-verified':
        answer.update(kind='verified-backlog',tasks=[])
        for item in answer['source_items']: item.update(status='verified',tasks=[])
    if mode=='duplicate-source': answer['source_items'].append(answer['source_items'][0])
    if mode=='unmapped-task': answer['tasks'].append({**answer['tasks'][0],'id':'g2'})
    if mode=='report-code': answer['tasks'][0]['files']=['app.py']
    if mode=='routine': answer['assumptions']=[{'id':'style','choice':'Keep the existing README layout','reason':'A reversible presentation choice within the requested scope'}]
    if mode.startswith('critical-'):
        answer['decisions']=[{'id':'d1','kind':mode.removeprefix('critical-'),'question':'Which existing boundary applies?','reason':'The request does not identify this critical boundary'}]
    if mode=='multiple-decisions':
        answer['decisions']=[{'id':i,'kind':'authority','question':'Which boundary applies to '+i+'?','reason':'Authority is unspecified'} for i in ('d1','d2')]
    if q.get('change'): answer['tasks'][0]['definition']=q['change']
    if mode=='echo-question': answer={**q,'schema':answer}
    if mode=='undeclared-source-items': answer['source_items']=[{'id':'NEXT','source_sha256':'0'*64,'tasks':['g1'],'status':'pending','evidence':'BACKLOG.md NEXT'}]
    if mode=='empty-files' or (mode=='empty-files-once' and not q.get('previous_rejection')):
        answer['tasks'].append({**answer['tasks'][0],'id':'g2','definition':'Compile the review report','files':[],'dependent':['g1']})
    if mode=='plan-outside-sources' or (mode=='plan-outside-sources-once' and not q.get('previous_rejection')):
        answer['tasks'][0]['files']=['README.md','HANDOVER.md']
    if mode=='section-as-source-item-always' or (mode=='section-as-source-item' and not q.get('previous_rejection')):
        answer['source_items']=[{'source_sha256':'c'*64,'backlog_item_id':s['id'],'path':s['path'],'start_line':s['start_line'],'end_line':s['end_line']} for s in q['backlog']['sections']]
    if mode=='ids-in-tasks': answer['tasks'][0]['definition']+=' (BACKLOG.md NEXT)'
else:
    acceptance=[{'id':c['id'],'status':'PASS','evidence':'README.md and original request inspected separately'} for c in q['criteria']]
    red=mode=='reject' or (mode=='reject-once' and not q.get('change'))
    if red: acceptance[0].update(status='FAIL',evidence='README.md:1 claim contradicts the source')
    answer={'revision':q['revision'],'verdict':'REVISE' if red else 'PASS','acceptance':acceptance}
    if mode in ('review-critical','review-hidden-critical'): answer['decisions']=[{'id':'review-d1','kind':'purpose','question':'Which existing goal is intended?','reason':'The plan assumes a goal without request evidence'}]
    if mode=='review-malformed': answer['decisions']='guess'
if mode=='review-hidden-critical':
    answer=json.dumps({**answer,'acceptance':[]})+'\n'+json.dumps({**answer,'decisions':[]})
sid='ses_'+os.environ['CADRE_TASK_ID']
if mode=='product-wrapped' and q.get('kind')=='review-output':
    print(json.dumps({'type':'text','sessionID':sid,'part':{'type':'text','text':'Source files read {intermediate note}.'}}),flush=True)
    answer='```json\n'+json.dumps(answer)+'\n```'
if mode=='product-reused' and q.get('kind')=='review-acceptance':
    sid=json.loads((home/'runs'/os.environ['CADRE_RUN_ID']/'review-output.json').read_text())['session_id']
print(json.dumps({'type':'step_start','sessionID':sid,'part':{'type':'step-start'}}),flush=True)
print(json.dumps({'type':'text','sessionID':sid,'part':{'type':'text','text':answer if isinstance(answer,str) else json.dumps(answer)}}),flush=True)
''')

    def enter(self, manager):
        result = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        return result

    def test_code_plan_budget_policy_is_frozen_at_request_creation_and_review_policy_stays_separate(self):
        from limits import review_policy
        code = self.store.read(self.rid)
        self.assertEqual(code['review_limits'], review_policy('code'))
        self.assertEqual(code['review_limits']['plan-review']['pay_percent'], 100)
        review = self.store.request({**self.raw, 'submission_id': 'review-phase-policy', 'input_mode': 'goal',
                                     'prompt': 'ROUND: review\nInspect README without changing source.'})
        self.assertEqual(review['review_limits'], review_policy('review'))
        self.assertEqual(review['review_limits']['plan-review']['pay_percent'], 20)
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.store.update(code['run_id'], review_limits=review['review_limits'])
        self.assertEqual(self.store.request(self.raw)['review_limits'], code['review_limits'])

    def test_typed_implementation_shell_keeps_the_reviewed_task_file_scope(self):
        import shlex
        from test_run_engine import AdapterTests
        self.store.update(self.rid, state='cancelled')
        self.raw.update(submission_id='tool-scope', input_mode='goal', prompt='Update README.md only; preserve the other source files.')
        self.rid = self.store.request(self.raw)['run_id']
        self.approve()
        generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
        self.store.update(self.rid, worktree=str(self.repo))
        before = (self.repo / 'README.md').read_bytes()
        env = {**os.environ, **self.store.environment(self.rid), 'CADRE_TASK_ID': 'g1',
               'CADRE_ENGINE_ROOT': str(LIB.parent), 'HOME': str(self.home / 'tool-home')}
        Path(env['HOME']).mkdir()
        env.pop('CADRE_READ_ONLY', None)
        tools = [{'tool': 'bash', 'command': shlex.join([sys.executable, '-c', code])} for code in (
            "from pathlib import Path; p=Path('README.md'); p.write_bytes(p.read_bytes())",
            "from pathlib import Path; Path('unapproved.py').write_text('unauthorized')",
            "from pathlib import Path; Path('BACKLOG.md').unlink()",
        )]
        results, attempt = AdapterTests.project_tool_execute(self, self.repo, env, tools, model='opencode-go/glm-5.3')
        self.assertEqual(results[0]['status'], 0, results)
        for result in results[1:]:
            self.assertNotEqual(result['status'], 0, result)
        self.assertEqual((self.repo / 'README.md').read_bytes(), before)
        self.assertTrue((self.repo / 'BACKLOG.md').is_file())
        self.assertFalse((self.repo / 'unapproved.py').exists())
        policy = json.loads((attempt.directory / 'tool-policy.json').read_text())
        self.assertEqual(policy['write_paths'], [str(self.repo / 'README.md')])

    def test_request_proposal_checks_source_before_budget_observation(self):
        import qe_core as core
        for name, source in (('plain-source', self.home / 'plain-source'),
                             ('nested-source', self.repo / 'nested-source')):
            with self.subTest(name=name):
                source.mkdir()
                (source / 'README.md').write_text('Public review source without its own Git baseline.\n')
                project = self.home / 'projects' / name
                project.mkdir()
                (project / 'repo-path').write_text(str(source))
                rid = self.store.request({**self.raw, 'submission_id': name, 'project': name,
                                          'target_path': str(source)})['run_id']
                with patch.object(core, 'budget_json') as observation:
                    with self.assertRaisesRegex(ValueError, 'request-source-unavailable'):
                        request_proposal(self.store, rid)
                    observation.assert_not_called()
                record = self.store.read(rid)
                self.assertIsNone(record.get('request_permission'))
                self.assertFalse(record['tasks'])
                self.assertFalse((self.home / 'chain/request-budget.db').exists())

    def test_request_proposal_accepts_the_root_of_a_real_linked_worktree(self):
        import qe_core as core
        from run_store import plan_product
        target = self.home / 'linked-source'
        subprocess.run(['git', '-C', str(self.repo), 'worktree', 'add', '--detach', str(target), 'HEAD'],
                       check=True, capture_output=True)
        project = self.home / 'projects/linked-source'
        project.mkdir()
        (project / 'repo-path').write_text(str(target))
        rid = self.store.request({**self.raw, 'submission_id': 'linked-source', 'project': 'linked-source',
                                  'target_path': str(target)})['run_id']
        with patch.object(core, 'budget_json', return_value=None):
            proposal = request_proposal(self.store, rid)
        self.assertTrue((target / '.git').is_file())
        self.assertEqual(proposal['action']['source'], plan_product(target))
        self.assertEqual(proposal['action']['target_path'], str(target))
        self.assertIsNone(self.store.read(rid).get('request_permission'))

    def test_final_judge_capacity_is_required_before_execution_but_not_for_plan_only(self):
        import qe_core as core
        with patch.object(core.ROUTER, 'chain', side_effect=lambda role, **kwargs: ([], ['unavailable']) if role == 'quality' else (['opencode-go/glm-5.3'], ['fixture'])):
            proposal = request_proposal(self.store, self.rid)
            self.assertEqual(proposal['action']['models']['quality'], [])
            rid = self.store.request({**self.raw, 'submission_id': 'requires-final-judge', 'input_mode': 'goal'})['run_id']
            with self.assertRaisesRegex(ValueError, 'final-review-no-capacity'):
                request_proposal(self.store, rid)
        record = self.store.read(rid)
        self.assertIsNone(record.get('request_permission'))
        self.assertFalse(record['tasks'])
        self.assertFalse((self.home / 'chain/request-budget.db').exists())

    def test_review_tools_read_only_accepted_plan_files_and_cannot_recover_other_files_via_git(self):
        import shlex
        import uuid

        from session import cleanup_tool_scratch, project_tool_policy
        self.rid = self.store.request({**self.raw, 'submission_id': 'review-read-scope', 'input_mode': 'goal',
                                       'prompt': 'Explain README only; do not make changes.'})['run_id']
        self.approve()
        generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
        before = {name: (self.repo / name).read_bytes() for name in ('README.md', 'BACKLOG.md')}
        for role, task in (('impl', 'review-author-abcdef012345'), ('quality', 'review-final-abcdef012345')):
            with self.subTest(role=role):
                env = {**os.environ, **self.store.environment(self.rid), 'CADRE_TASK_ID': task}
                directory = self.store.task_dir(self.rid, task) / 'attempts' / uuid.uuid4().hex
                policy = project_tool_policy(self.store, self.store.read(self.rid), directory, str(self.repo), 'opencode', role, env)
                self.assertIsNotNone(policy)
                self.addCleanup(cleanup_tool_scratch, self.store, policy)
                def command(text, policy=policy, env=env):
                    return subprocess.run([policy['tool'], text], cwd=self.repo, env=env, text=True,
                                          capture_output=True, timeout=20, check=False)
                allowed = command('cat README.md')
                self.assertEqual((allowed.returncode, allowed.stdout), (0, before['README.md'].decode()), allowed.stderr)
                for text in ('cat BACKLOG.md', 'git show HEAD:README.md', 'printf modified > README.md',
                             shlex.join([sys.executable, '-c', "from pathlib import Path;print(Path('BACKLOG.md').read_text())"])):
                    denied = command(text)
                    self.assertNotEqual(denied.returncode, 0, (text, denied.stdout))
                    self.assertNotIn(before['BACKLOG.md'].decode(), denied.stdout)
                self.assertEqual(policy['read_paths'], [str(self.repo.resolve() / 'README.md')])
        self.assertEqual({name: (self.repo / name).read_bytes() for name in before}, before)

    def test_source_section_is_an_explicit_review_only_card_selection_bound_to_the_request(self):
        from run_store import RequestError, request_backlog
        raw = {**self.raw, 'input_mode': 'goal', 'prompt': 'ROUND: review\nInspect README against the NEXT card only.'}
        rid = self.store.request({**raw, 'submission_id': 'section-ok', 'backlog_path': 'BACKLOG.md#NEXT'})['run_id']
        request = self.store.read(rid)['request']
        self.assertEqual((request['backlog_path'], request['backlog_items']), (str(self.repo.resolve() / 'BACKLOG.md'), ['NEXT']))
        self.assertEqual((self.store.path(rid) / 'backlog.md').read_text(), self.backlog)
        context = request_backlog(self.store, rid)
        self.assertEqual((context['items'], context['section_only']), ([], True))
        self.assertNotIn('full_path', context)
        self.assertEqual(context['text'], '3\t### [ ] NEXT — Pending item\n4\tUpdate README.md.')
        self.assertEqual([(s['id'], s['path'], s['start_line'], s['end_line']) for s in context['sections']],
                         [('NEXT', 'BACKLOG.md', 3, 4)])
        for entry_name, change in (('missing-card', {'backlog_path': 'BACKLOG.md#MISSING'}), ('empty-selector', {'backlog_path': 'BACKLOG.md#'}),
                               ('whole-file', {'backlog_path': 'BACKLOG.md'}), ('repeated', {'backlog_path': 'BACKLOG.md#NEXT,NEXT'}),
                               ('code-job', {'backlog_path': 'BACKLOG.md#NEXT', 'prompt': 'Update README.md only.'}),
                               ('plan-only', {'backlog_path': 'BACKLOG.md#NEXT', 'input_mode': 'plan_only'}),
                               ('backlog-mode', {'backlog_path': 'BACKLOG.md#NEXT', 'input_mode': 'backlog'})):
            with self.subTest(entry_name=entry_name), self.assertRaises(RequestError):
                self.store.request({**raw, 'submission_id': 'section-' + entry_name, **change})
        (self.repo / 'BACKLOG.md').write_text(self.backlog.replace('Update README.md.', 'Update every file.'))
        with self.assertRaisesRegex(ValueError, 'request-backlog-changed'):
            request_backlog(self.store, rid)

    def test_planner_that_maps_the_selected_section_as_a_source_item_gets_a_precise_single_retry(self):
        self.rid = self.store.request({**self.raw, 'submission_id': 'section-plan-retry', 'input_mode': 'goal',
                                       'backlog_path': 'BACKLOG.md#NEXT',
                                       'prompt': 'ROUND: review\nInspect README against the NEXT card only.'})['run_id']
        self.approve()
        (self.home / 'pipeline-mode').write_text('section-as-source-item')
        generate_request_plan(self.store, self.rid, str(self.fake))
        plan = self.store.plan_view(self.rid)['history'][-1]['plan']
        self.assertEqual(plan['source_items'], [])
        retry = [c for c in self.calls() if c.get('previous_rejection')]
        self.assertEqual(len(retry), 1)
        self.assertIn('source_items=[]', retry[0]['previous_rejection'])
        self.assertIn('source section', retry[0]['previous_rejection'])
        self.assertNotIn('None', retry[0]['previous_rejection'])
        self.assertTrue(all(set(section) == {'id', 'path', 'start_line', 'end_line'} for section in self.calls()[0]['backlog']['sections']),
                        'a selected section does not carry a hash like a source item')

    def test_section_request_keeps_an_empty_source_item_rule_from_schema_to_retry_without_silent_repair(self):
        from run_store import request_backlog, request_plan_coverage
        raw = {**self.raw, 'input_mode': 'goal', 'backlog_path': 'BACKLOG.md#NEXT',
               'prompt': 'ROUND: review\nInspect README against the NEXT card only.'}
        self.rid = self.store.request({**raw, 'submission_id': 'section-coverage-fails'})['run_id']
        self.approve()
        backlog = request_backlog(self.store, self.rid)
        with self.assertRaisesRegex(ValueError, 'request-backlog-coverage'):
            request_plan_coverage({'tasks': [{'id': 'g1'}], 'source_items': [
                {'id': 'NEXT', 'source_sha256': '0' * 64, 'tasks': ['g1'], 'status': 'pending', 'evidence': 'card'}]}, backlog)
        self.assertEqual(request_plan_coverage({'tasks': [{'id': 'g1'}], 'source_items': []}, backlog)['verified'], [])
        (self.home / 'pipeline-mode').write_text('section-as-source-item-always')
        with self.assertRaisesRegex(ValueError, 'request-backlog-coverage'):
            generate_request_plan(self.store, self.rid, str(self.fake))
        calls = self.calls()
        self.assertEqual(len(calls), 2, 'one retry, no third paid planner call')
        self.assertEqual(calls[0]['schema']['source_items'], [])
        self.assertIn('source_items=[]', calls[1]['previous_rejection'])
        self.assertFalse(self.store.plan_view(self.rid)['history'], 'a plan that maps the section is never saved by stripping it')
        self.assertIn('source_items=[]', (self.home / 'pipeline-prompt.txt').read_text().split('SOURCE SECTION', 1)[1])

    def test_review_report_task_without_source_files_gets_a_named_retry_and_is_never_filled_in_silently(self):
        raw = {**self.raw, 'input_mode': 'goal', 'backlog_path': 'BACKLOG.md#NEXT', 'source_paths': 'README.md',
               'prompt': 'ROUND: review\nInspect README against the NEXT card only.'}
        self.rid = self.store.request({**raw, 'submission_id': 'empty-files-once'})['run_id']
        self.approve()
        (self.home / 'pipeline-mode').write_text('empty-files-once')
        generate_request_plan(self.store, self.rid, str(self.fake))
        retry = [c for c in self.calls() if c.get('previous_rejection')]
        self.assertEqual(len(retry), 1)
        for part in ('g2', 'files', 'README.md', 'BACKLOG.md'):
            self.assertIn(part, retry[0]['previous_rejection'])
        self.assertNotIn('explicit file list required', retry[0]['previous_rejection'])
        self.assertTrue(all(g['files'] for g in self.store.plan_view(self.rid)['history'][-1]['plan']['tasks']))
        self.rid = self.store.request({**raw, 'submission_id': 'empty-files-always'})['run_id']
        self.approve()
        before = len(self.calls())
        (self.home / 'pipeline-mode').write_text('empty-files')
        with self.assertRaisesRegex(ValueError, 'request-plan-invalid'):
            generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(len(self.calls()) - before, 2)
        self.assertFalse(self.store.plan_view(self.rid)['history'], 'an empty file list is never filled in by the engine')
        self.assertIn('Every task must list files', (self.home / 'pipeline-prompt.txt').read_text().split('REVIEW DELIVERY', 1)[1])

    def test_explicit_review_sources_are_validated_bound_and_cannot_be_widened_by_the_plan(self):
        from run_store import RequestError
        (self.repo / 'HANDOVER.md').write_text('handoff outside the requested scope\n')
        raw = {**self.raw, 'input_mode': 'goal', 'backlog_path': 'BACKLOG.md#NEXT', 'source_paths': 'README.md',
               'prompt': 'ROUND: review\nInspect README against the NEXT card only.'}
        self.rid = self.store.request({**raw, 'submission_id': 'sources-ok'})['run_id']
        self.assertEqual(self.store.read(self.rid)['request']['source_paths'], ['README.md'])
        (self.repo / 'scope-link').symlink_to(self.repo / 'README.md')
        for entry_name, change in (('outside', {'source_paths': '../outside.md'}), ('absolute', {'source_paths': '/etc/hosts'}),
                               ('missing', {'source_paths': 'missing.md'}), ('git', {'source_paths': '.git/HEAD'}),
                               ('section-file', {'source_paths': 'README.md, BACKLOG.md'}), ('repeat', {'source_paths': 'README.md,README.md'}),
                               ('symlink', {'source_paths': 'scope-link'}), ('code-job', {'prompt': 'Update README.md only.', 'backlog_path': ''})):
            with self.subTest(entry_name=entry_name), self.assertRaises(RequestError):
                self.store.request({**raw, 'submission_id': 'sources-' + entry_name, **change})
        self.approve()
        (self.home / 'pipeline-mode').write_text('plan-outside-sources-once')
        generate_request_plan(self.store, self.rid, str(self.fake))
        retry = [c for c in self.calls() if c.get('previous_rejection')]
        self.assertEqual(len(retry), 1)
        self.assertIn('request-source-scope', retry[0]['previous_rejection'])
        self.assertIn('HANDOVER.md', retry[0]['previous_rejection'])
        self.assertEqual({n for g in self.store.plan_view(self.rid)['history'][-1]['plan']['tasks'] for n in g['files']}, {'README.md'})
        rid = self.store.request({**raw, 'submission_id': 'sources-widened'})['run_id']
        self.rid = rid
        self.approve()
        (self.home / 'pipeline-mode').write_text('plan-outside-sources')
        with self.assertRaisesRegex(ValueError, 'request-source-scope'):
            generate_request_plan(self.store, rid, str(self.fake))
        self.assertFalse(self.store.plan_view(rid)['history'])

    def test_every_reading_role_is_limited_to_the_explicit_request_sources(self):
        import uuid

        from session import cleanup_tool_scratch, project_tool_policy, review_source_paths
        (self.repo / 'HANDOVER.md').write_text('handoff outside the requested scope\n')
        self.rid = self.store.request({**self.raw, 'submission_id': 'sources-read-scope', 'input_mode': 'goal',
                                       'backlog_path': 'BACKLOG.md#NEXT', 'source_paths': 'README.md',
                                       'prompt': 'ROUND: review\nInspect README against the NEXT card only.'})['run_id']
        self.approve()
        generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
        readme = (self.repo / 'README.md').read_text()
        for role, task in (('plan', 'plan-input-abcdef012345'), ('review', 'plan-review-1-abcdef01'),
                           ('impl', 'review-author-abcdef012345'), ('quality', 'review-final-abcdef012345')):
            with self.subTest(role=role):
                env = {**os.environ, **self.store.environment(self.rid), 'CADRE_TASK_ID': task}
                directory = self.store.task_dir(self.rid, task) / 'attempts' / uuid.uuid4().hex
                policy = project_tool_policy(self.store, self.store.read(self.rid), directory, str(self.repo), 'opencode', role, env)
                self.addCleanup(cleanup_tool_scratch, self.store, policy)
                def command(text, policy=policy, env=env):
                    return subprocess.run([policy['tool'], text], cwd=self.repo, env=env, text=True,
                                          capture_output=True, timeout=20, check=False)
                allowed = command('cat README.md')
                self.assertEqual((allowed.returncode, allowed.stdout), (0, readme), allowed.stderr)
                self.assertIn('README.md', command('ls').stdout)
                for text in ('cat HANDOVER.md', 'cat BACKLOG.md', 'grep -rn handoff .'):
                    denied = command(text)
                    self.assertNotIn('handoff outside', denied.stdout, (role, text))
                    self.assertNotIn('Existing item', denied.stdout, (role, text))
        root = self.repo.resolve()
        with self.assertRaisesRegex(PermissionError, 'review-read-scope-invalid'):
            review_source_paths({'tasks': [{'files': ['README.md', 'HANDOVER.md']}]}, root, None, ['README.md'])

    def test_every_reading_role_is_denied_the_whole_section_file_its_git_copy_and_the_run_snapshot(self):
        import shlex
        import uuid

        from session import cleanup_tool_scratch, project_tool_policy
        self.rid = self.store.request({**self.raw, 'submission_id': 'section-read-scope', 'input_mode': 'goal',
                                       'backlog_path': 'BACKLOG.md#NEXT',
                                       'prompt': 'ROUND: review\nInspect README against the NEXT card only.'})['run_id']
        self.approve()
        generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
        snapshot = self.store.path(self.rid) / 'backlog.md'
        readme = (self.repo / 'README.md').read_text()
        for role, task in (('plan', 'plan-input-abcdef012345'), ('review', 'plan-review-1-abcdef01'),
                           ('impl', 'review-author-abcdef012345'), ('quality', 'review-final-abcdef012345')):
            with self.subTest(role=role):
                env = {**os.environ, **self.store.environment(self.rid), 'CADRE_TASK_ID': task, 'VIRTUAL_ENV': str(self.home)}
                directory = self.store.task_dir(self.rid, task) / 'attempts' / uuid.uuid4().hex
                policy = project_tool_policy(self.store, self.store.read(self.rid), directory, str(self.repo), 'opencode', role, env)
                self.addCleanup(cleanup_tool_scratch, self.store, policy)
                def command(text, policy=policy, env=env):
                    return subprocess.run([policy['tool'], text], cwd=self.repo, env=env, text=True,
                                          capture_output=True, timeout=20, check=False)
                allowed = command('cat README.md')
                self.assertEqual((allowed.returncode, allowed.stdout), (0, readme), allowed.stderr)
                for text in ('cat BACKLOG.md', 'grep -n NEXT BACKLOG.md', 'git show HEAD:README.md',
                             'cat ' + shlex.quote(str(snapshot)),
                             shlex.join([sys.executable, '-c', "from pathlib import Path;print(Path('BACKLOG.md').read_text())"])):
                    denied = command(text)
                    self.assertNotEqual(denied.returncode, 0, (text, denied.stdout))
                    self.assertNotIn('Existing item', denied.stdout)

    def test_section_review_gives_every_role_only_the_selected_card_and_delivers_with_semantic_decisions(self):
        from outcome import approved
        self.rid = self.store.request({**self.raw, 'submission_id': 'section-review', 'input_mode': 'goal',
                                       'backlog_path': 'BACKLOG.md#NEXT', 'source_paths': 'README.md',
                                       'prompt': 'ROUND: review\nInspect README against the NEXT card only.'})['run_id']
        self.approve()
        import report_mode
        with (patch('acceptance.qa_gate', side_effect=AssertionError('review must not run code QA')),
              patch.object(report_mode, 'claim_package', wraps=report_mode.claim_package) as package):
            self.assertEqual(execute_run(self.store, self.rid), 0, self.store.read(self.rid).get('reason'))
        record = self.store.read(self.rid)
        self.assertTrue(approved(record))
        context = package.call_args.kwargs
        self.assertEqual((context['scope_names'], context['section_names']), (['README.md'], ['BACKLOG.md']))
        self.assertIn('Inspect README against the NEXT card only.', context['request'])
        self.assertIn(self.raw['acceptance'], context['request'])
        prompts = {p.parts[-4].rsplit('-', 1)[0]: p.read_text() for p in self.store.path(self.rid).glob('tasks/*/attempts/*/prompt.md')}
        self.assertEqual({k.removesuffix('-1') for k in prompts}, {'plan-input', 'plan-review', 'review-author', 'review-final'})
        for task in ('review-author', 'review-final'):
            with self.subTest(role_outside=task):
                self.assertNotIn('source_items:[{id', prompts[task])
                self.assertNotIn('use source_items=[] in the plan', prompts[task])
                self.assertNotIn('plan task files', prompts[task])
                self.assertIn('READING SCOPE', prompts[task])
                parts = [json.loads(l) for l in prompts[task].splitlines() if l.startswith('{')]
                job_request = next(p for p in parts if p.get('kind') in ('review-output', 'review-acceptance'))
                self.assertNotIn('backlog', job_request, 'the selected card appears once in shared source context')
                self.assertEqual(parts[-1]['backlog']['text'], '3\t### [ ] NEXT — Pending item\n4\tUpdate README.md.')
        for task, text in prompts.items():
            with self.subTest(task=task):
                self.assertIn('### [ ] NEXT — Pending item', text)
                self.assertIn('Update README.md.', text)
                self.assertNotIn('Existing item', text)
                self.assertNotIn('README.md must retain', text)
                self.assertIn('SOURCE SECTION', text)
        final = self.calls()[-1]
        self.assertEqual([o['id'] for o in final['report_elements']], ['Findings-1', 'Uncertainties-1', 'Method-1'])
        self.assertEqual([i['id'] for i in final['claims']], ['Findings-1', 'Method-1'])
        self.assertIn((self.repo / 'README.md').read_text().splitlines()[0], final['evidence_texts']['K1'])
        self.assertEqual(final['claims'][0]['evidence_ids'], ['K1'])
        self.assertTrue(all('text' not in i and 'code_lines' not in i for i in final['claims']),
                        'source lines in evidence_texts are the single source of truth')
        by_id = {o['id']: o for o in final['report_elements']}
        self.assertEqual(by_id['Findings-1'], {'id': 'Findings-1', 'section': 'Findings', 'evidence_ids': ['K1']})
        self.assertTrue(all(by_id[i['id']]['evidence_ids'] == i['evidence_ids'] for i in final['claims']))
        self.assertEqual(json.dumps(final, ensure_ascii=False).count('README source preserved'), 1,
                         'the judge receives the exact rendered sentence once, with section/item identifiers retained')
        output = json.loads((self.store.path(self.rid) / 'review-output.json').read_text())
        self.assertEqual(final['report'], output['report'])
        expanded = {**final, 'report_elements': report_mode.review_elements(output['report_input'])}
        self.assertLess(len(json.dumps(final, ensure_ascii=False)), len(json.dumps(expanded, ensure_ascii=False)))
        self.assertEqual([g['id'] for g in final['requirements']][:1], ['R1'])
        review = record['final_result']['review']
        self.assertEqual({a['id']: a['status'] for a in review['acceptance'] if a['id'] in ('claim_support', 'request_scope')},
                         {'claim_support': 'PASS', 'request_scope': 'PASS'})

    def test_judge_pass_with_an_unsupported_claim_is_a_rejection_that_uses_the_one_repair(self):
        from outcome import approved
        self.rid = self.store.request({**self.raw, 'submission_id': 'claim-support-repair', 'input_mode': 'goal',
                                       'prompt': 'ROUND: review\nInspect README without changes.'})['run_id']
        self.approve()
        (self.home / 'pipeline-mode').write_text('product-claim-unsupported-once')
        self.assertEqual(execute_run(self.store, self.rid), 0, self.store.read(self.rid).get('reason'))
        self.assertTrue(approved(self.store.read(self.rid)))
        self.assertEqual(len(self.calls()), 6, 'one repair; two independent final judgments')
        rejected = json.loads(next(self.store.path(self.rid).glob('review-rejected-*.json')).read_text())['result']['review']
        self.assertEqual((rejected['verdict'], rejected['status']), ('REVISE', 'FAIL'))
        self.assertIn('Findings-1', next(a['evidence'] for a in rejected['acceptance'] if a['id'] == 'claim_support'))
        self.assertIn('claim_support', (self.home / 'pipeline-author-prompt.txt').read_text())

    def test_legacy_judge_pass_without_semantic_decisions_is_not_delivered_or_repaid_on_resume(self):
        from outcome import approved
        self.rid = self.store.request({**self.raw, 'submission_id': 'legacy-judge', 'input_mode': 'goal',
                                       'prompt': 'ROUND: review\nInspect README without changes.'})['run_id']
        self.approve()
        (self.home / 'pipeline-mode').write_text('product-final-legacy')
        self.assertEqual(execute_run(self.store, self.rid), 78)
        record = self.store.read(self.rid)
        self.assertFalse(approved(record))
        self.assertEqual(record['final_result']['review']['status'], 'ERROR')
        self.assertIn('semantic decision', record['final_result']['review']['reason'])
        calls = len(self.calls())
        (self.home / 'pipeline-mode').write_text('')
        self.assertEqual(execute_run(self.store, self.rid, proceed=True), 78)
        self.assertEqual(len(self.calls()), calls)

    def test_review_approval_requires_the_engine_semantic_decisions_in_the_delivered_result(self):
        from run_store import atomic_json
        from outcome import approved
        self.rid = self.store.request({**self.raw, 'submission_id': 'semantic-approval', 'input_mode': 'goal',
                                       'prompt': 'ROUND: review\nInspect README without changes.'})['run_id']
        self.approve()
        self.assertEqual(execute_run(self.store, self.rid), 0, self.store.read(self.rid).get('reason'))
        record = self.store.read(self.rid)
        self.assertTrue(approved(record))
        artifact = Path(record['review_delivery']['artifact'])
        delivery = json.loads(artifact.read_text())
        for data in (delivery['result'], record['final_result']):
            data['review']['acceptance'] = [a for a in data['review']['acceptance'] if a['id'] not in ('claim_support', 'request_scope')]
        atomic_json(artifact, delivery)
        from run_store import plan_digest
        record['review_delivery']['digest'] = plan_digest(delivery)
        self.assertFalse(approved(record), 'a delivered PASS without separate claim and coverage decisions is not acceptance')

    def test_review_read_scope_keeps_explicit_backlog_and_rejects_invalid_plan_paths_without_a_fallback(self):
        from session import review_source_paths
        root = self.repo.resolve()
        plan = {'tasks': [{'files': ['README.md']}]}
        self.assertEqual(review_source_paths(plan, root, {'full_path': str(root / 'BACKLOG.md')}),
                         [root / 'BACKLOG.md', root / 'README.md'])
        for invalid in ({}, {'tasks': []}, {'tasks': [{'files': ['../outside']}]},
                        {'tasks': [{'files': ['.git/HEAD']}]}):
            with self.subTest(plan=invalid), self.assertRaisesRegex(PermissionError, 'review-read-scope-'):
                review_source_paths(invalid, root)
        (root / 'scope-link').symlink_to(root / 'README.md')
        with self.assertRaisesRegex(PermissionError, 'review-read-scope-invalid'):
            review_source_paths({'tasks': [{'files': ['scope-link']}]}, root)

    def test_review_output_format_reaches_the_planner_and_plan_reviewer_without_changing_code_requests(self):
        from run_store import REVIEW_OUTPUT_RULE
        from report_mode import REVIEW_SECTIONS
        for section in REVIEW_SECTIONS:
            self.assertIn(section, REVIEW_OUTPUT_RULE)
        for kind, prompt in (('review', 'ROUND: review\nInspect README only without changes.'),
                             ('code', 'Update README only.')):
            with self.subTest(kind=kind):
                self.rid = self.store.request({**self.raw, 'submission_id': 'format-' + kind, 'prompt': prompt})['run_id']
                self.approve()
                generate_request_plan(self.store, self.rid, str(self.fake))
                self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
                for task in ('plan-input-*', 'plan-review-*'):
                    text = next(self.store.path(self.rid).glob('tasks/' + task + '/attempts/*/prompt.md')).read_text()
                    self.assertEqual(REVIEW_OUTPUT_RULE in text, kind == 'review', (kind, task))

    def test_supervision_policy_is_snapshotted_in_the_typed_request_and_bound_permission(self):
        from run_store import atomic_json, plan_digest
        policy_path = self.home / 'projects/lab/supervision.json'
        policy = {'version': 1, 'mode': 'step'}
        atomic_json(policy_path, policy)
        self.rid = self.store.request({**self.raw, 'submission_id': 'supervised-request'})['run_id']
        proposal = self.approve()
        self.assertEqual(proposal['action']['supervision'], policy)
        policy_path.unlink()
        self.assertEqual(request_proposal(self.store, self.rid)['action']['supervision'], policy)
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.store.update(self.rid, supervision=None)
        changed = {**proposal, 'action': {**proposal['action'], 'supervision': None}}
        changed['token'] = plan_digest(changed['action'])
        with self.assertRaisesRegex(ValueError, 'request-proposal-changed'):
            request_permission(self.store, self.rid, changed)

    def approve(self):
        proposal = request_proposal(self.store, self.rid)
        first = request_permission(self.store, self.rid, proposal)
        self.assertEqual(first['status'], 'first-approved')
        second = request_permission(self.store, self.rid, proposal, first['confirmation'])
        self.assertEqual(second['status'], 'approved')
        return proposal

    def test_qa_profile_is_frozen_bound_and_checked_before_external_observation(self):
        from run_store import atomic_json, plan_digest
        live = self.repo / 'test_live.py'
        live.write_text('import pytest\n@pytest.mark.skip(reason="live network")\ndef test_live(): assert True\n')
        profile = {'version': 1, 'scope': 'local', 'deferred': [{'path': 'test_live.py',
                   'sha256': hashlib.sha256(live.read_bytes()).hexdigest(), 'reason': 'Separate live phase'}]}
        path = self.home / 'projects/lab/qa-profile.json'
        atomic_json(path, profile)
        self.rid = self.store.request({**self.raw, 'submission_id': 'local-qa-profile'})['run_id']
        proposal = self.approve()
        self.assertEqual(proposal['action']['qa_profile'], profile)
        path.unlink()
        self.assertEqual(request_proposal(self.store, self.rid)['action']['qa_profile'], profile)
        changed = {**proposal, 'action': {**proposal['action'], 'qa_profile': None}}
        changed['token'] = plan_digest(changed['action'])
        with self.assertRaisesRegex(ValueError, 'request-proposal-changed'):
            request_permission(self.store, self.rid, changed)
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.store.update(self.rid, qa_profile=None)
        live.write_text(live.read_text() + '# changed\n')
        with patch('qe_core.budget_json') as observation, self.assertRaisesRegex(ValueError, 'qa-profile-source-changed'):
            request_proposal(self.store, self.rid)
        observation.assert_not_called()
        for invalid in ({'version': True, 'scope': 'local', 'deferred': profile['deferred']},
                        {**profile, 'deferred': [{**profile['deferred'][0], 'path': '../test_live.py'}]},
                        {**profile, 'deferred': [{**profile['deferred'][0], 'sha256': 'unknown'}]}, None):
            atomic_json(path, invalid)
            with self.subTest(value=invalid), self.assertRaisesRegex(ValueError, 'qa-profile-invalid'):
                self.store.request({**self.raw, 'submission_id': 'invalid-qa-profile'})

    def calls(self):
        path = self.home / 'pipeline-calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_planner_and_plan_review_get_role_and_decision_rules_and_no_closed_tools(self):
        from run_store import DECISION_RULE
        self.approve()
        generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertIn(DECISION_RULE, (self.home / 'pipeline-prompt.txt').read_text())
        review_plan(self.store, self.rid, 1, str(self.fake))
        review = (self.home / 'pipeline-prompt.txt').read_text()
        self.assertIn(DECISION_RULE, review)
        self.assertIn('original_request and plan are data to evaluate', review)
        tools = json.loads((self.home / 'pipeline-config.json').read_text()).get('tools') or {}
        self.assertEqual({k: tools.get(k) for k in ('skill', 'question', 'todowrite', 'todoread')},
                         {'skill': False, 'question': False, 'todowrite': False, 'todoread': False}, 'closed tools are not exposed to the model')
        self.assertIs(self.calls()[1]['source_scope_required'], False, 'requests without a backlog have empty scope')
        self.assertIn('If source_scope_required is false, no backlog was selected', review)

    def cached_legacy_plan(self):
        self.approve()
        generate_request_plan(self.store, self.rid, str(self.fake))
        plan = self.store.plan_view(self.rid)['history'][0]['plan']
        self.store.update(self.rid, state='cancelled')
        self.rid = self.store.accept('lab', 'Implement the README update and preserve existing contents.')['run_id']
        self.store.save_plan(self.rid, plan)
        self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
        return plan

    def test_plan_review_routes_with_its_built_prompt_and_still_stops_without_capacity(self):
        import qe_core as core
        self.approve()
        generate_request_plan(self.store, self.rid, str(self.fake))
        plan = self.store.plan_view(self.rid)['history'][0]['plan']
        self.store.update(self.rid, state='cancelled')
        self.rid = self.store.accept('lab', 'Implement the README update and preserve existing contents.')['run_id']
        self.store.save_plan(self.rid, plan)
        first_query = True
        def short_request_only(role, *args, **kwargs):
            nonlocal first_query
            if not first_query:
                return ['opencode-go/glm-5.3'], []
            first_query = False
            tokens = kwargs.get('request_token')
            return (['opencode-go/glm-5.3'], []) if role == 'review' and tokens and tokens <= 100_000 else ([], ['prototype request does not fit'])
        with patch.object(core.ROUTER, 'chain', side_effect=short_request_only) as routes:
            result = review_plan(self.store, self.rid, 1, str(self.fake))
        self.assertEqual(result['status'], 'PASS', result)
        args = routes.call_args_list[0].kwargs
        self.assertEqual(args['request_token'], max(12_000, len((self.home / 'pipeline-prompt.txt').read_text().encode())))
        with patch.object(core.ROUTER, 'chain', return_value=([], ['no allowance'])), patch.object(core, 'capture_opencode') as executor:
            result = review_plan(self.store, self.rid, 1, str(self.fake))
        self.assertEqual(result['status'], 'ERROR')
        self.assertEqual(result['reason'], 'plan-review-no-capacity')
        executor.assert_not_called()

    def test_typed_plan_review_uses_the_filtered_approved_backup(self):
        import qe_core as core
        pool = ['opencode-go/glm-5.3', 'opencode-go/qwen3.8-max']
        with patch.object(core.ROUTER, 'chain', return_value=(pool, [])):
            self.approve()
            generate_request_plan(self.store, self.rid, str(self.fake))
        with patch.object(core.ROUTER, 'chain', return_value=([pool[1]], ['first candidate unavailable'])), \
                patch.object(core, 'capture_opencode', wraps=core.capture_opencode) as executor:
            result = review_plan(self.store, self.rid, 1, str(self.fake))
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(executor.call_args.args[2][1], pool[1])
        self.assertEqual(self.store.read(self.rid)['request_permission']['action']['models']['review'], pool)

    def test_typed_approval_binds_the_openrouter_endpoint_policy(self):
        import run_store
        import qe_core as core
        from run_store import plan_digest, plan_ready, request_authorized
        sol = 'openrouter/openai/gpt-6.1-sol'

        def chain(role, **kwargs):
            return ([sol], ['fixture']) if role in ('review', 'quality') else (['opencode-go/glm-5.3'], ['fixture'])
        with patch.object(core.ROUTER, 'chain', side_effect=chain):
            proposal = request_proposal(self.store, self.rid)
            policy = json.loads(run_store.OR_POLICY.read_text())['openai/gpt-6.1-sol']
            self.assertEqual(proposal['action']['or_policy'], {sol: policy})
            self.assertEqual((policy['tiers'], policy['max_price']), (['openai'], {'prompt': 2, 'completion': 10}))
            first = request_permission(self.store, self.rid, proposal)
            request_permission(self.store, self.rid, proposal, first['confirmation'])
        record = self.store.read(self.rid)
        self.assertTrue(request_authorized(record))
        self.assertTrue(request_authorized(record, sol))
        changed = self.home / 'policy-changed.json'
        changed.write_text(json.dumps({'openai/gpt-6.1-sol': {**policy, 'tiers': ['openai/flex']}}))
        with patch.object(run_store, 'OR_POLICY', changed):
            self.assertFalse(request_authorized(record), 'changed endpoint policy does not inherit old permission')
            with patch.object(core.ROUTER, 'chain', side_effect=chain), \
                    self.assertRaisesRegex(ValueError, 'request-proposal-changed'):
                request_permission(self.store, self.rid, proposal, first['confirmation'])
            with self.assertRaisesRegex(ValueError, 'request-plan-gate'):
                plan_ready(self.store, self.rid, str(self.fake))
        self.assertTrue(request_authorized(self.store.read(self.rid)))
        legacy = json.loads(json.dumps(record))
        del legacy['request_permission']['action']['or_policy']
        legacy['request_permission']['token'] = plan_digest(legacy['request_permission']['action'])
        self.assertFalse(request_authorized(legacy), 'legacy permission without recorded endpoint policy does not authorize the OpenRouter pool')
        legacy['request_permission']['action']['models'] = {
            role: ['opencode-go/glm-5.3'] for role in legacy['request_permission']['action']['models']}
        legacy['request_permission']['token'] = plan_digest(legacy['request_permission']['action'])
        self.assertTrue(request_authorized(legacy), 'legacy permission format preserved in non-OpenRouter pools')

    def test_typed_plan_review_never_launches_without_an_eligible_approved_candidate(self):
        import qe_core as core
        self.approve()
        generate_request_plan(self.store, self.rid, str(self.fake))
        for eligible in ([], ['opencode-go/qwen3.8-max']):
            with self.subTest(eligible=eligible), \
                    patch.object(core.ROUTER, 'chain', return_value=(eligible, ['no approved capacity'])), \
                    patch.object(core, 'capture_opencode') as executor:
                result = review_plan(self.store, self.rid, 1, str(self.fake))
            self.assertEqual((result['status'], result['reason']), ('ERROR', 'plan-review-no-capacity'))
            executor.assert_not_called()

    def test_typed_plan_capacity_wait_is_not_reported_as_a_rejected_plan(self):
        import qe_core as core
        from run_store import request_pipeline
        self.approve()
        generate_request_plan(self.store, self.rid, str(self.fake))
        with patch.object(core.ROUTER, 'chain', return_value=([], ['no approved capacity'])), \
                patch.object(core, 'capture_opencode') as executor:
            self.assertEqual(request_pipeline(self.store, self.rid, str(self.fake)), 78)
        record = self.store.read(self.rid)
        self.assertEqual((record['state'], record['planning']['status'], record['reason']),
                         ('retryable', 'provider-wait', 'plan-review-no-capacity'))
        self.assertIsNotNone(record['retry_after'])
        executor.assert_not_called()

    def test_typed_plan_review_can_use_an_approved_backup_after_a_provider_outage(self):
        import qe_core as core
        from budget_gate import ProviderWait
        pool = ['opencode-go/glm-5.3', 'opencode-go/qwen3.8-max']
        with patch.object(core.ROUTER, 'chain', return_value=(pool, [])):
            self.approve()
            generate_request_plan(self.store, self.rid, str(self.fake))
        capture = core.capture_opencode
        tried = []
        def first_route_outage(*args, **kwargs):
            tried.append(args[2][1])
            if len(tried) == 1:
                raise ProviderWait('provider-wait: opencode-go/glm-5.3 until 9999999999')
            return capture(*args, **kwargs)
        with patch.object(core.ROUTER, 'chain', return_value=(pool, [])), \
                patch.object(core, 'capture_opencode', side_effect=first_route_outage):
            result = review_plan(self.store, self.rid, 1, str(self.fake))
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(tried, pool, 'an outage neither repeats the failed candidate nor opens an unapproved model')

    def test_every_typed_review_role_uses_the_eligible_approved_pool(self):
        import qe_core as core
        pool = ['opencode-go/glm-5.3', 'opencode-go/qwen3.8-max']
        self.rid = self.store.request({**self.raw, 'submission_id': 'filtered-review-roles', 'input_mode': 'goal',
                                       'prompt': 'ROUND: review\nInspect README without changes.'})['run_id']
        with patch.object(core.ROUTER, 'chain', return_value=(pool, [])):
            self.approve()
        with patch.object(core.ROUTER, 'chain', return_value=([pool[1]], ['first candidate unavailable'])), \
                patch.object(core, 'capture_opencode', wraps=core.capture_opencode) as executor:
            self.assertEqual(execute_run(self.store, self.rid), 0, self.store.read(self.rid).get('reason'))
        selected = [(call.kwargs['role'], call.args[2][1]) for call in executor.call_args_list]
        self.assertEqual(selected, [(role, pool[1]) for role in ('plan', 'review', 'impl', 'quality')])

    def test_typed_review_capacity_waits_before_the_unavailable_helper_role(self):
        import qe_core as core
        for missing in ('plan', 'impl', 'quality'):
            with self.subTest(missing=missing):
                self.rid = self.store.request({**self.raw, 'submission_id': 'capacity-' + missing, 'input_mode': 'goal',
                                               'prompt': 'ROUND: review\nInspect README without changes.'})['run_id']
                self.approve()
                def capacity(role, *args, missing=missing, **kwargs):
                    return ([], ['no capacity']) if role == missing else (['opencode-go/glm-5.3'], [])
                with patch.object(core.ROUTER, 'chain', side_effect=capacity), \
                        patch.object(core, 'capture_opencode', wraps=core.capture_opencode) as executor:
                    status = execute_run(self.store, self.rid)
                record = self.store.read(self.rid)
                self.store.update(self.rid, state='cancelled')
                self.assertEqual(status, 78)
                self.assertEqual((record['state'], record['planning']['status'], record['reason']),
                                 ('retryable', 'provider-wait', 'plan-review-no-capacity'))
                self.assertIsNotNone(record['retry_after'])
                self.assertNotIn(missing, [c.kwargs['role'] for c in executor.call_args_list])

    def loaded_package(self, managed=False):
        import tempfile
        import session
        import qe_core as core
        from qa_fixture import instructions
        base = Path(tempfile.mkdtemp(prefix='loader-')).resolve()
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        engine = base / 'package/engine'
        shutil.copytree(LIB, engine / 'lib', ignore=shutil.ignore_patterns('__pycache__'))
        instructions(engine)
        frontend = base / 'frontend'
        frontend.mkdir()
        (frontend / 'opencode.jsonc').write_text('{}')
        (frontend / 'AGENTS.md').write_text('Frontend remains unchanged')
        loaded = engine.parent / 'config'
        if managed:
            for name in session.INSTRUCTION_PATHS:
                (frontend / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(loaded / name, frontend / name)
            shutil.rmtree(loaded)
            loaded = frontend
            (engine / 'chain').mkdir()
            self.own(engine, frontend)
        self.enter(patch.object(core, 'CODE', engine))
        self.enter(patch.object(core, '__file__', str(engine / 'lib/qe_core.py')))
        self.enter(patch.object(session, '__file__', str(engine / 'lib/session.py')))
        self.enter(patch.dict(os.environ, {'OPENCODE_CFG': str(frontend), 'CADRE_ENGINE_ROOT': str(engine)}))
        self.store.update(self.rid, state='cancelled')
        self.raw.update(submission_id='loader-' + ('managed' if managed else 'source'))
        self.rid = self.store.request(self.raw)['run_id']
        return engine, loaded, frontend

    def own(self, engine, config, version='fixture-version'):
        import session
        manifest = {'version': version, 'files': {str(config / name): {'sha256': hashlib.sha256((config / name).read_bytes()).hexdigest()}
                                                  for name in session.INSTRUCTION_PATHS}}
        (engine / 'chain/runtime-manifest.json').write_text(json.dumps(manifest))
        (engine / '.runtime-generation').write_text(version)

    def loader_receipt(self, **values):
        import tempfile
        import session
        directory = Path(tempfile.mkdtemp(prefix='loader-proof-', dir=self.home))
        env = {**self.store.environment(self.rid), 'CADRE_ROLE': 'review',
               'OPENCODE_CONFIG_CONTENT': json.dumps({'agent': {'build': {'prompt': 'Existing review policy'}}}), **values}
        receipt = {}
        session.loader_environment(directory, env, receipt)
        self.addCleanup(session.retire_loader, directory, receipt)
        return receipt['instructions']

    def legacy_backlog_run(self):
        self.store.update(self.rid, state='cancelled')
        self.rid = self.store.accept('lab', 'Preserve the original README acceptance; use the selected backlog.')['run_id']
        (self.home / 'legacy-plan.json').write_text(json.dumps(self.fixture.plan))
        return self.rid

    def legacy_backlog_plan(self):
        from copy import deepcopy
        from run_store import request_backlog
        plan = deepcopy(self.fixture.plan)
        plan['source_items'] = [{'id': item['id'], 'source_sha256': item['sha256'], 'tasks': ['g1'],
                                 'status': 'pending', 'evidence': 'README and original criterion inspected'}
                                for item in request_backlog(self.store, self.rid)['items']]
        return plan

    def test_legacy_selected_backlog_reaches_source_planner_and_clean_review(self):
        from dispatch_util import extract_plan
        from run_store import capture_source_plan, request_backlog
        self.backlog = '### [x] OLD — Existing item\n' + 'Original acceptance preserved.\n' * 3000 + '### [ ] NEXT — Pending item\nUpdate README.md.\n'
        (self.repo / 'BACKLOG.md').write_text(self.backlog)
        self.legacy_backlog_run()
        original = (self.store.path(self.rid) / 'prompt.md').read_bytes()
        budget = self.store.read(self.rid)['operator_budget_usd']
        self.store.bind_backlog(self.rid, 'BACKLOG.md')
        output = capture_source_plan(self.store, self.rid, str(self.fake), 'opencode-go/glm-5.3',
                                     'Produce the existing requested plan.', 'legacy-source-plan', 10)
        self.store.save_plan(self.rid, extract_plan(str(output)))
        review = review_plan(self.store, self.rid, 1, str(self.fake))
        self.assertEqual(review['status'], 'PASS', review)
        contexts = self.calls()
        inline = self.backlog.encode()[:40_000].decode('utf-8', 'ignore')
        self.assertEqual([c['backlog']['text'] for c in contexts], [inline, inline])
        self.assertTrue(all(c['backlog']['truncated'] and c['backlog']['items'][-1]['truncated'] for c in contexts))
        self.assertTrue({'source:OLD', 'source:NEXT'} <= {c['id'] for c in contexts[-1]['criteria']})
        receipts = [json.loads(path.read_text()) for path in self.home.glob('backlog-read-*.json')]
        self.assertEqual(len(receipts), 2)
        self.assertTrue(all(receipt == {'sha256': hashlib.sha256(self.backlog.encode()).hexdigest(), 'bytes': len(self.backlog.encode())}
                            for receipt in receipts))
        self.assertEqual(request_backlog(self.store, self.rid)['sha256'], hashlib.sha256(self.backlog.encode()).hexdigest())
        self.assertEqual((self.store.path(self.rid) / 'prompt.md').read_bytes(), original)
        self.assertEqual(self.store.read(self.rid)['operator_budget_usd'], budget)
        self.assertEqual(self.store.plan_view(self.rid)['history'][-1]['plan']['source_coverage']['merged'],
                         [{'task': 'g1', 'source_items': ['OLD', 'NEXT']}])

    def test_legacy_backlog_mapping_cannot_omit_forge_or_reimplement_verified_source(self):
        from copy import deepcopy
        self.legacy_backlog_run()
        self.store.bind_backlog(self.rid, 'BACKLOG.md')
        valid = self.legacy_backlog_plan()
        for mode in ('missing', 'hash', 'verified-worker', 'no-evidence', 'unmapped-task', 'false-coverage'):
            with self.subTest(mode=mode):
                bad = deepcopy(valid)
                if mode == 'missing':
                    bad['source_items'].pop()
                elif mode == 'hash':
                    bad['source_items'][0]['source_sha256'] = '0' * 64
                elif mode == 'verified-worker':
                    bad['source_items'][0]['status'] = 'verified'
                elif mode == 'no-evidence':
                    bad['source_items'][0]['evidence'] = ''
                elif mode == 'unmapped-task':
                    bad['tasks'].append({**bad['tasks'][0], 'id': 'g2'})
                else:
                    bad['source_coverage'] = {'verified': ['OLD', 'NEXT']}
                with self.assertRaisesRegex(ValueError, 'request-backlog-coverage'):
                    self.store.save_plan(self.rid, bad)
        self.assertEqual(self.store.plan_view(self.rid)['revision'], 0)
        self.assertEqual(self.calls(), [])

    def test_legacy_backlog_migration_requires_stop_new_revision_and_review(self):
        from run_store import plan_ready, validate_plan_review
        self.cached_legacy_plan()
        before = self.store.plan_view(self.rid)
        with self.assertRaisesRegex(ValueError, 'verified-stop-required'):
            self.store.bind_backlog(self.rid, 'BACKLOG.md')
        self.store.change_plan(self.rid, 'bind-source', 1, 'Bind the selected backlog to the same acceptance.')
        self.store.stop_plan(self.rid, 'bind-source')
        self.store.bind_backlog(self.rid, 'BACKLOG.md')
        with self.assertRaisesRegex(ValueError, 'execution-plan-mismatch'):
            plan_ready(self.store, self.rid, str(self.fake))
        with self.assertRaisesRegex(ValueError, 'request-backlog-coverage'):
            validate_plan_review(self.store, self.rid, self.store.plan_view(self.rid))
        self.assertEqual(self.store.plan_view(self.rid)['history'], before['history'])
        self.store.save_plan(self.rid, self.legacy_backlog_plan(), expected=1)
        with self.assertRaisesRegex(ValueError, 'current-plan-review-required'):
            self.store.resume_plan(self.rid, 'bind-source', 2)
        review = review_plan(self.store, self.rid, 2, str(self.fake))
        self.assertEqual(review['status'], 'PASS', review)
        self.store.resume_plan(self.rid, 'bind-source', 2)
        self.assertEqual(self.store.plan_view(self.rid)['revision'], 2)
        self.assertEqual(self.store.plan_view(self.rid)['history'][0], before['history'][0])

    def test_legacy_backlog_changes_reject_cached_pass_and_new_planning(self):
        from run_store import capture_source_plan, plan_ready, start_run
        self.legacy_backlog_run()
        self.store.bind_backlog(self.rid, 'BACKLOG.md')
        self.store.save_plan(self.rid, self.legacy_backlog_plan())
        self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
        calls = len(self.calls())
        (self.repo / 'BACKLOG.md').write_text(self.backlog + '\nChanged acceptance\n')
        for operation in (lambda: plan_ready(self.store, self.rid, str(self.fake)),
                          lambda: start_run(self.store, self.rid),
                          lambda: capture_source_plan(self.store, self.rid, str(self.fake), 'opencode-go/glm-5.3', 'Plan', 'legacy-plan', 10)):
            with self.assertRaisesRegex(ValueError, 'request-backlog-changed'):
                operation()
        self.assertEqual(len(self.calls()), calls)
        self.assertTrue((self.repo / 'BACKLOG.md').read_text().endswith('Changed acceptance\n'))

    def test_legacy_backlog_selection_rejects_external_secret_symlink_and_fifo(self):
        self.legacy_backlog_run()
        (self.home / 'outside.md').write_text(self.backlog)
        (self.repo / 'alias.md').symlink_to(self.home / 'outside.md')
        (self.repo / '.env-private.md').write_text('synthetic forbidden content')
        os.mkfifo(self.repo / 'pipe.md')
        os.link(self.home / 'outside.md', self.repo / 'hardlink.md')
        for selected in ('../outside.md', str(self.home / 'outside.md'), 'alias.md', '.env-private.md', 'pipe.md', 'hardlink.md', '.git/credentials.md'):
            with self.subTest(selected=selected), patch('run_store.plan_product', side_effect=AssertionError('Invalid path must fail before source hashing')), self.assertRaises(ValueError):
                self.store.bind_backlog(self.rid, selected)
        self.assertNotIn('backlog_binding', self.store.read(self.rid)['source'])
        self.assertFalse((self.store.path(self.rid) / 'backlog.md').exists())
        self.assertEqual(self.calls(), [])

    def test_legacy_backlog_binding_cli_replays_concurrently_and_is_operator_only(self):
        self.legacy_backlog_run()
        env = {k: v for k, v in os.environ.items() if k not in ('CADRE_RUN_ID', 'CADRE_OWNER_FDS', 'CADRE_COORDINATOR_PID')}
        env.update(CADRE_HOME=str(self.home), CADRE_ENGINE_ROOT=str(LIB.parent))
        command = ['bash', str(LIB.parent / 'bin/cadre.sh'), 'plan-backlog', self.rid, 'BACKLOG.md']
        results = [subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(3)]
        bindings = []
        for proc in results:
            stdout, stderr = proc.communicate(timeout=20)
            self.assertIn(proc.returncode, (0, 78), stderr)
            if proc.returncode == 0:
                bindings.append(json.loads(stdout))
            else:
                self.assertIn('plan-review-owner-active', stderr)
        self.assertTrue(bindings)
        before = (self.store.path(self.rid) / 'run.json').read_bytes()
        replay = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertTrue(all(binding == json.loads(replay.stdout) for binding in bindings))
        self.assertEqual((self.store.path(self.rid) / 'run.json').read_bytes(), before)
        denied = subprocess.run(command, env={**env, 'CADRE_RUN_ID': self.rid}, capture_output=True, text=True, timeout=20)
        self.assertEqual(denied.returncode, 78)
        self.assertIn('plan-backlog-operator-command-required', denied.stderr)
        self.assertEqual((self.store.path(self.rid) / 'run.json').read_bytes(), before)
        self.assertEqual(self.calls(), [])

    def test_legacy_backlog_binding_recovers_failed_publication_without_success_record(self):
        from run_store import atomic_json
        self.legacy_backlog_run()
        before = (self.store.path(self.rid) / 'run.json').read_bytes()
        def fail_record(path, value):
            if path.name == 'run.json' and value.get('source', {}).get('backlog_binding'):
                raise OSError('simulated publication failure')
            atomic_json(path, value)
        with patch('run_store.atomic_json', side_effect=fail_record), self.assertRaises(OSError):
            self.store.bind_backlog(self.rid, 'BACKLOG.md')
        self.assertNotIn('backlog_binding', self.store.read(self.rid)['source'])
        self.assertEqual((self.store.path(self.rid) / 'run.json').read_bytes(), before)
        self.assertEqual(self.store.bind_backlog(self.rid, 'BACKLOG.md')['relative_path'], 'BACKLOG.md')
        self.assertEqual(self.calls(), [])

    def test_legacy_backlog_unjournaled_plan_is_preserved_before_source_migration(self):
        from run_store import atomic_json
        self.legacy_backlog_run()
        atomic_json(self.store.path(self.rid) / 'plan.json', self.fixture.plan)
        before = (self.store.path(self.rid) / 'plan.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'legacy-plan-must-be-preserved'):
            self.store.bind_backlog(self.rid, 'BACKLOG.md')
        self.assertEqual((self.store.path(self.rid) / 'plan.json').read_bytes(), before)
        self.assertNotIn('backlog_binding', self.store.read(self.rid)['source'])
        self.store.save_plan(self.rid, self.fixture.plan)
        self.store.change_plan(self.rid, 'source-migration', 1, 'Bind the actual source backlog without changing the original acceptance.')
        self.store.stop_plan(self.rid, 'source-migration')
        self.store.bind_backlog(self.rid, 'BACKLOG.md')
        self.store.save_plan(self.rid, self.legacy_backlog_plan(), expected=1)
        self.assertEqual(self.store.plan_view(self.rid)['history'][0]['plan'], self.fixture.plan)
        self.assertEqual(self.store.plan_view(self.rid)['revision'], 2)
        self.assertEqual(review_plan(self.store, self.rid, 2, str(self.fake))['status'], 'PASS')
        self.store.resume_plan(self.rid, 'source-migration', 2)

    def test_legacy_backlog_changed_during_selection_never_publishes_a_false_binding(self):
        from run_store import backlog_file
        self.legacy_backlog_run()
        def changed_source(source, selected):
            result = backlog_file(source, selected)
            (self.repo / 'BACKLOG.md').write_text(self.backlog + '\nChanged during source selection\n')
            return result
        with patch('run_store.backlog_file', side_effect=changed_source), self.assertRaisesRegex(ValueError, 'plan-review-source-modified'):
            self.store.bind_backlog(self.rid, 'BACKLOG.md')
        self.assertNotIn('backlog_binding', self.store.read(self.rid)['source'])
        self.assertFalse((self.store.path(self.rid) / 'backlog.md').exists())
        self.assertTrue((self.repo / 'BACKLOG.md').read_text().endswith('Changed during source selection\n'))
        self.assertEqual(self.calls(), [])

    def test_legacy_backlog_invalid_identities_cannot_publish_or_replace_a_binding(self):
        self.legacy_backlog_run()
        for invalid in ('No source identities\n', self.backlog + '### [ ] OLD — Duplicate\n'):
            (self.repo / 'BACKLOG.md').write_text(invalid)
            with self.assertRaisesRegex(ValueError, 'request-backlog-identities-invalid'):
                self.store.bind_backlog(self.rid, 'BACKLOG.md')
            self.assertNotIn('backlog_binding', self.store.read(self.rid)['source'])
            self.assertFalse((self.store.path(self.rid) / 'backlog.md').exists())
        (self.repo / 'BACKLOG.md').write_text(self.backlog)
        binding = self.store.bind_backlog(self.rid, 'BACKLOG.md')
        (self.repo / 'OTHER.md').write_text(self.backlog)
        with self.assertRaisesRegex(ValueError, 'plan-backlog-already-fixed'):
            self.store.bind_backlog(self.rid, 'OTHER.md')
        self.assertEqual(self.store.read(self.rid)['source']['backlog_binding'], binding)

    def test_legacy_backlog_publication_survives_real_process_death_and_owner_conflict(self):
        import signal
        from run_store import FileLock
        self.legacy_backlog_run()
        before = (self.store.path(self.rid) / 'run.json').read_bytes()
        code = '''import run_store,sys,time
publish=run_store.atomic_json
def interrupted(path,value):
    if value.get('source',{}).get('backlog_binding'):
        print('ready',flush=True)
        time.sleep(60)
    publish(path,value)
run_store.atomic_json=interrupted
run_store.RunStore(sys.argv[1]).bind_backlog(sys.argv[2],'BACKLOG.md')
'''
        with subprocess.Popen([sys.executable, '-c', code, str(self.home), self.rid],
                              env={**os.environ, 'PYTHONPATH': str(LIB)}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
            self.assertEqual(proc.stdout.readline().strip(), 'ready')
            with self.assertRaisesRegex(ValueError, 'plan-review-owner-active'):
                self.store.bind_backlog(self.rid, 'BACKLOG.md')
            proc.send_signal(signal.SIGKILL)
            proc.communicate(timeout=5)
        self.assertEqual((self.store.path(self.rid) / 'run.json').read_bytes(), before)
        self.assertNotIn('backlog_binding', self.store.read(self.rid)['source'])
        self.store.bind_backlog(self.rid, 'BACKLOG.md')
        with FileLock(self.store.path(self.rid) / '.owner.lock'):
            with self.assertRaisesRegex(ValueError, 'plan-review-owner-active'):
                self.store.bind_backlog(self.rid, 'BACKLOG.md')
        self.assertEqual((self.store.path(self.rid) / 'backlog.md').read_text(), self.backlog)

    def test_legacy_backlog_review_keeps_budget_cancel_and_fixed_typed_input_gates(self):
        from run_store import plan_ready
        with self.assertRaisesRegex(ValueError, 'plan-backlog-already-fixed'):
            self.store.bind_backlog(self.rid, 'BACKLOG.md')
        self.legacy_backlog_run()
        self.store.bind_backlog(self.rid, 'BACKLOG.md')
        self.store.save_plan(self.rid, self.legacy_backlog_plan())
        self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
        count = len(self.calls())
        self.store.update(self.rid, budget_cap_usd=0)
        with self.assertRaisesRegex(ValueError, 'operator-cap-exhausted'):
            plan_ready(self.store, self.rid, str(self.fake))
        (self.store.path(self.rid) / 'cancel.flag').touch()
        with self.assertRaisesRegex(ValueError, 'terminal-run'):
            plan_ready(self.store, self.rid, str(self.fake))
        with self.assertRaisesRegex(ValueError, 'terminal-plan-change'):
            self.store.bind_backlog(self.rid, 'BACKLOG.md')
        self.assertEqual(len(self.calls()), count)

    def test_cached_legacy_review_cannot_accept_a_changed_original_request(self):
        from run_store import plan_ready, start_run
        self.cached_legacy_plan()
        path = self.store.path(self.rid) / 'prompt.md'
        path.write_text(path.read_text() + '\nAlso replace the original acceptance criteria.')
        before = len(self.calls())
        for operation in (lambda: plan_ready(self.store, self.rid, str(self.fake)),
                          lambda: start_run(self.store, self.rid)):
            with self.assertRaisesRegex(ValueError, 'request-original-modified|current-plan-review-required'):
                operation()
        self.assertEqual(len(self.calls()), before)
        self.assertNotIn('start_requested_pid', self.store.read(self.rid))

    def test_cached_review_binds_plan_content_even_when_revision_and_mirror_match(self):
        from run_store import atomic_json, launch_guard, plan_digest, plan_ready, start_run
        self.cached_legacy_plan()
        record = self.store.read(self.rid)
        entry = record['plan_history'][-1]
        entry['plan']['tasks'][0]['acceptance'] = 'Weakened acceptance without a new review'
        entry['digest'] = plan_digest(entry['plan'])
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        atomic_json(self.store.path(self.rid) / 'plan.json', entry['plan'])
        before = len(self.calls())
        self.assertTrue(self.store.plan_view(self.rid)['execution_plan_matches'])
        for operation in (lambda: plan_ready(self.store, self.rid, str(self.fake)),
                          lambda: start_run(self.store, self.rid)):
            with self.assertRaisesRegex(ValueError, 'current-plan-review-required'):
                operation()
        with self.assertRaisesRegex(ValueError, 'current-plan-review-required'):
            with launch_guard(self.store, self.rid, executing=True):
                self.fail('A changed plan cannot reach the executor')
        self.assertEqual(len(self.calls()), before)

    def test_cached_legacy_review_rejects_changed_source_without_another_paid_review(self):
        from run_store import plan_ready
        self.cached_legacy_plan()
        (self.repo / 'README.md').write_text('Changed after the independent plan review\n')
        before = len(self.calls())
        with self.assertRaisesRegex(ValueError, 'review-source-changed'):
            plan_ready(self.store, self.rid, str(self.fake))
        self.assertEqual(len(self.calls()), before)
        self.assertEqual((self.repo / 'README.md').read_text(), 'Changed after the independent plan review\n')

    def test_cached_review_cannot_substitute_an_unrelated_source_path(self):
        from run_store import atomic_json, plan_ready, start_run
        self.cached_legacy_plan()
        record = self.store.read(self.rid)
        record['plan_reviews'][-1]['source_path'] = str(self.home / 'unrelated-source')
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        before = len(self.calls())
        with patch('run_store.plan_product', side_effect=AssertionError('Unrelated review source must never be opened')):
            for operation in (lambda: plan_ready(self.store, self.rid, str(self.fake)),
                              lambda: start_run(self.store, self.rid)):
                with self.assertRaisesRegex(ValueError, 'review-source-changed'):
                    operation()
        self.assertEqual(len(self.calls()), before)

    def test_reviewed_source_stays_bound_when_an_owned_worktree_is_created_for_the_same_run(self):
        from run_store import launch_guard, plan_ready
        self.cached_legacy_plan()
        worktree = self.home / 'owned-worktree'
        subprocess.run(['git', '-C', str(self.repo), 'worktree', 'add', '--detach', str(worktree), 'HEAD'],
                       capture_output=True, check=True, timeout=10)
        self.store.update(self.rid, worktree=str(worktree))
        original = self.store.plan_view(self.rid)['reviews'][-1]
        before = len(self.calls())
        self.assertEqual(plan_ready(self.store, self.rid, str(self.fake)), original)
        with launch_guard(self.store, self.rid, executing=True):
            (worktree / 'README.md').write_text('The accepted worker advances its owned product.\n')
        self.assertEqual(plan_ready(self.store, self.rid, str(self.fake)), original)
        self.assertEqual(len(self.calls()), before)
        (self.repo / 'README.md').write_text('The reviewed original source has now changed.\n')
        with self.assertRaisesRegex(ValueError, 'review-source-changed'):
            plan_ready(self.store, self.rid, str(self.fake))
        self.assertEqual(len(self.calls()), before)

    def test_cached_review_without_identity_evidence_cannot_authorize_start_or_resume(self):
        from run_store import atomic_json, plan_ready, start_run
        self.cached_legacy_plan()
        original = self.store.read(self.rid)
        before = len(self.calls())
        for field in ('plan_digest', 'original_request_sha256', 'source_path', 'source_product', 'session_id', 'attempt_id'):
            with self.subTest(field=field):
                record = json.loads(json.dumps(original))
                record['plan_reviews'][-1].pop(field)
                atomic_json(self.store.path(self.rid) / 'run.json', record)
                for operation in (lambda: plan_ready(self.store, self.rid, str(self.fake)),
                                  lambda: start_run(self.store, self.rid)):
                    with self.assertRaisesRegex(ValueError, 'current-plan-review-required'):
                        operation()
                self.assertNotIn('start_requested_pid', self.store.read(self.rid))
        atomic_json(self.store.path(self.rid) / 'run.json', original)
        self.store.change_plan(self.rid, 'identity-recheck', 1, 'Refine the same requested update')
        self.store.stop_plan(self.rid, 'identity-recheck')
        record = self.store.read(self.rid)
        record['plan_reviews'][-1].pop('original_request_sha256')
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        with self.assertRaisesRegex(ValueError, 'current-plan-review-required'):
            self.store.resume_plan(self.rid, 'identity-recheck', 1)
        self.assertTrue(self.store.plan_view(self.rid)['held'])
        self.assertEqual(len(self.calls()), before)

    def test_cached_backlog_review_rechecks_source_mapping_before_reuse(self):
        from run_store import atomic_json, plan_digest, plan_ready
        self.rid = self.store.request({**self.raw, 'submission_id': 'cached-backlog',
                                      'input_mode': 'backlog', 'backlog_path': 'BACKLOG.md'})['run_id']
        self.approve()
        generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
        record = self.store.read(self.rid)
        entry = record['plan_history'][-1]
        entry['plan']['source_items'][-1].pop('source_sha256')
        entry['digest'] = plan_digest(entry['plan'])
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        atomic_json(self.store.path(self.rid) / 'plan.json', entry['plan'])
        before = len(self.calls())
        with self.assertRaisesRegex(ValueError, 'request-backlog-coverage'):
            plan_ready(self.store, self.rid, str(self.fake))
        self.assertEqual(len(self.calls()), before)

    def test_legacy_review_without_a_prompt_snapshot_still_binds_the_reviewed_original(self):
        from run_store import atomic_json, launch_guard, plan_ready
        self.cached_legacy_plan()
        record = self.store.read(self.rid)
        record['source'].pop('original_prompt_sha256', None)
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        (self.store.path(self.rid) / 'prompt.md').write_text('A different goal with the old plan and PASS review.')
        before = len(self.calls())
        with self.assertRaisesRegex(ValueError, 'current-plan-review-required'):
            plan_ready(self.store, self.rid, str(self.fake))
        with self.assertRaisesRegex(ValueError, 'current-plan-review-required'):
            with launch_guard(self.store, self.rid, executing=True):
                self.fail('An old review cannot authorize a new original request')
        self.assertEqual(len(self.calls()), before)

    def test_real_request_delivery_cannot_reuse_a_pass_for_changed_plan_content(self):
        from run_store import atomic_json, plan_digest
        self.approve()
        generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
        record = self.store.read(self.rid)
        entry = record['plan_history'][-1]
        entry['plan']['tasks'][0]['acceptance'] = 'Changed acceptance without independent review'
        entry['digest'] = plan_digest(entry['plan'])
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        atomic_json(self.store.path(self.rid) / 'plan.json', entry['plan'])
        before = len(self.calls())
        self.assertEqual(execute_run(self.store, self.rid), 78)
        self.assertEqual(self.store.read(self.rid)['reason'], 'current-plan-review-required')
        self.assertFalse((self.store.path(self.rid) / 'accepted-plan.json').exists())
        self.assertEqual(len(self.calls()), before)

    def test_unchanged_review_is_reused_across_concurrent_real_cli_calls(self):
        self.cached_legacy_plan()
        before = len(self.calls())
        original = {p.name: p.read_bytes() for p in self.store.path(self.rid).glob('*.json')}
        command = [sys.executable, str(LIB / 'run_store.py'), 'plan-ready', self.rid]
        env = {**os.environ, **self.store.environment(self.rid), 'CADRE_REAL_OPENCODE': str(self.fake)}
        processes = [subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(3)]
        for process in processes:
            out, err = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, (out, err))
            self.assertEqual(json.loads(out)['status'], 'PASS')
        self.assertEqual(len(self.calls()), before)
        self.assertEqual(original, {p.name: p.read_bytes() for p in self.store.path(self.rid).glob('*.json')})

    def test_cr51_review_reuse_is_bound_to_rule_policy_and_instructions_and_rereviews_once(self):
        import run_store
        from run_store import plan_ready, validate_plan_review
        _, loaded, _ = self.loaded_package()
        first = run_store.review_environment()
        self.assertEqual(first, run_store.review_environment(), 'the fingerprint is deterministic, avoiding a fresh review on every call')
        self.cached_legacy_plan()
        self.assertEqual(self.store.plan_view(self.rid)['reviews'][-1]['environment'], first)
        before = len(self.calls())
        self.assertEqual(plan_ready(self.store, self.rid, str(self.fake))['status'], 'PASS')
        self.assertEqual(len(self.calls()), before, 'unchanged identity: no additional model call')
        review = loaded / 'agents/cadre-review.md'
        review.write_text(review.read_text() + '\nchanged review instruction\n')
        changed = run_store.review_environment()
        self.assertNotEqual(changed['instruction'], first['instruction'])
        with self.assertRaisesRegex(ValueError, 'current-plan-review-required'):
            validate_plan_review(self.store, self.rid, self.store.plan_view(self.rid))
        self.assertEqual(plan_ready(self.store, self.rid, str(self.fake))['status'], 'PASS')
        self.assertEqual(len(self.calls()), before + 1, 'changed environment: single fresh clean review')
        self.assertEqual(self.store.plan_view(self.rid)['reviews'][-1]['environment'], changed)
        self.assertEqual(plan_ready(self.store, self.rid, str(self.fake))['status'], 'PASS')
        self.assertEqual(len(self.calls()), before + 1, 'the fresh review is reused without a loop')
        with patch.object(run_store, 'PLAN_REVIEW_PROMPT', run_store.PLAN_REVIEW_PROMPT + ' Fresh rule.'):
            self.assertNotEqual(run_store.review_environment()['rule'], changed['rule'])
            with self.assertRaisesRegex(ValueError, 'current-plan-review-required'):
                validate_plan_review(self.store, self.rid, self.store.plan_view(self.rid))
        with patch('model_selection.POLICY_VERSION', 'difficulty-preference-role-v2'):
            self.assertNotEqual(run_store.review_environment()['policy'], changed['policy'])

    def test_tsr02_active_config_and_engine_policy_bind_review_reuse_without_secrets(self):
        import tempfile
        import run_store
        from run_store import plan_ready, validate_plan_review
        config = Path(tempfile.mkdtemp(prefix='cfg-'))
        self.addCleanup(shutil.rmtree, config, ignore_errors=True)
        (config / 'agents').mkdir()
        (config / 'agents/cadre-review.md').write_text('read-only review')
        cfg = config / 'opencode.jsonc'
        secret = 'sk-or-v1-' + 'a1' * 20

        def write(protected, key=secret, comment=''):
            cfg.write_text(comment + json.dumps({'compaction': {'auto': True, 'prune': False, 'preserve_recent_tokens': protected},
                                               'provider': {'openrouter': {'options': {'apiKey': key}}}}, indent=1))
        write(16384)
        self.enter(patch.dict(os.environ, {'OPENCODE_CFG': str(config)}))
        first = run_store.review_environment()
        self.assertNotIn(secret, json.dumps(first), 'environment identity carries no secret')
        self.assertEqual(first, run_store.review_environment())
        self.cached_legacy_plan()
        self.assertEqual(self.store.plan_view(self.rid)['reviews'][-1]['environment'], first)
        before = len(self.calls())
        write(16384, key='sk-or-v1-' + 'b2' * 20, comment='// comments and formatting only\n')
        self.assertEqual(run_store.review_environment(), first, 'comments, formatting and secret values are not behavior')
        self.assertEqual(plan_ready(self.store, self.rid, str(self.fake))['status'], 'PASS')
        self.assertEqual(len(self.calls()), before, 'unchanged effective config: no additional model call')
        write(8192)
        changed = run_store.review_environment()
        self.assertNotEqual(changed['config'], first['config'])
        with self.assertRaisesRegex(ValueError, 'current-plan-review-required'):
            validate_plan_review(self.store, self.rid, self.store.plan_view(self.rid))
        self.assertEqual(plan_ready(self.store, self.rid, str(self.fake))['status'], 'PASS')
        self.assertEqual(len(self.calls()), before + 1, 'behavior changed: one fresh clean review')
        self.assertEqual(self.store.plan_view(self.rid)['reviews'][-1]['environment'], changed)
        self.assertEqual(plan_ready(self.store, self.rid, str(self.fake))['status'], 'PASS')
        self.assertEqual(len(self.calls()), before + 1, 'the fresh review is reused without a loop')
        record = self.store.read(self.rid)
        with patch.dict(os.environ, {'HOME': str(config / 'qa-home-dir')}):
            os.environ.pop('OPENCODE_CFG', None)
            os.environ.pop('CADRE_ENGINE_ROOT', None)
            self.assertEqual(run_store.review_environment(record), changed, 'QA tools without OPENCODE_CFG measure the same recorded root')
            validate_plan_review(self.store, self.rid, self.store.plan_view(self.rid), check_source=False)
        reviews = len(self.store.plan_view(self.rid)['reviews'])
        for broken in ('{"compaction": ', '[1, 2]', b'\xff\xfe{}'):
            cfg.write_bytes(broken) if isinstance(broken, bytes) else cfg.write_text(broken)
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, 'review-environment-unreadable'):
                    plan_ready(self.store, self.rid, str(self.fake))
                with self.assertRaisesRegex(ValueError, 'review-environment-unreadable'):
                    validate_plan_review(self.store, self.rid, self.store.plan_view(self.rid))
        write(8192)
        cfg.chmod(0)
        self.addCleanup(cfg.chmod, 0o600)
        with self.assertRaisesRegex(ValueError, 'review-environment-unreadable'):
            plan_ready(self.store, self.rid, str(self.fake))
        cfg.chmod(0o600)
        config.chmod(0o300)
        self.addCleanup(config.chmod, 0o700)
        with self.assertRaisesRegex(ValueError, 'review-environment-unreadable'):
            plan_ready(self.store, self.rid, str(self.fake))
        config.chmod(0o700)
        self.assertEqual(len(self.calls()), before + 1, 'broken or unreadable config makes no paid call and does not reuse an old PASS')
        self.assertEqual(len(self.store.plan_view(self.rid)['reviews']), reviews)
        self.assertEqual(plan_ready(self.store, self.rid, str(self.fake))['status'], 'PASS')
        self.assertEqual(len(self.calls()), before + 1, 'the repaired identical config reuses its earlier valid review')
        engine = Path(tempfile.mkdtemp(prefix='engine-'))
        self.addCleanup(shutil.rmtree, engine, ignore_errors=True)
        (engine / 'lib').mkdir()
        policy = json.loads((LIB / 'limits.json').read_text())
        (engine / 'lib/limits.json').write_text(json.dumps(policy))
        with patch.dict(os.environ, {'CADRE_ENGINE_ROOT': str(engine)}):
            same = run_store.review_environment()
            self.assertEqual(same, changed, 'same effective policy with different formatting has the same id')
            policy['effort']['easy_lane'] = 'low'
            (engine / 'lib/limits.json').write_text(json.dumps(policy))
            self.assertNotEqual(run_store.review_environment()['limits'], changed['limits'], 'a policy change requires another review')
            (engine / 'lib/limits.json').write_text('{')
            with self.assertRaisesRegex(ValueError, 'review-environment-unreadable'):
                run_store.review_environment()

    def qa_process_identity(self, engine):
        home = Path(self.home / 'qa-home')
        home.mkdir(exist_ok=True)
        code = ('import json, sys; sys.path.insert(0, sys.argv[1]); import run_store; '
                'print(json.dumps(run_store.review_environment(json.loads(open(sys.argv[2]).read()))))')
        result = subprocess.run([sys.executable, '-B', '-c', code, str(engine / 'lib'), str(self.store.path(self.rid) / 'run.json')],
                                env={'PATH': os.environ['PATH'], 'HOME': str(home)}, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        return json.loads(result.stdout)

    def test_tsr02_r1_review_identity_is_the_sealed_loader_digest_of_the_source_package(self):
        from run_store import plan_ready, validate_plan_review
        engine, loaded, frontend = self.loaded_package()
        self.cached_legacy_plan()
        self.assertEqual(self.store.read(self.rid)['provenance']['config']['root'], str(frontend))
        first = self.store.plan_view(self.rid)['reviews'][-1]['environment']
        receipt = self.loader_receipt(OPENCODE_CFG=str(frontend))
        self.assertEqual(receipt['source'], str(loaded))
        self.assertEqual(receipt['sha256'], first['instruction'], 'approval identity is the sealed loader receipt digest')
        before = len(self.calls())
        (frontend / 'AGENTS.md').write_text('Frontend revision the source package never loads')
        (frontend / 'agents').mkdir()
        (frontend / 'agents/cadre-review.md').write_text('---\ndescription: unloaded\n---\nUnloaded frontend review policy')
        self.assertEqual(plan_ready(self.store, self.rid, str(self.fake))['status'], 'PASS')
        self.assertEqual(len(self.calls()), before, 'changing an unloaded instruction makes no additional call')
        target = loaded / 'agents/cadre-review.md'
        target.write_text(target.read_text() + '\nIndependent local probe: inspect all acceptance criteria.\n')
        with self.assertRaisesRegex(ValueError, 'current-plan-review-required'):
            validate_plan_review(self.store, self.rid, self.store.plan_view(self.rid))
        self.assertEqual(plan_ready(self.store, self.rid, str(self.fake))['status'], 'PASS')
        self.assertEqual(len(self.calls()), before + 1, 'a loaded instruction changed: one fresh clean review')
        changed = self.store.plan_view(self.rid)['reviews'][-1]['environment']
        receipt = self.loader_receipt(OPENCODE_CFG=str(frontend))
        self.assertEqual(receipt['files']['agents/cadre-review.md']['sha256'], hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertEqual(receipt['sha256'], changed['instruction'])
        self.assertNotEqual(changed['instruction'], first['instruction'])
        self.assertEqual(plan_ready(self.store, self.rid, str(self.fake))['status'], 'PASS')
        self.assertEqual(len(self.calls()), before + 1, 'the fresh review is reused')
        self.assertEqual(self.qa_process_identity(engine), changed, 'QA without OPENCODE_CFG/CADRE_* measures the same source')
        reviews = len(self.store.plan_view(self.rid)['reviews'])
        original = target.read_bytes()
        other = loaded.parent / 'other.md'
        other.write_bytes(original)
        for broken in ('model', 'bytes', 'mode', 'symlink', 'hardlink', 'missing', 'oversize'):
            with self.subTest(broken=broken):
                target.unlink()
                if broken == 'model':
                    target.write_bytes(original.replace(b'---\n', b'---\nmodel: fixture/pinned\n', 1))
                elif broken == 'bytes':
                    target.write_bytes(original + b'\xff\xfe')
                elif broken == 'mode':
                    target.write_bytes(original)
                    target.chmod(0)
                elif broken == 'symlink':
                    target.symlink_to(other)
                elif broken == 'hardlink':
                    os.link(other, target)
                elif broken == 'oversize':
                    target.write_bytes(original + b'x' * 50_001)
                for _ in range(2):
                    with self.assertRaisesRegex(ValueError, 'review-environment-unreadable'):
                        plan_ready(self.store, self.rid, str(self.fake))
                    with self.assertRaisesRegex(ValueError, 'review-environment-unreadable'):
                        validate_plan_review(self.store, self.rid, self.store.plan_view(self.rid), check_source=False)
                target.unlink(missing_ok=True)
                target.write_bytes(original)
        self.assertEqual(len(self.calls()), before + 1, 'broken or unreadable instruction makes no paid call')
        self.assertEqual(len(self.store.plan_view(self.rid)['reviews']), reviews, 'old PASS is not reused and no new record is written')
        self.assertEqual(plan_ready(self.store, self.rid, str(self.fake))['status'], 'PASS')
        self.assertEqual(len(self.calls()), before + 1, 'the repaired identical instruction reuses its earlier valid review')

    def test_tsr02_r1_managed_runtime_identity_binds_the_recorded_owned_config(self):
        from run_store import review_environment, plan_ready, validate_plan_review
        engine, loaded, frontend = self.loaded_package(managed=True)
        self.cached_legacy_plan()
        first = self.store.plan_view(self.rid)['reviews'][-1]['environment']
        elsewhere = self.home / 'xdg'
        (elsewhere / 'opencode').mkdir(parents=True)
        for values in ({'OPENCODE_CFG': str(frontend)}, {}, {'XDG_CONFIG_HOME': str(elsewhere)},
                       {'OPENCODE_CFG': str(elsewhere / 'opencode')}):
            with self.subTest(env=sorted(values)):
                receipt = self.loader_receipt(**values)
                self.assertEqual(receipt['source'], str(frontend), 'the loader seals the run’s recorded config root')
                self.assertEqual(receipt['sha256'], first['instruction'])
        record = self.store.read(self.rid)
        with patch.dict(os.environ, {'HOME': str(self.home / 'qa-home')}):
            for key in ('OPENCODE_CFG', 'CADRE_ENGINE_ROOT', 'XDG_CONFIG_HOME'):
                os.environ.pop(key, None)
            self.assertEqual(review_environment(record), first)
        self.assertEqual(self.qa_process_identity(engine), first)
        before = len(self.calls())
        self.assertEqual(plan_ready(self.store, self.rid, str(self.fake))['status'], 'PASS')
        self.assertEqual(len(self.calls()), before, 'unchanged managed instruction: no additional call')
        target = frontend / 'agents/cadre-impl.md'
        original = target.read_bytes()
        reviews = len(self.store.plan_view(self.rid)['reviews'])
        target.write_bytes(original + b'\nUnowned local edit.\n')
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, 'review-environment-unreadable'):
                plan_ready(self.store, self.rid, str(self.fake))
            with self.assertRaisesRegex(ValueError, 'review-environment-unreadable'):
                validate_plan_review(self.store, self.rid, self.store.plan_view(self.rid), check_source=False)
        target.write_bytes(original)
        (engine / '.runtime-generation').write_text('other-version')
        with self.assertRaisesRegex(ValueError, 'review-environment-unreadable'):
            plan_ready(self.store, self.rid, str(self.fake))
        (engine / '.runtime-generation').write_text('fixture-version')
        self.assertEqual(len(self.calls()), before, 'unowned or generation-mismatched instruction makes no paid call')
        self.assertEqual(len(self.store.plan_view(self.rid)['reviews']), reviews)
        self.assertEqual(plan_ready(self.store, self.rid, str(self.fake))['status'], 'PASS')
        self.assertEqual(len(self.calls()), before)
        target.write_bytes(original + b'\nOwned runtime revision.\n')
        self.own(engine, frontend)
        self.assertEqual(plan_ready(self.store, self.rid, str(self.fake))['status'], 'PASS')
        self.assertEqual(len(self.calls()), before + 1, 'owned instruction changed: one fresh clean review')
        changed = self.store.plan_view(self.rid)['reviews'][-1]['environment']
        self.assertEqual(self.loader_receipt()['sha256'], changed['instruction'])
        self.assertNotEqual(changed['instruction'], first['instruction'])
        self.assertEqual(plan_ready(self.store, self.rid, str(self.fake))['status'], 'PASS')
        self.assertEqual(len(self.calls()), before + 1)

    def race_capture(self, target, revision, *, late=False):
        import qe_core as core
        real_capture, original, seen = core.capture_opencode, target.read_bytes(), {}

        def race(*args, **kwargs):
            if not late:
                target.write_bytes(revision)
            try:
                raw = real_capture(*args, **kwargs)
                seen['receipt'] = raw.attempt.loader_policy['instructions']['sha256']
                return raw
            finally:
                target.write_bytes(revision if late else original)
        return patch.object(core, 'capture_opencode', side_effect=race), seen

    def sealed_receipt(self, review):
        return self.store.path(self.rid) / 'tasks' / review['task_id'] / 'attempts' / review['attempt_id'] / 'loader-policy.json'

    def assert_receipt_bound_pass(self, environment):
        review = self.store.plan_view(self.rid)['reviews'][-1]
        self.assertEqual((review['status'], review['environment']), ('PASS', environment))
        sealed = json.loads(self.sealed_receipt(review).read_text())
        self.assertEqual(sealed['instructions']['sha256'], environment['instruction'], 'PASS is bound to the real attempt’s sealed loader receipt')
        return review

    def test_tsr02_r2_legacy_review_loader_race_records_no_pass_and_never_inherits_one(self):
        import run_store
        import qe_core as core
        from run_store import atomic_json, plan_ready, start_run, validate_plan_review
        _, loaded, _ = self.loaded_package()
        self.cached_legacy_plan()
        environment = run_store.review_environment(self.store.read(self.rid))
        self.assert_receipt_bound_pass(environment)
        target = loaded / 'agents/cadre-review.md'
        original = target.read_bytes()
        before = len(self.calls())
        race, seen = self.race_capture(target, original + b'\nInstruction revision arriving during review launch.\n')
        with race:
            review = review_plan(self.store, self.rid, self.store.plan_view(self.rid)['revision'], str(self.fake))
        self.assertEqual(len(self.calls()), before + 1, 'the concurrent attempt was actually invoked')
        self.assertNotEqual(seen['receipt'], environment['instruction'], 'the loader sealed the changed instruction at call start')
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual((review['status'], review.get('reason')), ('ERROR', 'plan-review-instructions-unverified'))
        self.assertEqual(self.store.plan_view(self.rid)['reviews'][-1]['reason'], 'plan-review-instructions-unverified')
        with self.assertRaisesRegex(ValueError, 'current-plan-review-required'):
            validate_plan_review(self.store, self.rid, self.store.plan_view(self.rid))
        self.assertEqual(run_store.review_environment(self.store.read(self.rid)), environment)
        self.assertEqual(plan_ready(self.store, self.rid, str(self.fake))['status'], 'PASS')
        self.assertEqual(len(self.calls()), before + 2, 'old PASS is not inherited: one fresh clean review with a valid instruction')
        self.assert_receipt_bound_pass(environment)
        self.assertEqual(plan_ready(self.store, self.rid, str(self.fake))['status'], 'PASS')
        self.assertEqual(len(self.calls()), before + 2, 'unchanged environment: no additional call')
        validate_plan_review(self.store, self.rid, self.store.plan_view(self.rid))

        late = original + b'\nInstruction revision arriving after the loader sealed the review.\n'
        race, seen = self.race_capture(target, late, late=True)
        with race:
            review = review_plan(self.store, self.rid, self.store.plan_view(self.rid)['revision'], str(self.fake))
        self.assertEqual(seen['receipt'], environment['instruction'])
        self.assertEqual((review['status'], review.get('reason')), ('ERROR', 'review-environment-changed'))
        changed = run_store.review_environment(self.store.read(self.rid))
        self.assertNotEqual(changed['instruction'], environment['instruction'])
        self.assertEqual(plan_ready(self.store, self.rid, str(self.fake))['status'], 'PASS')
        self.assertEqual(len(self.calls()), before + 4, 'one fresh review with the valid environment at acceptance time')
        self.assert_receipt_bound_pass(changed)

        reviews = len(self.store.plan_view(self.rid)['reviews'])
        target.write_bytes(late.replace(b'---\n', b'---\nmodel: fixture/pinned\n', 1))
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, 'review-environment-unreadable'):
                plan_ready(self.store, self.rid, str(self.fake))
        target.write_bytes(late)
        self.assertEqual(len(self.calls()), before + 4, 'a broken environment stops without a call')
        self.assertEqual(len(self.store.plan_view(self.rid)['reviews']), reviews)
        self.assertEqual(plan_ready(self.store, self.rid, str(self.fake))['status'], 'PASS')
        self.assertEqual(len(self.calls()), before + 4)

        real_capture = core.capture_opencode
        for lost in ('memory', 'disk'):
            with self.subTest(writing_receipt=lost):
                def drop(*args, **kwargs):
                    raw = real_capture(*args, **kwargs)
                    if lost == 'memory':
                        raw.attempt.loader_policy.pop('instructions')
                    else:
                        (raw.attempt.directory / 'loader-policy.json').unlink()
                    return raw
                calls = len(self.calls())
                with patch.object(core, 'capture_opencode', side_effect=drop):
                    review = review_plan(self.store, self.rid, self.store.plan_view(self.rid)['revision'], str(self.fake))
                self.assertEqual((review['status'], review.get('reason')), ('ERROR', 'plan-review-instructions-unverified'))
                self.assertEqual(plan_ready(self.store, self.rid, str(self.fake))['status'], 'PASS')
                self.assertEqual(len(self.calls()), calls + 2)
                self.assert_receipt_bound_pass(changed)

        for broken in ('missing', 'altered', 'inconsistent', 'symlink', 'unbound'):
            with self.subTest(acceptance_receipt=broken):
                current = self.store.plan_view(self.rid)['reviews'][-1]
                path = self.sealed_receipt(current)
                data = json.loads(path.read_text())
                if broken == 'missing':
                    path.unlink()
                elif broken in ('altered', 'inconsistent'):
                    data['instructions']['files']['AGENTS.md']['sha256'] = '1' * 64
                    if broken == 'altered':
                        data['instructions']['sha256'] = hashlib.sha256(json.dumps(
                            {name: row['sha256'] for name, row in data['instructions']['files'].items()}, sort_keys=True).encode()).hexdigest()
                    path.write_text(json.dumps(data))
                elif broken == 'symlink':
                    moved = path.with_name('moved-policy.json')
                    path.rename(moved)
                    path.symlink_to(moved)
                else:
                    record = self.store.read(self.rid)
                    record['plan_reviews'][-1].pop('task_id')
                    atomic_json(self.store.path(self.rid) / 'run.json', record)
                calls = len(self.calls())
                for operation in (lambda: validate_plan_review(self.store, self.rid, self.store.plan_view(self.rid)),
                                  lambda: plan_ready(self.store, self.rid, str(self.fake)),
                                  lambda: start_run(self.store, self.rid)):
                    with self.assertRaisesRegex(ValueError, 'current-plan-review-required'):
                        operation()
                self.assertNotIn('start_requested_pid', self.store.read(self.rid))
                self.assertEqual(len(self.calls()), calls, 'missing or mismatched receipt: no inherited PASS or paid call')
                review = review_plan(self.store, self.rid, self.store.plan_view(self.rid)['revision'], str(self.fake))
                self.assertEqual(review['status'], 'PASS')
                self.assertEqual(len(self.calls()), calls + 1, 'operator again review single call')
                self.assert_receipt_bound_pass(changed)
                self.assertEqual(plan_ready(self.store, self.rid, str(self.fake))['status'], 'PASS')
                self.assertEqual(len(self.calls()), calls + 1)

    def test_tsr02_r2_typed_request_loader_race_blocks_delivery_until_one_receipt_bound_review(self):
        import run_store
        from run_store import atomic_json, plan_ready
        _, loaded, _ = self.loaded_package()
        self.approve()
        generate_request_plan(self.store, self.rid, str(self.fake))
        environment = run_store.review_environment(self.store.read(self.rid))
        target = loaded / 'agents/cadre-review.md'
        original = target.read_bytes()
        before = len(self.calls())
        race, seen = self.race_capture(target, original + b'\nInstruction revision arriving during review launch.\n')
        with race:
            self.assertEqual(execute_run(self.store, self.rid), 78)
        self.assertEqual(len(self.calls()), before + 1)
        self.assertNotEqual(seen['receipt'], environment['instruction'])
        saved = self.store.read(self.rid)
        self.assertEqual((saved['state'], saved['reason']), ('retryable', 'current-plan-review-required'))
        self.assertEqual(saved['plan_reviews'][-1]['reason'], 'plan-review-instructions-unverified')
        self.assertFalse((self.store.path(self.rid) / 'accepted-plan.json').exists())

        target.write_bytes(original.replace(b'---\n', b'---\nmodel: fixture/pinned\n', 1))
        self.assertEqual(execute_run(self.store, self.rid), 78)
        saved = self.store.read(self.rid)
        self.assertEqual((saved['reason'], saved['plan_reviews'][-1]['reason']), ('current-plan-review-required', 'review-environment-unreadable'))
        self.assertEqual(len(self.calls()), before + 1, 'a broken environment stops without a call')
        target.write_bytes(original)

        self.assertEqual(plan_ready(self.store, self.rid, str(self.fake))['status'], 'PASS')
        self.assertEqual(len(self.calls()), before + 2, 'one fresh clean review with a valid instruction')
        self.assert_receipt_bound_pass(environment)
        record = self.store.read(self.rid)
        record['plan_reviews'][-1].pop('task_id')
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        self.assertEqual(execute_run(self.store, self.rid), 78)
        self.assertEqual(self.store.read(self.rid)['reason'], 'current-plan-review-required')
        self.assertFalse((self.store.path(self.rid) / 'accepted-plan.json').exists())
        self.assertEqual(len(self.calls()), before + 2, 'PASS without a bound receipt is not inherited and no paid call is made')
        self.assertEqual(review_plan(self.store, self.rid, self.store.plan_view(self.rid)['revision'], str(self.fake))['status'], 'PASS')
        self.assertEqual(execute_run(self.store, self.rid), 0, self.store.read(self.rid))
        self.assertEqual(len(self.calls()), before + 3, 'operator re-review makes one call; delivery makes no new call')
        delivered = json.loads(Path(self.store.read(self.rid)['plan_delivery']['artifact']).read_text())
        self.assertEqual((delivered['review']['status'], delivered['review']['environment']), ('PASS', environment))
        self.assertEqual(json.loads(self.sealed_receipt(delivered['review']).read_text())['instructions']['sha256'], environment['instruction'])
        self.assertEqual(execute_run(self.store, self.rid), 0)
        self.assertEqual(len(self.calls()), before + 3, 'unchanged environment: no additional call')

    def test_typed_plan_and_review_use_the_plan_policy_deadline_bounded_by_the_request_time(self):
        import qe_core as core
        from limits import deadline
        real_capture, seen = core.capture_opencode, []

        def spy(*args, **kwargs):
            seen.append((kwargs.get('role'), args[3]))
            return real_capture(*args, **kwargs)
        self.approve()
        with patch.object(core, 'capture_opencode', side_effect=spy):
            generate_request_plan(self.store, self.rid, str(self.fake))
            self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
        self.assertEqual([role for role, _ in seen], ['plan', 'review'])
        for role, duration in seen:
            self.assertGreater(duration, 120, 'real planning and review are not limited to a fixed 120 seconds')
            self.assertLessEqual(duration, deadline('plan'), 'the planning deadline comes from policy')
        self.store.update(self.rid, state='cancelled')
        self.raw.update(submission_id='short-deadline', duration_minutes='3')
        self.rid = self.store.request(self.raw)['run_id']
        seen.clear()
        self.approve()
        with patch.object(core, 'capture_opencode', side_effect=spy):
            generate_request_plan(self.store, self.rid, str(self.fake))
            review_plan(self.store, self.rid, 1, str(self.fake))
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(duration <= 180 for _, duration in seen), 'cannot exceed the request duration')

    def test_cached_review_does_not_bypass_cancel_deadline_or_budget(self):
        import time
        from budget_gate import BudgetLedger
        from run_store import atomic_json, plan_ready
        from test_budget_period import snapshot
        self.approve()
        generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
        before = len(self.calls())
        record = self.store.read(self.rid)
        changed = json.loads(json.dumps(record))
        changed['request_permission']['status'] = 'first-approved'
        atomic_json(self.store.path(self.rid) / 'run.json', changed)
        with self.assertRaisesRegex(ValueError, 'request-plan-gate'):
            plan_ready(self.store, self.rid, str(self.fake))
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        ledger = BudgetLedger(self.home / 'chain/request-budget.db')
        reservation = ledger.reserve('opencode-go', 1, snapshot(100), {'run_id': self.rid, 'role': 'plan'}, run_cap=2_000_000)
        ledger.settle(reservation, 2_000_000, 'cached-review-budget-receipt')
        with self.assertRaisesRegex(ValueError, 'operator-cap-exhausted'):
            plan_ready(self.store, self.rid, str(self.fake))
        with patch('run_store.time.time', return_value=time.time() + 3601), self.assertRaisesRegex(ValueError, 'operator-cap-exhausted'):
            plan_ready(self.store, self.rid, str(self.fake))
        self.duration_fill()
        with self.assertRaisesRegex(ValueError, 'request-time-limit'):
            plan_ready(self.store, self.rid, str(self.fake))
        (self.store.path(self.rid) / 'cancel.flag').touch()
        with self.assertRaisesRegex(ValueError, 'terminal-run'):
            plan_ready(self.store, self.rid, str(self.fake))
        self.assertEqual(len(self.calls()), before)

    def test_real_coordinator_delivers_plan_only_after_two_bound_permissions_and_clean_review(self):
        self.assertEqual(execute_run(self.store, self.rid), 78)
        proposal = request_proposal(self.store, self.rid)
        first = request_permission(self.store, self.rid, proposal)
        self.assertEqual(execute_run(self.store, self.rid), 78)
        self.assertEqual(self.calls(), [])
        request_permission(self.store, self.rid, proposal, first['confirmation'])
        original = (self.repo / 'README.md').read_bytes()
        self.assertEqual(execute_run(self.store, self.rid), 0, self.store.read(self.rid))
        saved = self.store.read(self.rid)
        self.assertEqual((saved['state'], saved['phase']), ('completed', 'plan-delivered'))
        delivered = json.loads(Path(saved['plan_delivery']['artifact']).read_text())
        self.assertEqual(delivered['review']['status'], 'PASS')
        self.assertEqual(delivered['request_hash'], saved['request_input_hash'])
        attempts = [json.loads(p.read_text()) for p in self.store.path(self.rid).glob('tasks/*/attempts/*/attempt.json')]
        self.assertEqual({a['role'] for a in attempts}, {'plan', 'review'})
        self.assertEqual(len({a['session_id'] for a in attempts}), 2)
        self.assertEqual(original, (self.repo / 'README.md').read_bytes())
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(execute_run(self.store, self.rid), 0)
        self.assertEqual(len(self.calls()), 2)

    def test_review_rejection_keeps_task_open_and_cannot_enter_dispatch(self):
        self.approve()
        (self.home / 'pipeline-mode').write_text('reject')
        self.assertEqual(execute_run(self.store, self.rid), 78)
        saved = self.store.read(self.rid)
        self.assertEqual(saved['state'], 'retryable')
        self.assertEqual(saved['planning']['status'], 'rejected')
        self.assertFalse((self.store.path(self.rid) / 'accepted-plan.json').exists())
        self.assertEqual(saved['plan_reviews'][-1]['status'], 'FAIL')

    def test_rejected_plan_is_not_replanned_when_the_review_tool_budget_is_exhausted(self):
        from budget_gate import BudgetLedger
        from run_store import plan_review_tool_finished
        from limits import review_policy
        self.approve()
        self.assertFalse(plan_review_tool_finished(self.store, self.rid))
        (self.home / 'pipeline-mode').write_text('reject')
        with patch('run_store.plan_review_tool_finished', return_value=True):
            self.assertEqual(execute_run(self.store, self.rid), 78)
        saved = self.store.read(self.rid)
        self.assertEqual((saved['state'], saved['reason']), ('retryable', 'plan-review-tool-budget-exhausted'))
        self.assertEqual(len(self.calls()), 2, 'plan and one review; no blind replanning')
        ledger = BudgetLedger(self.home / 'chain/request-budget.db')
        for _ in range(review_policy()['plan-review']['tool']):
            ledger.review_tool(self.rid, 'plan-review-1-abcdef01', 'review')
        self.assertTrue(plan_review_tool_finished(self.store, self.rid))

    def test_a_rejected_plan_gets_one_bounded_correction_from_the_review_findings_and_a_clean_review_of_the_new_revision(self):
        self.approve()
        (self.home / 'pipeline-mode').write_text('reject-once')
        self.assertEqual(execute_run(self.store, self.rid), 0)
        saved = self.store.read(self.rid)
        self.assertEqual((saved['state'], saved['phase']), ('completed', 'plan-delivered'))
        self.assertEqual([(r['revision_number'], r['status']) for r in saved['plan_reviews']], [(1, 'FAIL'), (2, 'PASS')])
        calls = self.calls()
        self.assertEqual(len(calls), 4, 'plan, rejection, one corrected plan and a review of the new revision')
        self.assertIn('README.md:1 claim contradicts the source', calls[2]['change'], 'correction includes the judge’s FAIL evidence')
        self.assertEqual(json.loads((self.store.path(self.rid) / 'accepted-plan.json').read_text())['plan']['revision'], 2)

    def test_a_corrected_code_plan_is_resumed_and_handed_to_dispatch_on_the_reviewed_revision(self):
        from run_store import request_pipeline
        self.store.update(self.rid, state='cancelled')
        self.rid = self.store.request({**self.raw, 'submission_id': 'code-correction', 'input_mode': 'goal'})['run_id']
        self.approve()
        (self.home / 'pipeline-mode').write_text('reject-once')
        self.assertIsNone(request_pipeline(self.store, self.rid, str(self.fake)))
        view = self.store.plan_view(self.rid)
        self.assertEqual((view['revision'], view['held'], view['changes'][-1]['status']), (2, False, 'resumed'))
        self.assertEqual(self.store.read(self.rid)['planning']['status'], 'accepted')
        self.assertEqual(json.loads((self.store.path(self.rid) / 'plan.json').read_text()), view['history'][-1]['plan'])

    def test_typed_model_permission_uses_accumulated_work_instead_of_approval_wall_time(self):
        import permission
        self.approve()
        record = self.store.read(self.rid)
        model = record['request_permission']['action']['models']['plan'][0]
        provider, name = model.split('/', 1)
        limit = record['request']['duration_minutes'] * 60
        now = record['request_permission']['approved'] + limit + 7200
        child = self.store.accept('lab', 'same request child')['run_id']
        self.store.update(child, project_ref_parent=self.rid)
        for active, started, allowed in ((0, None, True), (limit - 1, None, True),
                                         (limit, None, False), (limit - 1, now - 2, False)):
            with self.subTest(active=active, running=started is not None):
                record['request_clock'] = {'active_sec': active, 'session': started}
                atomic_json(self.store.path(self.rid) / 'run.json', record)
                with patch('permission.time.time', return_value=now):
                    for rid in (self.rid, child):
                        if allowed:
                            permission.model_check(self.store, rid, provider, name)
                        else:
                            with self.assertRaisesRegex(PermissionError, 'operator-model-approval-required'):
                                permission.model_check(self.store, rid, provider, name)
                    with self.assertRaisesRegex(PermissionError, 'operator-model-approval-required'):
                        permission.model_check(self.store, self.rid, 'unapproved-provider', 'unapproved-model')

    def test_a_held_plan_lets_only_the_claimed_planning_task_call_a_model_like_the_launch_guard(self):
        import permission
        from run_store import atomic_json
        self.approve()
        generate_request_plan(self.store, self.rid, str(self.fake))
        model = self.store.read(self.rid)['request_permission']['action']['models']['plan'][0]
        provider, entry_name = model.split('/', 1)
        permission.model_check(self.store, self.rid, provider, entry_name)
        self.store.change_plan(self.rid, 'review-fix-1', 1, 'fix', automatic=True)
        self.store.stop_plan(self.rid, 'review-fix-1')
        with self.assertRaisesRegex(PermissionError, 'operator-model-stopped'):
            permission.model_check(self.store, self.rid, provider, entry_name)
        record = self.store.read(self.rid)
        for task, allowed in (('plan-input-0123456789ab', True), ('plan-review-1-0123abcd', True), ('g1', False)):
            with self.subTest(task=task):
                record['plan_review_claim'] = {'id': 'c', 'revision': 1, 'task_id': task}
                atomic_json(self.store.path(self.rid) / 'run.json', record)
                with patch.dict(os.environ, {'CADRE_TASK_ID': task}):
                    if allowed:
                        permission.model_check(self.store, self.rid, provider, entry_name)
                    else:
                        with self.assertRaisesRegex(PermissionError, 'operator-model-stopped'):
                            permission.model_check(self.store, self.rid, provider, entry_name)
                with patch.dict(os.environ, {'CADRE_TASK_ID': 'plan-input-ffffffffffff'}):
                    with self.assertRaisesRegex(PermissionError, 'operator-model-stopped'):
                        permission.model_check(self.store, self.rid, provider, entry_name)

    def test_a_correction_interrupted_after_its_safe_stop_continues_on_restart(self):
        from run_store import plan_correction_open
        self.approve()
        (self.home / 'pipeline-mode').write_text('reject-once')
        generate_request_plan(self.store, self.rid, str(self.fake))
        red = review_plan(self.store, self.rid, 1, str(self.fake))
        self.assertEqual(red['status'], 'FAIL')
        self.assertTrue(plan_correction_open(self.store, self.rid, self.store.plan_view(self.rid), red))
        self.assertEqual(execute_run(self.store, self.rid), 0, self.store.read(self.rid).get('reason'))
        self.assertEqual(self.store.read(self.rid)['phase'], 'plan-delivered')
        self.assertEqual([(r['revision_number'], r['status']) for r in self.store.read(self.rid)['plan_reviews']], [(1, 'FAIL'), (2, 'PASS')])

    def test_a_rejected_revision_is_never_rereviewed_and_a_failed_correction_waits_without_new_calls(self):
        self.approve()
        (self.home / 'pipeline-mode').write_text('reject')
        self.assertEqual(execute_run(self.store, self.rid), 78)
        self.assertEqual(len(self.calls()), 4, 'one correction opportunity: plan, rejection, correction, rejection')
        saved = self.store.read(self.rid)
        self.assertEqual((saved['state'], saved['reason']), ('retryable', 'current-plan-review-required'))
        self.assertEqual(execute_run(self.store, self.rid), 78)
        revision = self.store.plan_view(self.rid)['revision']
        self.assertEqual(review_plan(self.store, self.rid, revision, str(self.fake))['status'], 'FAIL')
        self.assertEqual(len(self.calls()), 4, 'a rejected revision is not reviewed again in the same environment, and its plan is not regenerated')

    def test_operator_can_correct_a_plan_after_the_single_automatic_correction_was_rejected(self):
        self.approve()
        (self.home / 'pipeline-mode').write_text('reject')
        self.assertEqual(execute_run(self.store, self.rid), 78)
        self.assertEqual(len(self.calls()), 4)
        view = self.store.plan_view(self.rid)
        self.assertEqual((view['changes'][-1]['id'], view['changes'][-1]['status']), ('review-fix-1', 'rejected'))
        change = self.store.change_plan(self.rid, 'op-fix-2', view['revision'], 'Operator: narrow the hash rule')
        self.assertEqual(change['status'], 'stop_requested')
        self.assertEqual(self.store.read(self.rid)['owner_pid'], os.getpid())
        self.store.update(self.rid, owner_pid=None)
        self.store.stop_plan(self.rid, 'op-fix-2')
        with self.assertRaisesRegex(ValueError, 'plan-change-already-pending'):
            self.store.change_plan(self.rid, 'op-fix-3', view['revision'], 'Second operator change while one is pending')
        (self.home / 'pipeline-mode').write_text('')
        generate_request_plan(self.store, self.rid, str(self.fake))
        view = self.store.plan_view(self.rid)
        self.assertEqual(view['revision'], 3)
        self.assertEqual(self.calls()[-1]['change'], 'Operator: narrow the hash rule')
        self.assertEqual(review_plan(self.store, self.rid, 3, str(self.fake))['status'], 'PASS')
        self.store.resume_plan(self.rid, 'op-fix-2', 3)
        self.assertEqual(self.store.plan_view(self.rid)['changes'][-1]['status'], 'resumed')
        self.assertFalse(self.store.plan_view(self.rid)['held'])
        self.assertEqual(len(self.calls()), 6, 'operator correction: one fresh plan and one clean review')

    def test_goal_plan_schema_has_no_example_source_item_so_a_schema_copying_planner_stays_valid(self):
        (self.home / 'pipeline-mode').write_text('schema-copy')
        self.approve()
        view = generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(self.calls()[-1]['schema']['source_items'], [], 'the schema shows no sample source item when no backlog items exist')
        self.assertEqual(view['history'][-1]['plan']['source_items'], [])
        self.assertEqual(len(self.calls()), 1, 'one planning call; no scope rejection or repeat')

    def test_a_plan_echoed_inside_the_question_is_rejected_with_a_visible_reason_and_the_prompt_asks_for_a_top_level_plan(self):
        from run_store import operation_error
        (self.home / 'pipeline-mode').write_text('echo-question')
        self.approve()
        with self.assertRaises(ValueError) as red:
            generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(operation_error(red.exception), 'request-plan-unparsable')
        self.assertFalse(self.store.plan_view(self.rid)['history'], 'an answer repeating the question is not a plan')
        prompt = (self.home / 'pipeline-prompt.txt').read_text().splitlines()[0]
        self.assertIn('The top level must contain only the plan object', prompt)
        self.assertIn('do not repeat the question object', prompt)
        (self.home / 'pipeline-mode').write_text('')
        self.assertTrue(generate_request_plan(self.store, self.rid, str(self.fake))['history'], 'a valid plan is published for the same request')

    def test_plan_only_carries_source_identities_in_tasks_because_undeclared_source_items_stay_rejected(self):
        (self.home / 'pipeline-mode').write_text('undeclared-source-items')
        self.approve()
        with self.assertRaisesRegex(ValueError, 'request-backlog-coverage'):
            generate_request_plan(self.store, self.rid, str(self.fake))
        prompt = (self.home / 'pipeline-prompt.txt').read_text().splitlines()[0]
        self.assertIn('carry ids from source files in task definitions and acceptance criteria instead of source_items', prompt)
        (self.home / 'pipeline-mode').write_text('ids-in-tasks')
        plan = generate_request_plan(self.store, self.rid, str(self.fake))['history'][-1]['plan']
        self.assertEqual((plan['source_items'], 'NEXT' in plan['tasks'][0]['definition']), ([], True))

    def test_plan_estimates_leave_room_for_review_qa_and_final_acceptance(self):
        self.approve()
        view = generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(self.calls()[-1]['estimate_cap_usd'], 0.8, 'the planner receives the actual cost cap (40% of the budget)')
        self.assertTrue(view['history'])
        self.store.update(self.rid, state='cancelled')
        self.raw.update(submission_id='full-budget-estimate')
        self.rid = self.store.request(self.raw)['run_id']
        (self.home / 'pipeline-mode').write_text('full-budget-estimate')
        self.approve()
        with self.assertRaisesRegex(ValueError, 'request-plan-estimate-exceeds-acceptance-room'):
            generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertFalse(self.store.plan_view(self.rid)['history'], 'a plan leaving no room for acceptance is not published')

    def test_full_backlog_snapshot_and_every_source_identity_reach_independent_review(self):
        raw = {**self.raw, 'submission_id': 'backlog', 'input_mode': 'backlog', 'backlog_path': 'BACKLOG.md'}
        self.rid = self.store.request(raw)['run_id']
        self.approve()
        generate_request_plan(self.store, self.rid, str(self.fake))
        review = review_plan(self.store, self.rid, 1, str(self.fake))
        self.assertEqual(review['status'], 'PASS', review)
        self.assertEqual(self.calls()[0]['backlog']['text'], self.backlog)
        self.assertEqual(self.calls()[1]['backlog']['text'], self.backlog)
        self.assertEqual({c['id'] for c in self.calls()[1]['criteria']} & {'source:OLD', 'source:NEXT'}, {'source:OLD', 'source:NEXT'})
        self.assertIs(self.calls()[1]['source_scope_required'], True, 'scope remains mandatory for a selected backlog')
        self.assertEqual({m['status'] for m in self.store.plan_view(self.rid)['history'][0]['plan']['source_items']}, {'pending'})

    def test_omitted_backlog_item_blocks_plan_publication(self):
        self.rid = self.store.request({**self.raw, 'submission_id': 'missing', 'input_mode': 'backlog', 'backlog_path': 'BACKLOG.md'})['run_id']
        self.approve()
        (self.home / 'pipeline-mode').write_text('omit')
        with self.assertRaisesRegex(ValueError, 'request-backlog-coverage'):
            generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(self.store.plan_view(self.rid)['history'], [])
        self.assertIsNone(self.store.read(self.rid)['plan_review_claim'])

    def test_large_backlog_keeps_full_source_and_tail_identities_in_both_clean_sessions(self):
        self.backlog = '### [x] OLD — Existing item\n' + 'Existing behavior preserved.\n' * 9000 + '### [ ] NEXT — Pending\nKeep README.\n### [ ] TAIL — Final criterion\nTail acceptance survives.\n'
        data = self.backlog.encode()
        (self.repo / 'BACKLOG.md').write_bytes(data)
        self.rid = self.store.request({**self.raw, 'submission_id': 'large-backlog', 'input_mode': 'backlog', 'backlog_path': 'BACKLOG.md'})['run_id']
        self.approve()
        generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
        questions = self.calls()
        self.assertEqual(len(questions), 2)
        for question in questions:
            context = question['backlog']
            self.assertTrue(context['truncated'])
            self.assertLessEqual(len(context['text'].encode()), 40_000)
            self.assertEqual(context['characters'], len(self.backlog))
            self.assertEqual(context['sha256'], hashlib.sha256(data).hexdigest())
            self.assertEqual(Path(context['full_path']).read_bytes(), data)
            self.assertEqual([item['id'] for item in context['items']], ['OLD', 'NEXT', 'TAIL'])
            lines = self.backlog.splitlines(keepends=True)
            for item in context['items']:
                chunk = ''.join(lines[item['start_line'] - 1:item['end_line']])
                self.assertEqual(hashlib.sha256(chunk.encode()).hexdigest(), item['sha256'])
            self.assertTrue(context['items'][-1]['truncated'])
            self.assertNotIn('Tail acceptance survives.', context['text'])
        receipts = [json.loads(p.read_text()) for p in self.home.glob('backlog-read-*.json')]
        self.assertEqual(receipts, [{'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}] * 2)
        plan = self.store.plan_view(self.rid)['history'][0]['plan']
        self.assertEqual(plan['source_coverage']['merged'], [{'task': 'g1', 'source_items': ['OLD', 'NEXT', 'TAIL']}])
        self.assertEqual(plan['source_coverage']['truncated'], ['OLD', 'NEXT', 'TAIL'])
        self.assertEqual((self.store.path(self.rid) / 'backlog.md').read_bytes(), data)
        self.assertEqual((self.repo / 'BACKLOG.md').read_bytes(), data)

    def test_backlog_inline_byte_boundary_and_multibyte_text_are_explicit(self):
        header = '### [ ] ITEM — Criterion\n'
        for token, body in (('at', 'a' * (40_000 - len(header.encode()))),
                            ('over', 'a' * (40_001 - len(header.encode()))), ('unicode', 'é' * 40_000)):
            with self.subTest(token=token):
                data = (header + body).encode()
                (self.repo / 'BACKLOG.md').write_bytes(data)
                self.rid = self.store.request({**self.raw, 'submission_id': 'boundary-' + token,
                                               'input_mode': 'backlog', 'backlog_path': 'BACKLOG.md'})['run_id']
                self.approve()
                generate_request_plan(self.store, self.rid, str(self.fake))
                context = self.calls()[-1]['backlog']
                self.assertEqual(context['truncated'], len(data) > 40_000)
                self.assertLessEqual(len(context['text'].encode()), 40_000)
                self.assertEqual(context['inline_characters'], len(context['text']))
                self.assertEqual(context['items'][0]['sha256'], hashlib.sha256(data).hexdigest())

    def test_invalid_mapping_cannot_publish_or_spend_on_review(self):
        for mode in ('bad-hash', 'empty-evidence', 'nontext-evidence', 'verified-dispatched', 'duplicate-source', 'unmapped-task'):
            with self.subTest(mode=mode):
                self.rid = self.store.request({**self.raw, 'submission_id': 'mapping-' + mode,
                                               'input_mode': 'backlog', 'backlog_path': 'BACKLOG.md'})['run_id']
                self.approve()
                (self.home / 'pipeline-mode').write_text(mode)
                before = len(self.calls())
                with self.assertRaisesRegex(ValueError, 'request-backlog-coverage'):
                    generate_request_plan(self.store, self.rid, str(self.fake))
                self.assertEqual(len(self.calls()), before + 2, 'structural rejection gets one correction attempt without a review call')
                self.assertTrue(self.calls()[-1]['previous_rejection'])
                self.assertEqual(self.store.plan_view(self.rid)['history'], [])
                self.assertIsNone(self.store.read(self.rid)['plan_review_claim'])

    def test_a_structurally_rejected_plan_is_regenerated_once_with_the_exact_reason_and_published_when_corrected(self):
        self.rid = self.store.request({**self.raw, 'submission_id': 'mapping-retry', 'input_mode': 'backlog', 'backlog_path': 'BACKLOG.md'})['run_id']
        self.approve()
        (self.home / 'pipeline-mode').write_text('bad-hash-once')
        before = len(self.calls())
        view = generate_request_plan(self.store, self.rid, str(self.fake))
        calls = self.calls()[before:]
        self.assertEqual(len(calls), 2)
        self.assertNotIn('previous_rejection', calls[0])
        last = calls[1]['backlog']['items'][-1]
        self.assertIn(last['id'], calls[1]['previous_rejection'])
        self.assertIn(last['sha256'], calls[1]['previous_rejection'], 'retry feedback gives the correct digest for the wrongly copied item')
        broadcast = view['history'][-1]['plan']
        self.assertEqual({i['id']: i['source_sha256'] for i in broadcast['source_items']}, {i['id']: i['sha256'] for i in calls[1]['backlog']['items']})

    def test_verified_existing_item_has_no_dispatch_task_and_unfinished_item_remains_mapped(self):
        self.rid = self.store.request({**self.raw, 'submission_id': 'verified-mapping',
                                       'input_mode': 'backlog', 'backlog_path': 'BACKLOG.md'})['run_id']
        self.approve()
        (self.home / 'pipeline-mode').write_text('verified')
        generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
        plan = self.store.plan_view(self.rid)['history'][0]['plan']
        self.assertEqual(plan['source_items'][0]['tasks'], [])
        self.assertEqual(plan['source_items'][1]['tasks'], ['g1'])
        self.assertEqual(plan['source_coverage']['verified'], ['OLD'])
        self.assertEqual(plan['source_coverage']['merged'], [])

    def test_empty_plan_requires_complete_verified_sources_and_cannot_bypass_direct_save(self):
        from copy import deepcopy
        self.rid = self.store.request({**self.raw, 'submission_id': 'verified-empty',
                                       'input_mode': 'backlog', 'backlog_path': 'BACKLOG.md'})['run_id']
        self.approve()
        (self.home / 'pipeline-mode').write_text('all-verified')
        generate_request_plan(self.store, self.rid, str(self.fake))
        plan = self.store.plan_view(self.rid)['history'][0]['plan']
        self.assertEqual(plan['tasks'], [])
        self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
        self.store.change_plan(self.rid, 'empty-plan-check', 1, 'Recheck the same completed source')
        self.store.stop_plan(self.rid, 'empty-plan-check')
        for mode in ('pending', 'missing', 'hash', 'evidence', 'kind', 'task'):
            with self.subTest(mode=mode):
                bad = deepcopy(plan)
                if mode == 'pending':
                    bad['source_items'][0]['status'] = 'pending'
                if mode == 'missing':
                    bad['source_items'].pop()
                if mode == 'hash':
                    bad['source_items'][0]['source_sha256'] = '0' * 64
                if mode == 'evidence':
                    bad['source_items'][0]['evidence'] = ''
                if mode == 'kind':
                    bad.pop('kind')
                if mode == 'task':
                    bad['tasks'] = [{'id': 'ghost'}]
                with self.assertRaises(ValueError):
                    self.store.save_plan(self.rid, bad, expected=1)
                self.assertEqual(self.store.plan_view(self.rid)['revision'], 1)
        legacy = self.store.accept('lab', self.raw['prompt'])
        with self.assertRaisesRegex(ValueError, 'request-backlog-coverage'):
            self.store.save_plan(legacy['run_id'], plan)

    def test_direct_plan_save_cannot_bypass_source_identity_validation(self):
        from copy import deepcopy
        self.rid = self.store.request({**self.raw, 'submission_id': 'direct-mapping',
                                       'input_mode': 'backlog', 'backlog_path': 'BACKLOG.md'})['run_id']
        self.approve()
        generate_request_plan(self.store, self.rid, str(self.fake))
        self.store.change_plan(self.rid, 'source-map-change', 1, 'Refine the same pending item')
        self.store.stop_plan(self.rid, 'source-map-change')
        plan = deepcopy(self.store.plan_view(self.rid)['history'][0]['plan'])
        plan['source_items'][-1]['source_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'request-backlog-coverage'):
            self.store.save_plan(self.rid, plan, expected=1)
        self.assertEqual(self.store.plan_view(self.rid)['revision'], 1)
        self.assertTrue(self.store.plan_view(self.rid)['held'])

    def test_worktree_backlog_must_match_snapshot_without_following_external_paths(self):
        import shutil
        self.rid = self.store.request({**self.raw, 'submission_id': 'worktree-source',
                                       'input_mode': 'backlog', 'backlog_path': 'BACKLOG.md'})['run_id']
        self.approve()
        worktree = self.home / 'planning-copy'
        shutil.copytree(self.repo, worktree)
        self.store.update(self.rid, worktree=str(worktree))
        (worktree / 'BACKLOG.md').write_text('unrelated source')
        with self.assertRaisesRegex(ValueError, 'request-backlog-changed'):
            generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(self.calls(), [])
        (worktree / 'BACKLOG.md').unlink()
        (worktree / 'BACKLOG.md').symlink_to(self.repo / 'BACKLOG.md')
        with self.assertRaises((ValueError, OSError)):
            generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(self.calls(), [])
        (worktree / 'BACKLOG.md').unlink()
        (worktree / 'BACKLOG.md').write_text(self.backlog)
        generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
        self.assertEqual(Path(self.calls()[0]['backlog']['full_path']), worktree.resolve() / 'BACKLOG.md')

    def test_request_fixes_work_type_and_report_scope_before_any_model_call(self):
        for token, prompt, kind, mode in (('code', 'Preserve README and implement the change.', 'code', 'application'),
                                           ('code-readonly-feature', 'Implement a read-only log viewer.', 'code', 'application'),
                                           ('code-preserve-settings', 'Review the code and implement the feature; do not modify settings.', 'code', 'application'),
                                           ('code-report-feature', 'Implement a report-only endpoint.', 'code', 'application'),
                                           ('code-review-readonly-feature', 'Implement a read-only log viewer and review the security checks.', 'code', 'application'),
                                           ('code-targeted-changes', 'Review the security checks and implement the feature; do not make changes to settings.', 'code', 'application'),
                                           ('review', 'Review README without making changes.', 'review', 'review'),
                                           ('review-en', 'Review README; do not write code.', 'review', 'review'),
                                           ('review-report-only', 'Report only on security findings in src/auth.py.', 'review', 'review'),
                                           ('review-only-report', 'Only a report on security findings in src/auth.py.', 'review', 'review'),
                                           ('review-contraction', "Review README; don't change any code.", 'review', 'review'),
                                           ('review-no-changes', 'Review the security of src/auth.py; do not make changes.', 'review', 'review'),
                                           ('review-prefixed-report', 'Give me only a report on security findings in src/auth.py.', 'review', 'review'),
                                           ('review-produce-report', 'Please produce only a report on security findings in src/auth.py.', 'review', 'review'),
                                           ('review-constraint-first', 'Do not change any code. Review the security of src/auth.py.', 'review', 'review'),
                                           ('review-explain', 'Explain the security of src/auth.py; do not make changes.', 'review', 'review'),
                                           ('explicit-review', 'ROUND: review\nREADME assessment.', 'review', 'review'),
                                           ('research', 'ROUND: report\nREPORT: ANALYSIS.md\nWrite source and calculation evidence.', 'research', 'report')):
            with self.subTest(kind=kind):
                saved = self.store.request({**self.raw, 'submission_id': 'typed-' + token, 'input_mode': 'goal', 'prompt': prompt})
                self.assertEqual(saved['request']['job_type'], kind)
                self.assertEqual(saved['mode'], mode)
                self.assertEqual(saved['planning']['job_type'], kind)
                if kind == 'research':
                    self.assertEqual(saved['report']['path'], 'ANALYSIS.md')
                    self.assertEqual(saved['report']['evidence'], 'ANALYSIS.evidence.jsonl')
                else:
                    self.assertIsNone(saved['report'])
        self.assertEqual(self.calls(), [])

    def test_model_cannot_change_fixed_work_type_in_initial_or_direct_revision_plan(self):
        from copy import deepcopy
        for token, prompt in (('code', self.raw['prompt']), ('review', 'Review README without making changes.'),
                              ('research', 'ROUND: report\nREPORT: ANALYSIS.md\nWrite source evidence.')):
            with self.subTest(token=token):
                self.rid = self.store.request({**self.raw, 'submission_id': 'type-change-' + token, 'prompt': prompt})['run_id']
                self.approve()
                (self.home / 'pipeline-mode').write_text('wrong-type')
                with self.assertRaisesRegex(ValueError, 'request-job-type-mismatch'):
                    generate_request_plan(self.store, self.rid, str(self.fake))
                self.assertEqual(self.store.plan_view(self.rid)['history'], [])
                (self.home / 'pipeline-mode').unlink()
                generate_request_plan(self.store, self.rid, str(self.fake))
                self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
                self.store.change_plan(self.rid, 'type-revision', 1, 'Refine the same accepted work')
                self.store.stop_plan(self.rid, 'type-revision')
                plan = deepcopy(self.store.plan_view(self.rid)['history'][0]['plan'])
                plan['job_kind'] = 'research' if plan['job_kind'] == 'code' else 'code'
                with self.assertRaisesRegex(ValueError, 'request-job-type-mismatch'):
                    self.store.save_plan(self.rid, plan, expected=1)
                self.assertEqual(self.store.plan_view(self.rid)['revision'], 1)

    def test_report_plan_cannot_authorize_code_and_retains_report_acceptance(self):
        from copy import deepcopy
        self.rid = self.store.request({**self.raw, 'submission_id': 'report-scope', 'input_mode': 'goal',
                                       'prompt': 'ROUND: report\nREPORT: ANALYSIS.md\nSECTIONS: Source; Account\nPreserve the evidence.'})['run_id']
        self.approve()
        (self.home / 'pipeline-mode').write_text('report-code')
        with self.assertRaisesRegex(ValueError, 'request-report-scope'):
            generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(self.store.plan_view(self.rid)['revision'], 0)
        (self.home / 'pipeline-mode').unlink()
        generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
        self.assertEqual(self.store.read(self.rid)['report']['sections'], ['Source', 'Account'])
        self.store.change_plan(self.rid, 'report-revision', 1, 'Refine the same report')
        self.store.stop_plan(self.rid, 'report-revision')
        plan = deepcopy(self.store.plan_view(self.rid)['history'][0]['plan'])
        plan['tasks'][0]['files'] = ['app.py']
        with self.assertRaisesRegex(ValueError, 'request-report-scope'):
            self.store.save_plan(self.rid, plan, expected=1)
        self.assertEqual(self.store.plan_view(self.rid)['revision'], 1)
        self.assertFalse((self.repo / 'app.py').exists())

    def test_legacy_plan_only_types_reach_planner_and_review_without_rewriting_request(self):
        from run_store import atomic_json
        for kind, prompt in (('review', 'ROUND: review\nInspect README without changing the source.'),
                             ('research', 'ROUND: report\nREPORT: ANALYSIS.md\nSECTIONS: Source; Account\nReport the evidence.')):
            with self.subTest(kind=kind):
                self.rid = self.store.request({**self.raw, 'submission_id': 'legacy-only-' + kind, 'prompt': prompt})['run_id']
                record = self.store.read(self.rid)
                record.pop('job_type', None)
                record['request'].pop('job_type')
                record['planning'].pop('job_type')
                path = self.store.path(self.rid) / 'run.json'
                atomic_json(path, record)
                original = path.read_bytes()
                self.store.read(self.rid)
                self.assertEqual(path.read_bytes(), original)
                proposal = self.approve()
                self.assertEqual(execute_run(self.store, self.rid), 0, self.store.read(self.rid))
                saved = self.store.read(self.rid)
                self.assertNotIn('job_type', saved['request'])
                self.assertNotIn('job_type', saved)
                self.assertEqual(saved['request'], record['request'])
                self.assertEqual(saved['request_input_hash'], record['request_input_hash'])
                self.assertEqual(saved['operator_budget_usd'], record['operator_budget_usd'])
                self.assertEqual(saved['request_permission']['token'], proposal['token'])
                self.assertEqual(self.calls()[-2]['request']['job_type'], kind)
                self.assertEqual(self.calls()[-1]['request']['job_type'], kind)
                self.assertEqual(saved['plan_history'][-1]['plan']['job_kind'], kind)
                self.assertEqual(saved['plan_reviews'][-1]['status'], 'PASS')
                self.assertEqual(saved['phase'], 'plan-delivered')

    def test_legacy_recorded_mode_survives_new_classification_without_granting_write(self):
        from run_store import atomic_json
        self.rid = self.store.request({**self.raw, 'submission_id': 'legacy-review-mode', 'input_mode': 'goal',
                                       'prompt': 'ROUND: review\nReview README only.'})['run_id']
        record = self.store.read(self.rid)
        record.pop('job_type', None)
        record['request'].pop('job_type')
        record['planning'].pop('job_type')
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        self.approve()
        with patch('run_store.operation_mode', return_value='application'):
            view = generate_request_plan(self.store, self.rid, str(self.fake))
            self.assertEqual(view['history'][-1]['plan']['job_kind'], 'review')
            self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
        config = json.loads((self.home / 'pipeline-config.json').read_text())
        self.assertEqual(config['permission']['edit'], 'deny')
        self.assertEqual(self.store.read(self.rid)['mode'], 'review')
        plan = dict(view['history'][-1]['plan'])
        for kind in ('code', None):
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'request-job-type-mismatch'):
                self.store.save_plan(self.rid, {**plan, 'job_kind': kind}, expected=1)
        self.assertEqual(len(self.calls()), 2)

    def test_conflicting_or_mutable_work_type_cannot_authorize_an_executor(self):
        import qe_core as core
        import panel_data
        from run_store import atomic_json
        record = self.store.read(self.rid)
        self.assertEqual(record['job_type'], 'code')
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.store.update(self.rid, job_type='review')
        self.store.update(self.rid, planning={**record['planning'], 'job_type': 'review'})
        node = panel_data.task_node(self.home, self.store.read(self.rid))
        self.assertIn(('field', ('Job type', 'code')), node['detail'])
        record['job_type'] = 'review'
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        self.assertEqual(self.store.read(self.rid)['job_type'], 'review')
        with self.assertRaisesRegex(ValueError, 'run-job-type-conflict'):
            core.run_opencode(str(self.fake), str(self.repo), ['Do the task'], self.home / 'conflicting-type.log')
        self.assertEqual(self.calls(), [])
        unknown = {key: value for key, value in record.items() if key not in ('job_type', 'mode', 'request', 'report')}
        atomic_json(self.store.path(self.rid) / 'run.json', unknown)
        self.assertEqual(self.store.read(self.rid), unknown)
        with self.assertRaisesRegex(ValueError, 'run-job-type-unavailable'):
            core.run_opencode(str(self.fake), str(self.repo), ['Do the task'], self.home / 'unknown-type.log')
        self.assertEqual(self.calls(), [])
        report = self.store.accept('invalid-report', 'ROUND: report\nREPORT: ANALYSIS.md\nReport evidence only.')
        for profile in (None, {}, {'path': '../outside.md', 'evidence': '../outside.evidence.jsonl', 'sections': []},
                        {**report['report'], 'evidence': 'app.py'}, {**report['report'], 'sections': 'Source'}):
            with self.subTest(profile=profile), patch.dict(os.environ, {'CADRE_RUN_ID': report['run_id']}):
                atomic_json(self.store.path(report['run_id']) / 'run.json', {**report, 'report': profile})
                with self.assertRaisesRegex(ValueError, 'run-report-profile-unavailable'):
                    core.run_opencode(str(self.fake), str(self.repo), ['Report only'], self.home / 'invalid-report.log')
        self.assertEqual(self.calls(), [])

    def test_previously_reviewed_plan_cannot_reuse_invalid_source_mapping(self):
        from run_store import atomic_json, plan_digest
        self.rid = self.store.request({**self.raw, 'submission_id': 'legacy-coverage',
                                       'input_mode': 'backlog', 'backlog_path': 'BACKLOG.md'})['run_id']
        self.approve()
        generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
        record = self.store.read(self.rid)
        entry = record['plan_history'][0]
        entry['plan']['source_items'][-1].pop('source_sha256')
        entry['digest'] = plan_digest(entry['plan'])
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        atomic_json(self.store.path(self.rid) / 'plan.json', entry['plan'])
        before = len(self.calls())
        with self.assertRaisesRegex(ValueError, 'request-backlog-coverage'):
            generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(len(self.calls()), before)
        result = review_plan(self.store, self.rid, 1, str(self.fake))
        self.assertEqual((result['status'], result['reason']), ('ERROR', 'request-backlog-coverage'))
        self.assertEqual(len(self.calls()), before)
        self.assertIsNone(self.store.read(self.rid)['plan_review_claim'])

    def test_changed_source_wrong_second_approval_and_unapproved_model_do_not_launch(self):
        proposal = request_proposal(self.store, self.rid)
        with self.assertRaisesRegex(ValueError, 'second-approval'):
            request_permission(self.store, self.rid, proposal, 'not-first-confirmation')
        self.approve()
        generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
        from session import Attempt
        with patch.dict(os.environ, self.store.environment(self.rid)), patch('session.OC.launch') as launch:
            attempt = Attempt('opencode', 'impl', 'openrouter/unapproved', 'Unapproved model')
            with self.assertRaisesRegex(ValueError, 'model-permission'):
                attempt.launch([str(self.fake)], str(self.repo), self.home / 'forbidden.log', dict(os.environ), ())
            launch.assert_not_called()
            self.assertEqual(attempt.data['outcome'], 'not_started')
        before = len(self.calls())
        (self.repo / 'README.md').write_text('changed after permission')
        with self.assertRaisesRegex(ValueError, 'request-source-changed'):
            generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(len(self.calls()), before)

    def test_new_empty_target_delivers_plan_without_creating_repo_or_initial_commit(self):
        target = self.home / 'new-project'
        self.rid = self.store.request({**self.raw, 'submission_id': 'new-plan', 'project': 'new-project', 'target_kind': 'new',
                                       'target_path': str(target)})['run_id']
        self.approve()
        self.assertEqual(execute_run(self.store, self.rid), 0, self.store.read(self.rid))
        self.assertEqual(self.store.read(self.rid)['plan_delivery']['status'], 'PASS')
        self.assertFalse(target.exists())
        self.assertEqual(len(self.calls()), 2)
        for role in ('plan', 'review'):
            with self.subTest(role=role):
                self.rid = self.store.request({**self.raw, 'submission_id': 'tamper-' + role, 'project': 'tamper-' + role,
                                               'target_kind': 'new', 'target_path': str(self.home / ('tamper-' + role))})['run_id']
                self.approve()
                if role == 'review':
                    generate_request_plan(self.store, self.rid, str(self.fake))
                (self.home / 'pipeline-mode').write_text('write')
                if role == 'plan':
                    with self.assertRaisesRegex(ValueError, 'source-modified'):
                        generate_request_plan(self.store, self.rid, str(self.fake))
                    self.assertEqual(self.store.plan_view(self.rid)['revision'], 0)
                else:
                    result = review_plan(self.store, self.rid, 1, str(self.fake))
                    self.assertEqual((result['status'], result['reason']), ('ERROR', 'plan-review-source-modified'))
                (self.home / 'pipeline-mode').unlink()

    def test_zero_cap_and_expired_duration_do_not_call_provider(self):
        self.rid = self.store.request({**self.raw, 'submission_id': 'zero', 'budget_usd': '0'})['run_id']
        self.approve()
        with self.assertRaises(ValueError):
            generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(self.calls(), [])

    def test_operator_stop_new_revision_review_and_explicit_resume_use_one_journal(self):
        self.rid = self.store.request({**self.raw, 'submission_id': 'revise', 'input_mode': 'goal'})['run_id']
        self.approve()
        generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
        record = self.store.read(self.rid)
        self.store.update(self.rid, planning={**record['planning'], 'status': 'accepted'}, phase='qa', state='running')
        from panel_data import task_node
        node = task_node(self.home, self.store.read(self.rid))
        self.assertNotIn('Awaiting plan gate', str(node['detail']))
        self.store.change_plan(self.rid, 'operator-change', 1, 'Preserve README and improve its wording')
        with self.assertRaisesRegex(ValueError, 'verified-stop-required'):
            generate_request_plan(self.store, self.rid, str(self.fake))
        self.store.stop_plan(self.rid, 'operator-change')
        generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(review_plan(self.store, self.rid, 2, str(self.fake))['status'], 'PASS')
        self.assertTrue(self.store.plan_view(self.rid)['held'])
        self.store.resume_plan(self.rid, 'operator-change', 2)
        view = self.store.plan_view(self.rid)
        self.assertFalse(view['held'])
        self.assertEqual(view['revision'], 2)
        self.assertEqual(view['history'][0]['plan']['tasks'][0]['definition'], 'measurable task')
        self.assertEqual(view['history'][1]['plan']['tasks'][0]['definition'], 'Preserve README and improve its wording')
        self.assertEqual(self.store.read(self.rid)['request']['prompt'], self.raw['prompt'])

    def test_automatic_revision_needs_actual_quiescence_review_and_preserves_history(self):
        from copy import deepcopy
        from run_store import automatic_revision, FileLock
        self.approve()
        generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
        plan = deepcopy(self.store.plan_view(self.rid)['history'][-1]['plan'])
        plan['tasks'][0]['definition'] = 'Reviewed automatic refinement'
        with FileLock(self.store.task_dir(self.rid, 'g1') / '.opencode-impl.lock'):
            with self.assertRaisesRegex(ValueError, 'plan-stop-executor-active'):
                automatic_revision(self.store, self.rid, plan, str(self.fake))
        self.assertEqual(self.store.plan_view(self.rid)['revision'], 1)
        result = automatic_revision(self.store, self.rid, plan, str(self.fake))
        self.assertEqual(result['status'], 'PASS', result)
        view = self.store.plan_view(self.rid)
        self.assertFalse(view['held'])
        self.assertEqual(view['revision'], 2)
        before = len(self.calls())
        self.assertEqual(automatic_revision(self.store, self.rid, plan, str(self.fake))['status'], 'PASS')
        self.assertEqual(before, len(self.calls()))

    def test_split_preserves_source_identity_acceptance_and_requires_a_new_clean_review(self):
        from copy import deepcopy
        from task_split import sub_plan_merge, split_prompt
        from run_store import automatic_revision, request_backlog, request_plan_coverage
        self.rid = self.store.request({**self.raw, 'submission_id': 'split', 'input_mode': 'backlog',
                                       'backlog_path': 'BACKLOG.md'})['run_id']
        self.approve()
        generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
        parent = deepcopy(self.store.plan_view(self.rid)['history'][-1]['plan'])
        parent['assumptions'] = [{'id': 'layout', 'choice': 'Preserve README layout', 'reason': 'Existing source convention'}]
        sub = {'job_id': 'split', 'job_kind': 'code', 'tasks': [
            {**parent['tasks'][0], 'id': name, 'dependent': []} for name in ('a', 'b')]}
        inherited, names = sub_plan_merge(parent, 'g1', json.dumps(sub), self.repo)
        self.assertEqual(names, ['g1-1', 'g1-2'])
        self.assertTrue(all(item['tasks'] == ['g1', *names] for item in inherited['source_items']))
        self.assertEqual(inherited['source_coverage'], request_plan_coverage(inherited, request_backlog(self.store, self.rid)))
        sub['source_items'] = [{**item, 'tasks': [name]} for item, name in zip(parent['source_items'], ('a', 'b'))]
        sub['assumptions'] = [{'id': 'order', 'choice': 'Preserve before updating', 'reason': 'Avoid overwriting existing content'}]
        split, names = sub_plan_merge(parent, 'g1', json.dumps(sub), self.repo)
        self.assertEqual([item['tasks'] for item in split['source_items']], [['g1', names[0]], ['g1', names[1]]])
        self.assertEqual(split['tasks'][0]['acceptance'], parent['tasks'][0]['acceptance'])
        self.assertEqual([item['id'] for item in split['assumptions']], ['layout', 'order'])
        prompt = split_prompt(parent, 'g1', 'rc=1')
        for item in parent['source_items']:
            self.assertIn(item['source_sha256'], prompt)
        permission = self.store.read(self.rid)['request_permission']
        self.assertEqual(automatic_revision(self.store, self.rid, split, str(self.fake))['status'], 'PASS')
        view = self.store.plan_view(self.rid)
        self.assertEqual((view['revision'], view['held']), (2, False))
        self.assertEqual(self.calls()[-1]['plan']['source_items'], split['source_items'])
        self.assertEqual(self.calls()[-1]['plan']['assumptions'], split['assumptions'])
        self.assertEqual(len(self.calls()), 3)
        self.assertEqual(self.store.read(self.rid)['request_permission'], permission)
        self.assertEqual((self.repo / 'BACKLOG.md').read_text(), self.backlog)

    def test_split_cannot_drop_sources_hide_critical_decisions_or_grant_a_new_type(self):
        from copy import deepcopy
        from task_split import sub_plan_merge
        from run_store import automatic_revision
        self.rid = self.store.request({**self.raw, 'submission_id': 'split-critical', 'input_mode': 'backlog',
                                       'backlog_path': 'BACKLOG.md'})['run_id']
        self.approve()
        generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
        parent = deepcopy(self.store.plan_view(self.rid)['history'][-1]['plan'])
        sub = {'job_id': 'split', 'job_kind': 'code', 'tasks': [
            {**parent['tasks'][0], 'id': name, 'dependent': []} for name in ('a', 'b')],
            'source_items': [{**item, 'tasks': [name]} for item, name in zip(parent['source_items'], ('a', 'b'))]}
        for mutate in (lambda p: p.update(source_items=None), lambda p: p['source_items'].pop(),
                       lambda p: p['source_items'][0].update(id='invented'),
                       lambda p: p['source_items'][0].update(source_sha256='0' * 64),
                       lambda p: p['source_items'][0].update(status='verified', tasks=[]),
                       lambda p: p['source_items'][0].update(tasks=['b']),
                       lambda p: p.update(job_kind='review'), lambda p: p.update(decisions=None),
                       lambda p: p['tasks'][0].update(files=['unapproved.py'])):
            bad = deepcopy(sub)
            mutate(bad)
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                sub_plan_merge(parent, 'g1', json.dumps(bad), self.repo)
        decision = {'id': 'split-authority', 'kind': 'authority', 'question': 'May this boundary be changed?',
                    'reason': 'No exact permission exists for this boundary'}
        sub['decisions'] = [decision]
        split, _ = sub_plan_merge(parent, 'g1', json.dumps(sub), self.repo)
        result = automatic_revision(self.store, self.rid, split, str(self.fake))
        self.assertEqual(result['status'], 'ERROR')
        view = self.store.plan_view(self.rid)
        self.assertEqual((view['revision'], view['held']), (2, True))
        self.assertEqual(view['decisions'][0]['question'], decision['question'])
        self.assertEqual(execute_run(self.store, self.rid, proceed=True), 78)
        self.assertEqual(len(self.calls()), 2)
        self.assertFalse(any(json.loads(path.read_text()).get('project_ref_parent') == self.rid
                             for path in (self.home / 'runs').glob('*/run.json')))

    def test_project_features_carry_reviewed_source_criteria_into_the_actual_child_record(self):
        from copy import deepcopy
        import project_mode
        from run_store import atomic_json
        self.rid = self.store.request({**self.raw, 'submission_id': 'project-source', 'input_mode': 'backlog',
                                       'backlog_path': 'BACKLOG.md'})['run_id']
        self.approve()
        generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
        parent = deepcopy(self.store.plan_view(self.rid)['history'][-1]['plan'])
        feature = {'id': 'f1', 'title': 'Preserve and update README', 'description': 'Keep existing content while updating README',
                   'acceptance': 'README contents retained and requested update present', 'qa_command': 'test -e README.md',
                   'source_tasks': ['g1']}
        raw = json.dumps({'features': [feature], 'assumptions': [
            {'id': 'project-order', 'choice': 'Preserve existing content first', 'reason': 'Reversible within selected source scope'}]})
        for bad in ({**feature, 'source_tasks': []}, {**feature, 'source_tasks': ['invented']},
                    {**feature, 'source_tasks': ['g1', 'g1']}, {**feature, 'source_items': []},
                    {**feature, 'source_acceptance': [{'id': 'g1', 'acceptance': 'weaker acceptance'}]}):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, 'project-source-coverage'):
                project_mode.project_ref_plan_save(self.store, self.rid, project_mode.feature_list(json.dumps({'features': [bad]})))
        features = project_mode.project_ref_plan_save(self.store, self.rid, project_mode.feature_list(raw), raw)
        view = self.store.plan_view(self.rid)
        self.assertEqual((view['revision'], view['held']), (2, False))
        self.assertEqual(features[0]['source_acceptance'], [{'id': 'g1', 'acceptance': parent['tasks'][0]['acceptance']}])
        self.assertEqual(features[0]['source_items'], [{k: item[k] for k in ('id', 'source_sha256', 'evidence')}
                                                    for item in parent['source_items']])
        self.assertIn('project:f1', [c['id'] for c in self.calls()[-1]['criteria']])
        self.assertEqual(self.calls()[-1]['plan']['project_features'][0]['source_items'], features[0]['source_items'])
        forged = deepcopy(view['history'][-1]['plan'])
        forged['project_features'][0]['source_items'][0]['source_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'project-source-coverage'):
            self.store.save_plan(self.rid, forged, expected=2)
        d = project_mode._fresh_status(self.rid, self.raw['prompt'], 1)
        d['features'] = features
        atomic_json(self.store.path(self.rid) / 'project_mode.json', d)
        self.store.update(self.rid, project_active_feature='f1')
        prompt = project_mode.feature_prompt(self.raw['prompt'], d, features[0])
        for item in features[0]['source_items']:
            self.assertIn(item['source_sha256'], prompt)
        self.assertIn(parent['tasks'][0]['acceptance'], prompt)
        child = project_mode.child_execute_real(self.store, self.rid, 'lab', prompt, '',
                                          execute=lambda store, rid: store.update(rid, state='implemented'))
        self.assertEqual(child['feature_acceptance']['source_items'], features[0]['source_items'])
        self.assertEqual(child['feature_acceptance']['source_acceptance'], features[0]['source_acceptance'])
        self.assertEqual(child['job_type'], 'code')
        self.assertEqual(len(self.calls()), 3)

    def test_project_critical_question_is_not_lost_by_list_parsing_or_restart(self):
        import project_mode
        from unittest.mock import Mock
        self.approve()
        generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
        feature = {'id': 'f1', 'title': 'Preserve README', 'description': 'Keep README content', 'acceptance': 'README retained',
                   'qa_command': 'test -e README.md', 'source_tasks': ['g1']}
        decision = {'id': 'project-scope', 'kind': 'scope', 'question': 'Which existing requirement has priority?',
                    'reason': 'The two requested outcomes conflict'}
        envelope = {'features': [feature], 'decisions': [decision]}
        for bad in ({**envelope, 'decisions': None}, {**envelope, 'decisions': [decision, decision]}):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, 'invalid-plan-uncertainty'):
                project_mode.feature_list(json.dumps(bad))
        with self.assertRaisesRegex(ValueError, 'one unambiguous'):
            project_mode.feature_list(json.dumps(envelope) + '\n' + json.dumps({'features': [feature]}))
        planner = Mock(return_value=json.dumps(envelope))
        child = Mock(side_effect=AssertionError('critical question must stop child dispatch'))
        merge = Mock(side_effect=AssertionError('critical question must stop merge'))
        self.assertEqual(project_mode.execute(self.store, self.rid, plan=planner, child_execute=child, merge=merge), 1)
        view = self.store.plan_view(self.rid)
        self.assertEqual((view['revision'], view['held']), (2, True))
        self.assertEqual(view['decisions'][0]['question'], decision['question'])
        self.assertEqual(project_mode.execute(self.store, self.rid, plan=planner, child_execute=child, merge=merge), 78)
        self.assertEqual(planner.call_count, 1)
        child.assert_not_called()
        merge.assert_not_called()
        self.assertEqual(len(self.calls()), 2)

    def test_review_author_and_final_review_use_policy_deadlines_bounded_by_the_request_time(self):
        import qe_core as core
        from limits import deadline
        real_capture, seen = core.capture_opencode, []

        def spy(*args, **kwargs):
            seen.append((os.environ.get('CADRE_TASK_ID', '').rsplit('-', 1)[0], args[3]))
            return real_capture(*args, **kwargs)
        for minute in ('60', '3'):
            seen.clear()
            self.store.update(self.rid, state='cancelled')
            self.rid = self.store.request({**self.raw, 'submission_id': 'review-deadline-' + minute, 'input_mode': 'goal',
                                           'duration_minutes': minute, 'prompt': 'ROUND: review\nInspect README; do not change code.',
                                           'output': 'Findings with source evidence'})['run_id']
            self.approve()
            with patch('acceptance.qa_gate', side_effect=AssertionError('review must not run code QA')), \
                    patch.object(core, 'capture_opencode', side_effect=spy):
                self.assertEqual(execute_run(self.store, self.rid), 0)
            durations = dict(s for s in seen if s[0] in ('review-author', 'review-final'))
            self.assertEqual(set(durations), {'review-author', 'review-final'}, seen)
            if minute == '60':
                self.assertGreater(min(durations.values()), 120, 'the review author and final judge are not restricted to a fixed 120 seconds')
                self.assertLessEqual(durations['review-author'], deadline('plan'))
                self.assertLessEqual(durations['review-final'], deadline('quality'))
            else:
                self.assertTrue(all(s <= 180 for s in durations.values()), 'cannot exceed the request duration')

    def test_review_author_output_wrapped_in_progress_text_and_a_json_fence_is_read_once_and_delivered(self):
        from outcome import approved
        self.rid = self.store.request({**self.raw, 'submission_id': 'review-wrapped', 'input_mode': 'goal',
                                       'prompt': 'ROUND: review\nInspect README; do not change code.', 'output': 'Findings with source evidence'})['run_id']
        self.approve()
        (self.home / 'pipeline-mode').write_text('product-wrapped')
        with patch('acceptance.qa_gate', side_effect=AssertionError('review must not run code QA')):
            self.assertEqual(execute_run(self.store, self.rid), 0, self.store.read(self.rid).get('reason'))
        record = self.store.read(self.rid)
        self.assertEqual((record['state'], record['phase']), ('completed', 'review-delivered'))
        self.assertTrue(approved(record))
        self.assertTrue(json.loads((self.store.path(self.rid) / 'review-output.json').read_text())['report'].startswith('# Findings'))
        prompt = (self.home / 'pipeline-author-prompt.txt').read_text()
        for rule in ('file|command|query|url|bug', 'identifies the EVIDENCE kind', '[K1, K2]', 'expected is a quote on that line',
                      'use it only when necessary and at most five times', 'at most 20 evidence items in total'):
            self.assertIn(rule, prompt, 'the writer sees the mechanical evidence schema in the prompt')

    def test_catalog_selection_delivers_real_file_evidence_and_unknown_reference_uses_the_same_single_repair(self):
        from outcome import approved
        for mode in ('product-catalog', 'product-catalog-invalid-once'):
            with self.subTest(mode=mode):
                self.rid = self.store.request({**self.raw, 'submission_id': mode, 'input_mode': 'goal',
                                               'backlog_path': 'BACKLOG.md#NEXT', 'source_paths': 'README.md',
                                               'prompt': 'ROUND: review\nInspect README against NEXT only; at most 5 file evidence.'})['run_id']
                self.approve()
                (self.home / 'pipeline-mode').write_text(mode)
                self.assertEqual(execute_run(self.store, self.rid), 0, self.store.read(self.rid).get('reason'))
                record = self.store.read(self.rid)
                self.assertTrue(approved(record))
                output = json.loads((self.store.path(self.rid) / 'review-output.json').read_text())
                catalog = next(c['evidence_catalog'] for c in reversed(self.calls()) if c.get('kind') == 'review-output')
                entry = next(k for k in catalog['evidence'] if k['source'].startswith('README.md:'))
                self.assertEqual(output['evidence'][0], {k: v for k, v in entry.items() if k != 'text'} | {'claim': 'README source packet'})
                self.assertNotIn('Existing item', json.dumps(catalog))
                self.assertEqual(output.get('repair', 0), int(mode.endswith('once')))
                self.assertEqual(output['evidence'][0]['round'], 'file')
                self.assertIn('do not produce an evidence array or round/source/expected fields yourself',
                              (self.home / 'pipeline-author-prompt.txt').read_text())

    def test_final_reviewer_gets_bound_mechanical_qa_and_global_constraints_reference_the_whole_report(self):
        from outcome import approved
        self.rid = self.store.request({**self.raw, 'submission_id': 'global-output-constraints', 'input_mode': 'goal',
                                       'prompt': 'ROUND: review\nInspect README only; at most 5 file evidence.',
                                       'acceptance': 'At most 5 file:line evidence references; Do not write test counts; No PASS claims'})['run_id']
        self.approve()
        (self.home / 'pipeline-mode').write_text('product-output-constraints')
        self.assertEqual(execute_run(self.store, self.rid), 0, self.store.read(self.rid).get('reason'))
        record = self.store.read(self.rid)
        self.assertTrue(approved(record))
        question = self.calls()[-1]
        self.assertEqual(question['qa']['counter']['evidence'], 1)
        self.assertEqual(question['qa']['revision'], record['final_result']['revision'])
        items = {o['id'] for o in question['report_elements']}
        decisions = {g['text']: g for g in record['final_result']['review']['requirements']}
        number = decisions.pop('At most 5 file:line evidence references')
        self.assertEqual((number['round'], number['status'], number['report']), ('mechanical', 'satisfied', []))
        self.assertNotIn('R1', [g['id'] for g in question['requirements'] if g['round'] != 'mechanical'])
        for requirement in decisions.values():
            self.assertEqual(set(requirement['report']), items)

    def test_review_phase_deadline_stops_the_real_executor_and_restart_never_launches_a_fresh_author(self):
        import sqlite3
        from contextlib import closing

        import limits
        from run_store import plan_product
        policy = limits.review_policy()
        policy['review-author']['duration_sec'] = 2
        with patch.object(limits, 'review_policy', return_value=policy):
            self.rid = self.store.request({**self.raw, 'submission_id': 'bounded-review', 'input_mode': 'goal',
                                           'prompt': 'ROUND: review\nInspect README only.', 'output': 'Findings with source evidence'})['run_id']
        self.approve()
        before = plan_product(self.repo)
        (self.home / 'pipeline-mode').write_text('product-wait')
        self.assertEqual(execute_run(self.store, self.rid), 78)
        record = self.store.read(self.rid)
        self.assertEqual((record['state'], record['reason']), ('retryable', 'TimeoutExpired'))
        self.assertNotIn('review_delivery', record)
        task, author = next((tid, t) for tid, t in record['tasks'].items() if tid.startswith('review-author-'))
        attempt_id = next(iter(author['executors'].values()))['attempt_id']
        attempt = json.loads((self.store.task_dir(self.rid, task) / 'attempts' / attempt_id / 'attempt.json').read_text())
        self.assertEqual(attempt['outcome'], 'finished')
        self.assertNotEqual(attempt['exit_code'], 0)
        calls = len(self.calls())
        self.assertEqual(execute_run(self.store, self.rid), 78)
        self.assertEqual(len(self.calls()), calls, 'expired author phase is not reset by a new task or restart')
        self.assertNotIn('review_delivery', self.store.read(self.rid))
        self.assertEqual(plan_product(self.repo), before)
        with closing(sqlite3.connect(self.home / 'chain/request-budget.db')) as con:
            self.assertEqual(con.execute("select count(*) from review_control where phase='review-author'").fetchone()[0], 1)

    def test_slow_final_review_does_not_spend_the_writer_repair_clock(self):
        import time

        import run_store
        import limits
        from outcome import approved
        policy = limits.review_policy()
        policy['review-author']['duration_sec'] = 2
        with patch.object(limits, 'review_policy', return_value=policy):
            self.rid = self.store.request({**self.raw, 'submission_id': 'slow-judge-writer-repair', 'input_mode': 'goal',
                                           'prompt': 'ROUND: review\nInspect README only.'})['run_id']
        self.approve()
        (self.home / 'pipeline-mode').write_text('product-final-reject-once')
        original = run_store.capture_source_plan
        waited = False

        def capture(*args, **kwargs):
            nonlocal waited
            if args[5].startswith('review-final-') and not waited:
                waited = True
                time.sleep(2.1)
            return original(*args, **kwargs)

        with patch.object(run_store, 'capture_source_plan', side_effect=capture):
            self.assertEqual(execute_run(self.store, self.rid), 0, self.store.read(self.rid).get('reason'))
        record = self.store.read(self.rid)
        self.assertTrue(approved(record))
        output = json.loads((self.store.path(self.rid) / 'review-output.json').read_text())
        self.assertEqual(output['repair'], 1, 'the existing shared single repair is retained')
        self.assertEqual(sum(c.get('kind') == 'review-output' for c in self.calls()), 2)

    def test_review_progress_shows_writer_and_final_review_after_the_plan_gate(self):
        import run_store
        from panel_data import task_node
        self.rid = self.store.request({**self.raw, 'submission_id': 'review-visible-phases', 'input_mode': 'goal',
                                       'prompt': 'ROUND: review\nInspect README only.'})['run_id']
        self.approve()
        original, seen = run_store.capture_source_plan, []

        def capture(*args, **kwargs):
            if args[5].startswith(('review-author-', 'review-final-')):
                record = self.store.read(self.rid)
                node = task_node(self.home, record)
                seen.append((record['planning']['status'], record['state'], record['phase'], node['stage_label'], node['input_waiting']))
            return original(*args, **kwargs)

        with patch.object(run_store, 'capture_source_plan', side_effect=capture):
            self.assertEqual(execute_run(self.store, self.rid), 0)
        self.assertEqual(seen, [('accepted', 'running', 'review-author', 'Review report', False),
                                ('accepted', 'running', 'review-final', 'Final review', False)])

    def test_unverified_executor_stop_never_pauses_the_writer_clock(self):
        import time

        import run_store
        import qe_core as core
        import limits
        policy = limits.review_policy()
        policy['review-author']['duration_sec'] = 2
        with patch.object(limits, 'review_policy', return_value=policy):
            self.rid = self.store.request({**self.raw, 'submission_id': 'unverified-phase-stop', 'input_mode': 'goal',
                                           'prompt': 'ROUND: review\nInspect README only.'})['run_id']
        self.approve()
        (self.home / 'pipeline-mode').write_text('product-final-reject-once')
        original, original_executor = run_store.capture_source_plan, core.capture_opencode

        def executor(*args, **kwargs):
            output = original_executor(*args, **kwargs)
            if kwargs.get('role') == 'impl':
                path = output.attempt.directory / 'process.json'
                receipt = json.loads(path.read_text())
                receipt.pop('stop_observed', None)
                atomic_json(path, receipt)
            return output

        def capture(*args, **kwargs):
            if args[5].startswith('review-final-'):
                time.sleep(2.1)
            return original(*args, **kwargs)

        with patch.object(run_store, 'capture_source_plan', side_effect=capture), \
                patch.object(core, 'capture_opencode', side_effect=executor):
            self.assertEqual(execute_run(self.store, self.rid), 78)
        self.assertEqual(self.store.read(self.rid)['reason'], 'BudgetDenied')
        self.assertEqual(sum(c.get('kind') == 'review-output' for c in self.calls()), 1)

    def test_review_delivery_stops_at_a_rejected_plan_before_author_or_final_judge(self):
        import acceptance
        for status, boundary in (('FAIL', None), ('ERROR', 'project-tool-sandbox-unavailable')):
            with self.subTest(status=status):
                self.rid = self.store.request({**self.raw, 'submission_id': 'review-plan-' + status,
                                               'input_mode': 'goal', 'prompt': 'ROUND: review\nInspect README only.'})['run_id']
                self.approve()
                generate_request_plan(self.store, self.rid, str(self.fake))
                review = {'status': status, 'revision_number': self.store.plan_view(self.rid)['revision']}
                record = self.store.read(self.rid)
                record.update(plan_reviews=[review], tool_boundary_wait=boundary)
                atomic_json(self.store.path(self.rid) / 'run.json', record)
                with patch('run_store.plan_ready', return_value=review), \
                        patch('run_store.capture_source_plan', side_effect=AssertionError('unreviewed plan must not launch a delivery role')):
                    self.assertEqual(acceptance.deliver_review(self.store, self.rid, str(self.fake)), 78)
                record = self.store.read(self.rid)
                self.assertEqual((record['state'], record['planning']['status']), ('retryable', 'rejected'))
                self.assertEqual(record['reason'], boundary or 'current-plan-review-required')
                self.assertEqual(record['plan_reviews'][-1], review)
                self.assertNotIn(record.get('phase'), ('review-author', 'review-final'))
                self.assertNotIn('review_delivery', record)
                self.assertFalse((self.store.path(self.rid) / 'review-output.json').exists())

    def test_review_delivery_preserves_cancellation_arriving_during_plan_review(self):
        import acceptance
        for terminal in (False, True):
            with self.subTest(terminal=terminal):
                self.rid = self.store.request({**self.raw, 'submission_id': 'review-cancel-' + str(terminal),
                                               'input_mode': 'goal', 'prompt': 'ROUND: review\nInspect README only.'})['run_id']
                self.approve()
                generate_request_plan(self.store, self.rid, str(self.fake))

                def interrupted(*args):
                    (self.store.path(self.rid) / 'cancel.flag').write_text('requested\n')
                    if terminal:
                        self.store.update(self.rid, state='cancelled', reason='operator-cancelled')
                    return {'status': 'ERROR'}

                before = self.store.read(self.rid)
                with patch('run_store.plan_ready', side_effect=interrupted), \
                        patch('run_store.capture_source_plan', side_effect=AssertionError('cancelled delivery must not launch a model')):
                    self.assertEqual(acceptance.deliver_review(self.store, self.rid, str(self.fake)), 130)
                record = self.store.read(self.rid)
                self.assertEqual(record['state'], 'cancelled' if terminal else before['state'])
                self.assertEqual(record['reason'], 'operator-cancelled' if terminal else before['reason'])
                self.assertEqual(record['planning'], before['planning'])
                self.assertNotIn('review_delivery', record)

    def test_review_product_delivers_source_bound_evidence_without_code_qa_or_source_edits(self):
        from outcome import approved
        from panel_data import task_node
        self.rid = self.store.request({**self.raw, 'submission_id': 'review-product', 'input_mode': 'backlog',
                                       'backlog_path': 'BACKLOG.md', 'prompt': 'ROUND: review\nInspect README; do not change code.',
                                       'output': 'Findings with source evidence'})['run_id']
        before = {name: (self.repo / name).read_bytes() for name in ('README.md', 'BACKLOG.md')}
        proposal = self.approve()
        self.assertEqual(proposal['action']['permissions'], ['model-calls'])
        with patch('acceptance.qa_gate', side_effect=AssertionError('review must not run code QA')):
            self.assertEqual(execute_run(self.store, self.rid), 0)
            record = self.store.read(self.rid)
            self.assertTrue(approved(record), record)
            self.assertEqual(execute_run(self.store, self.rid, proceed=True), 0)
        self.assertEqual((record['state'], record['phase']), ('completed', 'review-delivered'))
        self.assertEqual(record['final_result']['qa']['code_qa']['status'], 'NOT_APPLICABLE')
        self.assertEqual(len(self.calls()), 4)
        artifact = Path(record['review_delivery']['artifact'])
        data = artifact.read_bytes()
        delivery = json.loads(data)
        self.assertNotEqual(delivery['session_id'], delivery['result']['review']['session_id'])
        self.assertEqual(set(self.calls()[-1]['plan']['source_coverage']['merged'][0]['source_items']), {'OLD', 'NEXT'})
        node = task_node(self.home, record)
        self.assertIn('Review report', str(node))
        self.assertIn(str(artifact), str(node))
        artifact.write_bytes(data.replace(b'README', b'changed', 1))
        self.assertFalse(approved(record))
        artifact.write_bytes(data)
        (self.repo / 'README.md').write_text('changed after review\n')
        self.assertFalse(approved(record))
        (self.repo / 'README.md').write_bytes(before['README.md'])
        self.assertTrue(approved(record))
        self.assertEqual({name: (self.repo / name).read_bytes() for name in before}, before)
        self.assertFalse((self.repo / 'REVIEW.md').exists())

    def test_review_product_rejects_false_evidence_rejection_reused_session_and_critical_questions(self):
        from outcome import approved
        for mode, count, held in (('product-bad-evidence', 4, False), ('product-final-reject', 5, False),
                                  ('product-reused', 4, False), ('product-author-critical', 3, True),
                                  ('product-final-critical', 4, True), ('product-write', 3, False), ('product-budget', 3, False)):
            with self.subTest(mode=mode):
                project = mode.removeprefix('product-')
                folder = self.home / 'projects' / project
                folder.mkdir()
                (folder / 'repo-path').write_text(str(self.repo))
                self.rid = self.store.request({**self.raw, 'project': project, 'submission_id': mode, 'input_mode': 'goal',
                                               'prompt': 'ROUND: review\nInspect README without changes.'})['run_id']
                (self.home / 'pipeline-mode').write_text(mode)
                self.approve()
                before = len(self.calls())
                self.assertEqual(execute_run(self.store, self.rid), 78)
                record = self.store.read(self.rid)
                self.assertFalse(approved(record))
                self.assertNotEqual(record['state'], 'completed')
                self.assertEqual(self.store.plan_view(self.rid)['held'], held)
                self.assertEqual(len(self.calls()) - before, count)
                if mode == 'product-final-reject':
                    self.assertEqual(record['final_result']['qa']['status'], 'PASS', 'a matching quote does not prove its interpretation')
                    self.assertEqual(record['final_result']['review']['status'], 'FAIL')
                    (self.home / 'pipeline-mode').write_text('')
                    self.assertEqual(execute_run(self.store, self.rid, proceed=True), 78)
                    self.assertEqual(len(self.calls()) - before, count)
                if mode == 'product-bad-evidence':
                    self.assertEqual(record['reason'], 'review-evidence-not-passed')
                    self.assertEqual(execute_run(self.store, self.rid), 78)
                    self.assertEqual(len(self.calls()) - before, count, 'restarting does not renew the single repair allowance')
                if mode == 'product-write':
                    self.assertEqual(record['reason'], 'plan-review-source-modified')
                    self.assertTrue((self.repo / 'forbidden.txt').exists())
                    (self.repo / 'forbidden.txt').unlink()

    def test_review_author_gets_one_repair_with_the_evidence_findings_and_the_repaired_report_is_delivered(self):
        from outcome import approved
        self.rid = self.store.request({**self.raw, 'submission_id': 'review-repair', 'input_mode': 'goal',
                                       'prompt': 'ROUND: review\nInspect README without changes.'})['run_id']
        self.approve()
        (self.home / 'pipeline-mode').write_text('product-bad-evidence-once')
        with patch('acceptance.qa_gate', side_effect=AssertionError('review must not run code QA')):
            self.assertEqual(execute_run(self.store, self.rid), 0, self.store.read(self.rid).get('reason'))
        record = self.store.read(self.rid)
        self.assertEqual((record['state'], record['phase']), ('completed', 'review-delivered'))
        self.assertTrue(approved(record))
        prompt = (self.home / 'pipeline-author-prompt.txt').read_text()
        self.assertIn('EVIDENCE AUDIT FAIL', prompt)
        self.assertIn('invented content', prompt, 'repair sees the previous answer and finding')
        self.assertEqual(json.loads((self.store.path(self.rid) / 'review-output.json').read_text())['repair'], 1)
        self.assertEqual(len(self.calls()), 5, 'plan, plan review, writer, single repair, last judge')

    def test_review_evidence_commands_rerun_without_write_access_to_the_reviewed_source(self):
        import report_mode
        self.rid = self.store.request({**self.raw, 'submission_id': 'review-side-effect', 'input_mode': 'goal',
                                       'prompt': 'ROUND: review\nInspect README without changes.'})['run_id']
        self.approve()
        (self.home / 'pipeline-mode').write_text('product-side-effect')
        with patch('acceptance.qa_gate', side_effect=AssertionError('review must not run code QA')):
            self.assertEqual(execute_run(self.store, self.rid), 0, self.store.read(self.rid).get('reason'))
        self.assertFalse((self.repo / 'SIDE_EFFECT').exists(), 'review evidence cannot write to the source')
        self.assertEqual(self.store.read(self.rid)['phase'], 'review-delivered')
        temporary = self.home / 'scratch'
        rc, _ = report_mode.command_execute('touch SIDE_EFFECT; echo complete', self.repo, temporary, 10, source_read_only=True)
        self.assertEqual((rc, (self.repo / 'SIDE_EFFECT').exists()), (0, False))
        rc, _ = report_mode.command_execute('touch SIDE_EFFECT', self.repo, temporary, 10)
        self.assertTrue((self.repo / 'SIDE_EFFECT').exists(), 'the report lane’s working tree remains writable')
        (self.repo / 'SIDE_EFFECT').unlink()

    def test_independent_semantic_rejection_uses_the_one_repair_and_a_new_final_judge_on_changed_content(self):
        from outcome import approved
        self.rid = self.store.request({**self.raw, 'submission_id': 'semantic-review-repair', 'input_mode': 'goal',
                                       'prompt': 'ROUND: review\nInspect README without changes.'})['run_id']
        self.approve()
        (self.home / 'pipeline-mode').write_text('product-final-reject-once')
        self.assertEqual(execute_run(self.store, self.rid), 0, self.store.read(self.rid).get('reason'))
        record = self.store.read(self.rid)
        self.assertTrue(approved(record))
        output = json.loads((self.store.path(self.rid) / 'review-output.json').read_text())
        self.assertEqual(output['repair'], 1)
        self.assertNotIn('Source verifies clean installation', output['report'])
        self.assertEqual(len(self.calls()), 6, 'one semantic repair; two independent final judgments')
        rejected = list(self.store.path(self.rid).glob('review-rejected-*.json'))
        self.assertEqual(len(rejected), 1)
        previous = json.loads(rejected[0].read_text())['result']
        self.assertEqual((previous['qa']['status'], previous['review']['status']), ('PASS', 'FAIL'))
        self.assertNotEqual(previous['review']['session_id'], record['final_result']['review']['session_id'])

    def test_review_repair_is_reserved_before_an_interruption_and_cannot_repeat_after_restart(self):
        import run_store
        from run_store import capture_source_plan
        for mode, count in (('product-bad-evidence', 3), ('product-final-reject-once', 4)):
            with self.subTest(mode=mode):
                project = 'interrupted-' + mode
                folder = self.home / 'projects' / project
                folder.mkdir()
                (folder / 'repo-path').write_text(str(self.repo))
                self.rid = self.store.request({**self.raw, 'project': project, 'submission_id': project, 'input_mode': 'goal',
                                               'prompt': 'ROUND: review\nInspect README without changes.'})['run_id']
                self.approve()
                (self.home / 'pipeline-mode').write_text(mode)
                before = len(self.calls())
                def capture(*args, **kwargs):
                    if 'Previous answer:' in args[4]:
                        raise RuntimeError('fixture interrupted before repair provider call')
                    return capture_source_plan(*args, **kwargs)
                with patch.object(run_store, 'capture_source_plan', side_effect=capture):
                    self.assertEqual(execute_run(self.store, self.rid), 78, self.store.read(self.rid).get('reason'))
                saved = (self.store.path(self.rid) / 'review-output.json').read_bytes()
                self.assertEqual(json.loads(saved)['repair'], 1)
                self.assertEqual(len(self.calls()) - before, count)
                (self.home / 'pipeline-mode').write_text('')
                self.assertEqual(execute_run(self.store, self.rid, proceed=True), 78)
                self.assertEqual(len(self.calls()) - before, count, 'restart cannot spend another repair or judge on unchanged content')
                self.assertEqual((self.store.path(self.rid) / 'review-output.json').read_bytes(), saved)

    def test_review_structured_report_repairs_unknown_reference_once_without_bypassing_the_judge(self):
        from outcome import approved
        self.rid = self.store.request({**self.raw, 'submission_id': 'structured-review', 'input_mode': 'goal',
                                       'prompt': 'ROUND: review\nInspect README without changes.'})['run_id']
        self.approve()
        (self.home / 'pipeline-mode').write_text('product-bad-report-once')
        self.assertEqual(execute_run(self.store, self.rid), 0)
        record = self.store.read(self.rid)
        self.assertTrue(approved(record))
        output = json.loads((self.store.path(self.rid) / 'review-output.json').read_text())
        self.assertEqual(output['repair'], 1)
        self.assertEqual(output['report_errors'], [])
        self.assertIn('README source preserved [K1].', output['report'])
        self.assertIsInstance(output['report_input'], dict)
        self.assertEqual(len(self.calls()), 5)
        self.assertEqual(self.calls()[-1]['report'], output['report'], 'judge sees the exact rendered product')
        self.assertIn('report-format:', (self.home / 'pipeline-author-prompt.txt').read_text())

    def test_review_structured_report_failure_is_preserved_and_does_not_spend_again_on_resume(self):
        from outcome import approved
        self.rid = self.store.request({**self.raw, 'submission_id': 'structured-review-bad', 'input_mode': 'goal',
                                       'prompt': 'ROUND: review\nInspect README without changes.'})['run_id']
        self.approve()
        (self.home / 'pipeline-mode').write_text('product-bad-report')
        self.assertEqual(execute_run(self.store, self.rid), 78)
        record = self.store.read(self.rid)
        self.assertFalse(approved(record))
        self.assertEqual(record['reason'], 'review-evidence-not-passed')
        self.assertIn('report-format:', record['final_result']['qa']['findings'][0])
        self.assertEqual(len(self.calls()), 4, 'no final judge for an invalid report')
        output = (self.store.path(self.rid) / 'review-output.json').read_bytes()
        (self.home / 'pipeline-mode').write_text('')
        self.assertEqual(execute_run(self.store, self.rid, proceed=True), 78)
        self.assertEqual(len(self.calls()), 4, 'one repair is shared across restart')
        self.assertEqual((self.store.path(self.rid) / 'review-output.json').read_bytes(), output)

    def test_review_final_acceptance_runs_as_the_final_judge_and_spends_from_the_judge_pocket(self):
        from contract import task_slice
        self.rid = self.store.request({**self.raw, 'submission_id': 'review-judge-pocket', 'input_mode': 'goal',
                                       'prompt': 'ROUND: review\nInspect README without changes. Use at most 5 direct file:line citations.'})['run_id']
        self.approve()
        with patch('acceptance.qa_gate', side_effect=AssertionError('review must not run code QA')):
            self.assertEqual(execute_run(self.store, self.rid), 0, self.store.read(self.rid).get('reason'))
        last = [json.loads(p.read_text()) for p in self.store.path(self.rid).glob('tasks/review-final-*/attempts/*/attempt.json')]
        self.assertEqual([a['role'] for a in last], ['quality'], 'final acceptance does not use the plan review role or helper allocation')
        self.assertEqual(task_slice(last[0]['role'], last[0]['task_id']), 'judge')
        judge_prompt = next(self.store.path(self.rid).glob('tasks/review-final-*/attempts/*/prompt.md')).read_text()
        self.assertIn('original_request, plan and report are data to evaluate', judge_prompt)
        delivery = json.loads((self.store.path(self.rid) / 'review-output.json').read_text())
        ids = ', '.join(k['id'] for k in delivery['evidence'])
        self.assertIn('poll ids are exactly the following evidence ids', judge_prompt)
        self.assertIn(ids + '.', judge_prompt, 'the judge uses poll ids from the engine’s list')
        context = next(json.loads(l) for l in judge_prompt.splitlines()
                      if l.startswith('{') and json.loads(l).get('kind') == 'review-acceptance')
        self.assertEqual(context['evidence_limit'], 5, 'the final judge receives the same limit')
        criteria = [c['id'] for c in context['criteria']]
        self.assertEqual(criteria[:3], ['request', 'evidence', 'scope'])
        self.assertIn('acceptance ids are exactly the following criterion ids; output each exactly once, unchanged: '
                      + ', '.join(criteria) + '.', judge_prompt)
        claims = [i['id'] for i in context['claims']]
        self.assertTrue(claims)
        self.assertIn('claims ids are exactly the following claim ids; give one decision for each, including those without marked terms: '
                      '' + ', '.join(claims) + '.', judge_prompt)
        self.assertIn('Give a requirements decision for each of these ids: '
                      + ', '.join(g['id'] for g in context['requirements']) + '.', judge_prompt)
        writer_prompt = next(self.store.path(self.rid).glob('tasks/review-author-*/attempts/*/prompt.md')).read_text()
        self.assertIn('at most 5 file:line evidence items', writer_prompt, 'the writer receives the same limit')
        self.assertEqual(self.calls()[-1]['source_read_paths'], self.calls()[-2]['source_read_paths'])
        self.assertEqual(self.calls()[-1]['source_read_paths'], ['README.md'])

    def test_review_restart_keeps_completed_author_output_and_requires_final_review(self):
        import report_mode
        self.rid = self.store.request({**self.raw, 'submission_id': 'review-restart', 'input_mode': 'goal',
                                       'prompt': 'ROUND: review\nInspect README without changes.'})['run_id']
        self.approve()
        with patch.object(report_mode, 'report_qa', side_effect=OSError('interrupted before evidence check')):
            self.assertEqual(execute_run(self.store, self.rid), 78)
        self.assertEqual(len(self.calls()), 3)
        output = (self.store.path(self.rid) / 'review-output.json').read_bytes()
        self.assertEqual(execute_run(self.store, self.rid, proceed=True), 0)
        self.assertEqual((self.store.path(self.rid) / 'review-output.json').read_bytes(), output)
        self.assertEqual(len(self.calls()), 4)

    def test_review_judge_protocol_error_gets_one_fresh_judge_without_using_the_author_repair(self):
        from outcome import approved
        self.rid = self.store.request({**self.raw, 'submission_id': 'judge-protocol-once', 'input_mode': 'goal',
                                       'prompt': 'ROUND: review\nInspect README without changes.'})['run_id']
        self.approve()
        (self.home / 'pipeline-mode').write_text('product-final-protocol-once')
        self.assertEqual(execute_run(self.store, self.rid), 0, self.store.read(self.rid).get('reason'))
        record = self.store.read(self.rid)
        self.assertTrue(approved(record))
        self.assertEqual(record['judge_protocol_repair'], 1)
        self.assertEqual(len(self.calls()), 5, 'plan, plan review, author, invalid final judge and one fresh final judge')
        self.assertNotIn('repair', json.loads((self.store.path(self.rid) / 'review-output.json').read_text()))
        prompts = [p.read_text() for p in self.store.path(self.rid).glob('tasks/review-final-*/attempts/*/prompt.md')]
        prompts += [p.read_text() for p in self.store.path(self.rid).glob('tasks/review-recheck-*/attempts/*/prompt.md')]
        repair = [i for i in prompts if 'JUDGE PROTOCOL REPAIR' in i]
        self.assertEqual(len(list(self.store.path(self.rid).glob('tasks/review-recheck-*'))), 1, 'the protocol judge uses a separate recheck allocation')
        self.assertTrue(all(i.startswith('SEPARATE CLEAN READ-ONLY END REVIEW.') for i in prompts))
        self.assertEqual((len(prompts), len(repair)), (2, 1))
        self.assertIn('claim decision missing: Method-1', repair[0])
        sessions = {json.loads(p.read_text())['session_id']
                     for p in self.store.path(self.rid).glob('tasks/review-*/attempts/*/attempt.json') if 'review-author' not in str(p)}
        self.assertEqual(len(sessions), 2)
        self.assertIn(record['final_result']['review']['session_id'], sessions)

    def test_incomplete_fix_judge_sends_only_bound_findings_to_the_single_repair_and_recheck_has_its_own_share(self):
        import sqlite3
        from contextlib import closing

        from outcome import approved
        self.rid = self.store.request({**self.raw, 'submission_id': 'judge-incomplete-fix', 'input_mode': 'goal',
                                       'prompt': 'ROUND: review\nInspect README without changes.'})['run_id']
        self.approve()
        (self.home / 'pipeline-mode').write_text('product-final-incomplete-revise-once')
        self.assertEqual(execute_run(self.store, self.rid), 0, self.store.read(self.rid).get('reason'))
        record = self.store.read(self.rid)
        self.assertTrue(approved(record))
        self.assertIsNone(record.get('judge_protocol_repair'), 'a concrete REVISE finding does not seek PASS through a protocol recheck judge')
        self.assertEqual(len(self.calls()), 6, 'plan, plan review, author, incomplete judge, one repair and one recheck judge')
        rejected = [json.loads(p.read_text()) for p in self.store.path(self.rid).glob('review-rejected-*.json')]
        self.assertEqual([(r['result']['status'], r['result']['review']['status']) for r in rejected], [('FAIL', 'ERROR')],
                         'an incomplete answer is not acceptance')
        finding = rejected[0]['result']['review']['fix_findings']
        self.assertEqual([(b['round'], b['id']) for b in finding], [('claim', 'Findings-1')])
        writer = sorted(self.store.path(self.rid).glob('tasks/review-author-*/attempts/*/prompt.md'), key=lambda p: p.stat().st_mtime)
        self.assertIn('README line does not show clean installation or rollback', writer[-1].read_text())
        self.assertEqual(json.loads((self.store.path(self.rid) / 'review-output.json').read_text())['repair_source'], 'judge')
        first = list(self.store.path(self.rid).glob('tasks/review-final-*/attempts/*/attempt.json'))
        again = [json.loads(p.read_text()) for p in self.store.path(self.rid).glob('tasks/review-recheck-*/attempts/*/attempt.json')]
        self.assertEqual((len(first), len(again)), (1, 1))
        self.assertEqual(record['final_result']['review']['session_id'], again[0]['session_id'])
        with closing(sqlite3.connect(self.home / 'chain/request-budget.db')) as con:
            phases = dict(con.execute("select phase,calls from review_control where run_id=? and phase like 'review-%'",
                                      (self.rid,)).fetchall())
        self.assertLessEqual({'review-final', 'review-recheck'}, set(phases), 'the two judges have separate persistent phase counters')

    def test_incomplete_judge_without_concrete_findings_never_passes_and_opens_no_repair(self):
        from outcome import approved
        self.rid = self.store.request({**self.raw, 'submission_id': 'judge-incomplete-always', 'input_mode': 'goal',
                                       'prompt': 'ROUND: review\nInspect README without changes.'})['run_id']
        self.approve()
        (self.home / 'pipeline-mode').write_text('product-final-incomplete-always')
        self.assertEqual(execute_run(self.store, self.rid), 78)
        record = self.store.read(self.rid)
        self.assertFalse(approved(record))
        self.assertEqual((record['state'], record['reason'], record['final_result']['status'], record['final_result']['review']['status']),
                         ('retryable', 'review-not-passed', 'FAIL', 'ERROR'))
        self.assertEqual(record['judge_protocol_repair'], 1)
        self.assertEqual(len(self.calls()), 5, 'plan, plan review, author, incomplete judge and one fresh protocol judge')
        self.assertNotIn('repair', json.loads((self.store.path(self.rid) / 'review-output.json').read_text()))
        self.assertEqual(len(list(self.store.path(self.rid).glob('tasks/review-recheck-*'))), 1)

    def test_protocol_recheck_judge_rejection_opens_no_repair_because_no_judge_share_remains(self):
        from outcome import approved
        self.rid = self.store.request({**self.raw, 'submission_id': 'judge-protocol-reject', 'input_mode': 'goal',
                                       'prompt': 'ROUND: review\nInspect README without changes.'})['run_id']
        self.approve()
        (self.home / 'pipeline-mode').write_text('product-final-protocol-then-reject')
        self.assertEqual(execute_run(self.store, self.rid), 78)
        record = self.store.read(self.rid)
        self.assertFalse(approved(record))
        self.assertEqual((record['state'], record['reason'], record['final_result']['review']['status']), ('retryable', 'review-not-passed', 'FAIL'))
        self.assertEqual(len(self.calls()), 5, 'plan, plan review, author, first judge and protocol recheck judge; no repair')
        self.assertNotIn('repair', json.loads((self.store.path(self.rid) / 'review-output.json').read_text()))

    def test_review_invalid_probe_identity_is_preserved_without_repeating_paid_review_on_resume(self):
        from outcome import approved
        self.rid = self.store.request({**self.raw, 'submission_id': 'review-invalid-probe', 'input_mode': 'goal',
                                       'prompt': 'ROUND: review\nInspect README without changes.'})['run_id']
        self.approve()
        (self.home / 'pipeline-mode').write_text('product-final-bad-id')
        self.assertEqual(execute_run(self.store, self.rid), 78)
        record = self.store.read(self.rid)
        self.assertFalse(approved(record))
        self.assertEqual(record['final_result']['review']['status'], 'ERROR')
        self.assertIn('K1-verification', record['final_result']['review']['reason'])
        artifact = self.store.path(self.rid) / 'review-delivery.json'
        saved = artifact.read_bytes()
        calls = len(self.calls())
        self.assertEqual(calls, 5, 'plan, plan review, author, one final review and one fresh protocol review')
        (self.home / 'pipeline-mode').write_text('')
        self.assertEqual(execute_run(self.store, self.rid, proceed=True), 78)
        self.assertEqual(len(self.calls()), calls, 'unchanged invalid review must not start another paid source review')
        self.assertEqual(artifact.read_bytes(), saved)
        self.assertFalse(approved(self.store.read(self.rid)))

    def test_review_real_process_cancellation_and_second_owner_never_deliver_or_duplicate_author(self):
        import time
        self.rid = self.store.request({**self.raw, 'submission_id': 'review-cancel', 'input_mode': 'goal',
                                       'prompt': 'ROUND: review\nInspect README without changes.'})['run_id']
        self.approve()
        generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
        (self.home / 'pipeline-mode').write_text('product-wait')
        worker = subprocess.Popen([sys.executable, str(LIB / 'run_store.py'), 'run', self.rid],
                                  env=self.operator_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 15
            while not (self.home / 'pipeline-ready').exists() and worker.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            if worker.poll() is not None and not (self.home / 'pipeline-ready').exists():
                out, err = worker.communicate()
                self.fail(out + err)
            self.assertTrue((self.home / 'pipeline-ready').exists(), (worker.poll(), self.store.read(self.rid).get('reason')))
            second = self.operator_cli('run', self.rid)
            self.assertEqual(second.returncode, 75, second.stderr)
            (self.store.path(self.rid) / 'cancel.flag').touch()
            out, err = worker.communicate(timeout=15)
            self.assertEqual(worker.returncode, 130, out + err)
        finally:
            if worker.poll() is None:
                worker.terminate()
                worker.communicate(timeout=10)
        self.assertEqual(self.store.read(self.rid)['state'], 'cancelled')
        self.assertFalse((self.store.path(self.rid) / 'review-delivery.json').exists())
        self.assertEqual(len(self.calls()), 3)
        self.assertEqual(execute_run(self.store, self.rid, proceed=True), 130)
        self.assertEqual(len(self.calls()), 3)

    def test_legacy_reviewed_plan_uses_review_acceptance_without_reclassifying_or_running_code_qa(self):
        from outcome import approved
        self.store.update(self.rid, state='cancelled')
        self.rid = self.store.accept('lab', 'ROUND: review\nInspect README without edits.')['run_id']
        path = self.store.path(self.rid) / 'run.json'
        record = json.loads(path.read_text())
        record.pop('job_type')
        path.write_text(json.dumps(record))
        plan = {'job_id': 'legacy-review', 'job_kind': 'review', 'tasks': [
            {'id': 'g1', 'definition': 'Inspect README', 'acceptance': 'Findings reference README source', 'files': ['README.md'],
             'dependent': [], 'model': 'auto', 'difficulty': 2, 'parallel_group': 0, 'estimate_min': 1}]}
        self.store.save_plan(self.rid, plan)
        self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
        with patch('acceptance.qa_gate', side_effect=AssertionError('legacy review cannot run code QA')):
            self.assertEqual(execute_run(self.store, self.rid), 0)
        final = self.store.read(self.rid)
        self.assertTrue(approved(final))
        self.assertEqual(final['mode'], 'review')
        self.assertNotIn('job_type', final)
        self.assertEqual(final['final_result']['qa']['job_type'], 'review')
        self.assertEqual(len(self.calls()), 3)

    def test_review_delivery_survives_process_exit_before_terminal_record_without_repeating_a_model(self):
        from outcome import approved
        self.rid = self.store.request({**self.raw, 'submission_id': 'review-publication-exit', 'input_mode': 'goal',
                                       'prompt': 'ROUND: review\nInspect README without changes.'})['run_id']
        self.approve()
        generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')
        script = '''import os,sys
sys.path.insert(0,sys.argv[1])
import run_store
write=run_store.atomic_json
def interrupt(path,value):
    write(path,value)
    if path.name=='review-delivery.json': os._exit(91)
run_store.atomic_json=interrupt
raise SystemExit(run_store.execute_run(run_store.RunStore(),sys.argv[2]))
'''
        process = subprocess.run([sys.executable, '-c', script, str(LIB), self.rid], env=self.operator_env(),
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 91, process.stderr)
        self.assertEqual(len(self.calls()), 4)
        self.assertNotEqual(self.store.read(self.rid)['state'], 'completed')
        artifact = (self.store.path(self.rid) / 'review-delivery.json').read_bytes()
        self.assertEqual(execute_run(self.store, self.rid, proceed=True), 0)
        self.assertTrue(approved(self.store.read(self.rid)))
        self.assertEqual((self.store.path(self.rid) / 'review-delivery.json').read_bytes(), artifact)
        self.assertEqual(len(self.calls()), 4)

    def test_expired_request_does_not_call_provider(self):
        self.rid = self.store.request({**self.raw, 'submission_id': 'expired'})['run_id']
        self.approve()
        self.duration_fill()
        with self.assertRaisesRegex(ValueError, 'request-time-limit'):
            generate_request_plan(self.store, self.rid, str(self.fake))
        from run_store import start_run
        with patch('run_store.subprocess.run') as spawn, self.assertRaisesRegex(ValueError, 'request-time-limit'):
            start_run(self.store, self.rid)
        spawn.assert_not_called()
        self.assertEqual(self.calls(), [])

    def test_plan_publish_interruption_repairs_mirror_without_another_planner_call(self):
        import run_store
        self.approve()
        original = run_store.atomic_json
        def interrupt(path, value):
            if Path(path).name == 'plan.json':
                raise OSError('fixture interrupted mirror publication')
            return original(path, value)
        with patch('run_store.atomic_json', side_effect=interrupt):
            with self.assertRaises(OSError):
                generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(self.store.plan_view(self.rid)['revision'], 1)
        self.assertFalse(self.store.plan_view(self.rid)['execution_plan_matches'])
        self.assertEqual(len(self.calls()), 1)
        view = generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertTrue(view['execution_plan_matches'])
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'PASS')

    def test_original_request_tampering_and_stale_claim_are_handled_before_a_new_call(self):
        from run_store import atomic_json
        self.approve()
        record = self.store.read(self.rid)
        record['plan_review_claim'] = {'id': 'interrupted', 'task_id': 'plan-input-old', 'revision': 0}
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertIsNone(self.store.read(self.rid)['plan_review_claim'])
        self.assertEqual(self.store.read(self.rid)['plan_reviews'][0]['reason'], 'interrupted-planning')
        (self.store.path(self.rid) / 'prompt.md').write_text('different original request')
        before = len(self.calls())
        result = review_plan(self.store, self.rid, 1, str(self.fake))
        self.assertEqual(result['status'], 'ERROR')
        self.assertEqual(len(self.calls()), before)


    def operator_env(self):
        import qe_core as core
        import time
        snapshot = self.home / 'chain/request-budget.json'
        if json.loads(snapshot.read_text()).get('budget') is None:
            snapshot.write_text(json.dumps({'observed': time.time(), 'budget': core.budget_json()}))
        env = {**os.environ, **self.fixture.source.env, 'CADRE_HOME': str(self.home), 'CADRE_REAL_OPENCODE': str(self.fake),
               'CADRE_MODEL_CATALOG': str(self.home / 'models.json')}
        for key in ('CADRE_RUN_ID', 'CADRE_TASK_ID', 'CADRE_OWNER_FDS', 'CADRE_COORDINATOR_PID'):
            env.pop(key, None)
        return env

    def operator_cli(self, *args, text=None):
        return subprocess.run([sys.executable, str(LIB / 'run_store.py'), *args], input=text,
                              env=self.operator_env(), text=True, capture_output=True, timeout=30)

    def critical_plan(self, mode='critical-authority'):
        self.approve()
        (self.home / 'pipeline-mode').write_text(mode)
        self.assertEqual(execute_run(self.store, self.rid), 78)
        self.assertEqual(self.store.read(self.rid)['reason'], 'plan-decision-required')

    def test_routine_assumption_is_recorded_and_does_not_require_an_operator_decision(self):
        self.approve()
        (self.home / 'pipeline-mode').write_text('routine')
        self.assertEqual(execute_run(self.store, self.rid), 0)
        view = self.store.plan_view(self.rid)
        self.assertEqual(view['decisions'], [])
        self.assertEqual(view['history'][0]['plan']['assumptions'][0]['id'], 'style')
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(self.calls()[1]['plan']['assumptions'][0]['choice'], 'Keep the existing README layout')

    def test_critical_boundaries_wait_without_review_dispatch_retry_or_revival(self):
        from run_store import revive, start_run, launch_guard
        for kind in ('purpose', 'scope', 'authority', 'budget', 'ownership', 'data-loss', 'evidence'):
            with self.subTest(kind=kind):
                project = 'lab-' + kind
                folder = self.home / 'projects' / project
                folder.mkdir()
                (folder / 'repo-path').write_text(str(self.repo))
                self.rid = self.store.request({**self.raw, 'project': project, 'submission_id': 'decision-' + kind})['run_id']
                before = len(self.calls())
                original = (self.repo / 'README.md').read_bytes()
                self.critical_plan('critical-' + kind)
                view = self.store.plan_view(self.rid)
                self.assertEqual(view['decisions'][0]['kind'], kind)
                self.assertEqual(view['reviews'], [])
                self.assertEqual(execute_run(self.store, self.rid, proceed=True), 78)
                with self.assertRaisesRegex(ValueError, 'request-or-plan-change-gate'):
                    start_run(self.store, self.rid)
                with self.assertRaisesRegex(ValueError, 'plan-decision-required'):
                    with launch_guard(self.store, self.rid, executing=True):
                        self.fail('Unresolved critical decision launched a writer')
                self.assertEqual(revive(self.store, self.fixture.source.root), [])
                again = self.operator_cli('request-plan', self.rid)
                self.assertEqual(again.returncode, 78, again.stderr)
                self.assertEqual(len(self.calls()), before + 1)
                self.assertEqual((self.repo / 'README.md').read_bytes(), original)
                self.assertFalse((self.store.path(self.rid) / 'accepted-plan.json').exists())

    def test_review_discovered_decision_is_sticky_even_when_the_model_reports_pass(self):
        self.approve()
        generate_request_plan(self.store, self.rid, str(self.fake))
        (self.home / 'pipeline-mode').write_text('review-critical')
        review = review_plan(self.store, self.rid, 1, str(self.fake))
        self.assertEqual(review['status'], 'WAITING', review)
        self.assertEqual(review['verdict'], 'PASS')
        self.assertEqual(self.store.plan_view(self.rid)['decisions'][0]['id'], 'review-d1')
        (self.home / 'pipeline-mode').write_text('')
        result = review_plan(self.store, self.rid, 1, str(self.fake))
        self.assertEqual(result['status'], 'ERROR')
        self.assertEqual(result['reason'], 'plan-decision-required')
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(execute_run(self.store, self.rid), 78)
        answered = self.operator_cli('plan-answer', self.rid, '1', 'review-d1', text='Keep the original goal.')
        self.assertEqual(answered.returncode, 0, answered.stderr)
        regenerated = self.operator_cli('request-plan', self.rid)
        self.assertEqual(regenerated.returncode, 0, regenerated.stderr)
        self.assertEqual(self.store.plan_view(self.rid)['revision'], 2)
        self.assertEqual(execute_run(self.store, self.rid), 0)

    def test_answer_requires_a_new_plan_and_clean_review_and_does_not_grant_spending_permission(self):
        self.critical_plan()
        original = self.store.read(self.rid)
        first = self.operator_cli('plan-answer', self.rid, '1', 'd1', text='Keep the existing authority and scope.')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertNotIn('Keep the existing authority', first.stdout)
        self.assertNotIn('Which existing boundary', first.stdout)
        second = self.operator_cli('plan-answer', self.rid, '1', 'd1', text='Keep the existing authority and scope.')
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(execute_run(self.store, self.rid), 78)
        self.assertEqual(len(self.calls()), 1)
        (self.home / 'pipeline-mode').write_text('routine')
        planned = self.operator_cli('request-plan', self.rid)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        view = self.store.plan_view(self.rid)
        self.assertEqual(view['revision'], 2)
        self.assertEqual(view['history'][0]['plan']['decisions'][0]['id'], 'd1')
        self.assertEqual(view['decisions'], [])
        self.assertFalse(view['held'])
        self.assertEqual(view['reviews'][-1]['status'], 'PASS')
        self.assertEqual(len(self.store.read(self.rid)['plan_answers']), 1)
        self.assertEqual(self.calls()[1]['decision_answers'][0]['answer'], 'Keep the existing authority and scope.')
        self.assertEqual(self.calls()[2]['decision_answers'][0]['decision']['id'], 'd1')
        after = self.store.read(self.rid)
        for field in ('request', 'request_permission', 'operator_budget_usd', 'budget_cap_usd'):
            self.assertEqual(after[field], original[field])
        self.assertEqual(execute_run(self.store, self.rid), 0, self.store.read(self.rid))
        self.assertEqual(len(self.calls()), 3)
        self.assertEqual(self.store.read(self.rid)['phase'], 'plan-delivered')

    def test_partial_answers_and_plan_rewrites_cannot_erase_unresolved_decisions(self):
        self.critical_plan('multiple-decisions')
        from runtime_sync import lock_gate
        with lock_gate(self.home.resolve(), directory=True), patch.dict(os.environ, {'CADRE_RUN_ID': ''}):
            with self.assertRaisesRegex(ValueError, 'runtime-sync-active'):
                self.store.answer_plan(self.rid, 1, 'd1', 'Keep boundary d1.')
        self.assertEqual(self.store.read(self.rid).get('plan_answers', []), [])
        first = self.operator_cli('plan-answer', self.rid, '1', 'd1', text='Keep boundary d1.')
        self.assertEqual(first.returncode, 0, first.stderr)
        view = self.store.plan_view(self.rid)
        self.assertFalse(view['answer_ready'])
        self.assertEqual(view['changes'], [])
        rewritten = json.loads(json.dumps(view['history'][0]['plan']))
        rewritten['decisions'] = []
        with self.assertRaisesRegex(ValueError, 'plan-decision-required'):
            self.store.save_plan(self.rid, rewritten, expected=1)
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.store.update(self.rid, plan_answers=[])
        for args, text in ((('1','d1'), 'Different boundary'), (('0','d2'), 'Keep d2'), (('1','unknown'), 'Keep'), (('1','d2'), ' ')):
            result = self.operator_cli('plan-answer', self.rid, *args, text=text)
            self.assertEqual(result.returncode, 78, result.stderr)
        self.assertEqual(self.operator_cli('request-plan', self.rid).returncode, 78)
        self.assertEqual(len(self.calls()), 1)
        last = self.operator_cli('plan-answer', self.rid, '1', 'd2', text='Keep boundary d2.')
        self.assertEqual(last.returncode, 0, last.stderr)
        self.assertEqual(self.store.plan_view(self.rid)['changes'][-1]['status'], 'stopped')
        with self.assertRaisesRegex(ValueError, 'plan-decision-required'):
            self.store.resume_plan(self.rid, self.store.plan_view(self.rid)['changes'][-1]['id'], 1)

    def test_answer_source_change_budget_exhaustion_cancel_and_worker_origin_keep_the_gate_closed(self):
        self.critical_plan()
        saved = self.store.read(self.rid)
        before = (self.repo / 'README.md').read_bytes()
        (self.repo / 'README.md').write_text('A changed product must not reuse the decision.')
        answer = self.operator_cli('plan-answer', self.rid, '1', 'd1', text='Keep scope')
        self.assertEqual(answer.returncode, 78)
        self.assertIn('plan-answer-source-changed', answer.stderr)
        self.assertEqual(self.store.read(self.rid).get('plan_answers', []), [])
        (self.repo / 'README.md').write_bytes(before)
        with self.assertRaisesRegex(ValueError, 'plan-answer-operator-command-required'):
            self.store.answer_plan(self.rid, 1, 'd1', 'Keep scope')
        self.assertEqual(self.operator_cli('plan-answer', self.rid, '1', 'd1', text='Keep scope').returncode, 0)
        self.store.update(self.rid, budget_spent_usd=saved['budget_cap_usd'])
        (self.home / 'pipeline-mode').write_text('routine')
        planned = self.operator_cli('request-plan', self.rid)
        self.assertEqual(planned.returncode, 78, planned.stderr)
        self.assertIn('operator-cap-exhausted', planned.stderr)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.store.read(self.rid)['request_permission'], saved['request_permission'])
        (self.store.path(self.rid) / 'cancel.flag').touch()
        self.assertEqual(execute_run(self.store, self.rid), 130)
        self.assertEqual(self.store.read(self.rid)['state'], 'cancelled')
        self.assertEqual(self.operator_cli('plan-answer', self.rid, '1', 'd1', text='Keep scope').returncode, 78)

    def test_answer_survives_real_process_exit_before_stop_and_concurrent_replay_is_single(self):
        self.critical_plan()
        env = self.operator_env()
        script = "import os,sys;sys.path.insert(0,sys.argv[1]);from run_store import RunStore;RunStore.change_plan=lambda *a,**k:os._exit(91);RunStore().answer_plan(sys.argv[2],1,'d1','Keep scope')"
        interrupted = subprocess.run([sys.executable, '-c', script, str(LIB), self.rid], env=env, capture_output=True, timeout=15)
        self.assertEqual(interrupted.returncode, 91)
        self.assertEqual(len(self.store.read(self.rid)['plan_answers']), 1)
        self.assertEqual(self.store.plan_view(self.rid)['changes'], [])
        self.assertEqual(execute_run(self.store, self.rid), 78)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.operator_cli('plan-answer', self.rid, '1', 'd1', text='Keep scope'), range(4)))
        self.assertTrue(any(r.returncode == 0 for r in results), [r.stderr for r in results])
        self.assertTrue(all(r.returncode in (0, 78) for r in results))
        self.assertEqual(self.operator_cli('plan-answer', self.rid, '1', 'd1', text='Keep scope').returncode, 0)
        view = self.store.plan_view(self.rid)
        self.assertEqual(len(view['answers']), 1)
        self.assertEqual(len(view['changes']), 1)
        self.assertEqual(view['changes'][0]['status'], 'stopped')
        self.assertEqual(len(self.calls()), 1)

    def test_malformed_uncertainty_and_conflicting_review_decisions_never_publish_acceptance(self):
        from dispatch_util import validate_plan
        from outcome import parse_review
        self.approve()
        generate_request_plan(self.store, self.rid, str(self.fake))
        plan = self.store.plan_view(self.rid)['history'][0]['plan']
        for field, value in (('decisions', None), ('decisions', [{'id':'d','kind':'unknown','question':'Q','reason':'R'}]),
                              ('assumptions', [{'id':'a','choice':'Keep','reason':' '}])):
            with self.subTest(field=field,value=value), self.assertRaises(ValueError):
                validate_plan({**plan,field:value})
        (self.home / 'pipeline-mode').write_text('review-malformed')
        self.assertEqual(review_plan(self.store, self.rid, 1, str(self.fake))['status'], 'ERROR')
        raw = {'revision':'a'*64,'verdict':'PASS','acceptance':[{'id':'g1','status':'PASS','evidence':'README inspected'}]}
        critical = {**raw,'decisions':[{'id':'d','kind':'budget','question':'Which cap?','reason':'Missing cap'}]}
        parsed = parse_review(json.dumps(raw)+'\n'+json.dumps(critical), {'digest':'a'*64}, ['g1'], planning=True)
        self.assertEqual(parsed['status'], 'ERROR', parsed)
        self.assertEqual(parsed['reason'], 'conflicting review verdicts')
        malformed = {**raw, 'decisions': 'guess'}
        fallback = '\nREVISION: '+ 'a'*64 +'\nDECISION: PASS\nCRITERION g1: PASS - README inspected'
        for text in (json.dumps(malformed)+fallback, json.dumps(malformed)+'\n'+json.dumps(raw)):
            parsed = parse_review(text, {'digest':'a'*64}, ['g1'], planning=True)
            self.assertEqual(parsed['status'], 'ERROR', parsed)
            self.assertEqual(parsed['reason'], 'invalid-plan-uncertainty')
        for value in ({**malformed, 'acceptance': []}, {**malformed, 'revision': 'b'*64},
                      {**critical, 'acceptance': []}, {**critical, 'revision': 'b'*64}):
            for tail in (fallback, '\n'+json.dumps(raw)):
                with self.subTest(value=value, tail=tail):
                    parsed = parse_review(json.dumps(value)+tail, {'digest':'a'*64}, ['g1'], planning=True)
                    self.assertEqual(parsed['status'], 'ERROR', parsed)
                    if value.get('decisions') == critical['decisions'] and value['revision'] == 'a'*64:
                        self.assertEqual(parsed['decisions'], critical['decisions'])
        (self.home / 'pipeline-mode').write_text('review-hidden-critical')
        review = review_plan(self.store, self.rid, 1, str(self.fake))
        self.assertEqual(review['status'], 'ERROR', review)
        self.assertEqual(self.store.plan_view(self.rid)['decisions'][0]['id'], 'review-d1')
        self.assertEqual(execute_run(self.store, self.rid), 78)
        self.assertEqual(len(self.calls()), 3)

    def test_a_new_critical_revision_requires_its_own_answer_before_review_and_delivery(self):
        self.critical_plan()
        self.assertEqual(self.operator_cli('plan-answer', self.rid, '1', 'd1', text='Keep scope').returncode, 0)
        still_unclear = self.operator_cli('request-plan', self.rid)
        self.assertEqual(still_unclear.returncode, 78, still_unclear.stderr)
        self.assertEqual(self.store.plan_view(self.rid)['revision'], 2)
        self.assertEqual(self.store.plan_view(self.rid)['answers'], [])
        self.assertEqual(len(self.calls()), 2)
        stale = self.operator_cli('plan-answer', self.rid, '1', 'd1', text='Keep scope')
        self.assertEqual(stale.returncode, 78)
        self.assertIn('stale-plan-answer', stale.stderr)
        current = self.operator_cli('plan-answer', self.rid, '2', 'd1', text='Keep existing authority; no external action.')
        self.assertEqual(current.returncode, 0, current.stderr)
        (self.home / 'pipeline-mode').write_text('routine')
        planned = self.operator_cli('request-plan', self.rid)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.assertEqual(self.store.plan_view(self.rid)['revision'], 3)
        self.assertEqual(len(self.store.read(self.rid)['plan_answers']), 2)
        self.assertEqual(execute_run(self.store, self.rid), 0)
        self.assertEqual(len(self.calls()), 4)

    def test_answered_decision_cannot_extend_deadline_or_reuse_changed_original_request(self):
        import time
        self.critical_plan()
        prompt = self.store.path(self.rid) / 'prompt.md'
        original = prompt.read_bytes()
        prompt.write_text('A different purpose must not reuse the old question.')
        answer = self.operator_cli('plan-answer', self.rid, '1', 'd1', text='Keep scope')
        self.assertEqual(answer.returncode, 78)
        self.assertEqual(self.store.read(self.rid).get('plan_answers', []), [])
        prompt.write_bytes(original)
        self.assertEqual(self.operator_cli('plan-answer', self.rid, '1', 'd1', text='Keep scope').returncode, 0)
        record = self.store.read(self.rid)
        self.duration_fill()
        with self.assertRaisesRegex(ValueError, 'request-time-limit'):
            generate_request_plan(self.store, self.rid, str(self.fake))
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.store.read(self.rid)['created'], record['created'])

    def test_pending_parent_decision_blocks_the_actual_child_executor(self):
        import qe_core as core
        self.critical_plan()
        child = self.store.accept('child', 'Keep parent authority', parent_run_id=self.rid)['run_id']
        with patch.dict(os.environ, {**self.store.environment(child), 'CADRE_TASK_ID': 'g1'}):
            with self.assertRaisesRegex(ValueError, 'plan-change-hold'):
                core.run_opencode(str(self.fake), str(self.repo), ['Do the child task'], self.home / 'child.log')
        self.assertEqual(len(self.calls()), 1)
        attempts = [json.loads(p.read_text()) for p in self.store.path(child).glob('tasks/*/attempts/*/attempt.json')]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]['outcome'], 'not_started')

    def test_new_target_decision_replan_delivers_without_creating_a_repository(self):
        target = self.home / 'new-critical-project'
        self.rid = self.store.request({**self.raw, 'submission_id': 'new-critical', 'project': 'new-critical-project',
                                       'target_kind': 'new', 'target_path': str(target)})['run_id']
        self.critical_plan('critical-purpose')
        answer = self.operator_cli('plan-answer', self.rid, '1', 'd1', text='Keep the original goal; deliver a plan only.')
        self.assertEqual(answer.returncode, 0, answer.stderr)
        (self.home / 'pipeline-mode').write_text('routine')
        planned = self.operator_cli('request-plan', self.rid)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.assertEqual(execute_run(self.store, self.rid), 0, self.store.read(self.rid))
        self.assertFalse(target.exists())
        self.assertEqual(len(self.calls()), 3)
        self.assertEqual(self.store.read(self.rid)['plan_reviews'][-1]['source_product']['head'], '')

    def initialized_repo(self, target: Path) -> str:
        env = {**os.environ, 'GIT_AUTHOR_NAME': 'Fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.com',
               'GIT_COMMITTER_NAME': 'Fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.com'}
        target.mkdir()
        subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=target, check=True, env=env)
        subprocess.run(['git', 'commit', '-q', '--allow-empty', '-m', 'init'], cwd=target, check=True, env=env)
        return subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=target, check=True, capture_output=True, text=True, env=env).stdout.strip()

    def new_target_request(self, name: str) -> Path:
        target = self.home / name
        self.rid = self.store.request({**self.raw, 'submission_id': name, 'project': name, 'input_mode': 'goal',
                                       'target_kind': 'new', 'target_path': str(target)})['run_id']
        self.approve()
        self.assertEqual(execute_run(self.store, self.rid), 78)
        record = self.store.read(self.rid)
        self.assertEqual((record['state'], record['reason']), ('retryable', 'new-project-init-permission-required'))
        self.assertFalse(target.exists())
        self.assertEqual(len(self.calls()), 2)
        return target

    def test_new_target_continues_after_initial_commit_with_registration_and_pinned_source(self):
        from run_store import request_pipeline
        from panel_data import task_node
        target = self.new_target_request('fresh-app')
        node = str(task_node(self.home, self.store.read(self.rid))['detail'])
        self.assertIn('cadre init --create ' + str(target.resolve()), node)
        head = self.initialized_repo(target)
        self.assertIsNone(request_pipeline(self.store, self.rid, str(self.fake)))
        record = self.store.read(self.rid)
        self.assertEqual((record['planning']['status'], record['source_sha'], record['source']['branch']), ('accepted', head, 'main'))
        self.assertEqual(Path((self.home / 'projects/fresh-app/repo-path').read_text().strip()).resolve(), target.resolve())
        self.assertEqual((self.home / 'projects/fresh-app/base-branch').read_text().strip(), 'main')
        self.assertEqual(Path(record['constraints']['qa-command']).read_text().strip(), record['qa']['command'])
        self.assertEqual(record['qa']['source'], 'derived')
        self.assertIsNone(request_pipeline(self.store, self.rid, str(self.fake)))
        self.assertEqual(self.store.read(self.rid)['source_sha'], head)
        self.assertEqual(sorted(p.name for p in (self.home / 'projects').iterdir() if p.name.startswith('fresh')), ['fresh-app'])
        self.assertEqual([p.name for p in target.iterdir()], ['.git'])
        self.assertEqual(len(self.calls()), 2)

    def test_new_target_with_stray_files_or_foreign_registration_does_not_continue(self):
        import project_registry
        from run_store import request_pipeline
        target = self.new_target_request('fresh-bad')
        head = self.initialized_repo(target)
        (target / 'stray.txt').write_text('outside the approved empty product')
        with self.assertRaisesRegex(ValueError, 'request-source-changed'):
            request_pipeline(self.store, self.rid, str(self.fake))
        (target / 'stray.txt').unlink()
        foreign = self.home / 'projects/fresh-bad'
        foreign.mkdir()
        (foreign / 'repo-path').write_text(str(self.repo))
        with self.assertRaisesRegex(ValueError, 'new-project-registration-mismatch'):
            request_pipeline(self.store, self.rid, str(self.fake))
        from panel_data import task_node
        self.store.update(self.rid, state='retryable', reason='new-project-registration-mismatch')
        hint = str(task_node(self.home, self.store.read(self.rid))['detail'])
        self.assertIn('fresh-bad is registered to another folder', hint.lower())
        self.assertIn(str(self.repo), hint)
        record = self.store.read(self.rid)
        self.assertEqual(record['source_sha'], '')
        self.assertNotEqual(record['planning']['status'], 'accepted')
        (foreign / 'repo-path').unlink()
        foreign.rmdir()
        self.assertEqual(project_registry.save(target, home=self.home), 'fresh-bad')
        self.assertIsNone(request_pipeline(self.store, self.rid, str(self.fake)))
        record = self.store.read(self.rid)
        self.assertEqual((record['planning']['status'], record['source_sha']), ('accepted', head))
        self.assertEqual(len(self.calls()), 2)


class RequestUITests(unittest.TestCase):
    def test_critical_decision_form_records_bound_answer_without_start_or_new_permission(self):
        from test_panel import PanelTests
        script = r'''
import asyncio,json,os,sys
from pathlib import Path
sys.path[:0]=[sys.argv[1],sys.argv[2]]
from test_end_to_end import E2EFixture
import panel_app,panel_data,ui_messages
from textual.widgets import TextArea,Static
async def run(language,size):
    fixture=E2EFixture();fixture.setUp();previous=dict(os.environ)
    try:
        os.environ.update(fixture.env,CADRE_NO_NET='1')
        os.environ.pop('CADRE_RUN_ID',None)
        panel_app.RUNNER=fixture.root
        panel_data.clean()
        store=fixture.store;rid=store.accept('e2e','Keep the existing goal and authority')['run_id']
        plan={'job_id':'pending','tasks':[{'id':'g1','definition':'Keep README','acceptance':'README preserved','files':['README.md'],
              'model':'auto','difficulty':1,'parallel_group':0,'estimate_min':1,'estimate_usd':0,'dependent':[]}],
              'assumptions':[{'id':'style','choice':'Keep layout','reason':'Reversible local choice'}],
              'decisions':[{'id':'d1','kind':'authority','question':'Which authority? api_key=synthetic_fixture_secret\n'+'Critical boundary\n'*120,
                            'reason':'Authority not established'}]}
        store.save_plan(rid,plan);store.update(rid,state='retryable',reason='plan-decision-required')
        original=store.read(rid);source=(fixture.repo/'README.md').read_bytes()
        app=panel_app.Cadre(home=fixture.home)
        def capture(name):
            folder=os.environ.get('CADRE_DECISION_EVIDENCE')
            if folder:
                path=Path(folder);path.mkdir(parents=True,exist_ok=True)
                (path/f'{language}-{size[0]}-{name}.svg').write_text(app.export_screenshot())
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete();await pilot.pause(.1)
            node=panel_data.operation_node(fixture.home,store.read(rid),plan)
            task=panel_data.task_node(fixture.home,store.read(rid))
            assert ui_messages.t('Critical decision pending') in str(task['detail'])
            assert 'start' not in [n['id'] for n in node['child']]
            assert 'style' not in str(node['detail']) and 'Keep layout' in str(node['detail'])
            assert 'synthetic_fixture_secret' not in str(node['detail'])+str([(n['entry_name'],n['summary']) for n in node['child']])
            app.git(['tasks',rid,'operation','plan-answer-d1']);await pilot.pause();capture('waiting')
            await pilot.press('enter');await pilot.pause();assert isinstance(app.screen,panel_app.PlanChange)
            assert 'synthetic_fixture_secret' not in str(app.screen.query_one('#plan-change-question',Static).render())
            capture('form')
            area=app.screen.query_one('#plan-change-text',TextArea)
            assert area.region.bottom < size[1] and area.region.height==7,area.region
            await pilot.press('escape');await pilot.pause();assert store.read(rid).get('plan_answers',[])==[]
            app.git(['tasks',rid,'operation','plan-answer-d1']);await pilot.press('enter','ctrl+s');await pilot.pause()
            assert str(app.screen.query_one('#plan-change-error',Static).render())
            app.screen.query_one('#plan-change-text',TextArea).load_text('Keep existing authority')
            await pilot.press('ctrl+s');await pilot.pause(.1)
            done=store.read(rid);assert len(done['plan_answers'])==1
            task=panel_data.task_node(fixture.home,done)
            assert ui_messages.t('Answer recorded, awaiting a new plan') in str(task['detail'])
            assert done['plan_answers'][0]['revision']==1 and done['plan_answers'][0]['answer']=='Keep existing authority'
            assert done['operator_budget_usd']==original['operator_budget_usd'] and not done.get('request_permission')
            assert done['plan_changes'][-1]['status']=='stopped'
            assert not (fixture.home/'opencode-calls.jsonl').exists()
            assert (fixture.repo/'README.md').read_bytes()==source
            app.git(['tasks',rid,'operation']);await pilot.pause();capture('answered')
    finally:
        fixture.doCleanups();os.environ.clear();os.environ.update(previous)
language = 'en'
for size in ((200,50),(100,30)):
    asyncio.run(run(language,size))
print('4 UI cases; 12 screenshots; decision record only; no provider call')
'''
        result = subprocess.run([str(PanelTests().ui_python()), '-c', script, str(LIB), str(LIB.parent / 'tests')],
                                capture_output=True, text=True, timeout=100)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_bound_permissions_plan_delivery_and_history_and_sizes(self):
        from test_panel import PanelTests
        import subprocess
        python = PanelTests().ui_python()
        script = r'''
import asyncio,json,os,sys,time,subprocess
from pathlib import Path
sys.path[:0]=[sys.argv[1],sys.argv[2]]
from test_end_to_end import E2EFixture
import panel_app,panel_data,ui_messages
from run_store import RunStore
async def run(language,size):
    fixture=E2EFixture();fixture.setUp()
    previous=dict(os.environ)
    try:
        os.environ.update(fixture.env,CADRE_NO_NET='1')
        panel_app.RUNNER=fixture.root
        panel_data.clean()
        raw=dict(submission_id='ui-pipeline',project='e2e',target_path=str(fixture.repo),target_kind='existing',input_mode='plan_only',
                 prompt=fixture.PROMPT,output='Reviewed plan',acceptance='Original source preserved',preference='economic',difficulty_override=None,
                 duration_minutes='60',budget_usd='2',backlog_path='',source_token='')
        saved=fixture.store.request(raw);rid=saved['run_id']
        app=panel_app.Cadre(home=fixture.home)
        def capture(name):
            folder=os.environ.get('CADRE_REQUEST_PIPELINE_EVIDENCE')
            if folder:
                out=Path(folder);out.mkdir(parents=True,exist_ok=True)
                (out/f'{language}-{size[0]}-{name}.svg').write_text(app.export_screenshot())
        async def wait(pilot):
            await app.workers.wait_for_complete();await pilot.pause(.1)
        async with app.run_test(size=size) as pilot:
            await wait(pilot)
            app.git(['tasks',rid,'operation']);app.choice=next(i for i,n in enumerate(app.elements) if n.get('id')=='request-authorize');app.draw()
            await pilot.press('enter');await pilot.pause()
            if not isinstance(app.screen,panel_app.Approval):
                result=subprocess.run(['python3',str(fixture.root/'lib/run_store.py'),'request-proposal',rid],env=dict(os.environ),capture_output=True,text=True)
                raise AssertionError((result.returncode,result.stderr,result.stdout,app.sample))
            capture('permission-first')
            await pilot.press('escape');assert not fixture.store.read(rid).get('request_permission')
            await pilot.press('enter');await pilot.pause(panel_app.Approval.DEAD_DURATION_SEC+0.05);await pilot.press('enter');await pilot.pause();assert isinstance(app.screen,panel_app.Approval)
            assert not fixture.store.read(rid).get('request_permission'),'Enter alone is not written approval'
            await pilot.press(*'yes','enter');await pilot.pause();assert isinstance(app.screen,panel_app.Approval)
            assert fixture.store.read(rid)['request_permission']['status']=='first-approved'
            assert not (fixture.home/'opencode-calls.jsonl').exists()
            capture('permission-second')
            assert app.screen.query_one('#approval-hint').region.bottom < size[1]
            await pilot.press('end');await pilot.pause()
            scroll=app.screen.query_one('#approval-content')
            assert scroll.scroll_y==scroll.max_scroll_y and scroll.max_scroll_y>0
            capture('permission-command')
            await pilot.press('escape');assert fixture.store.read(rid)['request_permission']['status']=='first-approved'
            await pilot.press('enter');await pilot.pause(panel_app.Approval.DEAD_DURATION_SEC+0.05);await pilot.press(*'yes','enter');await pilot.pause(panel_app.Approval.DEAD_DURATION_SEC+0.05);await pilot.press(*'yes','enter');await pilot.pause()
            assert fixture.store.read(rid)['request_permission']['status']=='approved'
            assert not (fixture.home/'opencode-calls.jsonl').exists()
            app.git(['tasks',rid,'operation']);app.choice=next(i for i,n in enumerate(app.elements) if n.get('id')=='start');app.draw()
            await pilot.press('enter');await pilot.pause(panel_app.Approval.DEAD_DURATION_SEC+0.05);await pilot.press('enter')
            deadline=time.monotonic()+20
            while fixture.store.read(rid)['state']!='completed' and time.monotonic()<deadline:
                await pilot.pause(.1)
            done=fixture.store.read(rid)
            assert done['state']=='completed',done
            assert done['plan_reviews'][-1]['status']=='PASS'
            app.reload(True);await wait(pilot)
            path=['tasks','history',rid]
            app.git(path+['operation']);await pilot.pause();capture('accepted-decision')
            app.git(path)
            assert any(n['id']=='plan-delivery' for n in app.elements),[n['id'] for n in app.elements]
            entry=next(n for n in app.elements if n['id']=='input')
            assert ui_messages.t('Verified plan output') in entry['summary'],entry
            app.choice=next(i for i,n in enumerate(app.elements) if n['id']=='input');app.draw()
            await pilot.pause();capture('delivered-plan')
            assert not (fixture.repo/'a.py').exists() and not (fixture.repo/'b.py').exists()
            before=(fixture.store.path(rid)/'run.json').read_bytes();calls=(fixture.home/'opencode-calls.jsonl').read_bytes()
            view=panel_data.operation_node(fixture.home,done,json.loads((fixture.store.path(rid)/'plan.json').read_text()))
            history=next(n for n in view['child'] if n['id']=='plan-history')
            panel_data.file_read(history['file'])
            assert (fixture.store.path(rid)/'run.json').read_bytes()==before
            assert (fixture.home/'opencode-calls.jsonl').read_bytes()==calls
    finally:
        fixture.doCleanups();os.environ.clear();os.environ.update(previous)
language = 'en'
for size in ((200,50),(100,30)):
    asyncio.run(run(language,size))
print('4 UI cases; 20 screenshots; actual CLI permissions and coordinator; no external provider')
'''
        result = subprocess.run([python, '-c', script, str(LIB), str(LIB.parent / 'tests')],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)


class RequestCLITests(unittest.TestCase):
    def verified_fixture(self, *, broken=False, budget='2'):
        from test_end_to_end import E2EFixture
        import subprocess
        fixture = E2EFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.env['E2E_VERIFIED_BACKLOG'] = '1'
        for name in ('a.py', 'b.py'):
            (fixture.repo / name).write_text('print("COMPLETE")\n' if not broken else 'print("BROKEN")\n')
        (fixture.repo / 'BACKLOG.md').write_text('### [x] DONE — Existing output\na.py and b.py contain COMPLETE.\n')
        raw = dict(submission_id='verified-backlog', project='e2e', target_path=str(fixture.repo), target_kind='existing',
                   input_mode='backlog', backlog_path='BACKLOG.md', source_token='', prompt='Verify the existing output; preserve all files.',
                   output='Reviewed existing product', acceptance='a.py and b.py contain COMPLETE and real tests pass',
                   preference='balanced', difficulty_override='easy', duration_minutes='10', budget_usd=budget)
        def cli(*args, data=None):
            return subprocess.run([sys.executable, str(fixture.root / 'lib/run_store.py'), *args],
                                  input=json.dumps(data) if data is not None else None, text=True, capture_output=True,
                                  env=fixture.env, timeout=100)
        submitted = cli('request', data=raw)
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        rid = json.loads(submitted.stdout)['run_id']
        proposal = cli('request-proposal', rid)
        if budget == '0':
            self.assertEqual(proposal.returncode, 78)
            self.assertIn('plan-review-no-capacity', proposal.stderr)
            return fixture, rid, cli
        self.assertEqual(proposal.returncode, 0, proposal.stderr)
        proposal = json.loads(proposal.stdout)
        first = cli('request-approve', rid, data=proposal)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = cli('request-approve', rid, json.loads(first.stdout)['confirmation'], data=proposal)
        self.assertEqual(second.returncode, 0, second.stderr)
        return fixture, rid, cli

    def test_legacy_bound_verified_backlog_completes_actual_qa_without_reimplementation(self):
        fixture, typed, cli = self.verified_fixture()
        planned = cli('request-plan', typed)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        initial_calls = fixture.calls()
        self.assertEqual([call['role'] for call in initial_calls], ['other', 'plan-review'])
        plan = fixture.store.plan_view(typed)['history'][-1]['plan']
        fixture.store.update(typed, state='cancelled')
        original = 'Verify the existing output against the selected backlog; preserve all source and tests.'
        with patch.dict(os.environ, fixture.env):
            rid = fixture.store.accept('e2e', original)['run_id']
        bound = cli('plan-backlog', rid, 'BACKLOG.md')
        self.assertEqual(bound.returncode, 0, bound.stderr)
        fixture.store.save_plan(rid, plan)
        before = {path.name: path.read_bytes() for path in fixture.repo.iterdir() if path.is_file()}
        result = cli('run', rid)
        record = fixture.store.read(rid)
        self.assertEqual(result.returncode, 0, (result.stderr, record))
        self.assertEqual(record['state'], 'completed')
        self.assertEqual(record['final_result']['qa']['discovery']['status'], 'PASS')
        self.assertEqual(record['final_result']['review']['status'], 'PASS')
        self.assertTrue(record['final_result']['review']['independent'])
        self.assertEqual(fixture.store.plan_view(rid)['history'][-1]['plan']['tasks'], [])
        calls = fixture.calls()
        self.assertEqual([call['role'] for call in calls[len(initial_calls):]], ['plan-review', 'review'])
        question = json.loads(calls[-1]['args'][-1].splitlines()[-1])
        self.assertEqual({item['id'] for item in question['criteria']}, {'original_request', 'source:DONE'})
        self.assertEqual(question['original_request'], original)
        self.assertEqual(question['source_backlog']['items'][0]['id'], 'DONE')
        for name, data in before.items():
            self.assertEqual((fixture.repo / name).read_bytes(), data)
            self.assertEqual((Path(record['worktree']) / name).read_bytes(), data)
        self.assertEqual(cli('run', rid).returncode, 0)
        self.assertEqual(fixture.calls(), calls)

    def test_verified_backlog_uses_real_qa_and_separate_final_review_without_a_writer(self):
        fixture, rid, cli = self.verified_fixture()
        before = {p.name: p.read_bytes() for p in fixture.repo.iterdir() if p.is_file()}
        head = fixture.git('rev-parse', 'HEAD')
        result = cli('run', rid)
        record = fixture.store.read(rid)
        self.assertEqual(result.returncode, 0, (result.stderr, record))
        self.assertEqual(record['state'], 'completed')
        final = record['final_result']
        self.assertEqual(final['status'], 'PASS')
        self.assertEqual(final['qa']['discovery']['status'], 'PASS')
        self.assertEqual(final['qa']['status'], 'PASS')
        self.assertEqual(final['review']['status'], 'PASS')
        self.assertTrue(final['review']['independent'])
        plan = fixture.store.plan_view(rid)['history'][-1]['plan']
        self.assertEqual(plan['tasks'], [])
        self.assertEqual(plan['source_coverage']['verified'], ['DONE'])
        calls = fixture.calls()
        self.assertEqual(len(calls), 3, calls)
        self.assertEqual([c['role'] for c in calls], ['other', 'plan-review', 'review'])
        question = json.loads(calls[-1]['args'][-1].splitlines()[-1])
        self.assertEqual({c['id'] for c in question['criteria']}, {'original_request', 'request_acceptance', 'request_output', 'source:DONE'})
        self.assertEqual(question['plan']['source_items'][0]['status'], 'verified')
        backlog = question['source_backlog']
        self.assertEqual(Path(backlog['full_path']).resolve(), (Path(record['worktree']) / 'BACKLOG.md').resolve())
        self.assertEqual(backlog['text'], (fixture.repo / 'BACKLOG.md').read_text())
        self.assertEqual(backlog['items'][0]['id'], 'DONE')
        source_read = json.loads((fixture.home / 'final-backlog-read.json').read_text())
        self.assertEqual(source_read['sha256'], hashlib.sha256((fixture.repo / 'BACKLOG.md').read_bytes()).hexdigest())
        attempts = [json.loads(p.read_text()) for p in fixture.store.path(rid).glob('tasks/*/attempts/*/attempt.json')]
        self.assertEqual({a['role'] for a in attempts}, {'plan', 'review', 'quality'})
        self.assertEqual(len({a['session_id'] for a in attempts}), 3)
        self.assertFalse(any(a.get('resumed') for a in attempts))
        worktree = Path(record['worktree'])
        for name, data in before.items():
            self.assertEqual((fixture.repo / name).read_bytes(), data)
            self.assertEqual((worktree / name).read_bytes(), data)
        self.assertEqual(fixture.git('rev-parse', 'HEAD'), head)
        self.assertEqual(subprocess.check_output(['git', '-C', str(worktree), 'rev-parse', 'HEAD'], text=True).strip(), head)
        self.assertEqual(cli('run', rid).returncode, 0)
        self.assertEqual(fixture.calls(), calls)

    def test_verified_backlog_red_qa_cannot_trigger_reimplementation_or_claim_completion(self):
        fixture, rid, cli = self.verified_fixture(broken=True)
        result = cli('run', rid)
        record = fixture.store.read(rid)
        self.assertNotEqual(result.returncode, 0, record)
        self.assertNotEqual(record['state'], 'completed')
        self.assertEqual(record['final_result']['qa']['status'], 'FAIL')
        self.assertEqual(len(record['final_result']['qa']['attempts']), 1)
        self.assertFalse(any(c['role'] in ('worker', 'repair', 'review') for c in fixture.calls()))
        self.assertEqual((Path(record['worktree']) / 'a.py').read_text(), 'print("BROKEN")\n')

    def test_verified_backlog_final_rejection_is_not_plan_acceptance(self):
        fixture, rid, cli = self.verified_fixture()
        fixture.env['E2E_VERDICT'] = 'REVISE'
        result = cli('run', rid)
        record = fixture.store.read(rid)
        self.assertNotEqual(result.returncode, 0, record)
        self.assertEqual(record['plan_reviews'][-1]['status'], 'PASS')
        self.assertEqual(record['final_result']['qa']['status'], 'PASS')
        self.assertEqual(record['final_result']['status'], 'FAIL')
        self.assertFalse(any(c['role'] in ('worker', 'repair') for c in fixture.calls()))

    def test_verified_backlog_reused_review_session_is_rejected(self):
        fixture, rid, cli = self.verified_fixture()
        fixture.env['E2E_REUSE_REVIEW_SESSION'] = '1'
        result = cli('run', rid)
        record = fixture.store.read(rid)
        self.assertNotEqual(result.returncode, 0, record)
        self.assertNotEqual(record['state'], 'completed')
        self.assertFalse(record['final_result']['review']['independent'])
        self.assertFalse(any(c['role'] in ('worker', 'repair') for c in fixture.calls()))

    def test_verified_backlog_final_review_has_one_owner_and_cancel_preserves_output(self):
        import time
        fixture, rid, cli = self.verified_fixture()
        fixture.env['E2E_SLOW_FINAL_REVIEW'] = '1'
        with subprocess.Popen([sys.executable, str(fixture.root / 'lib/run_store.py'), 'run', rid], env=fixture.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
            self.addCleanup(lambda: proc.kill() if proc.poll() is None else None)
            deadline = time.monotonic() + 30
            while not (fixture.home / 'final-review-started').exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue((fixture.home / 'final-review-started').exists(), fixture.store.read(rid))
            self.assertEqual(cli('run', rid).returncode, 75)
            self.assertEqual(len(fixture.calls()), 3)
            cancelled = cli('cancel', rid)
            self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
            proc.communicate(timeout=10)
        record = fixture.store.read(rid)
        self.assertEqual(record['state'], 'cancelled')
        self.assertEqual(cli('run', rid).returncode, 130)
        self.assertEqual(len(fixture.calls()), 3)
        self.assertEqual((Path(record['worktree']) / 'a.py').read_text(), 'print("COMPLETE")\n')

    def test_verified_backlog_zero_budget_and_changed_source_do_not_start_another_call(self):
        fixture, rid, cli = self.verified_fixture(budget='0')
        self.assertEqual(cli('run', rid).returncode, 78)
        self.assertFalse((fixture.home / 'opencode-calls.jsonl').exists())
        fixture, rid, cli = self.verified_fixture()
        planned = cli('request-plan', rid)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        before = fixture.calls()
        (fixture.repo / 'a.py').write_text('changed after plan review\n')
        self.assertEqual(cli('run', rid).returncode, 78)
        self.assertEqual(fixture.calls(), before)

    def test_real_cli_delivers_reviewed_worktree_and_keeps_git_actions_separately_gated(self):
        from test_end_to_end import E2EFixture
        import subprocess
        fixture = E2EFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        command = [sys.executable, str(fixture.root / 'lib/run_store.py')]
        raw = dict(submission_id='cli-code', project='e2e', target_path=str(fixture.repo), target_kind='existing', input_mode='goal',
                   prompt=fixture.PROMPT, output='Working a.py and b.py', acceptance='Both files compile and contain COMPLETE',
                   preference='economic', difficulty_override='easy', duration_minutes='60', budget_usd='2', backlog_path='', source_token='')
        def cli(*args, data=None, timeout=30):
            result = subprocess.run([*command, *args], input=json.dumps(data) if data is not None else None,
                                    text=True, capture_output=True, env=fixture.env, timeout=timeout)
            return result
        original_head = fixture.git('rev-parse', 'HEAD')
        submitted = cli('request', data=raw)
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        rid = json.loads(submitted.stdout)['run_id']
        proposal = cli('request-proposal', rid)
        self.assertEqual(proposal.returncode, 0, proposal.stderr)
        proposal = json.loads(proposal.stdout)
        first = cli('request-approve', rid, data=proposal)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertFalse((fixture.home / 'opencode-calls.jsonl').exists())
        second = cli('request-approve', rid, json.loads(first.stdout)['confirmation'], data=proposal)
        self.assertEqual(second.returncode, 0, second.stderr)
        planned = cli('request-plan', rid)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        calls_before = fixture.calls()
        repeated = cli('request-plan', rid)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(fixture.calls(), calls_before)
        executed = cli('run', rid, timeout=100)
        saved = fixture.store.read(rid)
        self.assertEqual(executed.returncode, 0, saved)
        self.assertEqual(saved['state'], 'completed')
        self.assertEqual(saved['final_result']['status'], 'PASS')
        self.assertEqual(saved['final_result']['delivery']['kind'], 'reviewed-worktree')
        from panel_data import delivery_candidate
        self.assertTrue(delivery_candidate(saved))
        self.assertEqual(saved['final_result']['delivery']['git_permission'], 'required')
        self.assertEqual(saved['plan_reviews'][0]['status'], 'PASS')
        worktree = Path(saved['worktree'])
        self.assertTrue(worktree.is_dir())
        self.assertEqual(subprocess.check_output(['git', '-C', str(worktree), 'rev-parse', 'HEAD']).decode().strip(), original_head)
        self.assertIn('COMPLETE', (worktree / 'a.py').read_text())
        self.assertIn('COMPLETE', (worktree / 'b.py').read_text())
        self.assertFalse((fixture.repo / 'a.py').exists())
        self.assertFalse((fixture.repo / 'b.py').exists())
        self.assertEqual(fixture.git('rev-parse', 'HEAD'), original_head)
        self.assertFalse(any(c['role'].startswith('worker') for c in fixture.calls()[:2]))


class RequestClockAndDecisionCarryTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from run_store import RunStore
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RunStore(Path(self.tmp.name))
        self.rid = self.store.accept('hour', 'request clock')['run_id']
        from run_store import atomic_json
        record = self.store.read(self.rid)
        record['request'] = {'duration_minutes': 10}
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        approval = patch('run_store.request_authorized', return_value=True)
        approval.start()
        self.addCleanup(approval.stop)

    def test_waiting_before_first_work_does_not_consume_duration(self):
        from run_store import request_remaining_sec, request_duration_filled
        record = self.store.read(self.rid)
        now_ts = record['created'] + 7200
        self.assertEqual(request_remaining_sec(record, now_ts), 600)
        self.assertFalse(request_duration_filled(record, now_ts))

    def test_work_sessions_accumulate_and_human_waits_are_counted_separately(self):
        from run_store import request_operation_hour, request_remaining_sec, request_hour_summary
        t0 = self.store.read(self.rid)['created'] + 3600
        observed_at = [t0]
        with patch('run_store.time.time', side_effect=lambda: observed_at[0]):
            with request_operation_hour(self.store, self.rid):
                with request_operation_hour(self.store, self.rid):
                    observed_at[0] = t0 + 100
                self.assertEqual(self.store.read(self.rid)['request_clock']['session'], t0, 'nested sessions do not open another clock')
                observed_at[0] = t0 + 200
            observed_at[0] = t0 + 1000
            with request_operation_hour(self.store, self.rid):
                observed_at[0] = t0 + 1300
                self.assertEqual(request_remaining_sec(self.store.read(self.rid)), 100)
            record = self.store.read(self.rid)
            self.assertEqual((record['request_clock']['start'], record['request_clock']['active_sec'], record['request_clock']['session']),
                             (t0, 500, None))
            observed_at[0] = t0 + 2000
            self.assertEqual(request_remaining_sec(record), 100, 'waiting outside an operation does not reduce active duration')
            self.assertEqual(request_hour_summary(record), {'operation_sec': 500, 'wait_sec': 1500})

    def test_child_process_counts_on_the_root_clock_and_live_foreign_session_is_kept(self):
        from run_store import request_operation_hour, atomic_json
        child = self.store.accept('hour', 'child')['run_id']
        self.store.update(child, project_ref_parent=self.rid)
        observed_at = [5000.0]
        with patch('run_store.time.time', side_effect=lambda: observed_at[0]):
            with request_operation_hour(self.store, child):
                observed_at[0] = 5060.0
        self.assertEqual(self.store.read(self.rid)['request_clock']['active_sec'], 60.0, 'a child’s separate process counts against the root request clock')
        self.assertNotIn('request_clock', self.store.read(child))
        record = self.store.read(self.rid)
        record['request_clock'] = {'start': 5000.0, 'active_sec': 60.0, 'session': 6000.0, 'session_pid': os.getppid()}
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        observed_at[0] = 6100.0
        with patch('run_store.time.time', side_effect=lambda: observed_at[0]):
            with request_operation_hour(self.store, self.rid):
                observed_at[0] = 6200.0
        self.assertEqual(self.store.read(self.rid)['request_clock'], record['request_clock'], 'another live process’s session is not closed')
        record['request_clock']['session_pid'] = 2 ** 22 + 12345
        record['updated'] = 6050.0
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        with patch('run_store.time.time', side_effect=lambda: observed_at[0]), patch('run_store._process_live', return_value=False):
            with request_operation_hour(self.store, self.rid):
                self.assertEqual(self.store.read(self.rid)['request_clock']['active_sec'], 110.0, 'a dead process’s session closes at its last activity')

    def test_unapproved_request_session_does_not_start_the_clock(self):
        from run_store import request_operation_hour
        with patch('run_store.request_authorized', return_value=False), request_operation_hour(self.store, self.rid):
            pass
        self.assertNotIn('request_clock', self.store.read(self.rid))

    def test_restart_does_not_renew_and_stale_session_closes_at_last_activity(self):
        from run_store import request_operation_hour, request_remaining_sec, request_duration_filled, atomic_json
        record = self.store.read(self.rid)
        record.update(request_clock={'start': 1000.0, 'active_sec': 500.0, 'session': 1000.0}, updated=1060.0)
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        observed_at = [5000.0]
        with patch('run_store.time.time', side_effect=lambda: observed_at[0]):
            with request_operation_hour(self.store, self.rid):
                hour = self.store.read(self.rid)['request_clock']
                self.assertEqual((hour['active_sec'], hour['session']), (560.0, 5000.0), 'an interrupted session closes at its last activity')
                observed_at[0] = 5030.0
        record = self.store.read(self.rid)
        self.assertEqual((record['request_clock']['start'], record['request_clock']['active_sec']), (1000.0, 590.0),
                         'restarting does not renew the duration')
        self.assertEqual(request_remaining_sec(record, 9_999_999.0), 10.0)
        record['request_clock']['active_sec'] = 600.0
        self.assertTrue(request_duration_filled(record, 9_999_999.0))

    def test_review_timeout_uses_the_shared_work_clock(self):
        from acceptance import review_duration
        from run_store import atomic_json
        from limits import deadline
        full = float(deadline('plan'))
        record = self.store.read(self.rid)
        with patch('time.time', return_value=record['created'] + 86_400):
            self.assertEqual(review_duration(record, 'plan'), min(full, 600), 'waiting before an operation does not shorten its duration')
        record['request_clock'] = {'start': 1000.0, 'active_sec': 450.0, 'session': 2000.0}
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        with patch('run_store.time.time', return_value=2100.0), patch('time.time', return_value=2100.0):
            self.assertEqual(review_duration(self.store.read(self.rid), 'plan'), min(full, 50), 'restarting counts earlier execution')
        with patch('run_store.time.time', return_value=2200.0), patch('time.time', return_value=2200.0):
            self.assertLessEqual(review_duration(self.store.read(self.rid), 'plan'), 0)

    def decision_record(self, kind, question, fresh_question=None, fresh_kind=None, id='d1'):
        decision = {'id': id, 'kind': kind, 'question': question, 'reason': 'reason'}
        fresh = {'id': id, 'kind': fresh_kind or kind, 'question': fresh_question or question + ' This gap is recorded in the report.', 'reason': 'reason'}
        plan = lambda k: {'tasks': [], 'assumptions': [], 'decisions': [k]}
        return {'plan_history': [{'revision': 1, 'digest': 'a', 'plan': plan(decision)}, {'revision': 2, 'digest': 'b', 'plan': plan(fresh)}],
                'plan_reviews': [], 'plan_answers': [{'revision': 1, 'plan_digest': 'a', 'decision': decision, 'answer': 'Report it as a finding.',
                                                      'source': {}}]}

    def test_answered_finding_does_not_return_as_a_new_decision(self):
        from run_store import plan_decisions, plan_answer_ready
        question = "test_betafeed_stops_paginating_on_last_page claims pagination stops on the last page; is there a real last-page test?"
        record = self.decision_record('evidence', question)
        self.assertEqual(plan_decisions(record), [], 'an answered finding does not trigger another stop')
        self.assertTrue(plan_answer_ready(record))

    def test_authority_budget_new_and_different_decisions_still_stop(self):
        from run_store import plan_decisions
        question = 'May this job spend more on a paid provider and should the cap increase?'
        for kind in ('authority', 'budget', 'ownership', 'data-loss'):
            self.assertEqual(len(plan_decisions(self.decision_record(kind, question))), 1, kind)
        different = self.decision_record('scope', question, fresh_question='Should the second repository module be included or only the adapter?')
        self.assertEqual(len(plan_decisions(different)), 1, 'a different new question stops execution')
        round = self.decision_record('scope', question, fresh_kind='authority')
        self.assertEqual(len(plan_decisions(round)), 1, 'a change in round does not carry it forward')


class PlanReviewClockTests(unittest.TestCase):
    """N-RUN04: the plan review's phase clock stops at a confirmed close; producing a revision does not consume the phase time;
    new task/coordinator counters are not reset; an exhausted limit is recorded with its real reason and is not reopened."""

    def setUp(self):
        import time as hour_module
        self.base = RequestPipelineTests()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.store, self.rid, self.home = self.base.store, self.base.rid, self.base.home
        self.offset = self.home / 'hour-offset'
        real = hour_module.time

        def fake_hour():
            try:
                return real() + float(self.offset.read_text() or 0)
            except (OSError, ValueError):
                return real()
        self.base.enter(patch('time.time', fake_hour))
        import qe_core as core
        from datetime import datetime as date
        original_budget = core.budget_json

        def fresh_budget():
            observation = original_budget()
            if isinstance(observation, dict):
                now_ts = date.fromtimestamp(hour_module.time())
                observation = {**observation, 'ts': now_ts.isoformat(), 'today': now_ts.strftime('%Y-%m-%d')}
            return observation
        self.base.enter(patch.object(core, 'budget_json', fresh_budget))
        self.duration = self.store.read(self.rid)['review_limits']['plan-review']['duration_sec']

    def hour_record(self):
        from budget_gate import BudgetLedger
        ledger = BudgetLedger(self.home / 'chain/request-budget.db')
        with ledger.transaction() as con:
            check = con.execute("select started,deadline,calls,tools,policy from review_control where run_id=? and phase='plan-review'",
                                  (self.rid,)).fetchone()
            pause = con.execute("select paused_at,paused_total from review_pause where run_id=? and phase='plan-review'",
                                    (self.rid,)).fetchone()
        return check, pause

    def prepare(self, rule):
        self.base.approve()
        (self.home / 'pipeline-mode').write_text('reject-once')
        (self.home / 'hour-advance.json').write_text(json.dumps(rule))

    def test_after_revise_a_long_revision_does_not_consume_the_review_time_and_the_job_clock_keeps_running(self):
        self.prepare({'plan': 600, 'revision': 400})
        self.assertEqual(execute_run(self.store, self.rid), 0, self.store.read(self.rid).get('reason'))
        saved = self.store.read(self.rid)
        self.assertEqual([(r['revision_number'], r['status'], r.get('verdict')) for r in saved['plan_reviews']],
                         [(1, 'FAIL', 'REVISE'), (2, 'PASS', 'PASS')])
        self.assertEqual([r.get('phase_hour') for r in saved['plan_reviews']], ['stopped', 'stopped'])
        self.assertEqual(len(self.base.calls()), 4, 'plan, REVISE, revision, second review')
        check, pause = self.hour_record()
        self.assertGreaterEqual(pause[1], 400, 'revision generation counts against the phase clock')
        self.assertLess(pause[1], 600, 'initial planning precedes the phase and does not count against its clock')
        self.assertAlmostEqual(check[1], check[0] + self.duration + pause[1], places=3)
        self.assertIsNotNone(pause[0], 'final review stops with verified closure too')
        self.assertGreaterEqual(saved['request_clock']['active_sec'], 1000, 'total active job duration keeps counting during planning and revision')

    def test_expiring_window_is_recorded_with_its_real_reason_and_a_restart_does_not_open_it(self):
        self.prepare({'review': self.duration + 1, 'single': True})
        self.assertEqual(execute_run(self.store, self.rid), 78)
        saved = self.store.read(self.rid)
        self.assertEqual((saved['state'], saved['reason']), ('retryable', 'plan-review-limit-time'))
        self.assertEqual(saved['planning']['status'], 'review-limit', 'a time limit is not a provider wait')
        self.assertEqual([(r['revision_number'], r['status']) for r in saved['plan_reviews']], [(1, 'FAIL')])
        self.assertTrue(str(saved['plan_reviews'][0].get('phase_hour', '')).startswith('could not stop'), saved['plan_reviews'][0].get('phase_hour'))
        call = len(self.base.calls())
        self.assertEqual(call, 2, 'planning and expired review; no unreviewable revision is produced')
        check, pause = self.hour_record()
        self.assertIsNone(pause, 'an expired window cannot be paused')
        self.assertEqual(execute_run(self.store, self.rid, proceed=True), 78)
        self.assertEqual(self.store.read(self.rid)['reason'], 'plan-review-limit-time')
        self.assertEqual(len(self.base.calls()), call, 'no blind retry')
        self.assertEqual(self.hour_record()[0][:3], check[:3], 'restarting does not reopen the window or reset counters')

    def test_unconfirmed_process_closing_does_not_stop_the_clock_and_the_limit_is_visible_with_its_real_reason(self):
        self.prepare({'plan': 600, 'revision': 400})
        with patch('run_store.review_closing_confirmed', return_value=False):
            self.assertEqual(execute_run(self.store, self.rid), 78)
        saved = self.store.read(self.rid)
        self.assertEqual(saved['plan_reviews'][0].get('phase_hour'), 'closing-unconfirmed')
        self.assertEqual((saved['state'], saved['reason']), ('retryable', 'plan-review-limit-time'))
        self.assertIsNone(self.hour_record()[1], 'unverified closure leaves no pause record')

    def test_restart_after_a_crashed_revision_keeps_the_phase_counters_and_start(self):
        self.prepare({'plan': 600, 'revision': 400, 'revision-collapse': 1})
        self.assertEqual(execute_run(self.store, self.rid), 78)
        first = self.hour_record()
        self.assertIsNotNone(first[1][0], 'the clock stopped after REVISE')
        self.assertEqual(execute_run(self.store, self.rid, proceed=True), 0, self.store.read(self.rid).get('reason'))
        saved = self.store.read(self.rid)
        self.assertEqual([(r['revision_number'], r['status']) for r in saved['plan_reviews']], [(1, 'FAIL'), (2, 'PASS')])
        last = self.hour_record()
        self.assertEqual((last[0][0], last[0][4]), (first[0][0], first[0][4]), 'a new coordinator or task does not change the phase start or policy')
        self.assertGreaterEqual(last[1][1], 800, 'failed and successful revision durations count against the phase clock')
        self.assertAlmostEqual(last[0][1], last[0][0] + self.duration + last[1][1], places=3)

    def test_closing_confirmation_passes_only_with_a_matching_observed_receipt(self):
        from types import SimpleNamespace
        from run_store import review_closing_confirmed
        base = Path(self.home / 'confirmation')
        id = {'pid': 4242, 'started': 'fixture:4242', 'pgid': 4242}
        for entry_name, receipt, data, expected in (
                ('full', {'run_id': 'r', 'task_id': 't', 'attempt_id': 'a', 'process_identity': id, 'stop_observed': id}, {}, True),
                ('no-receipt', None, {}, False),
                ('unobserved', {'run_id': 'r', 'task_id': 't', 'attempt_id': 'a', 'process_identity': id}, {}, False),
                ('id-mismatch', {'run_id': 'r', 'task_id': 't', 'attempt_id': 'a', 'process_identity': id,
                                    'stop_observed': {**id, 'started': 'fixture:9'}}, {}, False),
                ('foreign', {'run_id': 'other', 'task_id': 't', 'attempt_id': 'a', 'process_identity': id, 'stop_observed': id}, {}, False),
                ('unfinished', {'run_id': 'r', 'task_id': 't', 'attempt_id': 'a', 'process_identity': id, 'stop_observed': id}, {'outcome': 'running'}, False),
                ('faulty-exit', {'run_id': 'r', 'task_id': 't', 'attempt_id': 'a', 'process_identity': id, 'stop_observed': id}, {'exit_code': 1}, False)):
            with self.subTest(entry_name):
                folder = base / entry_name
                folder.mkdir(parents=True)
                if receipt is not None:
                    (folder / 'process.json').write_text(json.dumps(receipt))
                attempt = SimpleNamespace(directory=folder, data={'outcome': 'finished', 'exit_code': 0, 'attempt_id': 'a', **data})
                self.assertIs(review_closing_confirmed(attempt, 'r', 't'), expected)

    def test_phase_limit_rejection_of_an_approved_model_is_not_hidden_as_no_capacity(self):
        import qe_core as core
        self.base.approve()
        generate_request_plan(self.store, self.rid, str(self.base.fake))
        model = self.store.read(self.rid)['request_permission']['action']['models']['review'][0]
        for rationale, expected in ((f'{model}: budget unreadable review limit: plan-review: time', 'plan-review-limit-time'),
                                  (f'{model}: budget unreadable review limit: plan-review: requests', 'plan-review-limit-requests'),
                                  (f'{model}: closed path', 'plan-review-no-capacity')):
            with self.subTest(rationale), patch.object(core.ROUTER, 'chain', return_value=([], [rationale])):
                review = review_plan(self.store, self.rid, 1, str(self.base.fake))
                self.assertEqual((review['status'], review['reason']), ('ERROR', expected))


if __name__ == '__main__':
    unittest.main()
