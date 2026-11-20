"""Versioned author review drafts; only a later formal upgrade grants authority.

Persist these before capture: editing/confirming a draft changes the whole DB
and deliberately makes any earlier archive stale. Records never write the book.
The product caller must own admission/ownership and check current source before
each mutation and again before any upgrade transaction.
"""
import copy
import json
from pathlib import Path
import re
from datetime import datetime,timezone
import uuid
from contextlib import contextmanager

from core.engine.chapter_fact_context import validate_trusted_chapter_context
from core.story_project.activation import validate_story_state_calibration_report
from core.story_project.identity import validate_project_identity
from core.story_project.paths import scan_prose_chapters
from core.story_project.read_set import verify_story_project_read_set
from core.story_project.reviewed_state_document import baseline_request,capture_sources,digest,read_project_bytes


PROTOCOL='author_baseline_review_v1'
CATEGORIES=('event','character_state','relationship','roster','location','inventory','counter')
_FIELDS={'schema_version','id','revision','project_id','book_id','chapter','status','state','sources',
    'static_references','calibration_report','identity_text','binding','read_set','confirmed_categories',
    'author_confirmed','previous_revision_sha256','created_at','record_sha256'}
_SHA=re.compile('[0-9a-f]{64}')
_MAX_PROJECT_BYTES=256*1024**2


class AuthorBaselineError(ValueError):
    code='author_baseline_invalid'


def _sha(value):
    return isinstance(value,str) and _SHA.fullmatch(value) is not None


def validate_baseline_revision(value):
    if (not isinstance(value,dict) or set(value)!=_FIELDS or value['schema_version']!=PROTOCOL
            or not isinstance(value['id'],str) or re.fullmatch('baseline_[0-9a-f]{32}',value['id']) is None
            or type(value['revision']) is not int or value['revision']<1
            or type(value['chapter']) is not int or value['chapter']<1
            or not isinstance(value['state'],dict)
            or not isinstance(value['project_id'],str) or not value['project_id']
            or value['status'] not in {'draft','confirmed'}
            or type(value['author_confirmed']) is not bool
            or not isinstance(value['created_at'],str)
            or not _sha(value['record_sha256'])):
        raise AuthorBaselineError('作者基线记录格式无效。')
    previous=value['previous_revision_sha256']
    if (value['revision']==1 and previous is not None or value['revision']>1 and not _sha(previous)):
        raise AuthorBaselineError('作者基线修订前序无效。')
    binding=value['binding'];read_set=value['read_set']
    if (not isinstance(binding,dict) or set(binding)!={'source_root','identity_sha256','context_sha256','read_set_sha256'}
            or not all(_sha(binding[key]) for key in ('identity_sha256','context_sha256','read_set_sha256'))
            or not isinstance(binding['source_root'],str) or not Path(binding['source_root']).is_absolute()
            or '..' in Path(binding['source_root']).parts or not isinstance(value['identity_text'],str)
            or digest(value['identity_text'].encode('utf-8'))!=binding['identity_sha256']
            or not isinstance(read_set,dict) or digest(read_set)!=binding['read_set_sha256']
            or read_set.get('book_id')!=value['book_id'] or read_set.get('chapter_index')!=value['chapter']+1
            or read_set.get('identity_revision')!=binding['identity_sha256']
            or read_set.get('root_identity',{}).get('resolved_path')!=binding['source_root']):
        raise AuthorBaselineError('作者基线来源绑定无效。')
    identity=validate_project_identity(json.loads(value['identity_text']))
    report=validate_story_state_calibration_report(value['calibration_report'])
    if (identity.book_id!=value['book_id'] or identity.ephemeral or identity.story_state_mode!='strict'
            or identity.authority['mode']!='legacy_markdown_v1' or not report['strict_eligible']
            or report['book_id']!=identity.book_id
            or any(report[key]!=identity.activation[target] for key,target in (
                ('report_sha256','calibration_report_sha256'),('parser_version','parser_version'),
                ('semantic_schema_version','semantic_schema_version'),('target_layout_profile_version','layout_profile_version')))):
        raise AuthorBaselineError('原校准报告不能证明当前 Strict 旧书资格。')
    baseline_request(book_id=value['book_id'],chapter=value['chapter'],state=value['state'],sources=value['sources'],
        old_identity_sha256=binding['identity_sha256'],static_references=value['static_references'])
    categories=value['confirmed_categories']
    expected=list(CATEGORIES) if value['status']=='confirmed' else []
    if categories!=expected or value['author_confirmed']!=(value['status']=='confirmed'):
        raise AuthorBaselineError('七类核对或作者确认与记录状态不同。')
    if value['record_sha256']!=digest({k:v for k,v in value.items() if k!='record_sha256'}):
        raise AuthorBaselineError('作者基线记录摘要不同。')
    return copy.deepcopy(value)


def validate_baseline_history(rows,*,project_id,book_id):
    """Pure retained DB history proof; never open recorded historical paths."""
    if len(rows)>1000:raise AuthorBaselineError('基线修订数超过支持范围，请保留原记录。')
    result=[];previous=None
    for sequence,row in enumerate(rows,1):
        if not isinstance(row['payload_json'],str) or len(row['payload_json'].encode('utf-8'))>2*1024**2:
            raise AuthorBaselineError('基线记录超过支持大小。')
        value=validate_baseline_revision(json.loads(row['payload_json']))
        if (any(row[key]!=value[key] for key in ('id','revision','project_id','book_id','record_sha256'))
                or value['revision']!=sequence or value['project_id']!=project_id or value['book_id']!=book_id
                or value['previous_revision_sha256']!=previous
                or not result and value['status']!='draft'
                or result and any(value[key]!=result[0][key] for key in ('id','book_id','chapter','binding','read_set',
                    'sources','identity_text','calibration_report','static_references'))
                or result and value['status']=='confirmed' and (result[-1]['status']!='draft' or value['state']!=result[-1]['state'])):
            raise AuthorBaselineError('基线修订链、确认内容或所属作品不同。')
        result.append(value);previous=value['record_sha256']
    return result


def build_baseline_draft(*,project_id,trusted_context,state,calibration_report,static_references=()):
    if (not isinstance(state,dict) or not isinstance(calibration_report,dict)
            or not isinstance(static_references,(list,tuple))):
        raise AuthorBaselineError('基线内容、完整原校准报告或摘录列表无效。')
    context=validate_trusted_chapter_context(trusted_context)
    root=Path(context['story_project_root']);read_set=context['read_set']
    verify_story_project_read_set(read_set,phase='prepare')
    raw=read_project_bytes(root,'.novelagent/project.json')
    value={'schema_version':PROTOCOL,'id':'baseline_'+uuid.uuid4().hex,'revision':1,
        'project_id':project_id,'book_id':context['project_identity']['book_id'],
        'chapter':context['chapter_index']-1,'status':'draft','state':copy.deepcopy(state),
        'sources':capture_sources(root),'static_references':copy.deepcopy(list(static_references)),
        'calibration_report':copy.deepcopy(calibration_report),'identity_text':raw.decode('utf-8'),
        'binding':{'source_root':str(root),'identity_sha256':digest(raw),'context_sha256':trusted_context.context_sha256,
            'read_set_sha256':digest(read_set)},'read_set':copy.deepcopy(read_set),
        'confirmed_categories':[],'author_confirmed':False,'previous_revision_sha256':None,
        'created_at':datetime.now(timezone.utc).isoformat()}
    value['record_sha256']=digest(value)
    result=validate_baseline_revision(value)
    assert_baseline_current(result,trusted_context=trusted_context)
    return result


def assert_baseline_current(value,*,trusted_context):
    value=validate_baseline_revision(value)
    context=validate_trusted_chapter_context(trusted_context)
    root=Path(context['story_project_root'])
    if (context['project_identity']['book_id']!=value['book_id'] or str(root)!=value['binding']['source_root']
            or context['chapter_index']!=value['chapter']+1
            or trusted_context.context_sha256!=value['binding']['context_sha256']
            or context['read_set']!=value['read_set']):
        raise AuthorBaselineError('作品来源或章节已变化，请重新建立基线核对。')
    verify_story_project_read_set(value['read_set'],phase='prepare')
    chapters=scan_prose_chapters(root)
    if (len(chapters)!=value['chapter'] or any(number!=expected for expected,number in enumerate(sorted(chapters),1))
            or any(len(paths)!=1 for paths in chapters.values())):
        raise AuthorBaselineError('基线需要从第1章起完整、连续且没有同章重复文件的正式正文。')
    if (capture_sources(root)!=value['sources'] or read_project_bytes(root,'.novelagent/project.json').decode('utf-8')!=value['identity_text']):
        raise AuthorBaselineError('原始资料已变化，旧基线确认不能继续使用。')
    # This also checks that references are actual current source excerpts.
    from core.story_project.reviewed_state_document import _references
    _references(value['static_references'],root=root)
    return value


class AuthorBaselineStore:
    def __init__(self,database):
        self.database=database
        database.initialize_component(self._initialize)

    def _initialize(self):
        with self.database.transaction() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS author_baseline_revisions (
                id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=1),
                project_id TEXT NOT NULL, book_id TEXT NOT NULL,
                payload_json TEXT NOT NULL, record_sha256 TEXT NOT NULL,
                PRIMARY KEY(id,revision), FOREIGN KEY(project_id) REFERENCES projects(id),
                FOREIGN KEY(book_id) REFERENCES projects(book_id))''')

    def _history(self,connection,identifier,project_id):
        self.database.require_project_access(project_id)
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='author_baseline_revisions'").fetchone() is None:
            return []
        rows=[];total=0
        for row in connection.execute('SELECT * FROM author_baseline_revisions WHERE id=? ORDER BY revision LIMIT 1001',(identifier,)):
            if not isinstance(row['payload_json'],str):raise AuthorBaselineError('基线内容不是文本。')
            if len(row['payload_json'].encode('utf-8'))>2*1024**2:raise AuthorBaselineError('基线记录超过支持大小。')
            total+=len(row['payload_json'].encode('utf-8'))
            if total>_MAX_PROJECT_BYTES:raise AuthorBaselineError('基线历史超过支持大小，请保留原记录。')
            rows.append(row)
        project=connection.execute('SELECT book_id FROM projects WHERE id=?',(project_id,)).fetchone()
        if project is None:raise AuthorBaselineError('实际作品登记不存在。')
        return validate_baseline_history(rows,project_id=project_id,book_id=project['book_id'])

    def history(self,identifier,*,project_id):
        with self.database.connection() as connection:return self._history(connection,identifier,project_id)

    def _append(self,connection,value):
        value=validate_baseline_revision(value)
        if value['revision']>1000:raise AuthorBaselineError('基线修订数超过支持范围，请新建核对并保留原记录。')
        raw=json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False)
        if len(raw.encode('utf-8'))>2*1024**2:raise AuthorBaselineError('基线记录超过支持大小。')
        count,total=connection.execute('''SELECT COUNT(*),COALESCE(SUM(length(CAST(payload_json AS BLOB))),0)
            FROM author_baseline_revisions WHERE project_id=?''',(value['project_id'],)).fetchone()
        if count>=100000 or total+len(raw.encode('utf-8'))>_MAX_PROJECT_BYTES:
            raise AuthorBaselineError('本作品审核历史已达到备份支持上限，原记录保留，未保存本次修改。')
        connection.execute('INSERT INTO author_baseline_revisions VALUES (?,?,?,?,?,?)',
            (value['id'],value['revision'],value['project_id'],value['book_id'],raw,value['record_sha256']))
        return value

    @contextmanager
    def _transaction(self,connection):
        if connection is None:
            with self.database.transaction() as owned:yield owned
        else:
            self.database.require_write_access()
            locations={row[1]:row[2] for row in connection.execute('PRAGMA database_list')}
            if not connection.in_transaction or Path(locations.get('main','')).resolve()!=self.database.path:
                raise AuthorBaselineError('基线写入必须属于当前工作台的实际事务。')
            yield connection

    def create(self,value,*,trusted_context,_connection=None):
        value=assert_baseline_current(value,trusted_context=trusted_context)
        if value['revision']!=1 or value['status']!='draft':raise AuthorBaselineError('只能新建未确认的首个草稿。')
        self.database.require_project_access(value['project_id'])
        with self._transaction(_connection) as connection:
            if self._history(connection,value['id'],value['project_id']):raise AuthorBaselineError('基线已存在。')
            project=connection.execute('SELECT book_id,root FROM projects WHERE id=?',(value['project_id'],)).fetchone()
            if project is None or project['book_id']!=value['book_id'] or Path(project['root'])!=Path(value['binding']['source_root']):
                raise AuthorBaselineError('基线不属于实际登记的作品。')
            assert_baseline_current(value,trusted_context=trusted_context)
            saved=self._append(connection,value)
            assert_baseline_current(saved,trusted_context=trusted_context)
            return saved

    def revise(self,identifier,*,project_id,expected_revision,expected_sha256,trusted_context,state=None,
               confirmed_categories=(),author_confirmed=False,_connection=None):
        with self._transaction(_connection) as connection:
            history=self._history(connection,identifier,project_id)
            if not history:raise AuthorBaselineError('作者基线不存在。')
            old=history[-1]
            if type(expected_revision) is not int or old['revision']!=expected_revision or old['record_sha256']!=expected_sha256:
                raise AuthorBaselineError('基线已被修改，请刷新后重新核对。')
            assert_baseline_current(old,trusted_context=trusted_context)
            confirming=author_confirmed is True
            if (not isinstance(confirmed_categories,(list,tuple)) or any(not isinstance(item,str) for item in confirmed_categories)
                    or type(author_confirmed) is not bool or confirming and (state is not None or set(confirmed_categories)!=set(CATEGORIES)
                    or len(confirmed_categories)!=len(CATEGORIES) or old['status']!='draft')
                    or not confirming and (confirmed_categories or state is None)):
                raise AuthorBaselineError('请逐类核对后明确确认，修改状态后须重新审核。')
            value=copy.deepcopy(old);value.update(revision=old['revision']+1,previous_revision_sha256=old['record_sha256'],
                status='confirmed' if confirming else 'draft',author_confirmed=confirming,
                confirmed_categories=list(CATEGORIES) if confirming else [],created_at=datetime.now(timezone.utc).isoformat())
            if not confirming:value['state']=copy.deepcopy(state)
            value['record_sha256']=digest({k:v for k,v in value.items() if k!='record_sha256'})
            assert_baseline_current(value,trusted_context=trusted_context)
            saved=self._append(connection,value)
            assert_baseline_current(saved,trusted_context=trusted_context)
            return saved
