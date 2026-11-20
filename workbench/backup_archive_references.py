"""Follow declared DB/run references inside a verified archive, never on disk."""
import json
from pathlib import Path, PurePosixPath

from core.engine.root_registry import validate_root_registry
from core.story_project.reviewed_state_document import digest
from workbench.backup_archive import BackupArchiveError
from workbench.backup_archive_database import _database
from workbench.backup_archive_view import verified_archive_view
from workbench.backup_references import inspect_metadata_references, ReferenceRoots
from workbench.backup_job_state import empty_cancelled_stdout
from workbench.backup_auxiliary import Evidence
from workbench.backup_lazy_calls import LazyModelCallReferences


class _ReferenceGraph:
    def __init__(self,view,book_root,connection):
        self.view=view;self.root=book_root
        self.inventories=view.manifest['preview']['scope']['inventories']
        self.bases={key:Path(tree['source_root']) for key,tree in self.inventories.items()}
        self.root_index=ReferenceRoots(self.bases)
        self.directories={key:set(tree['directories']) for key,tree in self.inventories.items()}
        self.references=[];self.blockers=[];self.pending=set();self.bindings={}
        self.nodes=0;self.bytes=0
        self.connection=connection;self.diagnostic_outputs=[]
        self.lazy_calls=LazyModelCallReferences(Evidence(book_root,self.inventories['story_project'],
            _read_bytes=lambda root,name:view.read_root('story_project',name)))
        self.historical=None
        proof=view.manifest['preview']['scope'].get('restored_provenance')
        if proof is not None:
            from workbench.backup_restore_provenance import validate_restored_database
            from workbench.restore_job_artifacts import RestoredJobArtifacts
            try:
                validate_restored_database(connection,proof)
                self.historical=RestoredJobArtifacts(book_root,proof['book_id'],proof)
                self.historical.validate_trees(self.inventories)
            except (ValueError,KeyError,TypeError) as exc:
                raise BackupArchiveError('归档历史引用的任务来源或产物无效。') from exc
        for key,base in self.bases.items():
            if not base.is_absolute() or '..' in base.parts:
                raise BackupArchiveError('归档引用根坐标无效。')
            if any(base.is_relative_to(other) or other.is_relative_to(base)
                   for name,other in self.bases.items() if name!=key):
                raise BackupArchiveError('归档引用根重叠，无法确定唯一来源。')

    def block(self,code,reference):
        item={'code':code,'reference':reference}
        if item not in self.blockers:self.blockers.append(item)

    def require(self,value,label,*,relative_to_book=False,kind='file',expected_sha256=None,follow_json=False,evidence=None):
        if not isinstance(value,str) or not value:
            self.block('invalid_file_reference',label);return
        path=Path(value)
        if '..' in path.parts:
            self.block('invalid_file_reference',label);return
        if relative_to_book and not path.is_absolute():path=self.root/path
        if not path.is_absolute():
            self.block('unresolved_file_reference',label);return
        if self.historical is not None:
            try:path=self.historical.path(path,evidence=evidence,inventories=self.inventories)
            except ValueError:self.block('historical_reference_unverified',label);return
        resolved=self.root_index.find(path)
        if resolved is not None:
            key,base=resolved
            name=path.relative_to(base).as_posix();tree=self.inventories[key]
            if kind=='directory':
                if name=='.' or name in self.directories[key]:
                    self.references.append({'source':label,'root':key,'path':name,'kind':'directory'})
                else:self.block('required_directory_missing_or_excluded',label)
            elif name in tree['files']:
                expected=tree['files'][name]['sha256']
                if expected_sha256 is not None and expected_sha256!=expected:
                    self.block('referenced_artifact_digest_mismatch',label);return
                self.references.append({'source':label,'root':key,'path':name,'sha256':expected})
                if follow_json:self.pending.add((key,name))
            else:self.block('required_file_missing_or_excluded',label)
            return
        self.block('unknown_external_file_reference',label)

    def metadata(self,value,label,evidence=None):
        self.nodes+=1
        if self.nodes>10000:
            raise BackupArchiveError('归档引用元数据数量超过支持范围。')
        def require(value,label,**options):return self.require(value,label,evidence=evidence,**options)
        inspect_metadata_references(value,source=label,book_root=self.root,require_path=require,
                                    block=self.block,root_bindings=self.bindings,lazy_calls=self.lazy_calls)

    def decode(self,raw,label):
        if not isinstance(raw,(str,bytes)):
            self.block('run_metadata_unreadable',label);return None
        size=len(raw.encode('utf-8') if isinstance(raw,str) else raw)
        self.bytes+=size
        if size>32*1024**2 or self.bytes>256*1024**2:
            raise BackupArchiveError('归档引用元数据大小超过支持范围。')
        try:
            value=json.loads(raw)
            if not isinstance(value,(dict,list)):raise ValueError('unstructured metadata')
            return value
        except ValueError:
            self.block('run_metadata_unreadable',label);return None

    def registry_bindings(self):
        for name in sorted(self.inventories['story_project']['files']):
            if PurePosixPath(name).name!='root_registry.json':continue
            try:
                registry=validate_root_registry(json.loads(self.view.read_root('story_project',name)))
                for key,binding in registry['roots'].items():
                    path=Path(binding['path']);label=name+':'+key
                    self.require(str(path),label,kind='directory')
                    if (not path.is_absolute() or '..' in path.parts or not path.is_relative_to(self.root)
                            or path!=self.root and path.relative_to(self.root).as_posix() not in self.directories['story_project']):
                        self.block('unknown_or_missing_registry_root',label);continue
                    self.bindings.setdefault((key,binding['root_uuid']),set()).add(str(path))
                    self.bindings.setdefault((key,None),set()).add(str(path))
            except (ValueError,RuntimeError):self.block('invalid_root_registry',name)

    def follow(self):
        visited=set()
        while self.pending:
            key,name=min(self.pending);self.pending.remove((key,name))
            if (key,name) in visited:continue
            visited.add((key,name));label=key+'/'+name
            raw=self.view.read_root(key,name)
            diagnostic=empty_cancelled_stdout(self.connection,key,name,raw)
            if diagnostic is not None:
                self.diagnostic_outputs.append(diagnostic)
                continue
            value=self.decode(raw,label)
            if value is not None:self.metadata(value,label,('file',key,name))
        return [{'root':key,'path':name} for key,name in sorted(visited)]


def inspect_archived_references(package,*,project_id,book_id,temporary_root=None):
    with verified_archive_view(package) as view, _database(view,temporary_root=temporary_root) as connection:
        preview=view.manifest['preview'];root=Path(preview['scope']['inventories']['story_project']['source_root'])
        row=connection.execute('SELECT book_id,root FROM projects WHERE id=?',(project_id,)).fetchone()
        identity=json.loads(view.read_root('story_project','.novelagent/project.json'))
        if (preview['project_id']!=project_id or preview['book_id']!=book_id or row is None
                or row['book_id']!=book_id or Path(row['root'])!=root or identity.get('book_id')!=book_id):
            raise BackupArchiveError('归档引用目标与数据库/作品身份不同。')
        graph=_ReferenceGraph(view,root,connection)
        tables={r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        jobs=[dict(row) for row in connection.execute('SELECT * FROM jobs WHERE project_id=? ORDER BY id LIMIT 10001',(project_id,))]
        if len(jobs)>10000:raise BackupArchiveError('归档目标任务数量超过支持范围。')
        ids={row['id'] for row in jobs}
        for key,base in graph.bases.items():
            if key=='story_project':continue
            if not key.startswith('jobs/') or key[5:] not in ids or base.name!=key[5:]:
                raise BackupArchiveError('归档包含未归属目标任务的外部根。')
        graph.registry_bindings()
        for table,field,relative in (('drafts','source_path',False),
            ('chapter_candidate_drafts','source_artifact_ref',True),('amendments','content_path',True),
            ('outline_batches','approval_manifest_path',False)):
            if table not in tables:continue
            for item in connection.execute(f'SELECT id,{field} FROM {table} WHERE project_id=? ORDER BY id',(project_id,)):
                if not item[field]:continue
                value=item[field]
                if table=='chapter_candidate_drafts':value=str(root/'.novelagent/runtime'/value)
                graph.require(value,table+'/'+item['id']+'/'+field,relative_to_book=relative)
        for job in jobs:
            for field in ('config_json','result_json'):
                if job[field]:
                    label=job['id']+'/'+field;value=graph.decode(job[field],label)
                    if value is not None:graph.metadata(value,label,('job',job['id'],field,job[field]))
            if 'job_events' in tables:
                for event in connection.execute('SELECT * FROM job_events WHERE job_id=? ORDER BY sequence',(job['id'],)):
                    label=job['id']+'/events/'+str(event['sequence']);value=graph.decode(event['payload_json'],label)
                    if value is not None:graph.metadata(value,label,('event',dict(event)))
        for key,tree in graph.inventories.items():
            for name in tree['files']:
                path=PurePosixPath(name)
                if (key=='story_project' and path.suffix=='.json' and str(path.parent)=='.novelagent/runtime/runs'
                        or key.startswith('jobs/') and path.name=='stdout.json'):
                    graph.pending.add((key,name))
        traversed=graph.follow()
        result={'schema_version':'archived_reference_graph_v1','archive_id':view.verified['archive_id'],
            'archive_receipt_sha256':view.verified['receipt_sha256'],'project_id':project_id,'book_id':book_id,
            'database_logical_sha256':view.manifest['database']['logical_sha256'],
            'references':graph.references,'metadata_files':traversed,'blockers':graph.blockers,
            'diagnostic_outputs':graph.diagnostic_outputs,
            'lazy_call_directories':graph.lazy_calls.recognized,
            'references_verified':not graph.blockers,'coverage':'target_db_artifact_fields_jobs_events_runs_and_followed_json',
            'restore_plan_ready':False,'recoverable':False,'write_activation_allowed':False}
        result['verification_sha256']=digest(result)
    return result
