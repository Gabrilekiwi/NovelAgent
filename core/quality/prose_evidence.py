"""Exact, bounded Unicode ranges tied to one complete final prose hash."""
import hashlib
from difflib import SequenceMatcher
import re


SHORT_REPETITION_REVIEW = {'schema_version':'short_prose_repetition_scope_v1',
    'minimum_normalized_chars':32,'maximum_normalized_chars':59,
    'minimum_similarity':0.92,'maximum_candidates':64,'maximum_pairs':8,
    'exact_line_integrity_minimum':36}


def prose_excerpt(text, *, start_char=0, end_char=None):
    if not isinstance(text,str):raise ValueError('prose evidence requires text')
    end_char=min(len(text),start_char+512) if end_char is None else end_char
    if (type(start_char) is not int or type(end_char) is not int
            or not 0<=start_char<end_char<=len(text) or end_char-start_char>512):
        raise ValueError('invalid prose evidence range')
    return {'schema_version':'quality_prose_excerpt_v1',
        'final_prose_sha256':hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'start_char':start_char,'end_char':end_char,'quote':text[start_char:end_char]}


def validate_prose_excerpt(value,text):
    if not isinstance(value,dict) or set(value)!={'schema_version','final_prose_sha256','start_char','end_char','quote'}:
        raise ValueError('invalid prose evidence fields')
    expected=prose_excerpt(text,start_char=value['start_char'],end_char=value['end_char'])
    if value!=expected:raise ValueError('prose evidence differs from the final text')
    return expected


def review_short_prose_repetition(text):
    """Bounded semantic suspicion; never a repair or publication verdict.

    Existing integrity checks intentionally exclude normalized near-duplicate
    units below 60 characters. This separately records shorter suspicious
    pairs for human review without changing that gate or its historical data.
    """
    if not isinstance(text,str):raise ValueError('prose review requires text')
    scope=SHORT_REPETITION_REVIEW
    candidates=[];complete=True
    for match in re.finditer(r'[^\n]+',text):
        normalized=re.sub(r'[\W_]+','',match.group()).casefold()
        if not scope['minimum_normalized_chars']<=len(normalized)<=scope['maximum_normalized_chars']:continue
        if len(match.group())>512 or len(candidates)>=scope['maximum_candidates']:
            complete=False;continue
        candidates.append((match.start(),match.end(),match.group(),normalized))
    pairs=[]
    for index,left in enumerate(candidates):
        for right in candidates[index+1:]:
            # The exact-line gate counts normalized characters and excludes
            # short dialogue. Raw length alone would silently skip risks.
            if (left[3]==right[3] and len(left[3])>=scope['exact_line_integrity_minimum']
                    and not re.match(r'^[“"「『]',left[2].strip())
                    and not re.match(r'^[“"「『]',right[2].strip())):continue
            similarity=SequenceMatcher(None,left[3],right[3],autojunk=False).ratio()
            if similarity<scope['minimum_similarity']:continue
            if len(pairs)>=scope['maximum_pairs']:
                complete=False;break
            pairs.append({'similarity':round(similarity,6),
                'left':prose_excerpt(text,start_char=left[0],end_char=left[1]),
                'right':prose_excerpt(text,start_char=right[0],end_char=right[1])})
        if len(pairs)>=scope['maximum_pairs'] and not complete:break
    return {'schema_version':'reviewed_short_prose_repetition_v1',
        'scope':dict(SHORT_REPETITION_REVIEW),
        'final_prose_sha256':hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'scope_complete':complete,'candidate_count':len(candidates),'pairs':pairs}
