// Mission Control — minimal client. No build step, no deps.
(function () {
  // ── toasts ──────────────────────────────────────────────────────────
  function toast(msg, kind) {
    let box = document.querySelector('.toasts');
    if (!box) { box = document.createElement('div'); box.className = 'toasts'; document.body.appendChild(box); }
    const t = document.createElement('div');
    t.className = 'toast ' + (kind || '');
    t.textContent = msg;
    box.appendChild(t);
    setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 300); }, 3200);
  }
  window.mcToast = toast;

  // ── source pills (research panel) ───────────────────────────────────
  document.querySelectorAll('.pill').forEach(p => {
    const cb = p.querySelector('input');
    if (!cb) return;
    const sync = () => p.classList.toggle('on', cb.checked);
    cb.addEventListener('change', sync); sync();
  });

  // ── control actions (services panel) ────────────────────────────────
  document.querySelectorAll('[data-action]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const url = btn.getAttribute('data-action');
      const confirmMsg = btn.getAttribute('data-confirm');
      if (confirmMsg && !window.confirm(confirmMsg)) return;
      btn.disabled = true; const label = btn.textContent; btn.textContent = '…';
      try {
        const r = await fetch(url, { method: 'POST' });
        const j = await r.json().catch(() => ({}));
        toast(j.message || (r.ok ? 'Done' : 'Failed'), r.ok ? 'ok' : 'bad');
      } catch (e) { toast('Request failed', 'bad'); }
      setTimeout(() => { btn.disabled = false; btn.textContent = label; refreshOverview(); }, 1200);
    });
  });

  // ── live overview polling ───────────────────────────────────────────
  const dotClass = s => s === 'up' ? 'ok' : s === 'degraded' ? 'warn' : s === 'down' ? 'bad' : 'idle';
  async function refreshOverview() {
    const host = document.querySelector('[data-overview]');
    if (!host) return;
    try {
      const r = await fetch('/api/overview'); if (!r.ok) return;
      const d = await r.json();
      (d.services || []).forEach(s => {
        const el = document.querySelector('[data-svc="' + s.name + '"]');
        if (el) { el.className = 'dot ' + dotClass(s.status) + (s.status === 'up' ? ' live' : ''); el.title = s.detail || s.status; }
      });
      setText('[data-mem]', d.mem_used + ' / ' + d.mem_total);
      setBar('[data-mem-bar]', d.mem_pct);
      setText('[data-disk]', d.disk_used + ' / ' + d.disk_total);
      setBar('[data-disk-bar]', d.disk_pct);
      setText('[data-cost-today]', '$' + d.cost_today);
      setText('[data-cost-total]', '$' + d.cost_total);
      setText('[data-runs]', d.runs_total);
      const sum = document.querySelector('[data-health-sum]');
      if (sum) { const up = (d.services || []).filter(s => s.status === 'up').length, n = (d.services || []).length;
        sum.textContent = up === n ? 'All systems operational' : (n - up) + ' service(s) need attention';
        sum.className = 'health-sum ' + (up === n ? '' : 'attn'); }
    } catch (e) { /* silent */ }
  }
  function setText(sel, v) { const e = document.querySelector(sel); if (e && v != null) e.textContent = v; }
  function setBar(sel, pct) { const e = document.querySelector(sel); if (e && pct != null) { e.style.width = Math.min(100, pct) + '%';
    e.parentElement.className = 'meter' + (pct > 90 ? ' bad' : pct > 75 ? ' warn' : ''); } }
  window.refreshOverview = refreshOverview;
  if (document.querySelector('[data-overview]')) { refreshOverview(); setInterval(refreshOverview, 5000); }
})();
