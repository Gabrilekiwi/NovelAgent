"""Collect declared file references in persisted job/run metadata.

Do not search prose for paths or grant access to a root named by a record.
Resolution must be against the caller's already inventoried, approved roots.
Unknown reference semantics remain a certification blocker.
"""
from pathlib import Path


_FILES={'path','checkpoint_path','draft_path','review_path','validation_report_path','manual_draft_path',
    'outline_manifest_path','candidate_result_path','commit_marker','receipt_path','source_path',
    'outline_path','previous_prose_path','canonical_path','summary_path','index_path','snapshot_path',
    'memory_path','output_path','progress_path','run_record_path'}
_DIRECTORIES={'journal_path','root','story_project_root','memory_root','runtime_root','run_dir',
    'artifacts_dir','chapter_dir','persistence_dir','event_store'}
_LOGICAL={'field_path','json_path','relative_path','original_path_hint','path_hint'}
_RELATIVE_BOOK={'outline_manifest_path','source_path','outline_path','previous_prose_path'}


def inspect_metadata_references(value,*,source,book_root,require_path,block,root_bindings=None,
                                max_nodes=100000,max_depth=80):
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
