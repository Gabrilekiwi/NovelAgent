"""Verify the advertised ZIP, docs and every archived member against the pinned Git blobs."""
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile
import hashlib
import json
import re
from build_candidate import ROOT, build

base = ROOT / 'deliverables/release-candidate'
readme = (base / 'README.md').read_text('utf-8-sig')
zip_name = re.search(r'\[下载 [^\]]+\]\(([^()/]+\.zip)\)', readme)[1]
report_name = re.search(r'\[本包验收记录及校验值\]\(([^()/]+\.json)\)', readme)[1]
report = json.loads((base / report_name).read_bytes())
if report['zip'] != zip_name:
    raise ValueError('Download and verification record name different packages')
archive = (base / zip_name).read_bytes()
if hashlib.sha256(archive).hexdigest() != report['sha256']:
    raise ValueError('Published ZIP digest mismatch')
for document in (ROOT / 'README.md', base / 'README.md', ROOT / 'docs/windows-author.md'):
    for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)', document.read_text('utf-8-sig')):
        if not target.startswith(('https://', 'http://', '#')) and not (document.parent / target.split('#')[0]).exists():
            raise ValueError(f'{document.name}: missing target {target}')
# Different zlib versions may choose another deflate stream. Authenticate the downloaded
# bytes above; compare uncompressed members to the reproducible build on every host.
with ZipFile(BytesIO(archive)) as actual, ZipFile(BytesIO(build(report['source_commit'], report['guide_commit']))) as expected:
    if actual.namelist()!=expected.namelist() or any(actual.read(n)!=expected.read(n) for n in expected.namelist()):
        raise ValueError('ZIP contents differ from pinned source or guides')
    for doc in ('docs/windows-author.md','docs/product-validation.md'):
        if (ROOT / doc).read_text('utf-8-sig') != actual.read(doc).decode('utf-8-sig').replace('\r\n','\n'):
            raise ValueError(f'Rebuild candidate to include the advertised {doc}')
    print(f'Verified {zip_name}: {len(actual.namelist())} members; {report["sha256"]}')
