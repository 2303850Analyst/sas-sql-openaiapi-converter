"""Read explicitly selected local EGPs; persist only hashes and aggregate evidence."""
from collections import Counter
import argparse
import json
import io
from pathlib import Path
import tempfile
import zipfile

from .context import task_context, analyze_source
from .sources import SourceVault, ingest


def audit(paths):
    result = {"kind": "real_egp_source_and_context_audit_not_equivalence", "files": []}
    for path in paths:
        with tempfile.TemporaryDirectory(prefix="sas-v2-audit-") as temporary:
            vault = SourceVault(Path(temporary))
            original = path.read_bytes()
            snapshot = ingest(path.name, original, vault)
            preserved, regions, issues = 0, 0, Counter()
            for source in snapshot["sources"]:
                if source["status"] == "available":
                    text = vault.text(source)
                    analysis = analyze_source(source, vault)
                    rebuilt = ''.join(text[r['start']:r['end']] for r in analysis['regions'])
                    assert rebuilt == text
                    assert len(vault.read(source['sha256'])) == source['bytes']
                    with zipfile.ZipFile(io.BytesIO(original)) as independent_archive:
                        assert vault.read(source['sha256']) == independent_archive.read(source['member'])
                    preserved += 1
                    regions += len(analysis['regions'])
                    issues.update(i['code'] for i in analysis['issues'])
            scenarios = []
            for scenario in snapshot['scenarios']:
                c = task_context(snapshot, scenario, vault)
                scenarios.append({'scenario_hash': __import__('hashlib').sha256(scenario['id'].encode()).hexdigest(),
                                  'program_positions':len(scenario['source_ids']),
                                  'detected_inputs':len(c['inputs']), 'detected_writes':len(c['writes']),
                                  'definitions':len(c['definitions']),
                                  'issues':dict(Counter(i['code'] for i in c['issues'])),
                                  'inventory_complete':False})
            result['files'].append({'archive_sha256':snapshot['archive_sha256'],
                                    'programs':len(snapshot['sources']), 'preserved_originals':preserved,
                                    'missing':len(snapshot['sources'])-preserved,
                                    'lossless_regions':regions, 'issues':dict(issues), 'scenarios':scenarios,
                                    'execution':'not_run', 'equivalence':'unavailable'})
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--egp',type=Path,action='append',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.resolve() in {p.resolve() for p in args.egp}:
        raise ValueError('Output cannot overwrite an input')
    result=audit(args.egp)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'files':len(result['files']), 'originals':sum(f['preserved_originals'] for f in result['files']),
                      'missing':sum(f['missing'] for f in result['files']), 'equivalence':'not_tested'}))


if __name__=='__main__': main()
