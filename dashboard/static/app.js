/* ═══════════════════════════════════════════════════════════════════
   Kushki Guardian — Dashboard Application
   ═══════════════════════════════════════════════════════════════════ */

const API = '';

// ── State ───────────────────────────────────────────────────────────
let currentPage = 'analyze';
let healthData = null;

// ── Init ────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  initNavigation();
  initUpload();
  initJsonInput();
  await checkHealth();
});

// ── Navigation ──────────────────────────────────────────────────────
function initNavigation() {
  document.querySelectorAll('.nav-item[data-page]').forEach(btn => {
    btn.addEventListener('click', () => {
      const page = btn.dataset.page;
      showPage(page);
    });
  });
}

function showPage(page) {
  currentPage = page;
  document.querySelectorAll('.page').forEach(p => p.classList.add('hidden'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));

  const el = document.getElementById(`page-${page}`);
  const nav = document.getElementById(`nav-${page}`);
  if (el) el.classList.remove('hidden');
  if (nav) nav.classList.add('active');

  if (page === 'examples') loadExamples();
  if (page === 'reports') loadReports();
  if (page === 'thresholds') loadThresholds();
}

// ── Health ──────────────────────────────────────────────────────────
async function checkHealth() {
  try {
    const res = await fetch(`${API}/api/health`);
    healthData = await res.json();
    document.getElementById('status-dot').style.background = 'var(--kk-green)';
    document.getElementById('status-text').textContent =
      `v${healthData.version.split('-').pop()} · ${healthData.gemini_configured ? 'Gemini ✓' : 'Sin Gemini'}`;
  } catch {
    document.getElementById('status-dot').style.background = '#ef4444';
    document.getElementById('status-text').textContent = 'Desconectado';
  }
}

// ── Upload & JSON Input ─────────────────────────────────────────────
function initUpload() {
  const zone = document.getElementById('upload-zone');
  const fileInput = document.getElementById('file-input');

  zone.addEventListener('click', () => fileInput.click());
  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('dragover'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('dragover');
    const file = e.dataTransfer.files[0];
    if (file) readFile(file);
  });

  fileInput.addEventListener('change', () => {
    if (fileInput.files[0]) readFile(fileInput.files[0]);
  });

  document.getElementById('btn-analyze').addEventListener('click', runAnalysis);
  document.getElementById('btn-clear').addEventListener('click', clearInput);
  document.getElementById('btn-paste-example').addEventListener('click', pasteExample);
}

function readFile(file) {
  const reader = new FileReader();
  reader.onload = e => {
    document.getElementById('json-input').value = e.target.result;
    validateInput();
  };
  reader.readAsText(file);
}

function initJsonInput() {
  const input = document.getElementById('json-input');
  input.addEventListener('input', validateInput);
}

function validateInput() {
  const input = document.getElementById('json-input');
  const btn = document.getElementById('btn-analyze');
  const status = document.getElementById('input-status');
  const raw = input.value.trim();

  if (!raw) {
    btn.disabled = true;
    status.textContent = '';
    return;
  }

  try {
    const data = JSON.parse(raw);
    const detail = data.detail || data;
    if (detail.schema_version === '1.0') {
      btn.disabled = false;
      status.textContent = `✓ Alerta v1.0 · MID: ${detail.merchant_code || '—'} · AR: ${fmtRate(detail.approval_rate)}`;
      status.style.color = 'var(--kk-green)';
    } else {
      btn.disabled = true;
      status.textContent = '⚠ schema_version debe ser 1.0';
      status.style.color = 'var(--requires-review)';
    }
  } catch {
    btn.disabled = true;
    status.textContent = '✕ JSON inválido';
    status.style.color = 'var(--confirmed)';
  }
}

function clearInput() {
  document.getElementById('json-input').value = '';
  document.getElementById('analysis-result').classList.add('hidden');
  document.getElementById('analysis-result').innerHTML = '';
  validateInput();
}

async function pasteExample() {
  try {
    const res = await fetch(`${API}/api/example/kipu-event.json`);
    const data = await res.json();
    document.getElementById('json-input').value = JSON.stringify(data, null, 2);
    validateInput();
  } catch {
    document.getElementById('json-input').value = JSON.stringify({
      schema_version: '1.0',
      merchant_code: '20000000100000000000',
      criticality: 'Critica',
      approval_rate: 0.20,
      rolling_avg_approval_rate: 0.25,
      total_transactions: 100,
      declined_count: 80,
    }, null, 2);
    validateInput();
  }
}

// ── Analysis ────────────────────────────────────────────────────────
async function runAnalysis() {
  const input = document.getElementById('json-input');
  const btn = document.getElementById('btn-analyze');
  const resultDiv = document.getElementById('analysis-result');

  let data;
  try { data = JSON.parse(input.value.trim()); } catch { return; }

  btn.disabled = true;
  btn.innerHTML = '<div class="loading-spinner"></div> Analizando...';
  resultDiv.classList.remove('hidden');
  resultDiv.innerHTML = '<div class="loading-overlay"><div class="loading-spinner"></div>Ejecutando evaluador determinístico...</div>';

  try {
    const res = await fetch(`${API}/api/analyze`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    const result = await res.json();

    if (!res.ok) {
      resultDiv.innerHTML = renderError(result.detail || 'Error al analizar la alerta');
      return;
    }

    resultDiv.innerHTML = renderAnalysisResult(result);
  } catch (err) {
    resultDiv.innerHTML = renderError(`Error de conexión: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '🚀 Analizar';
    validateInput();
  }
}

// ── Render Analysis Result ──────────────────────────────────────────
function renderAnalysisResult(result) {
  const analysis = result.analysis || result;
  const verdict = analysis.verdict || result.verdict;
  const reason = analysis.decision_reason || result.decision_reason || '—';
  const signals = analysis.signals || result.signals || [];
  const summary = analysis.summary || '';
  const findings = analysis.findings || [];
  const limitations = analysis.limitations || [];
  const nextSteps = analysis.next_steps || [];
  const metrics = result.alert_metrics || result.evidence_summary || {};
  const thresholds = result.thresholds || healthData?.thresholds || {};
  const mode = result.mode || 'unknown';

  const verdictConfig = {
    confirmed: { icon: '🔴', label: 'Alerta Confirmada', color: 'confirmed' },
    not_supported: { icon: '🟢', label: 'No Soportada', color: 'not_supported' },
    requires_review: { icon: '🟡', label: 'Requiere Revisión', color: 'requires_review' },
  };
  const vc = verdictConfig[verdict] || verdictConfig.requires_review;

  return `
    <div class="analysis-result">
      <!-- Verdict Hero -->
      <div class="verdict-hero ${vc.color}">
        <div class="verdict-icon">${vc.icon}</div>
        <div class="verdict-title">${vc.label}</div>
        <div class="verdict-reason">${formatReason(reason)}</div>
        <div class="mt-3">
          <span class="verdict-badge ${vc.color}">${verdict.toUpperCase().replace('_', ' ')}</span>
        </div>
      </div>

      <!-- Metrics -->
      <div class="metrics-grid">
        ${renderMetric('Tasa de Aceptación', fmtRate(metrics.approval_rate ?? metrics.alert_approval_rate_pct / 100), 'Alerta')}
        ${renderMetric('Transacciones', fmtNum(metrics.total_transactions ?? metrics.alert_total_transactions), 'Total')}
        ${renderMetric('Rechazos', fmtNum(metrics.declined_count ?? metrics.alert_declined_count), 'Declinadas')}
        ${renderMetric('Hist. Aceptación', metrics.historical_approval_rate_pct != null ? metrics.historical_approval_rate_pct.toFixed(1) + '%' : 'Sin datos', 'Período')}
        ${renderMetric('Días Observados', fmtNum(metrics.observed_days) || '0', `Mínimo: ${thresholds.min_observed_days || 7}`)}
        ${renderMetric('Trx Históricas', fmtNum(metrics.historical_total_transactions) || '0', `Mínimo: ${thresholds.min_total_transactions || 100}`)}
      </div>

      <!-- Signals -->
      ${signals.length > 0 ? `
        <div class="card mb-6">
          <div class="card-header"><h3>⚡ Señales Convergentes</h3></div>
          <div class="card-body">
            <div class="signal-list">
              ${signals.map(s => `<span class="signal-pill ${signalClass(s)}">${formatSignal(s)}</span>`).join('')}
            </div>
          </div>
        </div>
      ` : ''}

      <!-- Tabs: Summary / Findings / JSON -->
      <div class="card mb-6">
        <div class="card-body">
          <div class="tabs">
            <button class="tab active" onclick="switchTab(this, 'summary-tab')">Resumen</button>
            <button class="tab" onclick="switchTab(this, 'findings-tab')">Hallazgos</button>
            <button class="tab" onclick="switchTab(this, 'raw-tab')">JSON Completo</button>
          </div>

          <div id="summary-tab">
            ${summary ? `<p style="font-size:14px; line-height:1.8; color:var(--text-primary); margin-bottom:16px">${summary}</p>` : ''}
            ${limitations.length > 0 ? `
              <div class="section-title"><span class="icon">⚠️</span> Limitaciones</div>
              <ul style="list-style:none; padding:0">
                ${limitations.map(l => `<li class="finding-item"><span style="color:var(--requires-review)">⚠</span> ${l}</li>`).join('')}
              </ul>
            ` : ''}
            ${nextSteps.length > 0 ? `
              <div class="section-title mt-4"><span class="icon">➡️</span> Próximos Pasos</div>
              <ul style="list-style:none; padding:0">
                ${nextSteps.map(s => `<li class="finding-item"><span style="color:var(--kk-green)">→</span> ${s}</li>`).join('')}
              </ul>
            ` : ''}
            ${result.note ? `<p style="font-size:12px; color:var(--text-muted); margin-top:16px; font-style:italic">ℹ️ ${result.note}</p>` : ''}
          </div>

          <div id="findings-tab" class="hidden">
            ${findings.length > 0 ? findings.map(f => `
              <div class="finding-item">
                <div class="finding-observation">${f.observation}</div>
                ${f.evidence_ids ? `
                  <div class="finding-evidence">
                    ${f.evidence_ids.map(id => `<span class="evidence-tag">${id}</span>`).join('')}
                  </div>
                ` : ''}
              </div>
            `).join('') : '<div class="empty-state"><p>Sin hallazgos detallados para este análisis.</p></div>'}
          </div>

          <div id="raw-tab" class="hidden">
            <div class="json-viewer">${syntaxHighlight(JSON.stringify(result, null, 2))}</div>
          </div>
        </div>
      </div>

      <!-- Mode Badge -->
      <div style="text-align:center; padding:12px">
        <span class="signal-pill" style="font-size:10px">
          Modo: ${mode} · Versión: ${healthData?.version || '—'}
        </span>
      </div>
    </div>
  `;
}

function renderMetric(label, value, sub) {
  return `
    <div class="metric-card">
      <div class="metric-label">${label}</div>
      <div class="metric-value">${value}</div>
      <div class="metric-sub">${sub}</div>
    </div>
  `;
}

function renderError(msg) {
  return `
    <div class="card">
      <div class="card-body" style="text-align:center; padding:40px">
        <div style="font-size:40px; margin-bottom:12px">❌</div>
        <h3 style="color:var(--confirmed); margin-bottom:8px">Error en el Análisis</h3>
        <p style="color:var(--text-secondary); font-size:13px">${msg}</p>
      </div>
    </div>
  `;
}

// ── Tabs ─────────────────────────────────────────────────────────────
function switchTab(btn, tabId) {
  const parent = btn.closest('.card-body');
  parent.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');

  ['summary-tab', 'findings-tab', 'raw-tab'].forEach(id => {
    const el = parent.querySelector(`#${id}`) || document.getElementById(id);
    if (el) el.classList.toggle('hidden', id !== tabId);
  });
}

// ── Examples ────────────────────────────────────────────────────────
async function loadExamples() {
  const container = document.getElementById('examples-list');
  try {
    const res = await fetch(`${API}/api/examples`);
    const data = await res.json();

    if (!data.examples.length) {
      container.innerHTML = '<div class="empty-state"><div class="icon">📋</div><h3>Sin ejemplos</h3><p>No se encontraron archivos JSON en la carpeta examples/</p></div>';
      return;
    }

    container.innerHTML = data.examples.map(ex => `
      <div class="example-item" onclick="loadExample('${ex.filename}')">
        <div>
          <div class="name">📄 ${ex.filename}</div>
          <div class="meta">MID: ${ex.merchant_code} · AR: ${fmtRate(ex.approval_rate)} · Trx: ${fmtNum(ex.total_transactions)}</div>
        </div>
        <button class="btn btn-sm btn-primary">Cargar</button>
      </div>
    `).join('');
  } catch {
    container.innerHTML = '<div class="empty-state"><h3>Error</h3><p>No se pudieron cargar los ejemplos</p></div>';
  }
}

async function loadExample(filename) {
  try {
    const res = await fetch(`${API}/api/example/${filename}`);
    const data = await res.json();
    document.getElementById('json-input').value = JSON.stringify(data, null, 2);
    validateInput();
    showPage('analyze');
  } catch {
    alert('Error al cargar el ejemplo');
  }
}

// ── Reports ─────────────────────────────────────────────────────────
async function loadReports() {
  const container = document.getElementById('reports-content');
  try {
    const res = await fetch(`${API}/api/reports`);
    const data = await res.json();

    if (!data.reports.length) {
      container.innerHTML = `
        <div class="empty-state">
          <div class="icon">📊</div>
          <h3>Sin reportes guardados</h3>
          <p>Los reportes de Guardian se crearán al ejecutar análisis con contexto histórico completo.</p>
        </div>
      `;
      return;
    }

    container.innerHTML = `
      <table class="data-table">
        <thead>
          <tr>
            <th>Caso</th>
            <th>Veredicto</th>
            <th>Versión</th>
            <th>Fecha</th>
          </tr>
        </thead>
        <tbody>
          ${data.reports.map(r => `
            <tr>
              <td><span style="font-family:monospace; font-size:12px">${r.case_id.slice(0, 16)}…</span></td>
              <td>${r.verdict ? `<span class="verdict-badge ${r.verdict}">${r.verdict}</span>` : '—'}</td>
              <td style="font-size:11px; color:var(--text-muted)">${r.analysis_version || '—'}</td>
              <td style="font-size:11px; color:var(--text-muted)">${r.created_at ? new Date(r.created_at).toLocaleString('es-EC') : '—'}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;
  } catch {
    container.innerHTML = '<div class="empty-state"><h3>Error</h3><p>No se pudieron cargar los reportes</p></div>';
  }
}

// ── Thresholds ──────────────────────────────────────────────────────
async function loadThresholds() {
  const container = document.getElementById('thresholds-content');
  if (!healthData) await checkHealth();

  const t = healthData?.thresholds || {};
  container.innerHTML = `
    <div class="card mb-6">
      <div class="card-header"><h3>📐 Umbrales Determinísticos</h3></div>
      <div class="card-body">
        <div class="info-grid">
          <div class="info-item">
            <div class="label">Caída de Aceptación</div>
            <div class="value" style="color:var(--confirmed)">${t.approval_drop_pp || 10} pp</div>
          </div>
          <div class="info-item">
            <div class="label">Días Mínimos</div>
            <div class="value">${t.min_observed_days || 7}</div>
          </div>
          <div class="info-item">
            <div class="label">Transacciones Mínimas</div>
            <div class="value">${t.min_total_transactions || 100}</div>
          </div>
        </div>
      </div>
    </div>

    <div class="card mb-6">
      <div class="card-header"><h3>🔀 Lógica de Decisión</h3></div>
      <div class="card-body">
        <table class="data-table">
          <thead><tr><th>Condición</th><th>Veredicto</th></tr></thead>
          <tbody>
            <tr>
              <td>Cobertura < ${t.min_observed_days || 7} días o < ${t.min_total_transactions || 100} transacciones</td>
              <td><span class="verdict-badge requires_review">REQUIRES REVIEW</span></td>
            </tr>
            <tr>
              <td>Caída ≥ ${t.approval_drop_pp || 10} puntos porcentuales</td>
              <td><span class="verdict-badge confirmed">CONFIRMED</span></td>
            </tr>
            <tr>
              <td>Caída 0-${t.approval_drop_pp || 10}pp + tendencia descendente > 5pp</td>
              <td><span class="verdict-badge confirmed">CONFIRMED</span></td>
            </tr>
            <tr>
              <td>Caída 0-${t.approval_drop_pp || 10}pp sin tendencia</td>
              <td><span class="verdict-badge requires_review">REQUIRES REVIEW</span></td>
            </tr>
            <tr>
              <td>Sin caída + sin tendencia negativa</td>
              <td><span class="verdict-badge not_supported">NOT SUPPORTED</span></td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="card">
      <div class="card-header"><h3>🔌 Estado del Sistema</h3></div>
      <div class="card-body">
        <div class="info-grid">
          <div class="info-item">
            <div class="label">Versión</div>
            <div class="value" style="font-size:13px">${healthData?.version || '—'}</div>
          </div>
          <div class="info-item">
            <div class="label">Gemini</div>
            <div class="value" style="color:${healthData?.gemini_configured ? 'var(--kk-green)' : 'var(--confirmed)'}">
              ${healthData?.gemini_configured ? '✓ Configurado' : '✕ Sin clave'}
            </div>
          </div>
          <div class="info-item">
            <div class="label">Estado</div>
            <div class="value" style="color:var(--kk-green)">● Online</div>
          </div>
        </div>
      </div>
    </div>
  `;
}

// ── Helpers ─────────────────────────────────────────────────────────
function fmtRate(v) {
  if (v == null) return '—';
  return typeof v === 'number' ? `${(v * 100).toFixed(1)}%` : v;
}

function fmtNum(v) {
  if (v == null) return '—';
  return typeof v === 'number' ? v.toLocaleString('es-EC') : v;
}

function formatReason(reason) {
  const map = {
    'significant_approval_rate_drop': 'Caída significativa en tasa de aceptación (≥ 10pp)',
    'minor_drop_with_declining_trend': 'Caída menor con tendencia descendente convergente',
    'minor_drop_without_convergence': 'Caída menor sin evidencia de tendencia',
    'no_significant_deterioration': 'Sin deterioro significativo respecto al histórico',
    'insufficient_coverage': 'Cobertura insuficiente (días u operaciones bajo el mínimo)',
    'missing_alert_approval_rate': 'Tasa de aceptación no disponible en la alerta',
    'missing_historical_approval_rate': 'Tasa de aceptación histórica no disponible',
    'ambiguous_signals': 'Señales ambiguas entre alerta e histórico',
    'no_historical_data': 'Sin datos históricos disponibles para comparar',
  };
  return map[reason] || reason;
}

function formatSignal(signal) {
  const map = {
    'significant_approval_drop': '📉 Caída significativa',
    'declining_historical_trend': '📊 Tendencia descendente',
    'high_decline_volume': '🔺 Alto volumen de rechazos',
    'low_approval_rate': '⚠️ Tasa de aprobación baja',
    'high_decline_ratio': '🔴 Ratio de rechazo alto',
  };
  return map[signal] || signal;
}

function signalClass(signal) {
  if (signal.includes('drop') || signal.includes('decline_volume') || signal.includes('decline_ratio')) return 'danger';
  if (signal.includes('trend') || signal.includes('low_approval')) return 'warning';
  return 'active';
}

function syntaxHighlight(json) {
  return json
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"([^"]+)"(?=\s*:)/g, '<span class="key">"$1"</span>')
    .replace(/:\s*"([^"]*)"/g, ': <span class="string">"$1"</span>')
    .replace(/:\s*(\d+\.?\d*)/g, ': <span class="number">$1</span>')
    .replace(/:\s*(true|false)/g, ': <span class="boolean">$1</span>')
    .replace(/:\s*(null)/g, ': <span class="null">$1</span>');
}
