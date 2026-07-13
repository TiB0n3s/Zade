
    const token = () => localStorage.getItem('zadeKernelToken') || '';
    // RC1 bootstrap: fetch the local mutation token from the loopback kernel once,
    // so mutations carry X-Zade-Token without a manual paste. Reads need no token,
    // so fire-and-forget is safe — the token lands before any button is clicked.
    (async () => {
      if (localStorage.getItem('zadeKernelToken')) return;
      try {
        const r = await fetch('/session/token');
        if (r.ok) { const d = await r.json(); if (d.token) localStorage.setItem('zadeKernelToken', d.token); }
      } catch (e) { /* offline or non-loopback bind: leave token unset */ }
    })();
    const headers = () => token() ? { 'Content-Type': 'application/json', 'X-Zade-Token': token() } : { 'Content-Type': 'application/json' };
    const api = async (path, opts = {}) => {
      const response = await fetch(path, { ...opts, headers: { ...headers(), ...(opts.headers || {}) } });
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    };
    const byId = (id) => document.getElementById(id);
    const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    async function withBusy(btn, workingLabel, fn) {
      const original = btn.textContent;
      btn.disabled = true;
      btn.textContent = workingLabel;
      try { return await fn(); }
      finally { btn.disabled = false; btn.textContent = original; }
    }

    function metric(value, label, hint) {
      return '<div class="metric" title="' + esc(hint || '') + '"><strong>' + esc(value) + '</strong><span>' + esc(label) + '</span></div>';
    }

    // Signal codes and severities arrive as raw snake_case/enum values from the
    // scan; show them the way a non-technical founder would expect to read them.
    function prettyLabel(value) {
      if (value == null || value === '') return '';
      return String(value).replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
    }

    // Plain-English description of what triggered each signal type, so a founder
    // understands why something is flagged instead of just seeing a raw code.
    const KIND_LABELS = {
      kill_criteria_overdue: 'Kill/keep decision overdue',
      integrity_warning: 'Integrity warning',
      experiment_needs_decision: 'Experiment awaiting a decision',
      thesis_conflict: 'New evidence conflicts with an assumption',
      prediction_overdue: 'Prediction needs scoring',
      decision_revisit_due: 'Decision due for a second look',
      confidence_drop: 'Confidence dropped',
      override_review_due: 'Your override is due for review',
      assumption_review_due: 'Assumption due for review',
      experiment_needs_evidence: 'Experiment short on evidence',
      approvals_pending: 'Approvals waiting on you',
      connector_items_staged: 'External items staged for review',
      commitment_overdue: 'Commitment overdue',
      commitment_drifting: 'Commitment keeps getting pushed back',
      action_plan_stalled: 'Action plan stalled'
    };
    function kindLabel(kind) {
      return KIND_LABELS[kind] || prettyLabel(kind);
    }

    const SEVERITY_LABELS = { red: 'Urgent', orange: 'Important', yellow: 'Worth a look' };
    function severityLabel(severity) {
      return SEVERITY_LABELS[severity] || prettyLabel(severity);
    }

    function renderItem(item) {
      return `
        <article class="item ${esc(item.severity)}">
          <div class="topline">
            <strong title="${esc(kindLabel(item.kind))}">${esc(item.title)}</strong>
            <span class="pill ${esc(item.severity)}" title="How urgent this is (score ${esc(item.score)} of 100 — higher means more urgent)">${esc(severityLabel(item.severity))} · ${esc(item.score)}</span>
          </div>
          <div class="muted">${esc(item.detail)}</div>
          <div class="action-line">&rarr; ${esc(item.recommended_action)}</div>
          <div class="muted mono" style="font-size:11.5px;">${esc(kindLabel(item.kind))} · ${esc(prettyLabel(item.subject_type))}${item.subject_id ? ' #' + esc(item.subject_id) : ''}${item.opened_at ? ' · open since ' + esc(item.opened_at) : ''}</div>
        </article>
      `;
    }

    async function runScan() {
      const scan = await api('/surface/attention');
      byId('oneThing').textContent = scan.one_thing;
      byId('underweighted').textContent = scan.underweighted ? 'Also watch: ' + scan.underweighted : '';
      const bySeverity = scan.items.reduce((acc, i) => { acc[i.severity] = (acc[i.severity] || 0) + 1; return acc; }, {});
      byId('scanMetrics').innerHTML = [
        metric(scan.count, 'Total items', 'Everything currently flagged for your attention'),
        metric(bySeverity.red || 0, 'Urgent', 'Needs a decision now'),
        metric(bySeverity.orange || 0, 'Important', 'Should be handled soon'),
        metric(bySeverity.yellow || 0, 'Worth a look', 'Not urgent, but overdue for a check')
      ].join('');
      byId('scanItems').innerHTML = scan.items.length ? scan.items.map(renderItem).join('') : '<div class="muted">Nothing needs founder attention right now. Signals are clear.</div>';
      return scan;
    }

    async function generateBrief() {
      const notice = byId('briefNotice');
      notice.textContent = ''; notice.className = 'notice';
      try {
        const result = await api('/surface/brief', {
          method: 'POST',
          body: JSON.stringify({ narrate: byId('narrate').checked, force: byId('force').checked })
        });
        byId('briefOut').textContent = result.brief + (result.narrative ? '\n\nPlain-English summary:\n' + result.narrative : '');
        byId('briefMeta').textContent = [
          result.quiet ? 'Nothing new to report (not saved unless "Save anyway" is checked)' : 'Saved',
          result.memory_id ? 'saved as memory #' + result.memory_id : '',
          result.notification_id ? 'notification #' + result.notification_id : '',
          'event #' + result.event_id
        ].filter(Boolean).join(' · ');
        notice.textContent = 'Generated.'; notice.className = 'notice good';
        await runScan();
      } catch (err) { notice.textContent = 'Failed: ' + err.message; notice.className = 'notice err'; }
    }

    async function loadFounderBrief() {
      const result = await api('/founder/brief');
      byId('founderBriefOut').textContent = result.brief;
    }

    async function loadDailyBrief() {
      const result = await api('/brief/daily');
      byId('dailyBriefOut').textContent = result.brief;
    }

    async function loadAll() {
      await runScan();
    }

    byId('refreshBtn').addEventListener('click', () => withBusy(byId('refreshBtn'), 'Refreshing…', () => loadAll().catch((err) => { byId('oneThing').textContent = 'Load failed: ' + err.message; })));
    byId('scanBtn').addEventListener('click', () => withBusy(byId('scanBtn'), 'Scanning…', () => runScan().catch((err) => { byId('scanItems').innerHTML = `<div class="notice err">${esc(err.message)}</div>`; })));
    byId('briefBtn').addEventListener('click', () => withBusy(byId('briefBtn'), 'Generating…', generateBrief));
    byId('founderBriefBtn').addEventListener('click', () => withBusy(byId('founderBriefBtn'), 'Loading…', () => loadFounderBrief().catch((err) => { byId('founderBriefOut').textContent = 'Failed: ' + err.message; })));
    byId('dailyBriefBtn').addEventListener('click', () => withBusy(byId('dailyBriefBtn'), 'Loading…', () => loadDailyBrief().catch((err) => { byId('dailyBriefOut').textContent = 'Failed: ' + err.message; })));
    loadAll().catch((err) => { console.error('Initial surfacing load failed', err); });
  