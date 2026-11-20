"""Non-secret fingerprint of the on-disk implementation and dependency locks.

This records file hashes, never source contents, and does not claim that modules
already loaded by a long-running Python process match the current disk files.
"""
from pathlib import Path
import hashlib
import os
import re
import stat

from core.engine.safe_paths import assert_safe_local_tree
from core.story_project.reviewed_state_document import digest
from workbench.backup_files import _ordinary, _identity


SOURCE_ROOT = Path(__file__).resolve().parents[1]
_TREES = ('api', 'core', 'modules', 'prompts', 'rules', 'schemas', 'scripts', 'workbench', 'workflows')
_FILES = ('main.py', 'run.py', 'run_workbench.py', 'requirements.txt', 'requirements.lock',
          'requirements-dev.txt', 'requirements-dev.lock', '启动创作工作台.bat')
_SUFFIXES = {'.py', '.json', '.md', '.txt', '.yaml', '.yml', '.html', '.css', '.js', '.bat', '.ps1'}
SOURCE_SESSION_PROTOCOL = 'backup_source_session_v1'
SOURCE_SESSION_SCOPE = 'disk_source_stable_since_backup_service_construction'


class BackupSourceVersionError(ValueError):
    """The running service must restart before recording a new source version."""


def validate_source_session(evidence, software):
    """Validate recorded disk-session evidence, without claiming memory equality."""
    fields={'schema_version','instance_id','startup_fingerprint_sha256','scope',
            'loaded_modules_match_proven','evidence_sha256'}
    if (not isinstance(evidence,dict) or set(evidence)!=fields
            or evidence['schema_version']!=SOURCE_SESSION_PROTOCOL
            or evidence['scope']!=SOURCE_SESSION_SCOPE
            or not isinstance(evidence['instance_id'],str)
            or re.fullmatch('[0-9a-f]{32}',evidence['instance_id']) is None
            or evidence['loaded_modules_match_proven'] is not False
            or evidence['startup_fingerprint_sha256']!=software['fingerprint_sha256']
            or evidence['evidence_sha256']!=digest({k:v for k,v in evidence.items() if k!='evidence_sha256'})):
        raise BackupSourceVersionError('归档的服务启动版本证据无效。')
    return evidence


class SoftwareSourceSession:
    """Pin disk source at service construction; never infer loaded module bytes.

    The source root is captured internally, rather than accepted from an HTTP
    request. Old diagnostic archives may lack this newer evidence altogether.
    """
    def __init__(self,instance_id):
        self._root=assert_safe_local_tree(Path(SOURCE_ROOT).absolute())
        self._startup=fingerprint_software(self._root)
        self._evidence={'schema_version':SOURCE_SESSION_PROTOCOL,'instance_id':instance_id,
            'startup_fingerprint_sha256':self._startup['fingerprint_sha256'],
            'scope':SOURCE_SESSION_SCOPE,'loaded_modules_match_proven':False}
        self._evidence['evidence_sha256']=digest(self._evidence)
        validate_source_session(self._evidence,self._startup)

    def require_current(self):
        try:
            current=fingerprint_software(self._root)
            if current!=self._startup:raise BackupSourceVersionError('source changed')
        except (OSError,ValueError,RuntimeError) as exc:
            raise BackupSourceVersionError('软件来源已偏离预览或本次服务启动版本，请重启工作台后重新预览。') from exc
        return current,dict(self._evidence)


def fingerprint_software(root=None):
    root = assert_safe_local_tree(SOURCE_ROOT if root is None else Path(root).absolute())
    def lineage(path):
        values={}
        for parent in (path,*path.parents):
            info=_ordinary(parent)
            if not stat.S_ISDIR(info.st_mode):raise ValueError('软件来源父路径不是目录。')
            values[str(parent)]=(info.st_dev,info.st_ino)
        return values

    root_lineage=lineage(root)

    def members():
        names = []
        for name in _FILES:
            if os.path.lexists(root / name):
                names.append(name)
        for name in _TREES:
            tree = root / name
            if not os.path.lexists(tree):
                continue
            _ordinary(tree)
            for parent, directories, files in os.walk(tree, followlinks=False):
                directories[:] = sorted(d for d in directories if not d.startswith('.') and d != '__pycache__')
                for directory in directories:
                    _ordinary(Path(parent) / directory)
                names.extend((Path(parent) / filename).relative_to(root).as_posix()
                             for filename in sorted(files)
                             if not filename.startswith('.') and Path(filename).suffix.lower() in _SUFFIXES)
        if not names or len(names) > 20000:
            raise ValueError('软件来源文件缺失或超过支持的数量。')
        return sorted(names)

    names = members()
    files = {};identities={};parents={}
    total = 0
    for name in names:
        path=root/name;before=_identity(_ordinary(path));parent_before=lineage(path.parent)
        if not stat.S_ISREG(_ordinary(path).st_mode):raise ValueError('软件来源不是普通文件。')
        total += before['size']
        if total > 512 * 1024**2:
            raise ValueError('软件来源文件超过支持的容量。')
        checksum=hashlib.sha256();length=0
        with path.open('rb') as stream:
            if _identity(os.fstat(stream.fileno()))!=before:raise ValueError('软件文件被替换。')
            while chunk:=stream.read(1024*1024):
                length+=len(chunk)
                if length>before['size']:raise ValueError('软件文件读取期间增长。')
                checksum.update(chunk)
        if (_identity(_ordinary(path))!=before or length!=before['size']
                or lineage(path.parent)!=parent_before):raise ValueError('软件来源在读取期间发生变化。')
        files[name] = {'size':length,'sha256':checksum.hexdigest()}
        identities[name]=before;parents[path.parent]=parent_before
    if (members()!=names or lineage(root)!=root_lineage
            or any(_identity(_ordinary(root/name))!=record for name,record in identities.items())
            or any(lineage(parent)!=record for parent,record in parents.items())):
        raise ValueError('核验期间软件来源清单发生变化。')
    result = {'schema_version': 'backup_software_fingerprint_v1', 'scope': 'on_disk_source_and_dependency_locks',
              'loaded_modules_match_proven': False, 'files': files}
    result['fingerprint_sha256'] = digest(result)
    return result
