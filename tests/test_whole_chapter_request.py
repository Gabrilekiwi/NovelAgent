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
