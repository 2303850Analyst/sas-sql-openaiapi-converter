"""Synthetic fixture only. No native Enterprise Guide/SAS execution claim."""
from pathlib import Path
from xml.sax.saxutils import escape
import io
import zipfile

ROOT = Path(__file__).resolve().parent


def archive():
    source = (ROOT / "scenario.sas.txt").read_bytes()
    small = b'proc sql; create table active_ids as select station_id from source.stations where active=1; quit;'
    programs = [("CodeTask-pipeline", "Synthetic quality pipeline", source),
                ("CodeTask-ctas", "Synthetic active stations", small)]
    xml = '<ProjectCollection EGVersion="synthetic"><Elements>'
    for ident, label, code in programs:
        xml += f'<Element Type="SAS.EG.ProjectElements.CodeTask"><Element><ID>{ident}</ID><Label>{escape(label)}</Label></Element><CodeTask><Embedded>True</Embedded></CodeTask></Element>'
    xml += '</Elements></ProjectCollection>'
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as z:
        for path, raw in [("project.xml", xml.encode("utf-16")), *[(i+'/code.sas', c) for i, _, c in programs]]:
            info = zipfile.ZipInfo(path, (2026, 11, 6, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, raw)
    return stream.getvalue()


if __name__ == '__main__':
    target = ROOT / 'Quality_Pilot.egp'
    target.write_bytes(archive())
    print(target)
