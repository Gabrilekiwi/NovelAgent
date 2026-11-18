"""Recognize a legacy execution's not-yet-created call directory.

Executor persists provenance before its first call. ModelCallStore only creates
the call directory when there is an attempt. Absence is allowed only for that
exact layout with a zero-attempt final summary and verified provenance. This
does not settle costs; physical intents and budgets are checked separately.
"""
import re

from core.execution_provenance import validate_execution_provenance


class LazyModelCallReferences:
    def __init__(self, evidence):
        self.evidence = evidence
        self.recognized = []

    def accepts(self, value, label):
        if not re.fullmatch(r'story_project/\.novelagent/runtime/runs/[^/]+\.json/run/execution_evidence', label):
            return False
        execution = value.get('execution_id')
        if not isinstance(execution, str) or not re.fullmatch(r'execution_[a-zA-Z0-9_-]{1,180}', execution):
            return False
        base = 'executions/' + execution
        if (value.get('model_calls_ref') != base + '/model_calls'
                or value.get('provenance_artifact_ref') != base + '/provenance.json'):
            return False
        budget = value.get('budget')
        counters = ('provider_calls', 'unsettled_attempt_count', 'total_input_tokens',
                    'total_output_tokens', 'charged_output_tokens', 'reserved_output_tokens')
        if (not isinstance(budget, dict)
                or any(type(budget.get(k)) is not int or budget[k] != 0 for k in counters)
                or type(budget.get('estimated_cost')) not in (int, float) or budget['estimated_cost'] != 0):
            return False
        relative = '.novelagent/runtime/runs/' + base
        call_path = relative + '/model_calls'
        evidence = self.evidence
        if (relative not in evidence.directories or call_path in evidence.files | evidence.directories
                or any(p.startswith(call_path + '/') for p in evidence.files | evidence.directories)):
            return False
        try:
            source_path=label[len('story_project/'):].removesuffix('/run/execution_evidence')
            run=evidence.read(source_path).get('run',{})
            if (run.get('status') not in {'failed','committed'} or run.get('execution_evidence') != value
                    or run.get('id') != source_path.rsplit('/',1)[-1][:-5]):
                return False
            provenance = validate_execution_provenance(evidence.read(relative + '/provenance.json'))
            if provenance['provenance_hash'] != value.get('provenance_hash'):
                return False
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            return False
        item = {'source': label, 'path': call_path, 'execution_id': execution,
                'provenance_sha256': evidence.tree['files'][relative + '/provenance.json']['sha256'],
                'classification': 'legacy_zero_attempt_lazy_directory'}
        if item not in self.recognized:
            self.recognized.append(item)
        return True
