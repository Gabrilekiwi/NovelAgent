"""Immutable supported historical policy codecs, independent of latest config.

Add a new version/codec when behavior changes; never edit an old snapshot to
make an archived policy review look current. These are compatibility checks,
not permission to start new work under an obsolete grant.
"""
import copy
from core.memory_v2.canonical import canonical_json_hash


ACTION_POLICY_V1={'schema_version':'reviewed_quality_action_policy_v1',
    'scope':'automatic_admission_advice','max_repair_cycles':1,
    'repairable_codes':['duplicate_ending_hook','duplicate_opening','duplicate_scene_text','near_duplicate_scene_text'],
    'related_codes':['repetition_or_stalling'],'advisory_codes':['chapter_length_reasonable'],
    'semantic_review_scope':{'schema_version':'short_prose_repetition_scope_v1','minimum_normalized_chars':32,
        'maximum_normalized_chars':59,'minimum_similarity':0.92,'maximum_candidates':64,'maximum_pairs':8,
        'exact_line_integrity_minimum':36},
    'repair_producers':['deterministic_review','final_artifact_integrity'],
    'repair_requires':['exact_final_prose_ranges','complete_quality_coverage','valid_authoritative_state',
        'durable_cycle_claim','chapter_budget','full_fact_reextraction_and_recheck'],
    'unknown_or_unquoted_semantic_findings':'pause'}
STANDARD_POLICY_V1={'name':'standard','version':'1.0','threshold':'needs_revision','max_repair_attempts':2,
    'include_review':True,'llm_validator_required':False,
    'producers':['base_validation','blueprint_coverage','final_artifact_integrity','deterministic_review','narrative_rules']}


def _hash(value):return canonical_json_hash(value,exclude_environment_fields=False)


def validate_action_policy_snapshot(value):
    if value!=ACTION_POLICY_V1 or _hash(value)!=_hash(ACTION_POLICY_V1):
        raise ValueError('unsupported historical reviewed action policy snapshot')
    return copy.deepcopy(ACTION_POLICY_V1)


def validate_standard_policy_snapshot(value):
    if value!=STANDARD_POLICY_V1 or _hash(value)!=_hash(STANDARD_POLICY_V1):
        raise ValueError('unsupported historical reviewed quality policy snapshot')
    return copy.deepcopy(STANDARD_POLICY_V1)
