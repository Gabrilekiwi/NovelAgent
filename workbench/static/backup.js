(() => {
  "use strict";
  const root = document.querySelector('[data-backup-controls]');
  if (!root) return;
  const one = key => root.querySelector(`[data-backup-${key}]`);
  let maintenance = null, preview = null, onboarding = null, registration = null, certification = null, busy = false, initialized = false;
  const clearCertification = () => {
    certification = null;
    one('certification-confirm').checked = false;
    one('certification-summary').replaceChildren();
    one('certification-summary').hidden = true;
  };
  const clearRegistration = () => {
    registration = null;
    one('registration-confirm').checked = false;
    one('registration-summary').replaceChildren();
    one('registration-summary').hidden = true;
  };
  const clearOnboarding = () => {
    onboarding = null;
    one('onboarding-summary').replaceChildren();
    one('onboarding-summary').hidden = true;
  };
  const ready = () => maintenance?.state === 'quiescent' && maintenance?.purpose === 'backup';
  const clearPreview = () => {
    preview = null;
    one('summary').replaceChildren();
    one('summary').hidden = true;
  };
  const render = () => {
    const hints = {
      idle: '请先进入维护，再预览需要归档的作品。',
      draining: '正在等待已开始的操作完成，请稍后刷新归档状态。',
      interrupted: '请先继续原维护，再刷新归档状态。',
      capturing: '归档操作正在进行，请等待完成后刷新状态。',
      quiescent: '维护已就绪，可以预览、保存或校验归档。',
    };
    one('status').textContent = busy ? '正在处理归档，请稍候…' :
      hints[maintenance?.state] || '维护状态暂不可读，请刷新归档状态。';
    one('project').disabled = busy;
    one('baseline-link').hidden = busy || !one('project').value;
    one('baseline-link').href = '/baseline-review?project_id=' + encodeURIComponent(one('project').value);
    one('preview').disabled = busy || !ready() || !one('project').value;
    one('save').disabled = busy || !ready() || !preview ||
      preview.project_id !== one('project').value || preview.operation_id !== maintenance?.operation_id;
    one('refresh').disabled = busy;
    one('onboarding-archive').disabled = busy;
    one('onboarding-check').disabled = busy || !ready() || !one('project').value;
    one('registration-check').disabled = busy || !ready() || !one('project').value || !one('onboarding-archive').value;
    one('registration-confirm').disabled = busy || !ready() || !registration?.can_register;
    one('registration-save').disabled = busy || !ready() || !registration?.can_register ||
      !one('registration-confirm').checked || registration.project_id !== one('project').value ||
      registration.operation_id !== maintenance?.operation_id || registration.archive_id !== one('onboarding-archive').value;
    const selectedArchive = Boolean(one('project').value && one('onboarding-archive').value);
    const certificationCurrent = certification?.can_certify && certification.project_id === one('project').value &&
      certification.archive_id === one('onboarding-archive').value && certification.operation_id === maintenance?.operation_id;
    one('certification-check').disabled = busy || !ready() || !selectedArchive;
    one('certification-verify').disabled = busy || !ready() || !selectedArchive;
    one('certification-confirm').disabled = busy || !ready() || !certificationCurrent;
    one('certification-save').disabled = busy || !ready() || !certificationCurrent || !one('certification-confirm').checked;
    root.querySelectorAll('[data-verify-archive]').forEach(button => { button.disabled = busy || !ready(); });
  };
  const setMaintenance = value => {
    maintenance = value;
    if (preview && (preview.operation_id !== value?.operation_id || (!ready() && !busy))) clearPreview();
    if (onboarding && (onboarding.operation_id !== value?.operation_id || (!ready() && !busy))) clearOnboarding();
    if (registration && (registration.operation_id !== value?.operation_id || (!ready() && !busy))) clearRegistration();
    if (certification && (certification.operation_id !== value?.operation_id || (!ready() && !busy))) clearCertification();
    render();
  };
  const request = async (path, body) => {
    const response = await fetch(path, {
      method: body ? 'POST' : 'GET', credentials: 'same-origin', cache: 'no-store',
      ...(body ? {headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)} : {}),
    });
    const value = await response.json();
    if (!response.ok) throw new Error(window.NovelAgentUI.errorMessage(value, response.status));
    return value;
  };
  const paragraph = (parent, text) => {
    const node = document.createElement('p');
    node.textContent = text;
    parent.append(node);
  };
  const size = bytes => bytes < 1024 ? `${bytes} B` : bytes < 1024 ** 2 ?
    `${(bytes / 1024).toFixed(1)} KiB` : `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  const showPreview = value => {
    const panel = one('summary');
    panel.replaceChildren();
    const selected = one('project').selectedOptions[0]?.textContent || value.project_id;
    paragraph(panel, `归档作品：${selected}。预计总量 ${size(value.total_source_bytes)}，其中数据库 ${size(value.database_bytes)}。`);
    paragraph(panel, '数据库快照包含工作台中其他作品的登记信息；作品文件只包含本次列出的范围。');
    for (const tree of value.roots) {
      paragraph(panel, `${tree.source_root}：${tree.files} 个文件、${tree.directories} 个目录，${size(tree.total_bytes)}；排除 ${tree.excluded.length} 项。`);
    }
    paragraph(panel, value.scope_checks_passed ? '当前范围预检通过。完整历史、加载版本与恢复协议仍待验收。' :
      `发现 ${value.blockers.length} 项尚未解决的范围或状态问题，将随诊断归档保留；请保留原工程用于排查。`);
    paragraph(panel, `保存位置：${value.archive_root}`);
    paragraph(panel, '本次仅保存诊断归档，不会启用恢复或自动写作。');
    panel.hidden = false;
  };
  const loadProjects = async () => {
    const value = await request('/api/projects');
    const select = one('project'), previous = select.value;
    select.replaceChildren(new Option('请选择作品', ''));
    for (const project of value.items) select.add(new Option(project.title || project.name || project.id, project.id));
    if (value.items.some(project => project.id === previous)) select.value = previous;
    else if (value.items.length === 1) select.value = value.items[0].id;
    if (preview && preview.project_id !== select.value) clearPreview();
    if (onboarding && onboarding.project_id !== select.value) clearOnboarding();
    if (registration && registration.project_id !== select.value) clearRegistration();
    if (certification && certification.project_id !== select.value) clearCertification();
  };
  const loadArchives = async () => {
    const value = await request('/api/backups');
    const select = one('onboarding-archive'), previous = select.value;
    select.replaceChildren(new Option('暂不选择备份', ''));
    for (const archive of value.archives) select.add(new Option(archive.archive_id, archive.archive_id));
    if (value.archives.some(archive => archive.archive_id === previous)) select.value = previous;
    if (onboarding && (onboarding.archive?.archive_id || '') !== select.value) clearOnboarding();
    if (registration && registration.archive_id !== select.value) clearRegistration();
    if (certification && certification.archive_id !== select.value) clearCertification();
    const list = one('list');
    list.replaceChildren();
    if (!value.archives.length) {
      const item = document.createElement('li');
      item.textContent = '尚未发现归档。';
      list.append(item);
    }
    for (const archive of value.archives) {
      const item = document.createElement('li'), label = document.createElement('span');
      label.textContent = archive.archive_id;
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'button button-light button-small';
      button.dataset.verifyArchive = archive.archive_id;
      button.textContent = '校验完整性';
      const result = document.createElement('span');
      result.setAttribute('role', 'status'); result.textContent = '尚未校验';
      button.addEventListener('click', () => run(async () => {
        if (!ready()) throw new Error('请在维护就绪后校验归档。');
        result.textContent = '正在校验…';
        try {
          const checked = await request(`/api/backups/${encodeURIComponent(archive.archive_id)}/verify`,
            {operation_id: maintenance.operation_id});
          if (checked.archive_verified !== true || checked.recoverable !== false) throw new Error('归档校验结果无效。');
          result.textContent = '本次完整性校验通过 · 诊断归档';
        } catch (error) { result.textContent = '校验未通过'; throw error; }
      }));
      item.append(label, button, result); list.append(item);
    }
  };
  const run = async operation => {
    if (busy) return;
    busy = true; one('error').hidden = true; render();
    try { await operation(); }
    catch (error) {
      clearPreview();
      clearOnboarding();
      clearRegistration();
      clearCertification();
      one('error').textContent = error.message;
      one('error').hidden = false;
    } finally {
      // The HTTP response may be lost while the server still owns its worker.
      try { setMaintenance(await request('/api/maintenance')); }
      catch { setMaintenance(null); }
      busy = false;
      if (!ready()) { clearPreview(); clearOnboarding(); clearRegistration(); clearCertification(); }
      render();
      document.dispatchEvent(new CustomEvent('workbench:maintenance-status',{detail:maintenance}));
    }
  };
  one('preview').addEventListener('click', () => run(async () => {
    clearPreview(); one('result').hidden = true;
    if (!ready()) throw new Error('请在维护就绪后预览归档。');
    const value = await request(`/api/projects/${encodeURIComponent(one('project').value)}/backups/preview`,
      {operation_id: maintenance.operation_id});
    preview = value; showPreview(value);
  }));
  one('onboarding-check').addEventListener('click', () => run(async () => {
    clearOnboarding();
    if (!ready()) throw new Error('请在维护就绪后检查作品接入条件。');
    const value = await request(`/api/projects/${encodeURIComponent(one('project').value)}/onboarding/preview`, {
      operation_id: maintenance.operation_id, archive_id: one('onboarding-archive').value,
    });
    if (value.schema_version !== 'workbench_onboarding_preview_v1' || !Array.isArray(value.checks) ||
        value.project_id !== one('project').value || value.operation_id !== maintenance.operation_id ||
        value.can_upgrade !== false || value.can_enroll !== false || value.publication_authorized !== false)
      throw new Error('作品接入检查结果无效，请刷新重试。');
    onboarding = value;
    const panel = one('onboarding-summary');
    paragraph(panel, '本次接入检查完成，尚未升级或登记作品。执行前仍需重新核验。');
    for (const check of value.checks) paragraph(panel, `${check.passed ? '通过' : '待完成'}：${check.message}`);
    panel.hidden = false;
  }));
  one('save').addEventListener('click', () => run(async () => {
    if (!ready() || !preview || preview.project_id !== one('project').value ||
      preview.operation_id !== maintenance.operation_id) throw new Error('请重新预览归档范围。');
    const bound = preview;
    clearPreview(); one('result').hidden = true;
    const saved = await request(`/api/projects/${encodeURIComponent(bound.project_id)}/backups`, {
      operation_id: bound.operation_id, expected_preview_sha256: bound.preview_sha256, archive_purpose: 'diagnostic',
    });
    one('result').textContent = `诊断归档已保存：${saved.path}。恢复功能尚未验收，请保留原始工程。`;
    one('result').hidden = false;
    await loadArchives();
  }));
  one('registration-check').addEventListener('click', () => run(async () => {
    clearRegistration();
    const value = await request(`/api/projects/${encodeURIComponent(one('project').value)}/onboarding/registration/preview`, {
      operation_id: maintenance?.operation_id, archive_id: one('onboarding-archive').value,
    });
    if (value.schema_version !== 'legacy_writer_registration_preview_v1' ||
        value.project_id !== one('project').value || value.archive_id !== one('onboarding-archive').value ||
        value.operation_id !== maintenance?.operation_id || value.changes_format !== false ||
        value.archive_recoverable !== false || value.automatic_provider_calls_allowed !== false)
      throw new Error('旧书登记检查结果无效，请重新核对。');
    registration = value;
    paragraph(one('registration-summary'), value.state === 'registered' ? '当前原登记已完成。请重新备份，再继续核对升级条件。' :
      value.state === 'resumable' ? '发现原登记意图。再次确认后仅继续该登记，不创建新的资格。' : '原书、归档和当前登记已核对。请明确确认后登记写入保护。');
    one('registration-summary').hidden = false;
  }));
  one('registration-confirm').addEventListener('change', render);
  const certificationPath = (project, archive) => `/api/projects/${encodeURIComponent(project)}/backups/${encodeURIComponent(archive)}/certification`;
  const validCertificate = (value, project, archive) => value.schema_version === 'target_recovery_certificate_verification_v1' &&
    value.project_id === project && value.archive_id === archive && value.recoverable === true &&
    value.write_activation_allowed === false && value.automatic_provider_calls_allowed === false &&
    value.policy?.schema_version === 'windows_same_host_single_book_v1' &&
    value.policy.restore?.source_unavailable_write_activation_supported === false &&
    value.policy.restore?.unknown_usage_write_activation_allowed === false;
  one('certification-check').addEventListener('click', () => run(async () => {
    clearCertification();
    const project = one('project').value, archive = one('onboarding-archive').value;
    if (!ready() || !project || !archive) throw new Error('请在维护就绪后选择作品与归档。');
    const value = await request(certificationPath(project, archive) + '/preview', {operation_id: maintenance.operation_id});
    if (value.schema_version !== 'target_recovery_certification_preview_v1' || value.project_id !== project ||
        value.archive_id !== archive || value.operation_id !== maintenance.operation_id ||
        typeof value.can_certify !== 'boolean' || !Array.isArray(value.blockers) ||
        value.write_activation_allowed !== false || value.automatic_provider_calls_allowed !== false ||
        (value.can_certify && (!value.certificate || value.blockers.length)))
      throw new Error('恢复认证检查结果无效，请重新核对。');
    certification = value;
    const panel = one('certification-summary');
    paragraph(panel, value.can_certify ? '所选归档通过本次认证核对。请再次核对上方前提，明确确认后签发。' :
      `尚有 ${value.blockers.length} 项认证条件未通过，不能签发。请先核对作品登记、当前备份、任务和用量证据。`);
    if (value.blockers.some(item => item.code === 'source_changed_since_archive'))
      paragraph(panel, '作品、数据库或运行配置已偏离所选备份，请重新归档再核对。');
    panel.hidden = false;
  }));
  one('certification-confirm').addEventListener('change', render);
  one('certification-save').addEventListener('click', () => run(async () => {
    const bound = certification;
    if (!ready() || !bound?.can_certify || !one('certification-confirm').checked ||
        bound.project_id !== one('project').value || bound.archive_id !== one('onboarding-archive').value ||
        bound.operation_id !== maintenance.operation_id) throw new Error('请重新核对认证条件并明确确认。');
    // Consume the confirmation before sending. A lost response must not cause
    // refresh or a second click to replay an already-authorized operation.
    clearCertification();
    const value = await request(certificationPath(bound.project_id, bound.archive_id), {
      operation_id: bound.operation_id, expected_assessment_sha256: bound.assessment_sha256, confirmed: true,
    });
    if (!validCertificate(value, bound.project_id, bound.archive_id)) throw new Error('认证结果无法核对，请复验已有认证。');
    one('result').textContent = '有条件恢复认证已附加保存。原诊断归档保留；可写恢复仍须原来源、本机凭证和最新用量核验。';
    one('result').hidden = false;
  }));
  one('certification-verify').addEventListener('click', () => run(async () => {
    clearCertification();
    const project = one('project').value, archive = one('onboarding-archive').value;
    const value = await request(certificationPath(project, archive) + '/verify', {operation_id: maintenance?.operation_id});
    if (!validCertificate(value, project, archive)) throw new Error('认证结果无法核对。');
    paragraph(one('certification-summary'), '已有认证与归档内容复验通过。此结果不授予写入资格；恢复前仍需重新核对上方前提。');
    one('certification-summary').hidden = false;
  }));
  one('registration-save').addEventListener('click', () => run(async () => {
    if (!ready() || !registration?.can_register || !one('registration-confirm').checked)
      throw new Error('请先核对旧书登记条件并明确确认。');
    const bound = registration;
    const value = await request(`/api/projects/${encodeURIComponent(bound.project_id)}/onboarding/registration`, {
      operation_id: bound.operation_id, archive_id: bound.archive_id,
      expected_preview_sha256: bound.preview_sha256, confirmed: true,
    });
    if (value.state !== 'active' || value.project_id !== bound.project_id || value.changes_format !== false ||
        value.archive_recoverable !== false || value.automatic_provider_calls_allowed !== false)
      throw new Error('登记结果无法核对，请刷新并继续原登记。');
    clearRegistration(); clearPreview(); clearOnboarding(); clearCertification();
    one('result').textContent = '旧书写入保护已登记，正文和格式保留。请重新备份，完成可恢复认证后再升级格式。';
    one('result').hidden = false;
  }));
  one('project').addEventListener('change', () => { clearPreview(); clearOnboarding(); clearRegistration(); clearCertification(); one('result').hidden = true; render(); });
  one('onboarding-archive').addEventListener('change', () => { clearOnboarding(); clearRegistration(); clearCertification(); render(); });
  const refresh = () => run(async () => { clearOnboarding(); clearRegistration(); clearCertification(); await loadProjects(); await loadArchives(); });
  one('refresh').addEventListener('click', refresh);
  root.addEventListener('toggle', () => {
    if (root.open && !initialized) { initialized = true; refresh(); }
  });
  document.addEventListener('workbench:maintenance', event => setMaintenance(event.detail));
})();
