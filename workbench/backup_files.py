"""Stream a bounded, verified file inventory into a new backup staging root.

File identity and hash checks complement caller-held maintenance/authoritative
writer locks. An inventory is not a recoverability certificate. Excluded paths
must not be referenced by required recovery evidence; preflight decides that.
"""
import hashlib
import os
import re
from pathlib import Path
import stat
from core.engine.persistence import _HeldLockReader

from core.engine.safe_paths import (assert_safe_local_tree,FILE_ATTRIBUTE_REPARSE_POINT,
    SafePathResolver,RootBinding)
from core.story_project.reviewed_state_document import digest


class BackupFilesError(RuntimeError):
    code='backup_files_invalid'


def _ordinary(path):
    info=path.lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&FILE_ATTRIBUTE_REPARSE_POINT
            or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
            or stat.S_ISREG(info.st_mode) and info.st_nlink!=1):
        raise BackupFilesError('备份范围存在链接或不支持的文件类型。')
    return info


def _identity(info):
    return dict(device=info.st_dev,inode=info.st_ino,size=info.st_size,modified_ns=info.st_mtime_ns)


def _excluded(name):
    value=name.casefold()
    return (value=='.env' or value.startswith('.env.')
        or value in {'.secrets','credentials.json','secrets.json','credentials','id_rsa','id_ed25519'}
        or Path(value).suffix in {'.pem','.key','.p12','.pfx'})


def _stream_member(resolver,relative,*,output=None,locked_readers=None):
    resolved=resolver.resolve({'root_id':'external:backup_source','relative_path':relative})
    path=resolved.path;before=_ordinary(path)
    if not stat.S_ISREG(before.st_mode):raise BackupFilesError('备份成员不是普通文件。')
    checksum=hashlib.sha256();length=0
    reader=(locked_readers or {}).get(path)
    if reader is not None:
        if not isinstance(reader,_HeldLockReader):raise BackupFilesError('快照锁文件读取器无效。')
        content=reader.read(path)
        length=len(content);checksum.update(content)
        if output is not None:output.write(content)
    else:
        with path.open('rb') as source:
            opened=os.fstat(source.fileno())
            if _identity(opened)!=_identity(before) or opened.st_nlink!=1:
                raise BackupFilesError('文件在打开时发生变化。')
            while chunk:=source.read(1024*1024):
                length+=len(chunk);checksum.update(chunk)
                if length>before.st_size:raise BackupFilesError('读取期间文件超出原清单大小。')
                if output is not None:output.write(chunk)
    after=_ordinary(path)
    resolver.resolve(resolved.path_ref,expected_guard=resolved.guard)
    if _identity(before)!=_identity(after) or length!=before.st_size:
        raise BackupFilesError('文件在读取期间发生变化。')
    return dict(size=length,sha256=checksum.hexdigest(),source_identity=_identity(before))


def inventory_tree(root,*,max_files=100000,max_bytes=20*1024**3,locked_readers=None):
    if type(max_files) is not int or max_files<1 or type(max_bytes) is not int or max_bytes<1:
        raise BackupFilesError('备份清单上限必须为正整数。')
    root=assert_safe_local_tree(Path(root).absolute())
    if not stat.S_ISDIR(_ordinary(root).st_mode):raise BackupFilesError('必要的备份根不是目录。')
    resolver=SafePathResolver({'external:backup_source':RootBinding('external:backup_source','00000000-0000-4000-8000-000000000000',root)})
    original=_identity(_ordinary(root));files={};directories=[];excluded=[];total=0;names=set()
    def visit(directory):
        nonlocal total
        for path in sorted(directory.iterdir(),key=lambda item:item.name):
            relative=path.relative_to(root).as_posix()
            if relative.casefold() in names:raise BackupFilesError('备份路径大小写冲突。')
            names.add(relative.casefold())
            if len(names)>max_files:raise BackupFilesError('备份范围超过路径数量上限，未生成截断清单。')
            info=_ordinary(path)  # Inspect even exclusions before descending.
            if _excluded(path.name):
                excluded.append({'path':relative,'reason':'credential_or_environment_file'})
                continue
            if stat.S_ISDIR(info.st_mode):
                directories.append(relative);visit(path)
            else:
                if len(files)>=max_files or total+info.st_size>max_bytes:
                    raise BackupFilesError('备份范围超过本次支持的文件或容量上限，未生成截断清单。')
                files[relative]=_stream_member(resolver,relative,locked_readers=locked_readers)
                total+=files[relative]['size']
    visit(root)
    if _identity(_ordinary(root))!=original:raise BackupFilesError('备份根在扫描期间发生变化。')
    value=dict(schema_version='backup_tree_inventory_v1',source_root=str(root),
        root_identity=original,files=files,directories=directories,excluded=excluded,total_bytes=total)
    value['inventory_sha256']=digest(value)
    return value


def copy_inventory(root,destination,inventory,*,snapshot_permit,progress=None,locked_readers=None):
    """Only create a new tree, preserving any partial copy after a failure."""
    snapshot_permit.require()
    source=assert_safe_local_tree(Path(root).absolute())
    target=assert_safe_local_tree(Path(destination).absolute())
    if target.is_relative_to(source) or source.is_relative_to(target):
        raise BackupFilesError('备份暂存目录不能与来源重叠。')
    if os.path.lexists(target):raise BackupFilesError('备份暂存目标必须是新目录。')
    current=inventory_tree(source,locked_readers=locked_readers)
    if current!=inventory:raise BackupFilesError('来源已偏离预览清单，请重新预检。')
    target.mkdir()
    target_physical=target.stat().st_dev,target.stat().st_ino
    for directory in inventory['directories']:
        assert_safe_local_tree(target/directory).mkdir()
    resolver=SafePathResolver({'external:backup_source':RootBinding('external:backup_source','00000000-0000-4000-8000-000000000000',source)})
    writer=SafePathResolver({'external:backup_target':RootBinding('external:backup_target','00000000-0000-4000-8000-000000000000',target)})
    for relative,expected in inventory['files'].items():
        snapshot_permit.require()
        if (_ordinary(target).st_dev,_ordinary(target).st_ino)!=target_physical:
            raise BackupFilesError('备份暂存根被替换。')
        resolved=writer.resolve({'root_id':'external:backup_target','relative_path':relative})
        path=resolved.path
        with path.open('xb') as output:
            actual=_stream_member(resolver,relative,output=output,locked_readers=locked_readers)
            output.flush();os.fsync(output.fileno())
            opened=os.fstat(output.fileno())
        writer.resolve(resolved.path_ref,expected_guard=resolved.guard)
        if _identity(_ordinary(path))!=_identity(opened):raise BackupFilesError('备份目标文件被替换。')
        if actual!=expected:raise BackupFilesError('来源文件在复制前后发生变化。')
        if progress is not None:progress(relative)
    snapshot_permit.require()
    if (_ordinary(target).st_dev,_ordinary(target).st_ino)!=target_physical:
        raise BackupFilesError('备份暂存根被替换。')
    if inventory_tree(source,locked_readers=locked_readers)!=inventory:raise BackupFilesError('复制期间来源范围或内容发生变化。')
    copied=inventory_tree(target)
    def contents(value):return {k:{f:v[f] for f in ('sha256','size')} for k,v in value['files'].items()}
    if (contents(copied)!=contents(inventory) or copied['directories']!=inventory['directories']
            or copied['excluded']):
        raise BackupFilesError('备份副本未通过完整清单核验。')
    return {'schema_version':'backup_tree_copy_v1','inventory_sha256':inventory['inventory_sha256'],
        'files':contents(copied),'directories':copied['directories'],'excluded':inventory['excluded'],
        'total_bytes':copied['total_bytes']}


def archive_inventory(root,archive,prefix,inventory,*,snapshot_permit,locked_readers=None):
    """Stream source files directly to ZIP without materializing longer paths.

    The caller owns the new ZIP and all source locks. This shares the exact
    member/hash checks with copy_inventory, including held Windows lock files.
    """
    if not isinstance(prefix,str) or not re.fullmatch(r'roots/r[0-9]+',prefix):
        raise BackupFilesError('归档内部根标识无效。')
    snapshot_permit.require()
    source=assert_safe_local_tree(Path(root).absolute())
    if inventory_tree(source,locked_readers=locked_readers)!=inventory:
        raise BackupFilesError('来源已偏离预览清单，请重新预检。')
    resolver=SafePathResolver({'external:backup_source':RootBinding('external:backup_source','00000000-0000-4000-8000-000000000000',source)})
    archive.writestr(prefix+'/',b'')
    for directory in inventory['directories']:archive.writestr(prefix+'/'+directory+'/',b'')
    for relative,expected in inventory['files'].items():
        snapshot_permit.require()
        with archive.open(prefix+'/'+relative,'w',force_zip64=True) as output:
            actual=_stream_member(resolver,relative,output=output,locked_readers=locked_readers)
        if actual!=expected:raise BackupFilesError('来源文件在归档期间发生变化。')
    snapshot_permit.require()
    if inventory_tree(source,locked_readers=locked_readers)!=inventory:
        raise BackupFilesError('归档期间来源范围或内容发生变化。')
    return {'schema_version':'backup_tree_copy_v1','inventory_sha256':inventory['inventory_sha256'],
        'files':{name:{key:value[key] for key in ('sha256','size')} for name,value in inventory['files'].items()},
        'directories':inventory['directories'],'excluded':inventory['excluded'],'total_bytes':inventory['total_bytes']}
