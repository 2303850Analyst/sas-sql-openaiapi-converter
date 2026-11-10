"""Explicitly allowlisted V2 runtime/developer packages; never include private stores."""
from pathlib import Path
import hashlib
import json
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def sha(raw):return hashlib.sha256(raw).hexdigest()


def pack(target, files):
    contents={name:path.read_bytes() for name,path in files.items()}
    if any(part in name.split('/') for name in contents for part in ('originals','runtime','private_inputs','.env','node_modules')):
        raise ValueError('Private path in release')
    sums=''.join(sha(raw)+'  '+name+'\n' for name,raw in sorted(contents.items()))
    temp=target.with_suffix('.zip.pending')
    with zipfile.ZipFile(temp,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,raw in sorted(contents.items()):archive.writestr(name,raw)
        archive.writestr('SHA256SUMS',sums.encode())
    temp.replace(target)
    with zipfile.ZipFile(target) as archive:
        for line in archive.read('SHA256SUMS').decode().splitlines():
            value,name=line.split('  ',1)
            assert sha(archive.read(name))==value
    return {'files':len(contents),'sha256':sha(target.read_bytes()),'bytes':target.stat().st_size}


def main():
    release=ROOT/'releases'
    release.mkdir(exist_ok=True)
    for source in json.loads((ROOT/'vendor/repositories.json').read_text(encoding='utf-8'))['sources']:
        assert sha((ROOT/'vendor'/source['archive']).read_bytes())==source['sha256']
    runtime={
        'SasSqlMigrationV2-local.exe':release/'v2-windows-x64/SasSqlMigrationV2-local.exe',
        'README.md':ROOT/'README_V2.md',
        'THIRD_PARTY_NOTICES.txt':ROOT/'packaging_v2/THIRD_PARTY_NOTICES.txt',
        'smoke-report.json':release/'v2-windows-x64/smoke-report.json',
        'v2_isolated_exe.json':ROOT/'evidence/v2_isolated_exe.json',
        'IMPLEMENTATION_V2_20261031.md':ROOT/'IMPLEMENTATION_V2_20261031.md',
        'IMPLEMENTATION_V2_20261106.md':ROOT/'IMPLEMENTATION_V2_20261106.md',
        'Quality_Pilot.egp':ROOT/'fixtures/v2_pilot/Quality_Pilot.egp',
        'validation/pipeline.json':ROOT/'evidence/v2-pipeline-verified-20261106.json',
        'validation/pipeline.sql':ROOT/'evidence/v2-pipeline-verified-20261106.sql',
        'validation/mutants.json':ROOT/'evidence/v2-semantic-mutants-20261106.json',
        'validation/packaged-execution.json':ROOT/'evidence/v2-packaged-execution-20261106.json',
        'validation/clean-replay.json':ROOT/'evidence/v2-clean-replay-20261106.json',
        'validation/clean-mutants.json':ROOT/'evidence/v2-clean-mutants-20261106.json',
        'validation/interface.jpg':ROOT/'evidence/v2-ui-exe-20261106.jpg',
        'quality-pilot-draft.zip':release/'v2-windows-x64/quality-pilot-draft.zip',
        'NEW_ARCHITECTURE_DEVELOPMENT_REVIEW_20261031.md':ROOT/'NEW_ARCHITECTURE_DEVELOPMENT_REVIEW_20261031.md',
        'API_SERVICE_IMPLEMENTATION_PLAN.md':ROOT/'API_SERVICE_IMPLEMENTATION_PLAN.md',
        'API_ARCHITECTURE_UPDATE_20261028.md':ROOT/'API_ARCHITECTURE_UPDATE_20261028.md',
        'SOURCE_BINDINGS_UX_SPEC.md':ROOT/'SOURCE_BINDINGS_UX_SPEC.md',
    }
    developer={}
    for folder in ('migration_v2','tests_v2','packaging_v2','fixtures/v2_pilot'):
        for path in (ROOT/folder).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix in ('.py','.html','.js','.css','.spec','.lock','.txt','.json','.sql','.csv','.yaml','.md','.egp'):
                developer[path.relative_to(ROOT).as_posix()]=path
    for path in (ROOT/'vendor').rglob('*'):
        if path.is_file() and path.suffix in ('.zip','.whl','.json','.txt'):
            developer[path.relative_to(ROOT).as_posix()]=path
    for relative in ('desktop_v2.py','Build-V2.ps1','README_V2.md','IMPLEMENTATION_V2_20261031.md',
                     'IMPLEMENTATION_V2_20261106.md','tools/run_v2_pilot.py','tools/check_v2_mutants.py',
                     'NEW_ARCHITECTURE_DEVELOPMENT_REVIEW_20261031.md','AGENTS.md','ARCHITECTURE.md',
                     'API_SERVICE_IMPLEMENTATION_PLAN.md','API_ARCHITECTURE_UPDATE_20261028.md',
                     'SOURCE_BINDINGS_UX_SPEC.md','DELIVERY_PLAN.md','.gitignore',
                     'app/__init__.py','app/local_network.py','app/sas_blocks.py','app/sas_lexer.py','tools/inspect_sources.py'):
        developer[relative]=ROOT/relative
    for relative in ('evidence/v2_intake_20261031.json','evidence/v2_isolated_exe.json','evidence/v2-ui-synthetic.png'):
        developer[relative]=ROOT/relative
    for path in (ROOT/'evidence').glob('v2-*-20261106.*'):
        if path.suffix in ('.json','.sql','.jpg'):
            developer[path.relative_to(ROOT).as_posix()]=path
    results={
        'SasSqlMigrationV2-local-windows-x64.zip':pack(release/'SasSqlMigrationV2-local-windows-x64.zip',runtime),
        'SasSqlMigrationV2-offline-development.zip':pack(release/'SasSqlMigrationV2-offline-development.zip',developer),
    }
    (release/'v2-package-validation.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(results))


if __name__=='__main__':main()
