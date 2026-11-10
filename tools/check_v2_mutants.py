"""Offline semantic mutations of the actual synthetic model candidate. No API."""
import argparse
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from fixtures.v2_pilot.build import archive
from migration_v2.examples import load_example
from migration_v2.workflow import Workbench
from migration_v2.execution import execute
from migration_v2.runtime import Runtime


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,required=True,help='Store containing example-pipeline.json')
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    source=args.data
    wb=Workbench(source)
    baseline=wb.get(json.loads((source/'example-pipeline.json').read_text())['id'])['candidate']
    profile=Runtime.load(args.config).database
    variants=[('unchanged',None,None,True),
              ('drop_zero_boundary','value <= 0','value < 0',False),
              ('base_fixture_has_no_exact_start','observed_at >=','observed_at >',True),
              ('retain_duplicate_readings','WHERE rn = 1','WHERE rn >= 1',False),
              ('lose_calibration','COALESCE(c.bias, 0)','0',False),
              ('wrong_macro_argument','s.active = 1','s.active = 0',False),
              ('boundary_baseline',None,None,True),
              ('boundary_lost','observed_at >=','observed_at >',False)]
    results=[]
    for name,old,new,expected_pass in variants:
        c=json.loads(json.dumps(baseline));changed=0
        for a in c['artifacts']:
            if old and old in a['sql']:
                a['sql']=a['sql'].replace(old,new);changed+=1
        if old:assert changed>0,(name,'mutant did not change candidate')
        with TemporaryDirectory(prefix='sas-v2-mutant-') as temp:
            w=Workbench(Path(temp));p=w.import_egp('Quality_Pilot.egp',archive());p=load_example(w,p,'pipeline')
            run_profile=profile
            if name.startswith('boundary_'):
                plan=p['plan']
                for binding in plan['bindings']:binding['target']['parts'][0]='sky_boundary'
                for comparison in plan['comparisons']:comparison['reference']['parts'][0]='sky_boundary_expected'
                p=w.command(p['id'],p['revision'],'save_plan',plan)
                for artifact in c['artifacts']:artifact['sql']=artifact['sql'].replace('sky_raw.', 'sky_boundary.')
                run_profile=replace(profile,reference_schemas=('sky_boundary_expected',))
            p=w.command(p['id'],p['revision'],'approve_plan',{})
            c['task_hash']=p['task']['hash']
            p=w.command(p['id'],p['revision'],'submit_candidate',{'candidate_json':json.dumps(c)})
            assert p['evidence']['static_passed'],p['evidence']['errors']
            report=execute(w,p,run_profile)
            passed=report['checks']['comparison']=='passed'
            assert passed==expected_pass,(name,[(c['output'],c['passed']) for c in report['comparison']])
            results.append({'name':name,'static_passed':True,'comparison_passed':passed,
                            'mismatched_outputs':[c['output'] for c in report['comparison'] if not c['passed']],
                            'release_allowed':report['release_allowed']})
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps({'synthetic':True,'cases':results,'api_calls':0},indent=2),encoding='utf-8')
    print(json.dumps(results))


if __name__=='__main__':main()
