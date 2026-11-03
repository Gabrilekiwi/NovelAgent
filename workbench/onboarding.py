"""Maintenance-owned onboarding checks against an actual completed archive.

This preview cannot grant a writer contract or enroll a book. Recovery
certification and the normal upgrade transaction remain explicit next steps.
"""
import json
import re

from core.story_project.identity import validate_project_identity
from core.story_project.reviewed_identity import validate_reviewed_identity
from core.story_project.reviewed_state_document import digest, read_project_bytes
from workbench.backup_archive import BackupArchiveError
from workbench.backup_archive_view import verified_archive_view
from workbench.backup_archive_verification import verify_archived_target
from workbench.maintenance import MaintenanceError


class OnboardingPreviewService:
    def __init__(self, backups):
        self.backups = backups

    def preview(self, project_id, *, operation_id, archive_id='', snapshot_permit=None):
        if not isinstance(archive_id, str) or (archive_id and re.fullmatch('backup_[0-9a-f]{32}', archive_id) is None):
            raise BackupArchiveError('请选择本机归档列表中的备份。')
        backups = self.backups
        if backups.maintenance.status()['purpose'] != 'backup':
            raise MaintenanceError('作品接入检查需要备份维护状态。')
        # Use the same physical registration, machine, Provider, SQLite and
        # persistence boundaries as capture. No caller-provided check results.
        with backups._source(project_id, operation_id, snapshot_permit) as (snapshot, scope, _, permit):
            current = backups._preview(scope, snapshot, permit, operation_id)
            raw = read_project_bytes(scope['source_root'], '.novelagent/project.json')
            value = json.loads(raw)
            reviewed = value.get('schema_version') == '3.0'
            identity = (validate_reviewed_identity(value) if reviewed else validate_project_identity(value))
            checks, blockers = [], []

            def check(code, passed, message):
                checks.append({'code': code, 'passed': passed, 'message': message})
                if not passed:
                    blockers.append(code)

            supported = (not identity.ephemeral and identity.story_state_mode == 'strict'
                and identity.authority['mode'] == 'legacy_markdown_v1'
                and identity.authority['minimum_writer_contract'] <= 2)
            check('supported_identity', supported, '作品身份符合当前旧书接入范围。' if supported else
                '当前接入流程只支持已通过校准的 Strict 旧作品。')
            check('settled_source', scope['scope_checks_passed'], '当前任务与来源证据检查通过。'
                if scope['scope_checks_passed'] else '作品仍有未结算任务、缺失文件或无法核验的历史，请先处理。')
            archive = None
            if not archive_id:
                check('matching_backup', False, '尚未选择备份，请先保存归档并选择核对。')
            else:
                package = backups.archive_root / archive_id
                with verified_archive_view(package) as view:
                    saved = view.manifest['preview']
                    if saved['project_id'] != project_id or saved['book_id'] != scope['book_id']:
                        raise BackupArchiveError('所选归档属于另一部作品，请重新选择。')
                    # Ignore the former maintenance operation ID, but require
                    # the whole DB, all scoped files, physical identities,
                    # ownership, configuration and source build to still match.
                    differences = [key for key in ('scope', 'database', 'runtime_config', 'software')
                        if saved[key] != current[key]]
                    archive = {'archive_id': archive_id, 'receipt_sha256': view.verified['receipt_sha256'],
                        'source_matches': not differences, 'differences': differences,
                        'offline_data_verified': None, 'offline_blockers': [],
                        'continuation_blockers': [], 'recoverable': False}
                    check('matching_backup', not differences, '所选备份与当前作品及整库快照一致。'
                        if not differences else '所选备份已偏离当前作品、数据库或运行配置，请重新归档。')
                    if not differences:
                        evidence = verify_archived_target(package, project_id=project_id, book_id=scope['book_id'])
                        if evidence['archive_receipt_sha256'] != view.verified['receipt_sha256']:
                            raise BackupArchiveError('接入检查期间归档完成凭证发生变化。')
                        archive.update(offline_data_verified=evidence['offline_data_verified'],
                            offline_blockers=evidence['blockers'], continuation_blockers=evidence['continuation_blockers'],
                            verification_sha256=evidence['verification_sha256'])
                        check('backup_data_verified', evidence['offline_data_verified'],
                            '归档内的数据和历史核验通过。' if evidence['offline_data_verified'] else
                            '归档内部仍有未通过核验的数据或历史，请先处理。')
                        check('continuation_review', not evidence['continuation_blockers'],
                            '归档未发现需要另行处理的续写条件。' if not evidence['continuation_blockers'] else
                            '归档保留的作者修改或续写条件需要重新审核。')
            # Do not upgrade diagnostic archives by interpreting successful
            # offline checks as proof of writable recovery.
            check('recoverable_backup', False, '此处仅核对诊断归档；恢复认证须在专用升级预览中重新核验，不能据此授予升级许可。')
            check('onboarding_available', False, '请进入作者基线审核和作品升级确认页面，单独核对并明确确认；此诊断检查不会执行升级。')
            if not reviewed:
                check('author_baseline_review', False, '七类事实的作者确认及当前来源有效性须在专用升级预览中核验，此诊断检查不替代确认。')
            result = {'schema_version': 'workbench_onboarding_preview_v1', 'project_id': project_id,
                'book_id': scope['book_id'], 'operation_id': operation_id, 'identity_version': value['schema_version'],
                'source_preview_sha256': current['preview_sha256'], 'source_blockers': scope['blockers'],
                'checks': checks, 'blockers': blockers, 'archive': archive,
                'can_upgrade': False, 'can_enroll': False, 'publication_authorized': False}
            result['preview_sha256'] = digest(result)
        # The source context has rechecked every inventory before returning.
        return result
