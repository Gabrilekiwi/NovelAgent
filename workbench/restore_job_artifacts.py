"""Resolve immutable imported artifacts from recorded copies, never ancestors.

The caller must validate the current restored database. Stored job paths cannot
grant roots; mappings come only from the qualified restore plan and copy list.
"""
from pathlib import Path

from workbench.backup_job_state import first_boundary_pause
from workbench.backup_restore_provenance import validate_restore_provenance, RestoreProvenanceError


class RestoredJobArtifacts:
    def __init__(self, root, book_id, provenance):
        self.root=Path(root);self.lineages=[];self.artifacts={};self.imported=set()
        current=self.root
        while provenance is not None:
            previous=validate_restore_provenance(provenance,root=current,book_id=book_id)
            plan=provenance['plan'];copied=provenance['materialization']['copied']
            mappings={item['root']:item for item in plan['root_plan']['roots']}
            originals={item['id']:item['original'] for item in provenance['jobs_intent']['history']
                if item['original']['project_id']==provenance['project_id']}
            if not self.lineages:self.imported=set(originals)
            descriptors={}
            for key,mapping in mappings.items():
                if key=='story_project':continue
                if (not key.startswith('jobs/') or key[5:] not in originals
                        or Path(mapping['destination'])!=current.parent/'jobs'/key[5:]):
                    raise RestoreProvenanceError('历史任务产物根不属于原目标任务。')
                descriptors[key[5:]]=(key,'.')
            runtime=Path(plan['root_plan']['task_runtime_root'])
            if runtime.is_relative_to(current):
                for identity in originals:
                    relative=(runtime/identity).relative_to(current).as_posix()
                    if 'book/'+relative in copied['directories']:
                        if identity in descriptors:raise RestoreProvenanceError('历史任务产物根重复。')
                        descriptors[identity]=('story_project',relative)
            for identity,descriptor in descriptors.items():
                for retained in self.lineages:
                    known=retained['artifacts'].get(identity)
                    if known is None and descriptor[0]=='story_project':
                        # A later archive's runtime is fresh, while its whole
                        # book copy still contains the original internal job.
                        # Require this directory in every intervening copy;
                        # validate_trees checks each full subtree afterward.
                        if (identity not in retained['originals']
                                or 'book/'+descriptor[1] not in retained['copied']['directories']):
                            raise RestoreProvenanceError('历史项目内任务根在中间复制中缺失。')
                        retained['artifacts'][identity]=descriptor
                    elif known!=descriptor:
                        raise RestoreProvenanceError('历史任务产物在新复制计划中缺失或改变根。')
            if not self.lineages:self.artifacts=descriptors
            self.lineages.append({'proof':provenance,'previous':previous,'copied':copied,
                'mappings':mappings,'originals':originals,'artifacts':descriptors})
            current=previous;provenance=plan.get('source_restore_provenance')

    def target(self, identity):
        descriptor=self.artifacts.get(identity)
        if descriptor is None:return None
        key,name=descriptor
        base=self.root if key=='story_project' else Path(self.lineages[0]['mappings'][key]['destination'])
        return base if name=='.' else base/name

    def first_pause(self, identity):
        # A cancelled imported row is not itself evidence of an expected absence.
        for lineage in reversed(self.lineages):
            original=lineage['originals'].get(identity)
            if original is None:continue
            events=[row for row in lineage['proof']['jobs_intent'].get('event_history',[])
                if row['job_id']==identity]
            event=max(events,key=lambda row:row['sequence']) if events else None
            return first_boundary_pause(original,event)
        return False

    def validate_trees(self, inventories):
        for key in inventories:
            if key.startswith('jobs/') and key[5:] in self.imported and self.artifacts.get(key[5:])!=(key,'.'):
                raise RestoreProvenanceError('原任务没有此外部产物根的复制证据。')
        for lineage in self.lineages:
            for identity,(key,name) in lineage['artifacts'].items():
                tree=inventories.get(key)
                if tree is None:raise RestoreProvenanceError('原复制任务产物根缺失。')
                base=self.root if key=='story_project' else self.target(identity)
                if Path(tree['source_root'])!=base:
                    raise RestoreProvenanceError('原复制任务产物根与当前范围不同。')
                destination=Path(lineage['mappings'][key]['destination'])
                container=Path(lineage['proof']['plan']['root_plan']['container'])
                prefix=destination.relative_to(container).as_posix()
                if name!='.':prefix+='/'+name
                copied=lineage['copied']
                expected={path[len(prefix)+1:]:record for path,record in copied['files'].items()
                    if path.startswith(prefix+'/')}
                expected_dirs={path[len(prefix)+1:] for path in copied['directories'] if path.startswith(prefix+'/')}
                if prefix not in copied['directories']:
                    raise RestoreProvenanceError('历史任务根没有原复制目录证据。')
                local_prefix='' if name=='.' else name+'/'
                actual={path[len(local_prefix):]:{k:record[k] for k in ('size','sha256')}
                    for path,record in tree['files'].items() if path.startswith(local_prefix)}
                actual_dirs={path[len(local_prefix):] for path in tree['directories'] if path.startswith(local_prefix)}
                if (actual!=expected or actual_dirs!=expected_dirs
                        or name!='.' and name not in tree['directories']
                        or any(item['path'].startswith(local_prefix) for item in tree.get('excluded',[]))):
                    raise RestoreProvenanceError('原复制任务产物被修改、删除或追加。')

    def _original(self, lineage, evidence, inventories):
        if evidence is None:return False
        kind,*parts=evidence
        if kind=='job':
            identity,field,raw=parts;original=lineage['originals'].get(identity)
            return original is not None and field in {'config_json','result_json'} and original[field]==raw
        if kind=='event':
            row=parts[0]
            return row['job_id'] in lineage['originals'] and row in lineage['proof']['jobs_intent'].get('event_history',[])
        if kind=='file':
            key,name=parts;mapping=lineage['mappings'].get(key);tree=inventories.get(key)
            if mapping is None or tree is None:return False
            prefix=Path(mapping['destination']).relative_to(Path(lineage['proof']['plan']['root_plan']['container'])).as_posix()
            expected=lineage['copied']['files'].get(prefix+'/'+name);actual=tree['files'].get(name)
            return expected is not None and actual is not None and {k:actual[k] for k in ('size','sha256')}==expected
        return False

    def path(self, path, *, evidence, inventories):
        path=Path(path)
        if not path.is_absolute() or '..' in path.parts:return path
        if any(path.is_relative_to(Path(tree['source_root'])) for tree in inventories.values()):return path
        candidates=set()
        for lineage in self.lineages:
            if not self._original(lineage,evidence,inventories):continue
            for key,mapping in lineage['mappings'].items():
                old=Path(mapping['source_root']);current=inventories.get(key)
                if current is not None and path.is_relative_to(old):
                    candidates.add(Path(current['source_root'])/path.relative_to(old))
        if len(candidates)>1:raise RestoreProvenanceError('历史产物引用存在多个目标。')
        return next(iter(candidates)) if candidates else path
