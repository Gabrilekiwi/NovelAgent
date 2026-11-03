"""Preview-bound archive packaging; diagnostic completion is not recoverability.

The current archive purpose is deliberately diagnostic until the complete
history, root and writer-coverage certification gate is implemented. This
module implements the packaging required by both outcomes without offering an
unchecked switch that can label an archive recoverable.
"""
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import platform
import re
import sqlite3
import stat
import uuid
import zipfile

from core.engine.persistence import atomic_create_json
from core.engine.safe_paths import assert_safe_local_tree
from core.story_project.reviewed_state_document import digest,read_project_bytes
from workbench.backup_database import capture_database
from workbench.backup_files import archive_inventory,_ordinary,_identity
from workbench.backup_source import locked_backup_source
from workbench.backup_version import (fingerprint_software,SoftwareSourceSession,
    BackupSourceVersionError,validate_source_session)


class BackupArchiveError(RuntimeError):
    code='backup_archive_invalid'


def _json(value):
    return (json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode('utf-8')


def _member(name):
    if not isinstance(name,str):raise BackupArchiveError('归档成员路径无效。')
    path=PurePosixPath(name)
    if (not name or '\x00' in name or '\\' in name or ':' in name or path.is_absolute()
            or any(part in {'','.','..'} for part in name.rstrip('/').split('/'))):
        raise BackupArchiveError('归档成员路径不安全。')
    for part in path.parts:
        if (part.endswith((' ','.')) or any(ord(char)<32 or char in '<>"|?*' for char in part)
                or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]|CONIN\$|CONOUT\$)',part.split('.')[0],re.I)):
            raise BackupArchiveError('归档成员路径不兼容 Windows。')
    return path


def _lineage(path):
    assert_safe_local_tree(path)
    result={}
    for parent in (path,*path.parents):
        info=_ordinary(parent)
        if not stat.S_ISDIR(info.st_mode):raise BackupArchiveError('归档容器不是目录。')
        result[str(parent)]=(info.st_dev,info.st_ino)
    return result


def _file_hash(path):
    lineage=_lineage(path.parent)
    before=_identity(_ordinary(path))
    with path.open('rb') as stream:
        if _identity(os.fstat(stream.fileno()))!=before:raise BackupArchiveError('归档文件被替换。')
        checksum=hashlib.file_digest(stream,'sha256').hexdigest()
    if _identity(_ordinary(path))!=before or _lineage(path.parent)!=lineage:raise BackupArchiveError('归档文件在读取期间变化。')
    return {'sha256':checksum,'size':before['size']}


def _validate_config(config):
    # Only startup settings needed to explain the source; never copy environment,
    # SDK options, headers, endpoint URLs or arbitrary caller-supplied metadata.
    required={'workbench_version','job_execution_contract','worker_enabled','auto_import_active'}
    if (not isinstance(config,dict) or set(config)!=required
            or any(type(config[k]) is not bool for k in ('worker_enabled','auto_import_active'))
            or any(not isinstance(config[k],str) or not re.fullmatch('[A-Za-z0-9._-]{1,100}',config[k])
                   for k in ('workbench_version','job_execution_contract'))):
        raise BackupArchiveError('备份运行配置必须是明确的非秘密字段。')
    return {**config,'python_version':platform.python_version(),'sqlite_version':sqlite3.sqlite_version,
            'restore_start_worker':False,'restore_auto_import':False,'restore_provider_credentials':'reconfigure'}


class BackupArchiveService:
    def __init__(self,*,projects,maintenance,task_runtime_root,archive_root,runtime_config,machine_store=None):
        self.projects=projects;self.maintenance=maintenance
        self.task_runtime_root=Path(task_runtime_root).absolute()
        self.archive_root=assert_safe_local_tree(Path(archive_root).absolute())
        self.runtime_config=runtime_config
        self.machine_store=machine_store
        self._source_session=SoftwareSourceSession(maintenance.lease.instance_id)

    def _software(self):
        try:return self._source_session.require_current()
        except BackupSourceVersionError as exc:raise BackupArchiveError(str(exc)) from exc

    @property
    def runtime_config(self):
        return dict(self._runtime_config)

    @runtime_config.setter
    def runtime_config(self,value):
        _validate_config(value)
        self._runtime_config=dict(value)

    def _source(self,project_id,operation_id,snapshot_permit=None):
        return locked_backup_source(self.projects,project_id,maintenance=self.maintenance,
            operation_id=operation_id,task_runtime_root=self.task_runtime_root,machine_store=self.machine_store,
            snapshot_permit=snapshot_permit)

    def _preview(self,scope,snapshot,permit,operation_id):
        software,session=self._software()
        value={'schema_version':'target_archive_preview_v1','project_id':scope['project_id'],
            'book_id':scope['book_id'],'operation_id':operation_id,'scope':scope,
            'database':snapshot.fingerprint(self.projects.database,permit),'runtime_config':_validate_config(self.runtime_config),
            'software':software,'source_session':session,
            'archive_purpose':'diagnostic','recoverable':False,
            'certification_pending':['complete_history_and_root_coverage','external_writer_coverage',
                                     'restore_binding_protocol','certified_software_compatibility'],
            'archive_root':str(self.archive_root)}
        value['preview_sha256']=digest(value)
        return value

    def preview(self,project_id,*,operation_id,snapshot_permit=None):
        self._software()
        with self._source(project_id,operation_id,snapshot_permit) as (snapshot,scope,_,permit):
            return self._preview(scope,snapshot,permit,operation_id)

    def capture(self,project_id,*,operation_id,expected_preview_sha256,fault=None,snapshot_permit=None):
        if not isinstance(expected_preview_sha256,str) or re.fullmatch('[0-9a-f]{64}',expected_preview_sha256) is None:
            raise BackupArchiveError('必须提供已核对的预览摘要。')
        self._software()
        guards={}
        def require_package():
            if any(_lineage(Path(path))!=guard for path,guard in guards.items()):
                raise BackupArchiveError('归档暂存目录被替换。')
        def inject(event,path):
            if fault is not None:fault(event,path)
            require_package()
        package=None
        with self._source(project_id,operation_id,snapshot_permit) as (snapshot,scope,readers,permit):
            preview=self._preview(scope,snapshot,permit,operation_id)
            if preview['preview_sha256']!=expected_preview_sha256:
                raise BackupArchiveError('来源、数据库或配置已偏离预览，请重新核对。')
            if scope['total_source_bytes']>20*1024**3:raise BackupArchiveError('本次归档超过支持的总容量上限。')
            package=self.archive_root/('backup_'+uuid.uuid4().hex)
            for tree in scope['inventories'].values():
                source=Path(tree['source_root'])
                if package.is_relative_to(source) or source.is_relative_to(package):
                    raise BackupArchiveError('归档目录不能与来源文件范围重叠。')
            assert_safe_local_tree(self.archive_root).mkdir(parents=True,exist_ok=True)
            package.mkdir()
            guards[str(package)]=_lineage(package)
            manifest={'schema_version':'target_archive_manifest_v1','archive_id':package.name,
                'created_at':datetime.now(timezone.utc).isoformat(),'preview':preview,
                'archive_purpose':'diagnostic','recoverable':False,'roots':{},'members':{},'directories':[]}
            db=capture_database(self.projects.database,package/'workbench.sqlite',snapshot=snapshot,snapshot_permit=permit)
            manifest['database']=db
            manifest['members']['workbench.sqlite']={k:db[k] for k in ('sha256','size')}
            inject('after_database',package)
            manifest['directories'].append('roots/')
            archive=package/'archive.zip'
            require_package()
            with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED,allowZip64=True) as zipped:
                zipped.writestr('roots/',b'')
                zipped.write(package/'workbench.sqlite','workbench.sqlite')
                for index,(name,tree) in enumerate(sorted(scope['inventories'].items())):
                    relative=f'roots/r{index}'
                    copied=archive_inventory(tree['source_root'],zipped,relative,tree,snapshot_permit=permit,locked_readers=readers)
                    manifest['roots'][name]={'archive_path':relative,'source_root':tree['source_root'],
                        'inventory_sha256':copied['inventory_sha256'],'excluded':copied['excluded']}
                    manifest['directories'].append(relative+'/')
                    manifest['directories'].extend(relative+'/'+p+'/' for p in copied['directories'])
                    manifest['members'].update({relative+'/'+p:record for p,record in copied['files'].items()})
                    inject('after_root',package)
                manifest['manifest_sha256']=digest(manifest)
                zipped.writestr('manifest.json',_json(manifest))
            with archive.open('r+b') as stream:stream.flush();os.fsync(stream.fileno())
            require_package()
            atomic_create_json(package/'manifest.json',manifest)
            inject('before_source_release',package)
            if fingerprint_software()!=preview['software']:
                raise BackupArchiveError('归档期间软件来源发生变化，请重启后重新预览。')
        # The source context verifies all roots before releasing their locks.
        # The closed ZIP is independently verified after that consistency point.
        inject('after_zip',package)
        archive_record=_verify_payload(package,manifest)
        inject('before_completion',package)
        # Recheck after any delayed verification/fault hook, then publish last.
        if _verify_payload(package,manifest)!=archive_record:raise BackupArchiveError('完成前归档发生变化。')
        if json.loads(read_project_bytes(package,'manifest.json'))!=manifest:
            raise BackupArchiveError('完成前外部归档清单发生变化。')
        self._software()
        receipt={'schema_version':'target_archive_completion_v1','archive_id':package.name,
            'archive':archive_record,'manifest_sha256':manifest['manifest_sha256'],
            'preview_sha256':expected_preview_sha256,'archive_purpose':'diagnostic','recoverable':False}
        receipt['receipt_sha256']=digest(receipt)
        require_package()
        atomic_create_json(package/'completion.json',receipt)
        return {'archive_id':package.name,'path':str(package),'receipt':receipt}


def _verify_scope_members(manifest):
    """Bind every ZIP member to the inventories accepted in the preview."""
    preview=manifest['preview'];scope=preview['scope'];software=preview['software']
    if (scope.get('scope_sha256')!=digest({k:v for k,v in scope.items() if k!='scope_sha256'})
            or scope.get('project_id')!=preview.get('project_id') or scope.get('book_id')!=preview.get('book_id')
            or software.get('schema_version')!='backup_software_fingerprint_v1'
            or software.get('fingerprint_sha256')!=digest({k:v for k,v in software.items() if k!='fingerprint_sha256'})):
        raise BackupArchiveError('归档来源范围或版本摘要不一致。')
    if 'source_session' in preview:
        validate_source_session(preview['source_session'],software)
    config=preview['runtime_config']
    if _validate_config({k:config[k] for k in ('workbench_version','job_execution_contract','worker_enabled','auto_import_active')}) != {
            **config,'python_version':platform.python_version(),'sqlite_version':sqlite3.sqlite_version}:
        raise BackupArchiveError('归档运行配置含未允许字段或恢复策略不同。')
    db=manifest['database']
    if (db.get('schema_version')!='workbench_database_snapshot_v1' or db.get('scope')!='whole_database'
            or db.get('other_projects_default_disabled') is not True
            or db.get('logical_sha256')!=preview['database']['logical_sha256']
            or db.get('user_version')!=preview['database']['user_version']):
        raise BackupArchiveError('归档数据库说明与预览不同。')
    expected_members={'workbench.sqlite':{k:db[k] for k in ('size','sha256')}}
    expected_directories=['roots/'];expected_roots={}
    inventories=scope['inventories']
    if not inventories or 'story_project' not in inventories:
        raise BackupArchiveError('归档缺少目标作品根。')
    if scope.get('restored_provenance') is not None:
        from workbench.backup_restore_provenance import validate_restore_provenance
        validate_restore_provenance(scope['restored_provenance'],
            root=Path(inventories['story_project']['source_root']), book_id=scope['book_id'],
            ownership=scope['machine_ownership']['record'])
    for index,(name,tree) in enumerate(sorted(inventories.items())):
        if tree.get('inventory_sha256')!=digest({k:v for k,v in tree.items() if k!='inventory_sha256'}):
            raise BackupArchiveError('来源文件清单摘要不一致。')
        relative=f'roots/r{index}'
        expected_roots[name]={'archive_path':relative,'source_root':tree['source_root'],
            'inventory_sha256':tree['inventory_sha256'],'excluded':tree['excluded']}
        expected_directories.append(relative+'/')
        expected_directories.extend(relative+'/'+p+'/' for p in tree['directories'])
        expected_members.update({relative+'/'+p:{k:record[k] for k in ('size','sha256')}
            for p,record in tree['files'].items()})
    if (manifest['roots']!=expected_roots or manifest['members']!=expected_members
            or manifest['directories']!=expected_directories):
        raise BackupArchiveError('归档成员与已确认的来源范围不同。')


def _verify_payload(package,manifest):
    if (manifest.get('schema_version')!='target_archive_manifest_v1' or manifest.get('recoverable') is not False
            or manifest.get('archive_purpose')!='diagnostic'
            or digest({k:v for k,v in manifest.items() if k!='manifest_sha256'})!=manifest.get('manifest_sha256')):
        raise BackupArchiveError('归档清单或用途无效。')
    preview=manifest.get('preview')
    if (not isinstance(preview,dict) or preview.get('recoverable') is not False
            or preview.get('archive_purpose')!='diagnostic'
            or digest({k:v for k,v in preview.items() if k!='preview_sha256'})!=preview.get('preview_sha256')):
        raise BackupArchiveError('归档预览摘要或用途不一致。')
    _verify_scope_members(manifest)
    if (not isinstance(manifest.get('members'),dict) or not isinstance(manifest.get('directories'),list)
            or len(manifest['members'])+len(manifest['directories'])>200000):
        raise BackupArchiveError('归档成员清单无效或超过上限。')
    for name,record in manifest['members'].items():
        _member(name)
        if (name.endswith('/') or not isinstance(record,dict) or set(record)!={'size','sha256'}
                or type(record['size']) is not int or record['size']<0
                or not isinstance(record['sha256'],str) or re.fullmatch('[0-9a-f]{64}',record['sha256']) is None):
            raise BackupArchiveError('归档成员哈希或大小无效。')
    if sum(item['size'] for item in manifest['members'].values())>20*1024**3:
        raise BackupArchiveError('归档展开大小超过上限。')
    for name in manifest['directories']:
        _member(name)
        if not name.endswith('/'):raise BackupArchiveError('归档目录名称无效。')
    if len(manifest['directories'])!=len(set(manifest['directories'])):
        raise BackupArchiveError('归档目录清单重复。')
    expected=set(manifest['members'])|set(manifest['directories'])|{'manifest.json'}
    if len({p.rstrip('/').casefold() for p in expected})!=len(expected):
        raise BackupArchiveError('归档文件和目录名称冲突。')
    for name in expected:
        for parent in PurePosixPath(name).parents:
            if str(parent)!='.' and str(parent)+'/' not in manifest['directories']:
                raise BackupArchiveError('归档成员缺少父目录。')
    archive=package/'archive.zip';before=_file_hash(archive)
    with zipfile.ZipFile(archive,'r') as zipped:
        names=zipped.namelist()
        if len(names)!=len(set(names)) or set(names)!=expected or len({p.casefold() for p in names})!=len(names):
            raise BackupArchiveError('归档成员重复、缺失或超出清单。')
        for entry in zipped.infolist():
            _member(entry.filename)
            if entry.flag_bits&1 or stat.S_ISLNK(entry.external_attr>>16):raise BackupArchiveError('归档含加密或链接成员。')
            if entry.filename=='manifest.json':
                if entry.file_size>32*1024**2 or zipped.read(entry)!=_json(manifest):raise BackupArchiveError('归档内部清单不一致。')
            elif entry.is_dir():
                if entry.file_size:raise BackupArchiveError('归档目录含意外内容。')
            else:
                record=manifest['members'][entry.filename]
                if entry.file_size!=record['size']:raise BackupArchiveError('归档成员大小不同。')
                with zipped.open(entry) as stream:checksum=hashlib.file_digest(stream,'sha256').hexdigest()
                if checksum!=record['sha256']:raise BackupArchiveError('归档成员哈希不同。')
    if _file_hash(archive)!=before:raise BackupArchiveError('核验期间归档变化。')
    return before


def verify_archive(package):
    """Read-only package verification; never extract or enable a project."""
    package=assert_safe_local_tree(Path(package).absolute())
    try:
        lineage=_lineage(package)
        if (package/'manifest.json').stat().st_size>32*1024**2 or (package/'completion.json').stat().st_size>1024**2:
            raise BackupArchiveError('归档元数据超过大小上限。')
        metadata={name:_file_hash(package/name) for name in ('manifest.json','completion.json')}
        manifest=json.loads(read_project_bytes(package,'manifest.json'))
        receipt=json.loads(read_project_bytes(package,'completion.json'))
        if (receipt.get('schema_version')!='target_archive_completion_v1' or receipt.get('recoverable') is not False
                or receipt.get('archive_purpose')!='diagnostic' or receipt.get('archive_id')!=package.name
                or manifest.get('archive_id')!=package.name
                or receipt.get('receipt_sha256')!=digest({k:v for k,v in receipt.items() if k!='receipt_sha256'})
                or receipt.get('manifest_sha256')!=manifest.get('manifest_sha256')
                or receipt.get('preview_sha256')!=manifest['preview']['preview_sha256']
                or receipt.get('archive')!=_verify_payload(package,manifest)):
            raise BackupArchiveError('归档完成凭据与实际内容不一致。')
        if _lineage(package)!=lineage:raise BackupArchiveError('核验期间归档目录被替换。')
        if any(_file_hash(package/name)!=record for name,record in metadata.items()):
            raise BackupArchiveError('核验期间归档元数据变化。')
        return {'archive_id':package.name,'archive_verified':True,'archive_purpose':'diagnostic',
                'recoverable':False,'receipt_sha256':receipt['receipt_sha256'],'manifest':manifest}
    except (OSError,ValueError,KeyError,TypeError,AttributeError,zipfile.BadZipFile) as exc:
        raise BackupArchiveError('归档缺少有效完成凭据或内容无法通过核验。') from exc
