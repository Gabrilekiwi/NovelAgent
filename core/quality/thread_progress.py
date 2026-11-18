"""Local evidence of a Chinese story thread being acted on (not proof of resolution)."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

_CJK = re.compile(r"[\u3400-\u9fff]")
_WORD = re.compile(r"[\u3400-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_-]{1,}")
# Split at grammatical boundaries before comparing: e.g. a question contains
# a subject and an object, even though neither is delimited by whitespace.
_BOUNDARY = re.compile(
    r"为什么|怎么会|怎么样|怎么办|未解决|尚未|仍然|究竟|到底|是否|为何|如何|怎么|何时|"
    r"哪里|什么|哪个|哪位|是谁|能否|拥有|知道|必须|需要|之间|以及|或者|的|与|和|把|被|谁"
)
_SENTENCE = re.compile(r"[^。.!！?？；;\r\n]+[。.!！?？；;]?")
_CLAUSE = re.compile(r"[^，,：:。.!！?？；;“”「」\r\n]+")
_CONDITIONAL = re.compile(r"如果|假如|假设|倘若|要是|一旦|也许|或许|说不定|可能会|很可能|就能|就会")
_UNASSERTED = re.compile(r"为何|为什么|如何|是否|能否|怎么|究竟|难道|谁|不知道|不清楚|没想通|想不通|没弄明白|打算|准备去|计划去|猜测|怀疑|可能|(?:没有|并未|未能|没能|还没)(?:查|找|发现|确认|证实|验|测|读|看到)")
# Observable findings and result verbs. A bare name, setting or past-tense
# particle is not evidence. Unrecognised paraphrases stay reviewable.
_OBSERVATION = re.compile(
    r"显示|证实|记载|记录着|写着|刻着|印着|留下|发现|确认|查明|测得|刷卡|签收|交给|移交|解锁|"
    r"(?:查|找|搜|翻|挖|验|测|读|认|辨|取|拆|敲|叫|喊|听|拍|交|调|送|收|推|撬)"
    r"(?:到了?|出了?|开了?|走了?|回了?|下了?)"
)


# When a thread asks about a particular relation, co-locating the person and
# place isn't enough. Check for evidence about that relation in the same clause.
_RELATIONS = (
    (re.compile(r"权限|通行权|门禁|准入"), re.compile(r"权限|通行|门禁|准入|授权|放行|刷卡|验卡|工牌|门卡|通行证|解锁|闸机")),
    (re.compile(r"真实身份|真名|化名"), re.compile(r"身份|真名|本名|化名|姓名|签名|署名|指纹|户籍")),
)


# A timed plan can advance by becoming impossible. The second clause must
# refer back to that plan; a broken object or a new speaker is not a verdict.
_PLAN_OUTCOME = re.compile(
    r"(?:原定|原先|原本|预定|既定)(?P<plan>[^，,。!?！？；;]{2,80})[，,]\s*"
    r"(?:(?:这个|这项|这套|该)(?:计划|方案|安排))?(?:从(?:这里|此处|那一刻|此刻))?"
    r"(?:就|也|已经|彻底|正式|不得不|只能|被|再也)*"
    r"(?P<result>行不通了?|作废了?|取消了?|落空了?|泡汤了?|中止了?|终止了?|断了)[。.!！]?\s*$"
)
_DURATION = re.compile(r"[零〇一二两三四五六七八九十百千万0-9]+(?:个?小时|分钟|秒钟?|天)")
_OBJECT = re.compile(r"(?:开|关|修|拆|换|守|封|运|送|取)([\u3400-\u9fff]{1,5})$")


def _changed_plan(sentence: str, sources: list[str]) -> dict | None:
    match = _PLAN_OUTCOME.search(sentence.rstrip('”」'))
    if not match or _UNASSERTED.search(sentence):
        return None
    plan = match['plan']
    duration, target = _DURATION.search(plan), _OBJECT.search(plan)
    if not duration or not target:
        return None
    for source in sources:
        for clause in _CLAUSE.findall(source):
            if duration.group() in _DURATION.findall(clause) and target[1] in clause and match['result'] not in source:
                return {"thread": source, "matched_terms": [duration.group(), target[1]],
                        "observed_action": match['result'], "witness": match.group().strip()}
    return None


def is_chinese_thread(source: str) -> bool:
    return bool(_CJK.search(source))


def _anchors(parts: list[str], clause: str) -> list[str]:
    # A fragment may not count twice (e.g. 封闭 and 封闭区 at the same spot).
    hits = []
    for part in parts:
        if part in clause:
            start, size = clause.index(part), len(part)
        else:
            match = SequenceMatcher(None, part, clause, autojunk=False).find_longest_match()
            start, size = match.b, match.size
        if size >= 2:
            hits.append((start, start + size))
    selected = []
    for start, end in sorted(hits, key=lambda span: span[1] - span[0], reverse=True):
        if all(end <= a or b <= start for a, b in selected):
            selected.append((start, end))
    return [clause[a:b] for a, b in sorted(selected)]


def chinese_thread_witness(text: str, sources: list[str], cast: set[str]) -> dict | None:
    topics = []
    for source in dict.fromkeys(sources):
        parts = list(dict.fromkeys(_WORD.findall(_BOUNDARY.sub(" ", source))))
        if parts:
            label = len(parts) == 1 and source.strip() == parts[0] and 2 <= len(parts[0]) <= 8
            topics.append((source, parts, label, [evidence for topic, evidence in _RELATIONS if topic.search(source)]))
    if not topics:
        return None
    for sentence in _SENTENCE.finditer(text):
        unit = sentence.group()
        if unit.endswith(("?", "？")) or _CONDITIONAL.search(unit):
            continue
        plan = _changed_plan(unit, sources)
        if plan:
            return plan
        for fragment in _CLAUSE.finditer(unit):
            clause = fragment.group().strip()
            action = _OBSERVATION.search(clause)
            if not action or _UNASSERTED.search(clause):
                continue
            for source, parts, label, relations in topics:
                if relations and not any(relation.search(clause) for relation in relations):
                    continue
                if clause in source or (not label and source.strip(" 。!！?？") in clause):
                    continue  # Prose which merely embeds the remembered assertion.
                hits = _anchors(parts, clause)
                if not hits or all(h in cast for h in hits):
                    continue
                if len(hits) < 2 and not (label and parts[0] in hits and clause != parts[0]):
                    continue
                return {"thread": source, "matched_terms": hits, "observed_action": action.group(),
                        "witness": clause}
    return None
