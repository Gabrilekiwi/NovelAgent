"""Relate an immutable qualified-source archive to completed source renewals.

The archive keeps its original provenance. Only completed machine-side
reinstatements can extend that provenance to the current source record.
Neither an increasing generation nor rehashed copied evidence grants renewal.
"""
import json
from pathlib import Path

from workbench.backup_restore_provenance import validate_restore_provenance
from workbench.restore_handoff import RestoreHandoffError, _signed
from workbench.restore_reinstatement import load_reinstatements


def validate_qualified_archive_ancestry(archived, current, source, book_id, *, depth=0):
    """Check the exact immutable base and ordered prefix, without opening roots."""
    try:
        if (depth > 16 or len(json.dumps([archived, current], ensure_ascii=False).encode('utf-8')) > 32*1024**2):
            raise ValueError('qualified archive ancestry exceeds supported size')
        root = Path(source['binding']['root'])
        validate_restore_provenance(archived, root=root, book_id=book_id, depth=depth+1)
        validate_restore_provenance(current, root=root, book_id=book_id, ownership=source, depth=depth+1)
        old = archived['active_record']
        def unchanged_record(record):
            # Both complete provenance chains were checked above. Their
            # explicit upgrade renewal can change only this identity digest;
            # physical binding, witness and original qualification stay fixed.
            from workbench.book_ownership import _sha256
            if not _sha256(record['binding']['identity_sha256']):
                raise ValueError('invalid qualified identity binding')
            return {**{k:v for k,v in record.items() if k not in {'generation','record_sha256','binding'}},
                'binding':{k:v for k,v in record['binding'].items() if k!='identity_sha256'}}
        unchanged_proof = lambda proof: {k:v for k,v in proof.items() if k not in {
            'active_record', 'qualification_reinstatements', 'provenance_sha256'}}
        old_chain = archived.get('qualification_reinstatements', [])
        chain = current.get('qualification_reinstatements', [])
        if (old == source or unchanged_record(old) != unchanged_record(source)
                or unchanged_proof(archived) != unchanged_proof(current)
                or len(chain) <= len(old_chain) or chain[:len(old_chain)] != old_chain):
            raise ValueError('qualified archive and current provenance do not share the original qualification')
        return current
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise RestoreHandoffError('原归档与当前恢复资格的续接证明无法核验。') from exc


def verify_qualified_archive_ancestry_files(directory, archived, current, source, book_id):
    """Require every original intent, decision and completed renewal on disk."""
    validate_qualified_archive_ancestry(archived, current, source, book_id)
    try:
        chain = load_reinstatements(directory, current['activation_intent']['active_record'],
            current['qualification'], source)
        if chain != current['qualification_reinstatements']:
            raise RestoreHandoffError('当前恢复资格的机器续接凭据已经变化。')
        return current
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise RestoreHandoffError('原机器恢复资格续接证明缺失或无法核验。') from exc


def load_qualified_archive_ancestry(directory, archived, source, book_id):
    """Extend only with original machine evidence; never capture or repair it."""
    try:
        validate_restore_provenance(archived, root=Path(source['binding']['root']), book_id=book_id)
        chain = load_reinstatements(directory, archived['activation_intent']['active_record'],
            archived['qualification'], source)
        current = _signed({**archived, 'active_record':source,
            'qualification_reinstatements':chain}, 'provenance_sha256')
        return verify_qualified_archive_ancestry_files(directory, archived, current, source, book_id)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise RestoreHandoffError('原归档的恢复资格没有完整的机器续接证明。') from exc
