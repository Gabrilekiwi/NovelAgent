"""Explicit first-freeze and read-only chapter usage on owned reviewed books."""
import asyncio
import json

from fastapi import Body, HTTPException, Path as RoutePath
from fastapi.responses import JSONResponse

from core.chapter_budget_contract import freeze_chapter_budget, ChapterBudgetContractError
from core.chapter_budget_freeze import ChapterBudgetFreezeRegistry
from core.chapter_budget_store import ChapterBudgetStore, ChapterBudgetError, ChapterBudgetConflict
from core.chapter_execution_time import ChapterExecutionClock, ChapterTimeError
from core.engine.chapter_candidate import ChapterCandidateStore
from core.engine.chapter_fact_extraction import ChapterFactExtractionStore
from core.engine.reviewed_candidate_context import ReviewedChapterCandidateContextBuilder
from core.engine.safe_paths import assert_safe_local_tree
from core.story_project.reviewed_identity import load_verified_reviewed_identity


_CONFIG_FIELDS={'max_provider_calls','max_input_tokens','max_output_tokens','max_active_seconds',
                'amount_limit','currency','pricing'}


class ChapterBudgetService:
    def __init__(self,publication):
        self.publication=publication
        self.projects=publication.projects
        self.fault_injector=None

    def get(self,project_id,chapter):
        project=self.projects.get(project_id)
        registry=ChapterBudgetFreezeRegistry(project.root,book_id=project.book_id)
        result=registry.status(chapter)
        answer={'project_id':project.id,'book_id':project.book_id,'chapter':chapter,**result,
            'usage':None,'time':None}
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
        if not isinstance(configuration,dict) or set(configuration)!=_CONFIG_FIELDS:
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
                self._require_no_chapter_work(root,current['book_id'],chapter)
            registry.freeze(budget,before_write=preflight,fault_injector=self.fault_injector)
        return self.get(project_id,chapter)


def install_chapter_budget_routes(app,publication):
    service=ChapterBudgetService(publication)
    app.state.chapter_budgets=service

    async def error(request,exc):
        return JSONResponse({'error':{'code':getattr(exc,'code','chapter_budget_invalid'),
            'message':str(exc)}},status_code=409)
    for kind in (ChapterBudgetError,ChapterBudgetContractError,ChapterTimeError):
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
