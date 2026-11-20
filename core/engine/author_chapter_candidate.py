"""Immutable, source-bound import of author supplied prose; no model runtime."""
import copy
import json
from datetime import datetime, timezone

from core.engine.chapter_candidate import (
    AUTHOR_CANDIDATE_VERSION, ChapterCandidateStore, ChapterCandidateConflictError,
    _author_candidate_id, _authority_binding, _candidate_request_hash, _record_hash,
    _sha256_text, _create_text_idempotent, _create_json_idempotent, _validate_candidate_intent,
)
from core.engine.provider_safety import provider_execution_lock, list_unresolved_provider_calls


def import_author_candidate(*, root, chapter, confirmation, text, origin_label, idempotency_key, builder):
    if not isinstance(text, str) or not text.strip() or len(text) > 200000:
        raise ChapterCandidateConflictError('请输入 1 至 200000 字的已有稿件。')
    if not isinstance(origin_label, str) or not origin_label.strip() or len(origin_label) > 200:
        raise ChapterCandidateConflictError('请提供简短的稿件来源说明。')
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    store = ChapterCandidateStore(root)
    with provider_execution_lock(store.paths.run_dir):
        if list_unresolved_provider_calls(store.paths.run_dir):
            raise ChapterCandidateConflictError('请先处理未结束的模型请求。')
        trusted = builder.build(chapter)
        context = trusted.story_project_context
        if context['project_identity']['schema_version'] != '3.0':
            raise ChapterCandidateConflictError('导入已有稿件需要先完成本书的事实基线接入。')
        if confirmation['context_sha256'] != trusted.context_sha256:
            raise ChapterCandidateConflictError('稿件导入前，本章故事状态已变化。')
        read_set = copy.deepcopy(context['read_set'])
        identity = builder.validate_identity(context['project_identity'])
        authority = _authority_binding(identity, read_set)
        source = dict(story_project_read_set=read_set, context_digest=read_set['context_digest'],
            membership_fingerprint=read_set['membership_fingerprint'], context_builder_version='1.0',
            runtime_cache_policy='bypass', context_sha256=trusted.context_sha256,
            authority_projection_sha256=trusted.authority_projection_sha256, generation_state_view_sha256=None)
        metadata = dict(prose_sha256=_sha256_text(text), prose_chars=len(text), origin_label=origin_label.strip())
        input_text = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        author = {k:confirmation[k] for k in ('schema_version','card_sha256','context_sha256','confirmation_sha256')}
        key_hash = _sha256_text(idempotency_key)
        candidate_id = _author_candidate_id(book_id=identity.book_id, chapter_index=chapter, idempotency_key_sha256=key_hash)
        request_hash = _candidate_request_hash(book_id=identity.book_id, chapter_index=chapter,
            input_sha256=_sha256_text(input_text), source=source, authority=authority,
            author_confirmation=author, chapter_intent=confirmation['chapter_intent'])
        now = datetime.now(timezone.utc).isoformat()
        intent = dict(schema_version=AUTHOR_CANDIDATE_VERSION, record_type='chapter_candidate_intent',
            candidate_id=candidate_id, book_id=identity.book_id, chapter_index=chapter, created_at=now,
            idempotency_key_sha256=key_hash, request_sha256=request_hash,
            input=dict(artifact_ref=f'candidates/{candidate_id}/input.md',sha256=_sha256_text(input_text),chars=len(input_text)),
            source=source, authority=authority, author_confirmation=author,
            chapter_intent=confirmation['chapter_intent'], execution={'execution_kind':'author_import'}, author_import=metadata)
        intent['intent_hash'] = _record_hash(intent,'intent_hash')
        if store.has_intent(candidate_id):
            intent = store.load_intent(candidate_id)
            if intent['schema_version'] != AUTHOR_CANDIDATE_VERSION or intent['request_sha256'] != request_hash:
                raise ChapterCandidateConflictError('同一导入请求的稿件或故事状态已变化，请重新核对。')
        verify = lambda: builder.verify_binding(chapter_index=chapter, source=source, authority=authority, phase='prepare')
        before = verify()
        if store.has_record(candidate_id):
            return store.load_record(candidate_id)
        _validate_candidate_intent(intent)
        _create_text_idempotent(store.candidate_dir(candidate_id)/'input.md',input_text)
        _create_json_idempotent(store.intent_path(candidate_id), intent, contract='author draft import')
        prose = store.publish_draft(candidate_id,text)
        after = verify()
        record = {k:copy.deepcopy(intent[k]) for k in ('schema_version','candidate_id','book_id','chapter_index',
            'intent_hash','request_sha256','input','authority','execution')}
        record.update(record_type='chapter_candidate_record', status='completed', started_at=intent['created_at'],
            finished_at=now, source_verification={'before':before,'after':after},prose=prose,
            evidence={'intent_hashes':[],'receipt_hashes':[],'receipt_statuses':[], 'unresolved_calls':[],
                'selected_response':None,'budget':{'provider_calls':0,'billing':'not_applicable'}},error=None)
        record['record_hash'] = _record_hash(record,'record_hash')
        return store.publish_record(record)
