"""Read reviewed identity, upgrade and typed chapter proofs inside an archive."""
import json
from pathlib import Path, PurePosixPath

from core.engine.persistence_v2 import (validate_persistence_manifest_v2, _assert_receipt_matches_manifest,
    _validate_final_run_receipt_pointer, _artifact_digest_summary, canonical_json_hash)
from core.engine.safe_paths import validate_path_ref
from core.story_project.paths import _PROSE_RE
from core.story_project.reviewed_identity import validate_reviewed_identity, ReviewedIdentityError
from core.story_project.reviewed_state_document import digest
from core.story_project.reviewed_state_store import _read_reviewed_history, _content
from workbench.backup_archive import BackupArchiveError, _member
from workbench.backup_archive_view import verified_archive_view
from workbench.backup_history import verify_persistence_history


class _ArchivedReviewedEvidence:
    def __init__(self, view, book_id):
        self.view = view
        self.tree = view.manifest['preview']['scope']['inventories']['story_project']
        self.root = Path(self.tree['source_root'])
        if not self.root.is_absolute() or '..' in self.root.parts:
            raise BackupArchiveError('归档 reviewed 来源坐标无效。')
        self.directories = set(self.tree['directories'])
        self._children = {}
        for name in set(self.tree['files']) | self.directories:
            self._children.setdefault(str(PurePosixPath(name).parent), []).append(name)
        # Prove all retained v1/v2 transactions before a receipt can qualify for
        # the typed reader. These are content coordinates, never live guards.
        self.persistence = verify_persistence_history(self.root,self.tree,book_id=book_id,read_bytes=self.read,
            provenance=view.manifest['preview']['scope'].get('restored_provenance'))
        self.completed = {item['journal']:item for item in self.persistence['transactions']
                          if item.get('schema_version') == '2.1' and item['state'] == 'completed'}

    def relative(self, path):
        path = Path(path)
        if not path.is_absolute() or '..' in path.parts or not path.is_relative_to(self.root):
            raise BackupArchiveError('离线 reviewed 读取越过归档作品根。')
        return path.relative_to(self.root).as_posix()

    def directory(self, path):
        relative = self.relative(path)
        if relative != '.' and relative not in self.directories:
            raise BackupArchiveError('归档 reviewed 必要目录缺失。')
        return Path(path)

    def children(self, path):
        self.directory(path)
        return [self.root/name for name in sorted(self._children.get(self.relative(path),()))]

    def read(self, root, relative):
        _member(relative)
        name = self.relative(Path(root)/relative)
        return self.view.read_root('story_project',name)

    def prose(self, root, chapter):
        candidates = []
        for path in self.children(Path(root)/'正文'):
            match = _PROSE_RE.fullmatch(path.name)
            if (self.relative(path) in self.tree['files'] and match is not None
                    and int(match.group(1)) == chapter):
                candidates.append(path)
        return candidates[0] if len(candidates) == 1 else None

    def receipt(self, receipt, *, root_map):
        def locate(ref):
            value = validate_path_ref(ref)
            if value.root_id not in root_map:
                raise BackupArchiveError('reviewed 回执引用未提供的逻辑根。')
            return Path(root_map[value.root_id]),value.relative_path
        root,name = locate(receipt['manifest']['path_ref'])
        raw = self.read(root,name)
        manifest = validate_persistence_manifest_v2(json.loads(raw))
        journal = str(PurePosixPath(self.relative(root/name)).parent)
        if journal not in self.completed or self.completed[journal]['manifest_sha256'] != digest(raw):
            raise BackupArchiveError('reviewed 回执没有对应的已核验完整事务。')
        _assert_receipt_matches_manifest(receipt,manifest)
        if json.loads(self.read(*locate(receipt['receipt_path_ref']))) != receipt:
            raise BackupArchiveError('reviewed 回执内容与归档定位不同。')
        final = _content(receipt['final_run'],root_map,read_bytes=self.read)
        _validate_final_run_receipt_pointer(json.loads(final),receipt['receipt_id'],receipt['receipt_path_ref'])
        summaries = []
        for artifact in receipt['artifacts']:
            _content(artifact,root_map,read_bytes=self.read)
            summaries.append(_artifact_digest_summary(artifact))
        if canonical_json_hash(sorted(summaries,key=lambda item:item['target_id'])) != receipt['artifact_bundle_digest']:
            raise BackupArchiveError('reviewed 回执发布内容集合不一致。')
        return {'valid':True}


def verify_archived_reviewed_state(package, *, project_id, book_id):
    """Same typed state/upgrade/chapter rules; no source reads or write rights."""
    with verified_archive_view(package) as view:
        preview = view.manifest['preview']
        if preview['project_id'] != project_id or preview['book_id'] != book_id:
            raise BackupArchiveError('离线 reviewed 目标与归档不同。')
        reader = _ArchivedReviewedEvidence(view,book_id)
        raw = reader.read(reader.root,'.novelagent/project.json')
        identity = validate_reviewed_identity(json.loads(raw))
        if identity.book_id != book_id:
            raise ReviewedIdentityError('归档 reviewed 身份属于其他作品。')
        binding = identity.payload['reviewed_state']
        runtime = reader.root/'.novelagent/runtime'
        if "upgrade" in binding:
            receipt = json.loads(reader.read(runtime,binding['upgrade']['receipt_relative_path']))
            targets = [t for t in receipt.get('apply_targets',[]) if t.get('target_id') == 'reviewed_identity']
            if (len(targets) != 1 or targets[0].get('sha256') != digest(raw)
                    or receipt.get('run_id') != binding['upgrade']['run_id']
                    or receipt.get('receipt_id') != binding['upgrade']['receipt_id']):
                raise ReviewedIdentityError('归档身份缺少对应的原子升级凭据。')
        roots = {'story_project':reader.root,'runtime':runtime}
        history,records = _read_reviewed_history(root=reader.root,book_id=book_id,root_map=roots,
            baseline_document_sha256=binding['baseline_document_sha256'],_evidence=reader)
        result = {'schema_version':'archived_reviewed_state_v1','archive_id':view.verified['archive_id'],
            'archive_receipt_sha256':view.verified['receipt_sha256'],'project_id':project_id,'book_id':book_id,
            'identity':identity.to_dict(),'history':history,'persistence_history_sha256':reader.persistence['history_sha256'],
            'reviewed_state_verified':True,'coverage':('reviewed_new_book_start_and_typed_chapter_chain' if 'genesis' in binding else 'reviewed_identity_upgrade_and_typed_chapter_chain'),
            'retained_receipts':[{'path':name,'sha256':item['receipt']['receipt_hash']} for name,item in sorted(records.items())],
            'chapter_publications':[{'receipt_hash':item['receipt']['receipt_hash'],
                'chapter':item['value']['committed_through'],'prose_hash':digest(item['prose'])}
                for name,item in sorted(records.items()) if item['value']['head']['kind']=='chapter'],
            'restore_plan_ready':False,'recoverable':False,'write_activation_allowed':False}
        result['verification_sha256'] = digest(result)
    return result
