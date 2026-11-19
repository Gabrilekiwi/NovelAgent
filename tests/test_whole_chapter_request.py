"""Test the complete whole-chapter messages at the last offline boundary."""
import json

import pytest

from core.context_budget import ContextBudget, ContextBudgetError
from modules.chapter_generator import generator


def test_whole_chapter_fits_protocol_and_keeps_reviewed_evidence(monkeypatch):
    budget = ContextBudget(provider='openai', model='writer', model_context_window=32000, max_input_tokens=6300)
    prompt = 'writing rules. ' * 165
    packet = {'chapter': 4, 'facts': [{'summary': 'intervals differ', 'quotes': ['first line\n\nsecond line']}]}
    section = '# Previous Chapter Confirmed Facts\n' + json.dumps(packet)
    source = '# Director Decision\ncontinue\n\n' + section + '\n\n# Timeline\n' + 'earlier history. ' * 238
    monkeypatch.setattr(generator, 'default_context_budget', lambda: budget)
    monkeypatch.setattr(generator, '_load_prompt', lambda: prompt)
    sent = []
    def capture(messages, **kwargs):
        sent.append(messages)
        return 'The scout stepped back before the second alarm and carried the notebook home.'
    monkeypatch.setattr(generator, 'chat_completion', capture)
    generator.generate_chapter(source)
    system, body = sent[0]
    assert system['content'] == prompt and section in body['content'] and '# Timeline\n' not in body['content']
    assert budget.measure(body['content'], stage='chapter_generation', protocol_texts=(prompt,))['within_budget']
    # When even the confirmed facts cannot fit, never send a partially informed request.
    with pytest.raises(ContextBudgetError):
        generator.generate_chapter(source.replace('intervals differ', 'binding fact ' * 950))
    assert len(sent) == 1


def test_author_settings_survive_a_large_blueprint_or_no_request_is_sent(monkeypatch):
    opening = '# Author Confirmed Opening Settings\n' + json.dumps(['The night watch belongs to Lin.'])
    blueprint = {'chapter_blueprint': {'title': 'Stairs'}, 'read_set_context_digest': 'a'*64,
                 'extra_context': ['optional paragraph ' * 120 for _ in range(8)]}
    source = '# StoryProject Chapter Blueprint\n' + json.dumps(blueprint) + '\n\n' + opening
    messages, _ = generator.prepare_chapter_messages(source)
    assert opening in messages[-1]['content']
    monkeypatch.setattr(generator, 'default_context_budget', lambda: ContextBudget(provider='openai', model='writer', model_context_window=32000, max_input_tokens=6300))
    monkeypatch.setattr(generator, 'chat_completion', lambda *a, **kw: pytest.fail('must stop before dispatch'))
    with pytest.raises(ContextBudgetError):
        generator.generate_chapter(source.replace('The night watch belongs to Lin.', 'binding setting ' * 10000))


def test_chapter_quote_matches_persisted_reservation_not_just_message_text(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import openai
    from api import openai_client
    from core.model_call_runtime import ModelCallRuntimeContext
    from core.model_calls import ModelCallStore

    for key, value in {'OPENAI_BASE_URL':'https://fixture.invalid/v1','OPENAI_API_KEY':'fixture',
                       'OPENAI_MODEL':'fixture-writer','OPENAI_STREAM':'0','PROVIDER_MAX_ATTEMPTS':'1'}.items():
        monkeypatch.setenv(key,value)
    item = {'quotes':['门下传来\n\n回声。', '  她听见了。'], 'state':{'where':'窄梯'}}
    raw = '# Authoritative State\n' + json.dumps(item,ensure_ascii=False,indent=2) + '\n\n# Requirements\nkeep this:  x'
    messages, _ = generator.prepare_chapter_messages(raw)
    payload = messages[1]['content'].split('# Authoritative State\n')[1].split('\n#')[0]
    assert json.loads(payload)==item and 'keep this:  x' in messages[1]['content']
    quote = openai_client.estimate_chapter_request(messages)
    store = ModelCallStore(tmp_path/'calls'); runtime = ModelCallRuntimeContext(store)
    received=[]
    def receive(**request):
        received.append(request)
        return SimpleNamespace(id='stub',model='fixture-writer', usage=None,
            choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content='only local transport'))])
    monkeypatch.setattr(openai,'OpenAI',lambda **_:SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=receive))))
    openai_client.chat_completion(messages,stage='chapter_generation',model_call_runtime=runtime,call_id='estimate-parity')
    assert len(received)==1 and received[0]['messages']==messages
    assert store.load_intent('estimate-parity-a1')['budget_reservation']['reserved_input_tokens']==quote


def test_long_response_path_survives_guarded_read_and_candidate_selection(tmp_path):
    from core.local_file_io import local_io_path
    from core.model_calls import ModelCallStore
    from core.model_call_runtime import ModelCallRuntimeContext
    from core.engine.chapter_candidate import _successful_candidate_responses
    from core.story_project.reviewed_state_document import read_project_bytes
    from api.contracts import ModelResponse

    root = tmp_path
    while len(str(root)) < 195:
        root /= 'nested-book-location'
    store=ModelCallStore(root/'model_calls'); runtime=ModelCallRuntimeContext(store)
    text='第三阶的铜哨不见了。'; call='chapter-generation-'+('a'*61)
    runtime.execute_attempt(call_id=call,attempt_number=1,provider='openai',model='fixture',stage='chapter_generation',
        endpoint_type='openai_compatible',request={'messages':[]},max_output_tokens=64,operation=lambda:ModelResponse(text))
    artifact='responses/'+call+'-a1.txt'
    assert len(str(store.root/artifact))>260 and local_io_path(store.root/artifact).is_file()
    assert read_project_bytes(store.root,artifact)==text.encode() and _successful_candidate_responses(store)[0][1]==text
