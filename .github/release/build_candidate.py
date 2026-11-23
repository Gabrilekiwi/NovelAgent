"""Build a repeatable Windows candidate from Git objects, never from a user's checkout."""
import argparse
import hashlib
import io
import json
import posixpath
import re
import subprocess
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[2]
TREES = ('api', 'core', 'modules', 'prompts', 'rules', 'schemas', 'scripts', 'workbench', 'workflows')
FILES = ('main.py', 'run.py', 'run_workbench.py', 'requirements.txt', 'requirements.lock',
         'requirements-dev.txt', 'requirements-dev.lock', '启动创作工作台.bat')
EXTRAS = ('.env.example', 'NovelAgentLogo.png', 'data/snapshot.example.json', 'data/notion_memory.example.json')
GUIDES = ('docs/windows-author.md', 'docs/product-validation.md')
SUFFIXES = {'.py', '.json', '.md', '.txt', '.yaml', '.yml', '.html', '.css', '.js', '.bat', '.ps1'}


def git(*args, data=None):
    return subprocess.run(['git', *args], cwd=ROOT, input=data, stdout=subprocess.PIPE, check=True).stdout


def blobs(ref, paths):
    records = []
    for entry in git('ls-tree', '-rz', ref, '--', *paths).split(b'\0'):
        if not entry:
            continue
        head, name = entry.split(b'\t', 1)
        mode, kind, oid = head.split()
        path = name.decode('utf-8')
        if kind != b'blob' or mode not in (b'100644', b'100755') or any(p.startswith('.') for p in path.split('/') if path != '.env.example'):
            raise ValueError(f'unexpected distribution member: {path}')
        records.append((path, oid))
    stream = io.BytesIO(git('cat-file', '--batch', data=b''.join(oid+b'\n' for _, oid in records)))
    result = {}
    for name, oid in records:
        header = stream.readline().split()
        if header[:2] != [oid, b'blob']:
            raise ValueError(name)
        result[name] = stream.read(int(header[2]))
        if stream.read(1) != b'\n':
            raise ValueError(name)
    return result


def metadata(payload):
    return {'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}


def build(source, guides, archive_format=2):
    if archive_format not in (1, 2):
        raise ValueError("Unsupported archive format")
    files = blobs(source, (*TREES, *FILES, *EXTRAS))
    for name in (*FILES, *EXTRAS):
        if name not in files:
            raise ValueError(f'missing {name}')
    for name in files:
        if name not in (*FILES, *EXTRAS) and Path(name).suffix not in SUFFIXES:
            raise ValueError(f'review additional file before distributing: {name}')
    if archive_format == 2:
        # Git stores normalized LF; cmd.exe expects native CRLF in a Windows distribution.
        for name in files:
            if name.endswith(".bat"):
                files[name] = files[name].replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    files.update(blobs(guides, GUIDES))
    files['README.md'] = ('# NovelAgent · Windows 本地自用候选版\n\n首次使用，请打开[作者使用指南](docs/windows-author.md)。\n\n'
                          '[自用上线标准](docs/product-validation.md) · [包内文件校验清单](candidate-manifest.json)\n').encode()
    names = set(files) | {'candidate-manifest.json'}
    for name in ('README.md', *GUIDES):
        for link in re.findall(r'\[[^\]]*\]\(([^)]+)\)', files[name].decode('utf-8-sig')):
            if posixpath.normpath(posixpath.join(posixpath.dirname(name), link)) not in names:
                raise ValueError(f'{name}: missing local target {link}')
    manifest = {'source_commit': source, 'guide_commit': guides, 'stable_release': False,
                'members': {n:metadata(b) for n,b in sorted(files.items())}}
    if archive_format == 2:
        manifest['archive_format'] = archive_format
    files['candidate-manifest.json'] = (json.dumps(manifest, ensure_ascii=False, indent=2)+'\n').encode()
    out = io.BytesIO()
    with ZipFile(out, 'w') as archive:
        for name, body in sorted(files.items()):
            info = ZipInfo(name, (1980,1,1,0,0,0)); info.create_system = 3; info.external_attr = 0o100644 << 16
            archive.writestr(info, body, compress_type=ZIP_DEFLATED, compresslevel=9)
    return out.getvalue()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, help='Commit with verified runtime')
    parser.add_argument('--guides', required=True, help='Commit containing the author documentation')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--check', action='store_true', help='Compare to the existing archive without overwriting it')
    args = parser.parse_args()
    source, guides = (git('rev-parse','--verify',f'{ref}^{{commit}}').decode().strip() for ref in (args.source,args.guides))
    archive = build(source, guides)
    if args.check:
        if args.output.read_bytes() != archive:
            raise SystemExit('Archive does not reproduce from the pinned sources')
    else:
        with args.output.open('xb') as output:
            output.write(archive)
    print(f'{hashlib.sha256(archive).hexdigest()}  {args.output.name}')
