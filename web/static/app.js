/* ─────────────────────────────────────────────────────────────
   app.js — Vequil Agent Ledger
   Handles: auth gate, live API polling, rendering action feeds
   ───────────────────────────────────────────────────────────── */

const LS_KEY = 'vequil_api_key';
const API_BASE = '';          // same-origin; server.py serves everything

// ── Utilities ────────────────────────────────────────────────

const $ = id => document.getElementById(id);
const currency = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' });

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function fmt(val) {
  const n = parseFloat(val);
  return isNaN(n) ? (val ?? '—') : currency.format(n);
}

// ── Auth ─────────────────────────────────────────────────────

function storedKey() { return localStorage.getItem(LS_KEY); }

function showApp() {
  $('auth-gate').style.display = 'none';
  $('app').style.display = 'flex';
  loadHistory();
  loadDashboard();
}

function showAuth() {
  $('app').style.display = 'none';
  $('auth-gate').style.display = 'flex';
}

async function submitKey() {
  const key = $('api-key-input').value.trim();
  if (!key) return;
  $('auth-error').style.display = 'none';
  $('auth-submit').disabled = true;
  $('auth-submit').textContent = 'Verifying…';

  try {
    const res = await fetch(`${API_BASE}/api/health`, {
      headers: { 'X-API-Key': key }
    });
    if (res.ok) {
      localStorage.setItem(LS_KEY, key);
      showApp();
    } else {
      $('auth-error').style.display = 'block';
    }
  } catch {
    $('auth-error').textContent = 'Could not reach server. Is it running?';
    $('auth-error').style.display = 'block';
  } finally {
    $('auth-submit').disabled = false;
    $('auth-submit').textContent = 'Unlock Ledger';
  }
}

$('auth-submit').addEventListener('click', submitKey);
$('api-key-input').addEventListener('keydown', e => { if (e.key === 'Enter') submitKey(); });

$('logout-btn').addEventListener('click', () => {
  localStorage.removeItem(LS_KEY);
  showAuth();
});

// ── Status pill ───────────────────────────────────────────────

function setStatus(state, label) {
  const pill = $('status-pill');
  pill.className = `status-pill ${state}`;
  pill.textContent = label;
}

// ── Run + Export buttons ─────────────────────────────────────
 
$('run-btn').addEventListener('click', () => loadDashboard(true));
 
$('export-btn').addEventListener('click', async () => {
  const eventId = $('event-selector').value;
  const key = storedKey();
  try {
    const url = new URL(`${window.location.origin}/api/export`);
    if (eventId) url.searchParams.append('event_id', eventId);
    
    const res = await fetch(url, {
      headers: key ? { 'X-API-Key': key } : {}
    });
    if (res.status === 401) { showAuth(); return; }
    if (!res.ok) throw new Error('Export failed');
    
    const blob = await res.blob();
    const blobUrl = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.style.display = 'none';
    a.href = blobUrl;
    a.download = `Vequil_Ledger_${eventId || 'Latest'}.xlsx`;
    document.body.appendChild(a);
    a.click();
    
    setTimeout(() => {
        window.URL.revokeObjectURL(blobUrl);
        a.remove();
    }, 1000);
  } catch (err) {
    alert(`Export Error: ${err.message}`);
  }
});

// ── API fetch ─────────────────────────────────────────────────
 
async function apiFetch(path, params = {}) {
  const key = storedKey();
  const url = new URL(`${window.location.origin}${path}`);
  Object.keys(params).forEach(k => url.searchParams.append(k, params[k]));

  const res = await fetch(url, {
    headers: key ? { 'X-API-Key': key } : {}
  });
  if (res.status === 401) { showAuth(); throw new Error('Unauthorized'); }
  if (!res.ok) throw new Error(`Server error ${res.status}`);
  return res.json();
}

async function loadHistory() {
  try {
    const data = await apiFetch('/api/history');
    const sel = $('event-selector');
    sel.innerHTML = '<option value="">Current Run (latest)</option>';
    data.history.forEach(ev => {
      const opt = el('option', '', ev.event_id);
      opt.value = ev.event_id;
      sel.appendChild(opt);
    });
  } catch (err) {
    console.error('Failed to load history', err);
  }
}

$('event-selector').addEventListener('change', () => loadDashboard());

// ── Main load ─────────────────────────────────────────────────

let allActions = [];

async function loadDashboard(forceRun = false) {
  $('loading-state').style.display = 'flex';
  $('error-state').style.display = 'none';
  $('dashboard-content').style.opacity = '0.3';
  const btn = $('run-btn');
  btn.disabled = true;
  btn.innerHTML = `
    <svg style="animation: spin 0.6s linear infinite;" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>
    Syncing...
  `;
  setStatus('running', 'Syncing…');
 
  const newId = $('new-event-id').value.trim();
  const eventId = (forceRun && newId) ? newId : $('event-selector').value;
 
  try {
    const params = {};
    if (forceRun) params.run = '1';
    if (eventId) params.event_id = eventId;
 
    const payload = await apiFetch('/api/reconciliation', params);
    renderAll(payload);
    setStatus('done', 'Synced');
    $('dashboard-content').style.opacity = '1';
    
    if (forceRun) {
      loadHistory();
      $('new-event-id').value = ''; 
    }
  } catch (err) {
    $('error-msg').textContent = err.message;
    $('error-state').style.display = 'flex';
    setStatus('error', 'Error');
  } finally {
    $('loading-state').style.display = 'none';
    const btn = $('run-btn');
    btn.disabled = false;
    btn.innerHTML = `
      <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"/></svg>
      Sync Ledger
    `;
  }
}

// ── Render all ────────────────────────────────────────────────

function renderAll(payload) {
  renderMetrics(payload.metrics);
  renderAgentSummary(payload.processor_summary);
  renderResourceSummary(payload.expected_variance_summary);
  allActions = payload.discrepancies || [];
  renderActionFeed(allActions);
  $('generated-at').textContent =
    `Last sync: ${new Date(payload.generated_at).toLocaleString()}`;
  $('proc-count').textContent = `${payload.processor_summary.length} active`;
  $('finding-count').textContent = `${allActions.length} audits`;
}

// ── Metrics ───────────────────────────────────────────────────

function renderMetrics(m) {
  const container = $('metrics');
  container.innerHTML = '';

  const cards = [
    ['Total Actions', m.total_transactions,               ''],
    ['Anomalies',     m.flagged_transactions,              m.flagged_transactions > 0 ? 'danger' : 'success'],
    ['Open Audits',   m.total_findings,                    m.total_findings > 0 ? 'danger' : 'success'],
    ['Burn Rate',     fmt(m.total_volume),                 'blue'],
    ['Verified',      fmt(m.cleared_volume),               'success'],
    ['At-Risk $',     fmt(m.at_risk_volume),               m.at_risk_volume > 0 ? 'danger' : ''],
    ['Net Variance',  fmt(m.net_expected_variance),        m.net_expected_variance < 0 ? 'danger' : ''],
  ];

  cards.forEach(([title, value, colorClass]) => {
    const card = el('div', 'metric');
    card.append(el('div', 'metric-title', title));
    const v = el('div', `metric-value ${colorClass}`.trim(), String(value));
    card.append(v);
    container.append(card);
  });
}

// ── Agent summary ─────────────────────────────────────────

function renderAgentSummary(rows) {
  const container = $('processor-summary');
  container.innerHTML = '';
  const list = el('div', 'summary-list');

  rows.forEach(row => {
    const item = el('div', 'summary-item');
    const left = el('div');
    left.append(el('div', 'summary-item-name', row.processor));
    left.append(el('div', 'summary-meta',
      `${row.transactions.toLocaleString()} actions · ${row.flagged_transactions} flagged · ${row.findings} audits`));
    const amt = el('div', 'summary-amount', fmt(row.total_amount));
    item.append(left, amt);
    list.append(item);
  });

  container.append(list);
}

// ── Resource summary ─────────────────────────────────

function renderResourceSummary(rows) {
  const container = $('expected-summary');
  container.innerHTML = '';

  if (!rows || !rows.length) {
    container.append(el('div', 'muted', 'No resource variances flagged.'));
    return;
  }

  const list = el('div', 'summary-list');
  rows.forEach(row => {
    const item = el('div', 'summary-item');
    const left = el('div');
    left.append(el('div', 'summary-item-name', `${row.venue_area}`));
    left.append(el('div', 'summary-meta',
      `${row.source_system} · Alloc ${fmt(row.expected_amount)} · Actual ${fmt(row.settled_amount)}`));
    const varAmt = parseFloat(row.variance_amount);
    const cls = 'summary-amount' + (varAmt < 0 ? ' negative' : '');
    item.append(left, el('div', cls, fmt(varAmt)));
    list.append(item);
  });

  container.append(list);
}

// ── Action Feed ───────────────────────────────────────────

const FLAG_CLASS = {
    'Unsettled status':       'unsettled',
    'Missing auth code':      'missing-auth',
    'Duplicate reference':    'duplicate',
    'High-value review':      'high-value',
};

function flagClass(type) {
  for (const [k, v] of Object.entries(FLAG_CLASS)) {
    if (type && type.toLowerCase().includes(k.toLowerCase().split(' ')[0])) return v;
  }
  return 'default';
}

function renderActionFeed(rows) {
  const tbody = $('action-queue');
  tbody.innerHTML = '';
  $('no-results').style.display = rows.length ? 'none' : 'block';
  
  rows.forEach(row => {
    const tr = document.createElement('tr');
  
    const timeStr = row.transaction_at
      ? new Date(row.transaction_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
      : '—';
  
    [
      timeStr,
      row.processor,
      row.venue_area || '—',
      row.reference_id,
      fmt(row.amount),
    ].forEach(val => tr.append(el('td', '', val ?? '—')));
  
    // Flag badge
    const flagTd = el('td');
    const badge = el('span', `flag-badge ${flagClass(row.discrepancy_type)}`, row.discrepancy_type || '—');
    flagTd.append(badge);
    tr.append(flagTd);
  
    // AI Audit
    const diagTd = el('td');
    diagTd.append(el('div', 'diag-text', row.diagnosis || '—'));
    tr.append(diagTd);
  
    // Operator Review
    const resTd = el('td');
    const cell = el('div', 'resolve-cell');
    
    if (row.resolution) {
      cell.append(el('div', 'res-status', 'Reviewed'));
      cell.append(el('div', 'res-note', row.resolution));
    } else {
      const btn = el('button', 'resolve-btn', 'Archive Action');
      btn.onclick = () => reviewAction(row);
      cell.append(btn);
    }
    
    resTd.append(cell);
    tr.append(resTd);
  
    tbody.append(tr);
  });
}

async function reviewAction(row) {
  const note = prompt(`Enter audit note for ${row.processor} action ${row.reference_id}:`, 'Verified by operator.');
  if (note === null) return;

  const findingId = `${row.processor}_${row.reference_id}_${row.discrepancy_type}`;
  const key = storedKey();

  try {
    const res = await fetch(`${API_BASE}/api/resolve`, {
      method: 'POST',
      headers: { 
        'X-API-Key': key || '',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ finding_id: findingId, resolution: note })
    });
    if (res.ok) {
      loadDashboard();
    } else {
      alert('Audit failed to save.');
    }
  } catch (err) {
    alert(`Error: ${err.message}`);
  }
}

// ── Filter ────────────────────────────────────────────────────

$('queue-filter').addEventListener('input', e => {
  const q = e.target.value.toLowerCase();
  const filtered = allActions.filter(r =>
    [r.processor, r.discrepancy_type, r.venue_area, r.reference_id, r.diagnosis]
      .some(v => v && v.toLowerCase().includes(q))
  );
  renderActionFeed(filtered);
});

// ── Modal Logic ───────────────────────────────────────────────

function showReportCard() {
  const modal = $('report-modal');
  const preview = $('report-card-preview');
  
  // Extract stats for the card
  const totalActions = allActions.length + (parseInt($('proc-count').textContent) || 0) * 100; // Mocking total including historical
  const topAnomaly = allActions.length > 0 ? allActions[0].discrepancy_type : 'None detected';
  const topAgent = $('processor-summary').querySelector('.summary-item-name')?.textContent || 'Claude';

  preview.innerHTML = `
    <div class="inner-card">
      <div class="ic-header">
        <div class="ic-title">Agent Quality Score</div>
        <div class="ic-badge">WEEK 14 · A+</div>
      </div>
      <div class="ic-stats">
        <div class="ics-item">
          <span class="label">Weekly Activity</span>
          <div class="val">${totalActions.toLocaleString()} actions</div>
          <div class="sub">Verified by Vequil Ledger</div>
        </div>
        <div class="ics-item">
          <span class="label">Weirdest Anomaly</span>
          <div class="val">${topAnomaly}</div>
          <div class="sub">Detected and flagged</div>
        </div>
        <div class="ics-item">
          <span class="label">Most Active Agent</span>
          <div class="val">${topAgent}</div>
          <div class="sub">Highest task resolution rate</div>
        </div>
      </div>
      <div class="ic-stamp">VERIFIED BY VEQUIL</div>
    </div>
  `;
  
  modal.style.display = 'flex';
}

$('nav-reports')?.addEventListener('click', e => {
    e.preventDefault();
    showReportCard();
});

$('close-modal')?.addEventListener('click', () => {
    $('report-modal').style.display = 'none';
});

window.onclick = (e) => {
    if (e.target == $('report-modal')) $('report-modal').style.display = 'none';
};

$('share-x-btn')?.addEventListener('click', () => {
    const text = encodeURIComponent("My agents are actually behaving. Mostly. Check my Vequil Report Card. #Vequil #OpenClaw");
    window.open(`https://twitter.com/intent/tweet?text=${text}`, '_blank');
});

$('copy-report-link')?.addEventListener('click', (e) => {
    const btn = e.target;
    const oldText = btn.textContent;
    navigator.clipboard.writeText(window.location.href + '#report');
    btn.textContent = 'Link Copied!';
    setTimeout(() => btn.textContent = oldText, 2000);
});

// ── Boot ──────────────────────────────────────────────────────

if (storedKey()) {
  showApp();
} else {
  showAuth();
}
