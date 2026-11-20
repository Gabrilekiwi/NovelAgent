"""Offline whole-DB verification and target-row evidence; never a restored app."""
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from core.story_project.reviewed_state_document import digest
from workbench.backup_archive import BackupArchiveError, _file_hash
from workbench.backup_files import _ordinary
from workbench.backup_archive_view import verified_archive_view
from workbench.backup_database import _logical_digest, _read_authorizer


class ArchivedDatabaseError(BackupArchiveError):
    code = 'archived_database_unverified'


@contextmanager
def _database(view, *, temporary_root=None):
    """Materialize one checked member privately, then open it strictly read-only."""
    expected = view.manifest['database']
    # An exclusive file inherits the configured temporary parent's Windows
    # ACL. A mode-0700 directory may exclude a managed child token on Windows.
    temporary = tempfile.NamedTemporaryFile(prefix='na-db-',suffix='.sqlite',dir=temporary_root,delete=False)
    path = Path(temporary.name)
    info = os.fstat(temporary.fileno()); physical = (info.st_dev,info.st_ino)
    try:
        with temporary as stream:
            view.copy_member('workbench.sqlite', stream)
        with path.open('rb') as stream:
            header = stream.read(100)
        if len(header) != 100 or header[:16] != b'SQLite format 3\x00' or header[18:20] != b'\x01\x01':
            raise ArchivedDatabaseError('归档数据库不是独立的 SQLite 主文件。')
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1', uri=True, isolation_level=None)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA query_only=ON')
            connection.execute('PRAGMA trusted_schema=OFF')
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 32*1024**2)
            connection.set_authorizer(_read_authorizer)
            operations = 0
            def bounded():
                nonlocal operations
                operations += 10000
                return int(operations > 100000000)
            connection.set_progress_handler(bounded, 10000)
            if [tuple(row) for row in connection.execute('PRAGMA integrity_check')] != [('ok',)]:
                raise ArchivedDatabaseError('归档数据库完整性检查失败。')
            if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise ArchivedDatabaseError('归档数据库外键关系缺失。')
            if (connection.execute('PRAGMA user_version').fetchone()[0] != expected['user_version']
                    or _logical_digest(connection) != expected['logical_sha256']):
                raise ArchivedDatabaseError('归档数据库逻辑状态与快照凭据不同。')
            yield connection
        if _file_hash(path) != {k:expected[k] for k in ('sha256','size')}:
            raise ArchivedDatabaseError('离线验证期间临时数据库变化。')
    finally:
        temporary.close()
        info = _ordinary(path)
        if (info.st_dev,info.st_ino) != physical:
            raise ArchivedDatabaseError('离线临时文件被替换，拒绝清理未知文件。')
        path.unlink()


def _row_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def inspect_archived_database(package, *, project_id, book_id, temporary_root=None):
    """Bind actual SQL data to the archive, preserving all non-target rows.

    Row digests prove preservation, not domain validity. File references, typed
    state, budgets and ownership require the other offline checks before any
    restore plan can become ready. No source path is opened here.
    """
    try:
        with verified_archive_view(package) as view, _database(view,temporary_root=temporary_root) as connection:
            preview = view.manifest['preview']
            if preview['project_id'] != project_id or preview['book_id'] != book_id:
                raise ArchivedDatabaseError('离线数据库目标与归档不同。')
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {'projects','workbench_schema','jobs','job_events','drafts','outline_batches'}
            if not required <= tables or connection.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise ArchivedDatabaseError('归档数据库版本或必要表不受支持。')
            versions = [tuple(row) for row in connection.execute('SELECT singleton,version FROM workbench_schema')]
            if versions != [(1,1)]:
                raise ArchivedDatabaseError('归档数据库 schema 登记无效。')
            selected = [dict(row) for row in connection.execute('SELECT * FROM projects WHERE id=? LIMIT 2',(project_id,))]
            identity_raw = view.read_root('story_project','.novelagent/project.json')
            identity = json.loads(identity_raw)
            source = Path(preview['scope']['inventories']['story_project']['source_root'])
            restore_evidence = None
            provenance = preview['scope'].get('restored_provenance')
            if provenance is not None:
                from workbench.backup_restore_provenance import validate_restore_provenance, validate_restored_database
                validate_restore_provenance(provenance, root=source, book_id=book_id,
                    ownership=preview['scope']['machine_ownership']['record'])
                restore_evidence = validate_restored_database(connection, provenance)
            if (len(selected) != 1 or selected[0]['book_id'] != book_id or Path(selected[0]['root']) != source
                    or not isinstance(identity,dict) or identity.get('book_id') != book_id
                    or not source.is_absolute() or '..' in source.parts):
                raise ArchivedDatabaseError('数据库登记、作品身份与归档根不同。')

            if 'chapter_fact_reviews' in tables and connection.execute('''
                SELECT 1 FROM chapter_fact_reviews r JOIN chapter_candidate_drafts d ON d.id=r.draft_id
                WHERE (r.project_id=? OR d.project_id=?) AND
                    (r.project_id<>d.project_id OR r.chapter<>d.chapter OR r.candidate_id<>d.candidate_id) LIMIT 1
                ''',(project_id,project_id)).fetchone():
                raise ArchivedDatabaseError('归档事实审核引用其他作品、章节或候选草稿。')
            if 'cohorts' in tables and connection.execute('''
                SELECT 1 FROM cohorts c LEFT JOIN jobs j ON j.id=c.session_job_id
                WHERE c.project_id=? AND c.session_job_id IS NOT NULL AND
                    (j.id IS NULL OR j.project_id<>c.project_id) LIMIT 1
                ''',(project_id,)).fetchone():
                raise ArchivedDatabaseError('归档批次任务归属缺失或不同。')

            # Fixed relationships cover every current workbench table. Unknown
            # tables remain in the whole snapshot and are reported explicitly.
            direct = {'outline_batches','jobs','drafts','chapter_candidate_drafts','chapter_fact_reviews',
                      'planning_operations','amendments','cohorts','author_baseline_revisions'}
            queries = {table:(f'SELECT * FROM "{table}" WHERE project_id=? ORDER BY id',(project_id,))
                       for table in sorted(direct & tables)}
            if 'author_baseline_revisions' in tables:
                if connection.execute('''SELECT 1 FROM author_baseline_revisions r
                        LEFT JOIN projects p ON p.id=r.project_id
                        WHERE (r.project_id=? OR r.book_id=?) AND (p.id IS NULL OR r.book_id<>p.book_id) LIMIT 1''',
                        (project_id,book_id)).fetchone():
                    raise ArchivedDatabaseError('作者基线修订行的项目与作品归属不同。')
                queries['author_baseline_revisions']=('SELECT * FROM author_baseline_revisions WHERE project_id=? ORDER BY id,revision',(project_id,))
                from workbench.author_baselines import validate_baseline_history
                baseline_rows={};baseline_count=baseline_bytes=0
                for row in connection.execute(queries['author_baseline_revisions'][0],(project_id,)):
                    baseline_count+=1
                    raw=row['payload_json']
                    if not isinstance(raw,str):raise ArchivedDatabaseError('作者基线内容不是文本。')
                    size=len(raw.encode('utf-8'));baseline_bytes+=size
                    group=baseline_rows.setdefault(row['id'],[])
                    if baseline_count>100000 or size>2*1024**2 or baseline_bytes>256*1024**2 or len(group)>=1000:
                        raise ArchivedDatabaseError('作者基线历史超过支持范围。')
                    group.append(row)
                for rows in baseline_rows.values():validate_baseline_history(rows,project_id=project_id,book_id=book_id)
            queries['job_events'] = ('SELECT e.* FROM job_events e JOIN jobs j ON j.id=e.job_id WHERE j.project_id=? ORDER BY e.id',(project_id,))
            if 'chapter_fact_review_edits' in tables:
                queries['chapter_fact_review_edits'] = ('SELECT e.* FROM chapter_fact_review_edits e JOIN chapter_fact_reviews r ON r.id=e.review_id WHERE r.project_id=? ORDER BY e.review_id,e.to_revision',(project_id,))
            if 'chapter_evaluations' in tables:
                queries['chapter_evaluations'] = ('SELECT e.* FROM chapter_evaluations e JOIN cohorts c ON c.id=e.cohort_id WHERE c.project_id=? ORDER BY e.cohort_id,e.chapter',(project_id,))
            if 'chapter_publication_index' in tables:
                queries['chapter_publication_index'] = ('SELECT * FROM chapter_publication_index WHERE book_id=? ORDER BY chapter',(book_id,))
            evidence = {}; jobs = []; target_rows = encoded_bytes = 0
            for table,(sql,args) in sorted(queries.items()):
                rows = []
                for item in connection.execute(sql,args):
                    row = dict(item)
                    target_rows += 1
                    encoded_bytes += len(json.dumps(row,ensure_ascii=False).encode('utf-8'))
                    if target_rows > 100000 or encoded_bytes > 256*1024**2:
                        raise ArchivedDatabaseError('目标数据库记录超过离线支持上限。')
                    if table in {'drafts','chapter_candidate_drafts'}:
                        if (not isinstance(row['text'],str) or digest(row['text'].encode('utf-8')) != row['sha256']
                                or type(row['revision']) is not int or row['revision'] < 1):
                            raise ArchivedDatabaseError('归档作者草稿内容或修订与摘要不同。')
                    if table == 'jobs':
                        jobs.append({'id':row['id'],'state':row['state'],'next_chapter':row['next_chapter'],
                                     'execution_contract':row.get('execution_contract')})
                    keys = ('id','review_id','to_revision','cohort_id','chapter','job_id','sequence',
                            'book_id','idempotency_scope_sha256')
                    if table=='author_baseline_revisions':keys=(*keys,'revision')
                    rows.append({'key':{k:row[k] for k in keys if k in row},'sha256':_row_hash(row)})
                evidence[table] = {'count':len(rows),'rows':rows,'rows_sha256':digest(rows)}
            other = [row['id'] for row in connection.execute(
                'SELECT id FROM projects WHERE id<>? ORDER BY id LIMIT 100001',(project_id,))]
            disabled_jobs = [row['id'] for row in connection.execute(
                'SELECT id FROM jobs WHERE project_id<>? ORDER BY id LIMIT 100001',(project_id,))]
            if len(other)>100000 or len(disabled_jobs)>100000:
                raise ArchivedDatabaseError('恢复禁用范围超过支持上限。')
            result = {'schema_version':'archived_database_evidence_v1','archive_id':view.verified['archive_id'],
                'archive_receipt_sha256':view.verified['receipt_sha256'],
                'database_logical_sha256':view.manifest['database']['logical_sha256'],
                'database_file_sha256':view.manifest['database']['sha256'],
                'database_verified':True,'coverage':'whole_database_integrity_and_target_row_preservation',
                'project_id':project_id,'book_id':book_id,'source_root':str(source),
                'registration_sha256':_row_hash(selected[0]),'identity_sha256':digest(identity_raw),
                'target_tables':evidence,'target_jobs':jobs,
                'unclassified_tables':sorted(tables-required-set(queries)-{'sqlite_sequence'}-
                    (set(restore_evidence['classified_tables']) if restore_evidence else set())),
                'restore_policy':{'disable_other_project_ids':other,'disable_other_job_ids':disabled_jobs,
                    'target_jobs_require_explicit_revalidation':[row['id'] for row in jobs],
                    'start_worker':False,'auto_import':False,'provider_calls_allowed':False,
                    'write_activation_allowed':False,'enforced_in_restored_app':False},
                'restore_plan_ready':False,'recoverable':False}
            if restore_evidence is not None: result['restored_provenance'] = restore_evidence
            result['evidence_sha256'] = digest(result)
        return result
    except (sqlite3.Error,OSError,KeyError,TypeError,ValueError) as exc:
        raise ArchivedDatabaseError('离线数据库无法通过完整核验：'+str(exc)) from exc
