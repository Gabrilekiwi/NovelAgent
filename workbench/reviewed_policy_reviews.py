"""Owned preparation from actual grant/extraction; never invoke author approval."""
import json,os
from contextlib import nullcontext

from core.chapter_execution_budget import ChapterExecutionBudget,current_chapter_operation
from core.engine.reviewed_batch_control import current_policy_step,BatchControlError
from core.engine.chapter_commit_prepare import prepare_policy_reviewed_chapter
from core.engine.chapter_fact_extraction import ChapterFactExtractionStore
from core.engine.persistence import atomic_create_json
from core.engine.reviewed_candidate_context import ReviewedChapterCandidateContextBuilder,verify_preparation_sources
from core.engine.reviewed_chapter_quality import prepare_candidate_chapter_quality
from core.engine.reviewed_policy_review import build_policy_fact_review,PolicyReviewError
from core.quality.reviewed_disposition import project_reviewed_quality_disposition
from core.story_project.reviewed_state_document import digest,read_project_bytes
from workbench.reviewed_batches import ReviewedBatchService


class ReviewedPolicyReviewService:
    def __init__(self,publication):
        self.publication=publication;self.batches=ReviewedBatchService(publication)

    def prepare(self,*,project_id,batch_id,draft_id,extraction_id,expected_draft_revision,
                expected_draft_sha256,expected_extraction_record_hash):
        """Explicit local work, metered to original chapter; no Provider calls.

        This creates a typed policy review for quality evaluation, not a fact
        edit in the author's review table and not a publication request. An
        execution driver still needs its durable explicit step authorization.
        """
        p=self.publication
        permit=current_policy_step()
        if permit is not None and not callable(getattr(permit,'_owned_project',None)):
            raise BatchControlError('本地质量步骤缺少原拥有者边界。')
        with permit._owned_project(p,project_id) if permit is not None else p._owned_project(project_id) as context:
            current,connection,owner,owner_args,ownership,root,runtime,roots,tx_root=context
            p._require_settled_persistence(tx_root)
            require_grant=self.batches._require_current_execution if permit is not None else self.batches._require_current_start
            grant=require_grant(context,batch_id)['grant']
            draft,intent=p.preparations.fact_reviews.verify_draft_source(draft_id,connection=connection)
            if (draft.project_id!=project_id or draft.chapter not in grant['manifest']['chapters']
                    or type(expected_draft_revision) is not int or draft.revision!=expected_draft_revision
                    or draft.sha256!=expected_draft_sha256):
                raise PolicyReviewError('本次策略审核的原草稿或批次章节已变化。')
            budget=ChapterExecutionBudget(root,book_id=current['book_id'],chapter=draft.chapter)
            operation='execution_policy_review_'+digest({'draft':draft.to_dict(),'extraction':extraction_id,'batch':batch_id})[:32]
            permit=current_policy_step();action=current_chapter_operation()
            if permit is not None:
                if permit.binding['stage'] not in {'quality','repair_quality'}:raise BatchControlError('策略审核不属于本地质量阶段。')
                permit.require_scope(stage=permit.binding['stage'],book_id=current['book_id'],chapter=draft.chapter,
                    execution_id=permit.binding['execution_id'],input_sha256=permit.binding['input_sha256'])
                if action is None or action.provider_dispatch_allowed:
                    raise BatchControlError('批次质量检查必须使用原禁止模型调用的主动区间。')
                action.require_target(budget,permit.binding['execution_id'])
            elif action is not None:raise BatchControlError('质量检查不能复用其他未绑定的执行区间。')
            with nullcontext(action) if permit is not None else budget.operation(operation,allow_provider_dispatch=False) as active:
                store=ChapterFactExtractionStore(root);loaded=store.load(extraction_id)
                source=loaded['intent'];record=loaded['record'];proposal=store.load_proposal(extraction_id)
                trusted=ReviewedChapterCandidateContextBuilder(root).build(draft.chapter)
                authority={k:intent['authority'][k] for k in ('mode','authority_epoch','head_event_hash','identity_revision')}
                authority['authority_projection_sha256']=trusted.authority_projection_sha256
                if (not isinstance(record,dict) or record['status']!='completed'
                        or record['record_hash']!=expected_extraction_record_hash
                        or source['book_id']!=current['book_id'] or source['chapter_index']!=draft.chapter
                        or source['candidate']!={'candidate_id':draft.candidate_id,'intent_hash':intent['intent_hash']}
                        or any(source['draft'][k]!=v for k,v in {'draft_id':draft.id,'revision':draft.revision,'sha256':draft.sha256}.items())
                        or source['context']['context_sha256']!=trusted.context_sha256
                        or source['authority']!=intent['authority'] or store.load_source(extraction_id)!=draft.text
                        or 'policy_authorization' in source and (
                            source['policy_authorization']['grant_sha256']!=grant['grant_sha256']
                            or source['policy_authorization']['manifest_sha256']!=grant['manifest']['manifest_sha256'])):
                    raise PolicyReviewError('事实提取不是本次完整正文和原权威状态的已完成记录。')
                review=build_policy_fact_review(grant=grant,project_id=project_id,book_id=current['book_id'],chapter=draft.chapter,
                    draft=draft.to_dict(),candidate_intent=intent['intent_hash'],context_sha256=trusted.context_sha256,
                    authority=authority,extraction={'extraction_id':extraction_id,'intent_hash':source['intent_hash'],
                        'record_hash':record['record_hash'],'proposal_hash':proposal['proposal_hash'],
                        'execution_id':source['execution']['execution_id']},model_facts=proposal['facts'])
                prepared=prepare_policy_reviewed_chapter(trusted_context=trusted,project_id=project_id,
                    draft=draft.to_dict(),policy_review=review,idempotency_key='policy-'+review['id'])
                quality=prepare_candidate_chapter_quality(trusted_context=trusted,chapter_preparation=prepared)
                disposition=project_reviewed_quality_disposition(preparation=prepared,quality=quality)
                verify_preparation_sources(trusted.story_project_context,phase='prepare')
                if (p.preparations.drafts.get(draft_id,connection=connection)!=draft
                        or owner.require(**owner_args)!=ownership or store.load_record(extraction_id)!=record
                        or require_grant(context,batch_id)['grant']!=grant):
                    raise PolicyReviewError('策略审核计算期间原证据或草稿发生变化。')
                active.interval.remaining_seconds()
                path=runtime/'reviewed/policy_reviews'/(review['id']+'.json')
                from workbench.restore_execution_policy import require_restored_execution_paths
                require_restored_execution_paths(path)
                if os.path.lexists(path):
                    if json.loads(read_project_bytes(path.parent,path.name))!=review:
                        raise PolicyReviewError('原策略审核凭据不同，不能覆盖。')
                else:atomic_create_json(path,review)
            return {'policy_review':review,'preparation':prepared,'quality':quality,'quality_disposition':disposition,
                'review_source':'policy','publication_authorized':False,'execution_permit':False}
