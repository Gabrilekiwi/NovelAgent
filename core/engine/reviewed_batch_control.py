"""Durable batch control and process-local, revocable step scopes.

The owned Workbench service authenticates original grants and artifact receipts.
This store is a deterministic command log, never a Provider dispatcher or a
claim that caller-supplied result hashes prove a completed domain operation.
"""
import copy,json,os,re,threading
from contextlib import contextmanager
from contextvars import ContextVar

from core.chapter_budget_store import ChapterBudgetError
from core.engine.persistence import atomic_create_json,atomic_write_json
from core.story_project.reviewed_state_document import digest,read_project_bytes


class BatchControlError(ChapterBudgetError):
    code='reviewed_batch_control_invalid'


class BatchControlPaused(BatchControlError):
    code='reviewed_batch_paused'


def _sha(value):return isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) is not None
def _uuid(value):return isinstance(value,str) and re.fullmatch('[0-9a-f]{32}',value) is not None


def initial_control(grant):
    m=grant['manifest']
    value={'schema_version':'reviewed_batch_control_v1','binding':{
        'batch_id':m['batch_id'],'book_id':m['book_id'],'project_id':m['project_id'],
        'grant_sha256':grant['grant_sha256'],'batch_budget_sha256':m['budget']['batch_budget_sha256'],
        'ownership_sha256':m['ownership_sha256'],'start_chapter':m['start_chapter'],'end_chapter':m['end_chapter']},
        'commands':[]}
    value['record_sha256']=digest(value)
    return value


def _reduce(value):
    """Replay every state change; no stored status/next-stage boolean is trusted."""
    binding=value['binding']
    state={'status':'paused','chapter':binding['start_chapter'],'stage':'generation','run_epoch':None,'pending_step':None}
    keys=set();repaired=set();previous=initial_control({'manifest':{'batch_id':binding['batch_id'],'book_id':binding['book_id'],
        'project_id':binding['project_id'],'budget':{'batch_budget_sha256':binding['batch_budget_sha256']},
        'ownership_sha256':binding['ownership_sha256'],'start_chapter':binding['start_chapter'],
        'end_chapter':binding['end_chapter']},'grant_sha256':binding['grant_sha256']})['record_sha256']
    for index,command in enumerate(value['commands']):
        if (not isinstance(command,dict) or set(command)!={'sequence','previous_sha256','key_sha256','action','payload','command_sha256'}
                or type(command['sequence']) is not int or command['sequence']!=index+1
                or command['previous_sha256']!=previous or not _sha(command['key_sha256'])
                or command['key_sha256'] in keys or command['command_sha256']!=digest({k:v for k,v in command.items() if k!='command_sha256'})):
            raise BatchControlError('批次控制命令链不连续或字段无效。')
        keys.add(command['key_sha256']);action=command['action'];p=command['payload']
        if not isinstance(p,dict):raise BatchControlError('批次控制命令数据无效。')
        if action in {'start','resume'}:
            if (set(p)!={'run_epoch'} or not _uuid(p['run_epoch']) or state['status'] in {'stopped','completed'}
                    or state['stage']=='author_review'
                    or action=='start' and (index!=0 or state['pending_step'] is not None)):
                raise BatchControlError('批次不能从当前状态开始或继续。')
            state.update(status='running',run_epoch=p['run_epoch'])
        elif action in {'pause','stop'}:
            if p or state['status'] in {'stopped','completed'}:raise BatchControlError('批次已终止，不能再次切换状态。')
            state.update(status='paused' if action=='pause' else 'stopped',run_epoch=None)
        elif action=='author_takeover':
            if (set(p)!={'takeover_sha256'} or not _sha(p['takeover_sha256']) or state['status']!='paused'
                    or state['stage'] not in {'generation','extraction','quality','repair_generation','repair_extraction','repair_quality','publication'}):
                raise BatchControlError('只能暂停原自动章节后明确接管。')
            state.update(stage='author_review',pending_step=None,run_epoch=None)
        elif action=='finish_author_chapter':
            if (set(p)!={'takeover_sha256','readback_result_sha256'} or not all(_sha(p[k]) for k in p)
                    or state['status']!='paused' or state['stage']!='author_review'):
                raise BatchControlError('原作者接管章节尚未完成真实提交与回读。')
            takeover=next((c for c in reversed(value['commands'][:index]) if c['action']=='author_takeover'),None)
            if takeover is None or takeover['payload']['takeover_sha256']!=p['takeover_sha256']:
                raise BatchControlError('作者完成证明与原接管不同。')
            if state['chapter']==binding['end_chapter']:state.update(status='completed',stage='readback')
            else:state.update(chapter=state['chapter']+1,stage='generation')
        elif action=='begin_repair':
            step=state['pending_step']
            if (set(p)!={'run_epoch','step_id','quality_result_sha256','claim_sha256'}
                    or state['status']!='running' or state['stage']!='quality'
                    or p['run_epoch']!=state['run_epoch'] or step is None or p['step_id']!=step['step_id']
                    or not _sha(p['quality_result_sha256']) or not _sha(p['claim_sha256'])
                    or state['chapter'] in repaired):
                raise BatchControlError('只能为原章首次完整质量失败开始一个已消耗资格的修复周期。')
            repaired.add(state['chapter'])
            state.update(stage='repair_generation',pending_step=None)
        elif action=='claim_step':
            if (set(p)!={'run_epoch','step_id','execution_id','input_sha256'} or p['run_epoch']!=state['run_epoch']
                    or state['status']!='running' or state['pending_step'] is not None
                    or not _sha(p['input_sha256']) or not isinstance(p['execution_id'],str)
                    or re.fullmatch('execution_[A-Za-z0-9_-]{1,180}',p['execution_id']) is None):
                raise BatchControlError('本次执行步骤不能领取或与原运行不同。')
            expected='step_'+digest({'batch_id':binding['batch_id'],'chapter':state['chapter'],'stage':state['stage']})[:32]
            if p['step_id']!=expected:raise BatchControlError('执行步骤不属于原章号和阶段。')
            state['pending_step']={**copy.deepcopy(p),'chapter':state['chapter'],'stage':state['stage']}
        elif action=='finish_step':
            step=state['pending_step']
            if (set(p)!={'run_epoch','step_id','result_sha256'} or state['status']!='running'
                    or p['run_epoch']!=state['run_epoch'] or step is None or p['step_id']!=step['step_id']
                    or not _sha(p['result_sha256'])):
                raise BatchControlError('只能完成原运行的同一步骤。')
            state['pending_step']=None
            stages={'generation':'extraction','extraction':'quality','quality':'publication','publication':'readback',
                'repair_generation':'repair_extraction','repair_extraction':'repair_quality','repair_quality':'publication'}
            if state['stage']=='readback':
                if state['chapter']==binding['end_chapter']:state.update(status='completed',run_epoch=None)
                else:state.update(chapter=state['chapter']+1,stage='generation')
            else:state['stage']=stages[state['stage']]
        else:raise BatchControlError('未知批次控制命令。')
        previous=command['command_sha256']
    return state


def validate_control(value,grant):
    expected=initial_control(grant)
    if (not isinstance(value,dict) or set(value)!=set(expected) or value['schema_version']!=expected['schema_version']
            or value['binding']!=expected['binding'] or digest(value['binding'])!=digest(expected['binding'])
            or value['record_sha256']!=digest({k:v for k,v in value.items() if k!='record_sha256'})
            or not isinstance(value['commands'],list) or len(value['commands'])>10000):
        raise BatchControlError('批次控制记录与原作者授权不同。')
    state=_reduce(value)
    return copy.deepcopy(value),state


class BatchControlStore:
    def __init__(self,machine_path,book_path,grant):
        self.machine_path=machine_path;self.book_path=book_path;self.grant=grant

    @staticmethod
    def _read(path):
        try:return json.loads(read_project_bytes(path.parent,path.name))
        except (OSError,ValueError,RuntimeError) as exc:raise BatchControlError('原批次控制凭据缺失或不可读，不能归零。') from exc

    def initialize(self,*,before_write,fault_injector=None):
        expected=initial_control(self.grant)
        # Only explicit author initialization under original ownership may
        # call this, before a ready-v2 anchor exists or any chapter work.
        for path in (self.machine_path,self.book_path):
            if os.path.lexists(path) and self._read(path)!=expected:
                raise BatchControlError('控制初始化已有其他进度，不能重置。')
        for name,path in (('machine',self.machine_path),('book',self.book_path)):
            before_write()
            if not os.path.lexists(path):atomic_create_json(path,expected)
            if fault_injector is not None:fault_injector('after_control_'+name)
        return self.load()

    def load(self):
        value,state=validate_control(self._read(self.machine_path),self.grant)
        book,_=validate_control(self._read(self.book_path),self.grant)
        if book!=value or digest(book)!=digest(value):raise BatchControlError('批次控制两份记录不同，请显式续办原控制命令。')
        return value,state

    def apply(self,*,action,payload,key,expected_sha256,before_write,fault_injector=None):
        if not isinstance(key,str) or re.fullmatch('[A-Za-z0-9][A-Za-z0-9._:-]{0,191}',key) is None:
            raise BatchControlError('控制命令编号无效。')
        key_sha=digest(key.encode('utf-8'))
        machine,state=validate_control(self._read(self.machine_path),self.grant)
        book,_=validate_control(self._read(self.book_path),self.grant)
        # A command interrupted between machine and book writes is resumed
        # only by that exact original request/CAS, never by a fresh command.
        prior=next((c for c in machine['commands'] if c['key_sha256']==key_sha),None)
        if prior is not None:
            if prior['action']!=action or prior['payload']!=payload or digest(prior['payload'])!=digest(payload):
                raise BatchControlError('原控制命令编号不能改为另一动作。')
            prefix={**machine,'commands':machine['commands'][:prior['sequence']-1]}
            prefix['record_sha256']=digest({k:v for k,v in prefix.items() if k!='record_sha256'})
            if expected_sha256!=prefix['record_sha256']:raise BatchControlError('原命令的前序版本不同。')
            if book!=machine:
                if prior['sequence']!=len(machine['commands']) or book!=prefix:
                    raise BatchControlError('控制副本不属于这次原命令的前序。')
                before_write();atomic_write_json(self.book_path,machine)
            return self.load()
        if machine!=book or digest(machine)!=digest(book):raise BatchControlError('有未完成的原控制命令，请先显式续办。')
        if machine['record_sha256']!=expected_sha256:raise BatchControlError('控制状态已变化，请重新读取。')
        previous=machine['commands'][-1]['command_sha256'] if machine['commands'] else initial_control(self.grant)['record_sha256']
        command=dict(sequence=len(machine['commands'])+1,previous_sha256=previous,key_sha256=key_sha,action=action,payload=copy.deepcopy(payload))
        command['command_sha256']=digest(command)
        value={**machine,'commands':machine['commands']+[command]}
        value['record_sha256']=digest({k:v for k,v in value.items() if k!='record_sha256'})
        validate_control(value,self.grant)
        before_write();atomic_write_json(self.machine_path,value)
        if fault_injector is not None:fault_injector('after_control_machine')
        before_write();atomic_write_json(self.book_path,value)
        if fault_injector is not None:fault_injector('after_control_book')
        return self.load()


_ACTIVE_STEP=ContextVar('reviewed_batch_policy_step',default=None)
_PERMIT_KEY=object()


class PolicyStepPermit:
    def __init__(self,*,_key,binding,model,check):
        if _key is not _PERMIT_KEY:raise BatchControlError('步骤许可只能由当前拥有者创建。')
        self.binding=copy.deepcopy(binding);self.model=copy.deepcopy(model);self._check=check
        self._thread=threading.get_ident();self.closed=False

    def require_scope(self,*,stage,book_id,chapter,execution_id,input_sha256):
        if self.closed or threading.get_ident()!=self._thread or _ACTIVE_STEP.get() is not self:
            raise BatchControlPaused('旧步骤许可已关闭，或不属于当前线程/执行范围。')
        self._check()
        if (self.binding['stage']!=stage or self.binding['book_id']!=book_id or self.binding['chapter']!=chapter
                or self.binding['execution_id']!=execution_id or self.binding['input_sha256']!=input_sha256):
            raise BatchControlError('执行请求不属于这次原批次步骤。')

    def require_control(self):
        """Revocation check while typed persistence owns its allowed file writes."""
        if self.closed or threading.get_ident()!=self._thread or _ACTIVE_STEP.get() is not self:
            raise BatchControlPaused('旧步骤许可已关闭或不属于本次执行。')
        getattr(self,'_control_check',self._check)()

    def require(self,budget,*,provider,model,endpoint_fingerprint,stage):
        self.require_scope(**{k:self.binding[k] for k in ('stage','book_id','chapter','execution_id','input_sha256')})
        allowed={'generation':{'generation','chapter_generation','chapter_candidate_generation'},
            'extraction':{'chapter_fact_extraction','fact_extraction'},
            'repair_generation':{'generation','chapter_generation','chapter_candidate_generation'},
            'repair_extraction':{'chapter_fact_extraction','fact_extraction'}}
        if (budget is None or budget.execution_id!=self.binding['execution_id']
                or budget.binding['book_id']!=self.binding['book_id'] or budget.binding['chapter']!=self.binding['chapter']
                or provider!=self.model['provider'] or model!=self.model['openai_model']
                or endpoint_fingerprint!=self.model['endpoint_fingerprint'] or stage not in allowed.get(self.binding['stage'],set())):
            raise BatchControlError('模型调用不属于这次原批次、章节、步骤或冻结模型。')


def current_policy_step():return _ACTIVE_STEP.get()


@contextmanager
def policy_step_scope(*,binding,model,check):
    if _ACTIVE_STEP.get() is not None:raise BatchControlError('不能嵌套自动步骤许可。')
    permit=PolicyStepPermit(_key=_PERMIT_KEY,binding=binding,model=model,check=check)
    token=_ACTIVE_STEP.set(permit)
    try:
        check();yield permit
    finally:
        permit.closed=True;_ACTIVE_STEP.reset(token)
