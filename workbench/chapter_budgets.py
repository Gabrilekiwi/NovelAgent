"""Explicit first-freeze and read-only chapter usage on owned reviewed books."""
import asyncio
import json

from fastapi import Body, HTTPException, Path as RoutePath
from fastapi.responses import JSONResponse

from core.chapter_budget_contract import freeze_chapter_budget, ChapterBudgetContractError
from core.chapter_budget_freeze import ChapterBudgetFreezeRegistry
from core.chapter_budget_store import ChapterBudgetStore, ChapterBudgetError, ChapterBudgetConflict
from core.chapter_execution_time import ChapterExecutionClock, ChapterTimeError
from core.provider_billing_request import BillingRequestError
from core.config import get_config
from core.engine.chapter_candidate import ChapterCandidateStore
from core.engine.chapter_fact_extraction import ChapterFactExtractionStore
from core.engine.reviewed_candidate_context import ReviewedChapterCandidateContextBuilder
from core.engine.safe_paths import assert_safe_local_tree
from core.story_project.reviewed_identity import load_verified_reviewed_identity


_CONFIG_FIELDS={'max_provider_calls','max_input_tokens','max_output_tokens','max_active_seconds',
                'amount_limit','currency','pricing'}


def chapter_start_suggestion(registry, chapter, option):
    """Copy configuration only; never copy spending, time, grants or confirmation."""
    limits = dict(max_provider_calls=3, max_input_tokens=max(150000, 3 * option.get('input_reservation_per_call', 0)),
        max_output_tokens=48000, max_active_seconds='1200')
    target = {'amount':'1', 'currency':'USD'}
    suggestion = dict(source_chapter=None, limits=limits, target=target, pricing=None, tariff_changed=False)
    previous = registry.status(chapter - 1) if chapter > 1 else None
    if not previous or previous['state'] != 'ready':
        return suggestion
    old = previous['budget']
    suggestion.update(source_chapter=chapter-1, limits={k:old[k] for k in limits})
    if old['amount_limit'] is None:
        saved = old.get('spending_target', target)
        suggestion['target'] = {key:saved[key] for key in ('amount','currency')}
        if rates := saved.get('estimate_rates'):
            identity = option.get('estimate_identity')
            if identity and all(rates[k] == v for k,v in identity.items()):
                suggestion['estimate_rates'] = rates
            else:
                suggestion['tariff_changed'] = True
    else:
        suggestion['target'] = {'amount':old['amount_limit'], 'currency':old['currency']}
        rows = old['pricing']
        if (option.get('available') and len(rows) == 1
                and all(rows[0].get(k) == v for k,v in option['pricing_template'].items())):
            suggestion['pricing'] = rows[0]
        else:
            suggestion['tariff_changed'] = True
    return suggestion


class ChapterBudgetService:
    def __init__(self,publication):
        self.publication=publication
        self.projects=publication.projects
        self.fault_injector=None

    @staticmethod
    def monetary_option():
        """Expose only a checked configured identity, never keys or a URL."""
        from core.provider_billing_request import plan_openai_text_billing,openai_text_billing_policy
        config=get_config()
        try:
            plan=plan_openai_text_billing(dict(model=config.openai_model,messages=[{'role':'user','content':''}],
                max_completion_tokens=1,service_tier='default',prompt_cache_retention='in_memory',n=1,stream=False),
                base_url=config.openai_base_url or 'https://api.openai.com/v1')
        except BillingRequestError:
            from api.budget_endpoint import endpoint_fingerprint
            option = {'available':False,'message':'可填入自己服务商的单价，在下一次请求预计超出预算时停止。'}
            try:
                fingerprint = endpoint_fingerprint('openai',config.openai_base_url or 'https://api.openai.com/v1')
                if config.openai_model:
                    option['estimate_identity'] = dict(provider='openai',model=config.openai_model,endpoint_fingerprint=fingerprint)
            except ChapterBudgetError:
                option['message'] = '当前端点无法绑定单价，仅使用调用、token 和时间限额。'
            return option
        return {'available':True,'model':plan['model'],'service_tier':plan['service_tier'],
            'input_reservation_per_call':plan['max_billable_input_tokens'],
            'message':'请填入已核对且覆盖本章调用范围的价格。金额占用包含保守预留，实际账单不由工作台核对。',
            'pricing_template':dict(provider='openai',model=plan['model'],endpoint_type='official',
                endpoint_fingerprint=plan['endpoint_fingerprint'],input_token_semantics='input_includes_cache',
                billing_policy=openai_text_billing_policy())}

    def get(self,project_id,chapter):
        project=self.projects.get(project_id)
        registry=ChapterBudgetFreezeRegistry(project.root,book_id=project.book_id)
        result=registry.status(chapter)
        answer={'project_id':project.id,'book_id':project.book_id,'chapter':chapter,**result,
            'usage':None,'time':None,'monetary_option':self.monetary_option()}
        if result['state']=='not_configured':
            answer['suggestion']=chapter_start_suggestion(registry, chapter, answer['monetary_option'])
        if result['state']=='ready':
            store=ChapterBudgetStore(registry.root/f'c{chapter}',book_id=project.book_id,chapter=chapter)
            answer['usage']=store.report()
            answer['time']=ChapterExecutionClock(store.root,result['budget']).report()
        return answer

    @staticmethod
    def _require_no_chapter_work(root,book_id,chapter):
        # A fresh limit cannot erase earlier generation or extraction, even if
        # its old physical usage is unknown. Finish/reconcile the original work.
        count=0
        for store,prefix in ((ChapterCandidateStore(root),'candidate_'),
                             (ChapterFactExtractionStore(root),'fact_extraction_')):
            directory=assert_safe_local_tree(store.root)
            if not directory.exists():continue
            for path in directory.iterdir():
                count+=1
                if count>10000:raise ChapterBudgetError('chapter work history exceeds initialization preflight limit')
                assert_safe_local_tree(path)
                if not path.name.startswith(prefix):
                    raise ChapterBudgetError('unknown entry in chapter execution history')
                intent=store.load_intent(path.name)
                if intent['book_id']!=book_id:
                    raise ChapterBudgetConflict('chapter execution history belongs to another book')
                if intent['chapter_index']==chapter:
                    raise ChapterBudgetConflict('本章已有执行记录，不能给已发生的工作重新建立空预算。请先处理原执行。')

    def freeze(self,project_id,chapter,*,configuration,expected_context_sha256,author_confirmed):
        if author_confirmed is not True:
            raise ChapterBudgetConflict('请明确确认本章预算。')
        if not isinstance(configuration,dict) or set(configuration) not in (_CONFIG_FIELDS, _CONFIG_FIELDS | {'spending_target'}):
            raise ChapterBudgetConflict('预算配置字段不完整或包含不支持的字段。')
        # This existing boundary owns the machine book, Provider lock and DB
        # registration. Freeze must not bypass unresolved calls or publication.
        with self.publication._owned_project(project_id) as context:
            current,connection,owner,owner_args,ownership,root,runtime,roots,tx_root=context
            self.publication._require_settled_persistence(tx_root)
            budget=freeze_chapter_budget(book_id=current['book_id'],chapter=chapter,**configuration)
            registry=ChapterBudgetFreezeRegistry(root,book_id=current['book_id'])
            def preflight():
                if owner.require(**owner_args)!=ownership:
                    raise ChapterBudgetConflict('预算冻结期间作品所有权发生变化。')
                verified=load_verified_reviewed_identity(root)
                if verified['history']['value']['committed_through']+1!=chapter:
                    raise ChapterBudgetConflict('仅能为当前待写章节首次冻结预算。')
                trusted=ReviewedChapterCandidateContextBuilder(root).build(chapter)
                if trusted.context_sha256!=expected_context_sha256:
                    raise ChapterBudgetConflict('章节输入已变化，请刷新后重新确认预算。')
                if registry.status(chapter)['state']=='not_configured':
                    if rates := budget.get('spending_target',{}).get('estimate_rates'):
                        identity = self.monetary_option().get('estimate_identity')
                        if not identity or any(rates[k] != v for k,v in identity.items()):
                            raise ChapterBudgetConflict('模型或中转地址已变化，请重新核对单价。')
                    for row in budget['pricing']:
                        if 'billing_policy' not in row:continue
                        option=self.monetary_option()
                        if not option['available'] or any(row.get(key)!=value
                                for key,value in option['pricing_template'].items()):
                            raise ChapterBudgetConflict('模型或端点已变化，请重新读取并确认金额预算。')
                self._require_no_chapter_work(root,current['book_id'],chapter)
            registry.freeze(budget,before_write=preflight,fault_injector=self.fault_injector)
        return self.get(project_id,chapter)


def install_chapter_budget_routes(app,publication):
    service=ChapterBudgetService(publication)
    app.state.chapter_budgets=service

    async def error(request,exc):
        return JSONResponse({'error':{'code':getattr(exc,'code','chapter_budget_invalid'),
            'message':str(exc)}},status_code=409)
    for kind in (ChapterBudgetError,ChapterBudgetContractError,ChapterTimeError,BillingRequestError):
        app.add_exception_handler(kind,error)

    @app.get('/api/projects/{project_id}/chapters/{chapter}/budget')
    async def get(project_id:str,chapter:int=RoutePath(ge=1)):
        return await asyncio.to_thread(service.get,project_id,chapter)

    @app.post('/api/projects/{project_id}/chapters/{chapter}/budget')
    async def freeze(project_id:str,chapter:int=RoutePath(ge=1),payload:dict=Body(...)):
        if set(payload)!={'configuration','expected_context_sha256','author_confirmed'} or payload['author_confirmed'] is not True:
            raise HTTPException(422,'请提供完整预算、当前章节输入摘要并明确确认。')
        return await asyncio.to_thread(service.freeze,project_id,chapter,**payload)
    return service
