"""Collect declared file references in persisted job/run metadata.

Do not search prose for paths or grant access to a root named by a record.
Resolution must be against the caller's already inventoried, approved roots.
Unknown reference semantics remain a certification blocker.
"""
from pathlib import Path
import re
from core.schema import validate_schema, SchemaValidationError


_FILES={'path','checkpoint_path','draft_path','review_path','validation_report_path','manual_draft_path',
    'outline_manifest_path','candidate_result_path','commit_marker','receipt_path','source_path',
    'outline_path','previous_prose_path','canonical_path','summary_path','index_path','snapshot_path',
    'memory_path','output_path','progress_path','run_record_path','prose_path','candidate_artifact_path'}
_DIRECTORIES={'journal_path','root','story_project_root','memory_root','runtime_root','run_dir',
    'artifacts_dir','chapter_dir','persistence_dir','event_store'}
_LOGICAL={'field_path','json_path','relative_path','original_path_hint','path_hint'}
_RELATIVE_BOOK={'outline_manifest_path','source_path','outline_path','previous_prose_path','run_record_path','candidate_artifact_path'}
_OPTIONAL_WORKSPACE_MARKERS={'active_book','deployment_marker','story_setup_skill','codex_hooks',
    'codex_hook_adapter','agents_story_routing','story_agents','quality_script','package_scripts'}


class ReferenceRoots:
    """Resolve declared paths by ancestors, preserving inventory precedence.

    This index never opens paths and grants no new roots. Path objects preserve
    the host's case/drive semantics; string-prefix matches would be unsafe.
    """
    def __init__(self, bases):
        self.entries = {}
        for order, (key, base) in enumerate(bases.items()):
            self.entries.setdefault(base, (order, key, base))

    def find(self, path):
        matches = (self.entries[parent] for parent in (path, *path.parents) if parent in self.entries)
        found = min(matches, default=None)
        return None if found is None else found[1:]


def _compatibility_diagnostic(value, label, root, require_path):
    """Recognize the non-executing oh-story detector, never arbitrary metadata.

    Keep unknown detail fields for the ordinary reference walker. Only the
    detector's documented optional workspace probes are non-dependencies;
    present book directories still require ordinary inventory membership.
    """
    if not label.endswith('/story_project/oh_story'):
        return value
    try:
        validate_schema(value, 'oh_story_compatibility.schema.json')
    except SchemaValidationError:
        return value
    if (value['capabilities']['oh_story_js_execution'] is not False
            or value['capabilities']['oh_story_provider'] is not False
            or value['root'] is None or Path(value['root']) != root):
        return value
    result = {k: v for k, v in value.items() if k != 'workspace_root'}
    markers = []
    for index, marker in enumerate(value['markers']):
        kind = marker['kind']
        if marker['required'] or kind not in _OPTIONAL_WORKSPACE_MARKERS | {'story_project_core_dir'}:
            markers.append(marker)
            continue
        details = marker['details']
        expected_scope = 'story_project' if kind == 'story_project_core_dir' else 'workspace'
        if details.get('scope') != expected_scope:
            markers.append(marker)
            continue
        if kind == 'story_project_core_dir' and marker['present']:
            require_path(marker['path'], label + '/markers/' + str(index) + '/path', kind='directory')
        # These are observed detector search locations, never executable inputs.
        known_hints = {'existing_paths', 'candidate_paths', 'resolved_path'}
        markers.append({k: ({d: v for d, v in details.items() if d not in known_hints}
                            if k == 'details' else v)
                        for k, v in marker.items() if k != 'path'})
    result['markers'] = markers
    return result


def inspect_metadata_references(value,*,source,book_root,require_path,block,root_bindings=None,
                                max_nodes=100000,max_depth=80,lazy_calls=None):
    """Inspect structured declarations, including nested arrays and outboxes.

    require_path(value, source, relative_to_book=..., kind=..., expected_sha256=..., follow_json=...)
returns a verified reference or None. This walker never opens declared paths.
"""
    if type(max_nodes) is not int or max_nodes<1 or type(max_depth) is not int or max_depth<1:
        raise ValueError('invalid metadata traversal limits')
    root=Path(book_root);bindings=root_bindings or {};remaining=max_nodes
    stack=[(value,source,None,0)]
    while stack:
        current,label,journal,depth=stack.pop();remaining-=1
        if remaining<0 or depth>max_depth:
            block('metadata_reference_limits_exceeded',source);return
        if isinstance(current,list):
            if len(current)>remaining:
                block('metadata_reference_limits_exceeded',source);return
            stack.extend((item,label+'/'+str(i),journal,depth+1) for i,item in enumerate(current))
            continue
        if not isinstance(current,dict):continue
        current = _compatibility_diagnostic(current, label, root, require_path)
        if lazy_calls is not None and lazy_calls.accepts(current, label):
            current = {k: v for k, v in current.items() if k != 'model_calls_ref'}
        if len(current)>remaining:
            block('metadata_reference_limits_exceeded',source);return
        if 'root_id' in current and 'relative_path' in current:
            root_id=current['root_id'];relative=current['relative_path'];root_uuid=current.get('root_uuid')
            if (not isinstance(root_id,str) or not isinstance(relative,str) or not relative
                    or root_uuid is not None and not isinstance(root_uuid,str)):
                block('invalid_logical_file_reference',label);continue
            possible=bindings.get((root_id,root_uuid),())
            if len(possible)!=1:
                block('unresolved_logical_reference_root',label);continue
            path=Path(relative)
            if path.is_absolute() or path.drive or relative in {'.','..'} or '..' in path.parts:
                block('invalid_logical_file_reference',label);continue
            require_path(str(Path(next(iter(possible)))/path),label)
            continue
        if 'journal_path' in current:
            raw=current['journal_path']
            journal=Path(raw) if isinstance(raw,str) and Path(raw).is_absolute() else None
        for key,item in current.items():
            pointer=label+'/'+key
            if key in _LOGICAL or item is None:continue
            if (key == 'path' and current.get('kind') == 'committed_chapter'
                    and set(current) == {'kind', 'path'}
                    and re.search(r'/memory_v2/source_index/[^/]+$', label)
                    and isinstance(item, str) and re.fullmatch(r'chapter:[1-9][0-9]*', item)):
                # Memory's source index uses a logical chapter coordinate. The
                # actual book files/history are inventoried and checked apart.
                continue
            if key in {'staged_path','backup_path'}:
                if not isinstance(item,str) or journal is None or Path(item).is_absolute() or Path(item).drive or '..' in Path(item).parts:
                    block('unresolved_journal_file_reference',pointer)
                else:require_path(str(journal/item),pointer)
            elif key in _FILES or key in _DIRECTORIES:
                # Rollback can intentionally remove a newly created target.
                # Its before/staged evidence is checked by the history verifier.
                if key=='path' and current.get('status')=='rolled_back' and current.get('existed') is False:continue
                expected=current.get('sha256') if key=='path' and current.get('format') in {'json','markdown','text'} else None
                require_path(item,pointer,relative_to_book=key in _RELATIVE_BOOK,
                    kind='directory' if key in _DIRECTORIES else 'file',expected_sha256=expected,
                    follow_json=key=='path' and current.get('format')=='json')
            elif key in {'provenance_artifact_ref','model_calls_ref','artifact_ref'}:
                base=root/'.novelagent/runtime'
                if key!='artifact_ref':base=base/'runs'
                if not isinstance(item,str) or Path(item).is_absolute() or Path(item).drive or '..' in Path(item).parts:
                    block('invalid_runtime_artifact_reference',pointer)
                else:require_path(str(base/item),pointer,kind='directory' if key=='model_calls_ref' else 'file')
            elif key.endswith('_ref') and isinstance(item,dict):
                if not {'root_id','relative_path'}<=set(item):
                    block('unclassified_metadata_reference',pointer)
                else:stack.append((item,pointer,journal,depth+1))
            elif isinstance(item,(dict,list)):
                stack.append((item,pointer,journal,depth+1))
            elif isinstance(item,str) and key.endswith(('_path','_paths','_ref','_dir','_root')):
                block('unclassified_metadata_reference',pointer)
