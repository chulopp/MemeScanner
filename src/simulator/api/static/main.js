/* MemeScanner Simulator — Dashboard JavaScript */
'use strict';

// ═══ State ════════════════════════════════════════════════════════════════
let equityChart = null;
let heatmapChart = null;
let allTrades = [];
let sweepResults = [];
let defaults = {};

// ═══ Init ═════════════════════════════════════════════════════════════════
document.addEventListener('DOMContentLoaded', async () => {
  await loadMeta();
  await loadDefaults();
});

async function loadMeta() {
  try {
    const res = await fetch('/api/trades/meta');
    const meta = await res.json();
    document.getElementById('meta-total').textContent = `${meta.total} trades`;
    document.getElementById('meta-dates').textContent = `${meta.date_first} → ${meta.date_last}`;
  } catch (e) {
    document.getElementById('meta-dates').textContent = 'Error loading meta';
  }
}

async function loadDefaults() {
  try {
    const res = await fetch('/api/defaults');
    const data = await res.json();
    defaults = data.defaults;
  } catch (e) {
    console.error('Failed to load defaults', e);
  }
}

// ═══ Collect Config from Form ═════════════════════════════════════════════
function getConfigFromForm() {
  const fields = [
    'opportunity_threshold', 'stop_loss_pct', 'breakeven_trigger_pct', 'breakeven_sl_pct',
    'time_decay_tighten_minutes', 'time_decay_exit_minutes', 'time_decay_mfe_threshold_pct', 'time_decay_sl_tighten_pct',
    'tp0_pct', 'tp0_sell_fraction', 'tp1_pct', 'tp1_sell_fraction',
    'tp2_pct', 'tp2_sell_fraction', 'tp3_pct', 'tp3_sell_fraction',
    'trailing_start_return_pct', 'trailing_tier1_pct', 'trailing_tier2_pct', 'trailing_tier3_pct',
    'position_risk_pct', 'max_hold_hours',
  ];
  const config = {};
  for (const f of fields) {
    const el = document.getElementById(f);
    if (el) config[f] = parseFloat(el.value);
  }
  return config;
}

// ═══ Reset to v2.1 Defaults ═══════════════════════════════════════════════
function resetDefaults() {
  const d = {
    opportunity_threshold: 60, stop_loss_pct: -30, breakeven_trigger_pct: 50, breakeven_sl_pct: -10,
    time_decay_tighten_minutes: 15, time_decay_exit_minutes: 30, time_decay_mfe_threshold_pct: 15,
    time_decay_sl_tighten_pct: -15, tp0_pct: 50, tp0_sell_fraction: 0.15, tp1_pct: 100,
    tp1_sell_fraction: 0.25, tp2_pct: 300, tp2_sell_fraction: 0.25, tp3_pct: 500,
    tp3_sell_fraction: 0.15, trailing_start_return_pct: 500, trailing_tier1_pct: 25,
    trailing_tier2_pct: 35, trailing_tier3_pct: 45, position_risk_pct: 2, max_hold_hours: 2,
  };
  for (const [k, v] of Object.entries(d)) {
    const el = document.getElementById(k);
    if (el) el.value = v;
  }
}

// ═══ Run Single Scenario ══════════════════════════════════════════════════
async function runScenario() {
  const config = getConfigFromForm();
  showLoading('Running simulation...');
  try {
    const res = await fetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ config, include_trades: true }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'API error');
    renderScenarioResult(data);
    switchTab('chart');
  } catch (e) {
    alert('Error: ' + e.message);
  } finally {
    hideLoading();
  }
}

// ═══ Render Scenario Result ═══════════════════════════════════════════════
function renderScenarioResult(data) {
  // Summary Cards
  setCard('card-roi', fmtPct(data.roi_pct), data.roi_pct >= 0 ? 'green' : 'red');
  setCard('card-equity', `$${data.final_equity.toFixed(2)}`, data.roi_pct >= 0 ? 'green' : 'red');
  setCard('card-winrate', fmtPct(data.win_rate * 100), data.win_rate >= 0.2 ? 'green' : 'default');
  setCard('card-dd', `-${data.max_drawdown_pct.toFixed(1)}%`, 'red');
  setCard('card-trades', `${data.total_entered} / ${data.dataset_total}`, 'default');
  setCard('card-mfe-capt', fmtPct(data.avg_mfe_captured_pct * 100), 'default');

  // Equity Curve
  renderEquityCurve(data.equity_curve, data.label || 'Scenario');

  // Trade Inspector
  allTrades = data.trades || [];
  renderTradeTable(allTrades);
}

function setCard(id, value, color = 'default') {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = value;
  const card = el.closest('.card');
  card.className = 'card' + (color === 'green' ? ' card--green' : color === 'red' ? ' card--red' : '');
}

// ═══ Equity Curve Chart ═══════════════════════════════════════════════════
function renderEquityCurve(curve, label) {
  const ctx = document.getElementById('equity-chart').getContext('2d');
  if (equityChart) equityChart.destroy();

  const labels = curve.map((_, i) => i);
  const baselineLine = curve.map(() => 100);

  equityChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [
        {
          label,
          data: curve,
          borderColor: '#63b3ed',
          backgroundColor: 'rgba(99,179,237,0.08)',
          borderWidth: 2,
          pointRadius: 0,
          fill: true,
          tension: 0.3,
        },
        {
          label: 'Baseline $100',
          data: baselineLine,
          borderColor: 'rgba(255,255,255,0.15)',
          borderDash: [4, 4],
          borderWidth: 1,
          pointRadius: 0,
          fill: false,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { labels: { color: '#8fa0b5', font: { size: 11 } } },
        tooltip: {
          backgroundColor: '#141926',
          borderColor: 'rgba(255,255,255,0.1)',
          borderWidth: 1,
          callbacks: {
            label: ctx => ` ${ctx.dataset.label}: $${ctx.parsed.y.toFixed(2)}`,
          },
        },
      },
      scales: {
        x: {
          grid: { color: 'rgba(255,255,255,0.05)' },
          ticks: { color: '#4a5568', maxTicksLimit: 10 },
        },
        y: {
          grid: { color: 'rgba(255,255,255,0.05)' },
          ticks: { color: '#4a5568', callback: v => `$${v.toFixed(0)}` },
        },
      },
    },
  });
  document.getElementById('chart-hint').style.display = 'none';
}

// ═══ Trade Table ══════════════════════════════════════════════════════════
function renderTradeTable(trades) {
  const tbody = document.getElementById('trade-tbody');
  tbody.innerHTML = '';
  if (!trades.length) {
    tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:var(--text-muted)">No trades to show.</td></tr>';
    return;
  }
  trades.forEach((t, i) => {
    const simRet = t.simulated_return_pct;
    const actRet = t.actual_return_pct;
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${i + 1}</td>
      <td><strong>${esc(t.symbol)}</strong></td>
      <td class="cell-mono">${t.score.toFixed(1)}</td>
      <td class="cell-mono cell-yellow">+${t.mfe_pct.toFixed(1)}%</td>
      <td class="cell-mono">${t.hold_minutes.toFixed(0)}m</td>
      <td class="cell-mono ${simRet >= 0 ? 'cell-green' : 'cell-red'}">${fmtPct(simRet)}</td>
      <td class="cell-mono ${actRet >= 0 ? 'cell-green' : 'cell-red'}">${fmtPct(actRet)}</td>
      <td><span class="exit-reason-badge badge-${t.simulated_exit_reason}">${t.simulated_exit_reason}</span></td>
      <td class="cell-mono ${t.pnl_usd >= 0 ? 'cell-green' : 'cell-red'}">${t.pnl_usd >= 0 ? '+' : ''}$${t.pnl_usd.toFixed(3)}</td>
    `;
    tr.addEventListener('click', () => openTradeModal(t));
    tbody.appendChild(tr);
  });
}

function filterTrades() {
  const search = document.getElementById('trade-search').value.toLowerCase();
  const exitFilter = document.getElementById('trade-exit-filter').value;
  const filtered = allTrades.filter(t => {
    const matchSearch = !search || (t.symbol || '').toLowerCase().includes(search);
    const matchExit = !exitFilter || t.simulated_exit_reason === exitFilter;
    return matchSearch && matchExit;
  });
  renderTradeTable(filtered);
}

// ═══ Trade Detail Modal ═══════════════════════════════════════════════════
function openTradeModal(t) {
  const modal = document.getElementById('trade-modal');
  document.getElementById('trade-modal-title').textContent = `$${t.symbol} — Trade Detail`;

  const exits = (t.partial_exits || []).map(p => `
    <div class="partial-exit-item">
      <span class="exit-reason-badge badge-${p.reason}">${p.reason}</span>
      <span>Sell: ${(p.fraction * 100).toFixed(0)}% of position</span>
      <span class="cell-mono ${p.return_pct >= 0 ? 'cell-green' : 'cell-red'}">${fmtPct(p.return_pct)}</span>
    </div>
  `).join('');

  document.getElementById('trade-modal-body').innerHTML = `
    <div class="trade-detail-grid">
      <div class="trade-detail-card">
        <h4>Simulated Result</h4>
        <div class="detail-row"><span class="detail-label">Return</span><span class="detail-value ${t.simulated_return_pct >= 0 ? 'cell-green' : 'cell-red'}">${fmtPct(t.simulated_return_pct)}</span></div>
        <div class="detail-row"><span class="detail-label">Exit Reason</span><span class="detail-value">${t.simulated_exit_reason}</span></div>
        <div class="detail-row"><span class="detail-label">PnL</span><span class="detail-value ${t.pnl_usd >= 0 ? 'cell-green' : 'cell-red'}">${t.pnl_usd >= 0 ? '+' : ''}$${t.pnl_usd.toFixed(4)}</span></div>
        <div class="detail-row"><span class="detail-label">Position Size</span><span class="detail-value">$${t.position_size_usd.toFixed(3)}</span></div>
      </div>
      <div class="trade-detail-card">
        <h4>Actual (Paper Trade)</h4>
        <div class="detail-row"><span class="detail-label">Return</span><span class="detail-value ${t.actual_return_pct >= 0 ? 'cell-green' : 'cell-red'}">${fmtPct(t.actual_return_pct)}</span></div>
        <div class="detail-row"><span class="detail-label">Exit Reason</span><span class="detail-value">${t.actual_exit_reason}</span></div>
        <div class="detail-row"><span class="detail-label">MFE</span><span class="detail-value cell-yellow">+${t.mfe_pct.toFixed(1)}%</span></div>
        <div class="detail-row"><span class="detail-label">Hold</span><span class="detail-value">${t.hold_minutes.toFixed(0)} min</span></div>
      </div>
    </div>
    <div class="trade-detail-card">
      <h4>Partial Exits (Simulated)</h4>
      <div class="partial-exit-list">${exits || '<p style="color:var(--text-muted);font-size:12px">No partial exits.</p>'}</div>
    </div>
  `;
  modal.classList.remove('hidden');
}

function closeTradeModal() {
  document.getElementById('trade-modal').classList.add('hidden');
}

// ═══ Grid Sweep Modal ════════════════════════════════════════════════════
const SWEEP_PARAM_LABELS = {
  opportunity_threshold: 'Threshold',
  stop_loss_pct: 'Base SL (%)',
  breakeven_trigger_pct: 'Breakeven Trigger (%)',
  breakeven_sl_pct: 'Breakeven SL (%)',
  time_decay_tighten_minutes: 'Time-Decay Tighten (min)',
  time_decay_exit_minutes: 'Time-Decay Exit (min)',
  time_decay_mfe_threshold_pct: 'TD MFE Threshold (%)',
  time_decay_sl_tighten_pct: 'TD Tightened SL (%)',
  tp0_pct: 'TP0 Trigger (%)',
  tp0_sell_fraction: 'TP0 Sell Fraction',
  tp1_pct: 'TP1 Trigger (%)',
  tp1_sell_fraction: 'TP1 Sell Fraction',
  tp2_pct: 'TP2 Trigger (%)',
  tp2_sell_fraction: 'TP2 Sell Fraction',
  tp3_pct: 'TP3 Trigger (%)',
  tp3_sell_fraction: 'TP3 Sell Fraction',
  trailing_start_return_pct: 'Trailing Activate at (%)',
  trailing_tier1_pct: 'Trailing Tier1 (%)',
  trailing_tier2_pct: 'Trailing Tier2 (%)',
  trailing_tier3_pct: 'Trailing Tier3 (%)',
  position_risk_pct: 'Position Risk (%)',
  max_hold_hours: 'Max Hold (hours)',
};

function openSweepModal() {
  const form = document.getElementById('sweep-params-form');
  form.innerHTML = Object.entries(SWEEP_PARAM_LABELS).map(([k, label]) => `
    <div class="sweep-param-row">
      <label title="${k}">${label}</label>
      <input type="text" id="sweep_${k}" placeholder="e.g. 30,40,50" oninput="updateComboCount()">
    </div>
  `).join('');
  updateComboCount();
  document.getElementById('sweep-modal').classList.remove('hidden');
}

function closeSweepModal() {
  document.getElementById('sweep-modal').classList.add('hidden');
}

function updateComboCount() {
  let total = 1;
  let hasAny = false;
  for (const k of Object.keys(SWEEP_PARAM_LABELS)) {
    const el = document.getElementById('sweep_' + k);
    const vals = el && el.value.trim() ? el.value.split(',').filter(v => v.trim()) : [];
    if (vals.length > 0) { total *= vals.length; hasAny = true; }
  }
  const count = hasAny ? total : 0;
  document.getElementById('sweep-combo-count').textContent = `${count} combinations`;
  document.getElementById('sweep-combo-count').style.color = count > 200 ? 'var(--red)' : 'var(--text-secondary)';
}

async function runSweep() {
  const paramGrid = {};
  for (const k of Object.keys(SWEEP_PARAM_LABELS)) {
    const el = document.getElementById('sweep_' + k);
    if (!el || !el.value.trim()) continue;
    const vals = el.value.split(',').map(v => parseFloat(v.trim())).filter(v => !isNaN(v));
    if (vals.length > 0) paramGrid[k] = vals;
  }

  if (Object.keys(paramGrid).length === 0) {
    alert('Please enter at least one parameter to sweep.');
    return;
  }

  closeSweepModal();
  showLoading('Running grid sweep...');

  try {
    const res = await fetch('/api/sweep', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ param_grid: paramGrid }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Sweep failed');

    sweepResults = data.results || [];
    renderLeaderboard(sweepResults);
    populateHeatmapSelects(Object.keys(paramGrid));
    switchTab('leaderboard');
  } catch (e) {
    alert('Error: ' + e.message);
  } finally {
    hideLoading();
  }
}

// ═══ Leaderboard ══════════════════════════════════════════════════════════
function renderLeaderboard(results) {
  const empty = document.getElementById('leaderboard-empty');
  const table = document.getElementById('leaderboard-table');
  const tbody = document.getElementById('leaderboard-tbody');

  if (!results.length) {
    empty.style.display = 'block';
    table.style.display = 'none';
    return;
  }
  empty.style.display = 'none';
  table.style.display = 'table';
  tbody.innerHTML = '';

  results.forEach((r, i) => {
    const rank = i + 1;
    const rankClass = rank <= 3 ? `rank-${rank}` : '';
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="${rankClass}">${rank <= 3 ? ['🥇','🥈','🥉'][rank-1] : rank}</td>
      <td>${esc(r.label)}</td>
      <td class="cell-mono ${r.roi_pct >= 0 ? 'cell-green' : 'cell-red'}">${fmtPct(r.roi_pct)}</td>
      <td class="cell-mono">${(r.win_rate * 100).toFixed(1)}%</td>
      <td class="cell-mono cell-red">${r.max_drawdown_pct.toFixed(1)}%</td>
      <td class="cell-mono">${r.total_entered}</td>
      <td><button class="btn btn-ghost btn-sm" onclick="loadScenarioConfig(${i})">Load ↗</button></td>
    `;
    tbody.appendChild(tr);
  });
}

function loadScenarioConfig(idx) {
  const r = sweepResults[idx];
  if (!r) return;
  const cfg = r.config || {};
  for (const [k, v] of Object.entries(cfg)) {
    const el = document.getElementById(k);
    if (el) el.value = v;
  }
  switchTab('chart');
  runScenario();
}

// ═══ Heatmap ══════════════════════════════════════════════════════════════
function populateHeatmapSelects(sweptParams) {
  ['heatmap-x', 'heatmap-y'].forEach(id => {
    const sel = document.getElementById(id);
    sel.innerHTML = '<option value="">Select param...</option>';
    sweptParams.forEach(p => {
      const opt = document.createElement('option');
      opt.value = p;
      opt.textContent = SWEEP_PARAM_LABELS[p] || p;
      sel.appendChild(opt);
    });
  });
}

async function renderHeatmap() {
  const x = document.getElementById('heatmap-x').value;
  const y = document.getElementById('heatmap-y').value;
  const metric = document.getElementById('heatmap-metric').value;

  if (!x || !y) { alert('Select both X and Y parameters.'); return; }
  if (x === y) { alert('X and Y parameters must be different.'); return; }

  try {
    const res = await fetch('/api/heatmap', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ param_x: x, param_y: y, metric }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail);

    const ctx = document.getElementById('heatmap-chart').getContext('2d');
    if (heatmapChart) heatmapChart.destroy();

    // Flatten matrix for Chart.js scatter-like rendering
    const points = [];
    const values = [];
    data.matrix.forEach((row, yi) => {
      row.forEach((val, xi) => {
        if (val !== null) {
          points.push({ x: xi, y: yi, v: val });
          values.push(val);
        }
      });
    });

    const minVal = Math.min(...values);
    const maxVal = Math.max(...values);
    const range = maxVal - minVal || 1;

    function getColor(v) {
      const t = (v - minVal) / range; // 0 = worst, 1 = best
      // Red → Yellow → Green
      if (t < 0.5) return `rgba(252, 129, 129, ${0.3 + t * 0.7})`;
      return `rgba(72, 187, 120, ${0.3 + (t - 0.5) * 1.4})`;
    }

    heatmapChart = new Chart(ctx, {
      type: 'scatter',
      data: {
        datasets: [{
          data: points.map(p => ({ x: p.x, y: p.y })),
          backgroundColor: points.map(p => getColor(p.v)),
          pointRadius: 30,
          pointHoverRadius: 32,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: ctx => {
                const p = points[ctx.dataIndex];
                return `${data.y_values[p.y]} × ${data.x_values[p.x]}: ${p.v.toFixed(2)}`;
              },
            },
          },
        },
        scales: {
          x: {
            ticks: { color: '#8fa0b5', callback: v => data.x_values[v] ?? v },
            title: { display: true, text: SWEEP_PARAM_LABELS[x] || x, color: '#8fa0b5' },
            grid: { color: 'rgba(255,255,255,0.05)' },
          },
          y: {
            ticks: { color: '#8fa0b5', callback: v => data.y_values[v] ?? v },
            title: { display: true, text: SWEEP_PARAM_LABELS[y] || y, color: '#8fa0b5' },
            grid: { color: 'rgba(255,255,255,0.05)' },
          },
        },
      },
    });
    document.getElementById('heatmap-empty').style.display = 'none';
  } catch (e) {
    alert('Heatmap error: ' + e.message);
  }
}

// ═══ Tab Switching ════════════════════════════════════════════════════════
function switchTab(tab) {
  ['chart', 'trades', 'leaderboard', 'heatmap'].forEach(t => {
    document.getElementById('tab-' + t).classList.toggle('active', t === tab);
    document.getElementById('content-' + t).classList.toggle('hidden', t !== tab);
  });
}

// ═══ Dataset Refresh ═════════════════════════════════════════════════════
async function refreshDataset() {
  showLoading('Refreshing dataset...');
  try {
    const res = await fetch('/api/dataset/refresh', { method: 'POST' });
    const data = await res.json();
    await loadMeta();
  } catch (e) {
    alert('Refresh error: ' + e.message);
  } finally {
    hideLoading();
  }
}

// ═══ Utilities ════════════════════════════════════════════════════════════
function showLoading(text = 'Loading...') {
  document.getElementById('loading-text').textContent = text;
  document.getElementById('loading').classList.remove('hidden');
}
function hideLoading() {
  document.getElementById('loading').classList.add('hidden');
}
function fmtPct(v) {
  return (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
}
function esc(str) {
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
