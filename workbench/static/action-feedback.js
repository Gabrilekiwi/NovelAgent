// Track the actual user action, including response processing and follow-up reads.
// Background polling and instant controls do not create tasks here.
(() => {
  'use strict';
  const tasks = new Set(), activeControls = new WeakSet();
  let layer, message, timer;
  function draw() {
    clearTimeout(timer);
    const current = [...tasks].at(-1);
    if (!current || document.querySelector('#confirm-dialog[open]')) {
      if (layer) layer.hidden = true;
      return;
    }
    const wait = 120 - (performance.now() - current.start);
    if (wait > 0) { timer = setTimeout(draw, wait + 1); return; }
    if (!layer) {
      layer = document.createElement('div'); layer.className = 'action-feedback';
      layer.setAttribute('role', 'status'); layer.setAttribute('aria-live', 'polite');
      const spin = document.createElement('span'); spin.className = 'action-spinner'; spin.setAttribute('aria-hidden','true');
      message = document.createElement('span'); layer.append(spin, message);
    }
    // A status inside an open native dialog stays visible above its backdrop.
    const host = [...document.querySelectorAll('dialog[open]')].at(-1) || document.body;
    if (layer.parentElement !== host) host.append(layer);
    message.textContent = `${current.label} · 正在处理，请稍候…`;
    layer.hidden = false;
  }
  document.addEventListener('close', () => { if (tasks.size) draw(); }, true);
  function action(handler) {
    return function(event) {
      const control = event?.submitter || event?.target?.closest?.('button,[role="button"],input[type="submit"]') ||
        (event?.type === 'submit' ? event.target : null);
      if (control && activeControls.has(control)) { event.preventDefault(); return; }
      const label = (control?.getAttribute('aria-label') || control?.textContent || '加载页面').trim().slice(0,48);
      const result = handler.apply(this, arguments);
      if (!result || typeof result.then !== 'function') return result;
      const task = {label, start:performance.now()}; tasks.add(task);
      if (control) activeControls.add(control);
      draw();
      return Promise.resolve(result).finally(() => {
        tasks.delete(task); if (control) activeControls.delete(control); draw();
      });
    };
  }
  window.NovelAgentFeedback = Object.freeze({action});
})();
