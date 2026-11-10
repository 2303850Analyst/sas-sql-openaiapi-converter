from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, copy_metadata, collect_dynamic_libs

root = Path(SPECPATH).resolve().parent
from migration_v2.rule_identity import RULE_SOURCES
datas = [(str(root / "migration_v2" / "web"), "migration_v2/web"),
         (str(root / "migration_v2" / "example_contracts.json"), "migration_v2"),
         (str(root / "packaging_v2" / "THIRD_PARTY_NOTICES.txt"), "legal")]
for name in ("fastapi", "uvicorn", "sqlglot", "python-multipart", "pydantic", "opentelemetry-api", "psycopg", "psycopg-binary", "httpx"):
    datas += copy_metadata(name)
for source in RULE_SOURCES:
    datas.append((str(root / source), "rule_sources/" + str(Path(source).parent).replace("\\", "/")))
analysis = Analysis([str(root / "desktop_v2.py")], pathex=[str(root)], datas=datas, binaries=collect_dynamic_libs("psycopg_binary"),
                    hiddenimports=collect_submodules("sqlglot") + collect_submodules("psycopg_binary") + ["uvicorn.loops.asyncio", "uvicorn.protocols.http.h11_impl"],
                    excludes=["pytest", "_pytest", "IPython", "numpy", "pandas", "matplotlib"],
                    hookspath=[], hooksconfig={}, runtime_hooks=[], noarchive=False, optimize=0)
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, analysis.binaries, analysis.datas, [], name="SasSqlMigrationV2-local",
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          disable_windowed_traceback=True)
