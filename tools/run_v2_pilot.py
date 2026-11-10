"""Run the synthetic example through actual HTTP handlers and durable jobs.

No paid request is made unless --generate is explicitly supplied. Reruns reuse
the saved candidate. This is an integration exercise, not SAS equivalence.
"""
import argparse
import json
from pathlib import Path
import time

from fastapi.testclient import TestClient

from migration_v2.runtime import Runtime
from migration_v2.server import create_app
from fixtures.v2_pilot.build import archive


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--case', choices=['ctas', 'pipeline'], default='pipeline')
    proposal = parser.add_mutually_exclusive_group()
    proposal.add_argument('--generate', action='store_true')
    proposal.add_argument('--candidate-sql', type=Path, help='Replay saved SQL locally; no model call')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    app = create_app(args.data, 'synthetic-local-test', Runtime.load(args.config))
    with TestClient(app) as client:
        client.headers['X-Local-Session'] = 'synthetic-local-test'
        pointer = args.data / ('example-' + args.case + '.json')
        if pointer.exists():
            p = client.get('/api/projects/' + json.loads(pointer.read_text())['id']).json()
        else:
            r = client.post('/api/projects?filename=Quality_Pilot.egp', content=archive())
            r.raise_for_status(); p = r.json()
            r = client.post('/api/projects/' + p['id'] + '/example/' + args.case, json={'revision':p['revision']})
            r.raise_for_status(); p = r.json()
            r = client.post('/api/projects/' + p['id'] + '/commands', json={'revision':p['revision'],'action':'approve_plan','payload':{}})
            r.raise_for_status(); p = r.json()
            pointer.write_text(json.dumps({'id':p['id']}))
        def job(kind):
            nonlocal p
            r = client.post('/api/projects/' + p['id'] + '/jobs',json={'revision':p['revision'],'kind':kind})
            r.raise_for_status(); j = r.json()
            for _ in range(200):
                if j['state'] not in ('queued','running','cancelling'):
                    break
                time.sleep(1)
                j = client.get('/api/jobs/' + j['id']).json()
            print(kind, j['state'], j.get('error'), flush=True)
            p = client.get('/api/projects/' + p['id']).json()
            return j
        jobs = []
        if args.candidate_sql:
            if p['candidate']:
                raise ValueError('Candidate already present; use a fresh replay data directory')
            candidate = {'task_hash':p['task']['hash'],
                         'artifacts':[{'name':'001.sql','sql':args.candidate_sql.read_text(encoding='utf-8')}],
                         'source_claims':[r['id'] for r in p['context']['regions'] if r['kind'] != 'layout_or_comment']}
            r = client.post('/api/projects/' + p['id'] + '/commands', json={'revision':p['revision'],
                'action':'submit_candidate','payload':{'candidate_json':json.dumps(candidate)}})
            r.raise_for_status(); p = r.json()
        if args.generate:
            if p['candidate']:
                raise ValueError('Candidate already present; refusing duplicate paid generation')
            jobs.append(job('generate'))
        if args.execute and (p.get('evidence') or {}).get('static_passed'):
            jobs.append(job('execute'))
        status = client.get('/api/status').json()
        report = {'fixture':'synthetic_observatory_quality_pilot','case':args.case,
                  'project_id':p['id'],'revision':p['revision'],'state':p['state'],
                  'evidence':p['evidence'],'budget':status['budget'],
                  'jobs':[{'id':j['id'],'state':j['state'],'error':j['error'],
                           'provenance':(j.get('result') or {}).get('provenance')} for j in jobs],
                  'sas_runtime':'not_run','native_eg':'not_run'}
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        if p['candidate']:
            args.report.with_suffix('.sql').write_text('\n\n'.join(a['sql'] for a in p['candidate']['artifacts']),encoding='utf-8')
        print(json.dumps({'state':p['state'],'budget':status['budget'],'checks':(p['evidence'] or {}).get('checks'),'errors':(p['evidence'] or {}).get('errors')},ensure_ascii=False),flush=True)


if __name__ == '__main__':
    main()
