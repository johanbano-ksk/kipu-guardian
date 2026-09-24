const API = '';
let healthData = null;

document.addEventListener('DOMContentLoaded', async () => {
  initNavigation();
  initLiveAlerts();
  await checkHealth();
});

function initNavigation() {
  document.querySelectorAll('.nav-item[data-page]').forEach((button) => {
    button.addEventListener('click', () => showPage(button.dataset.page));
  });
}

function showPage(page) {
  document.querySelectorAll('.page').forEach((item) => item.classList.add('hidden'));
  document.querySelectorAll('.nav-item').forEach((item) => item.classList.remove('active'));
  document.getElementById(`page-${page}`)?.classList.remove('hidden');
  document.getElementById(`nav-${page}`)?.classList.add('active');
  if (page === 'thresholds') loadThresholds();
}

async function checkHealth() {
  const dot = document.getElementById('status-dot');
  const text = document.getElementById('status-text');
  try {
    const response = await fetch(`${API}/api/health`);
    healthData = await response.json();
    dot.classList.add('online');
    text.textContent = healthData.gemini_configured ? 'Operativo · Gemini' : 'Operativo';
  } catch {
    dot.classList.add('offline');
    text.textContent = 'Sin conexión';
  }
}

function initLiveAlerts() {
  const button = document.getElementById('btn-live-alerts');
  const dateInput = document.getElementById('live-date');
  const now = new Date();
  const localDate = new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  dateInput.value = localDate;
  button.addEventListener('click', loadLiveAlerts);
}

async function loadLiveAlerts() {
  const button = document.getElementById('btn-live-alerts');
  const dateInput = document.getElementById('live-date');
  const status = document.getElementById('live-status');
  const summary = document.getElementById('live-summary');
  const container = document.getElementById('live-alerts');
  const date = dateInput.value;

  if (!date) {
    status.textContent = 'Selecciona una fecha válida.';
    return;
  }

  button.disabled = true;
  button.innerHTML = '<span class="spinner"></span><span>Procesando</span>';
  status.textContent = 'Extrayendo alertas y consultando histórico...';
  summary.innerHTML = '';
  container.innerHTML = renderLoading();

  try {
    const response = await fetch(`${API}/api/live-alerts`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date, mode: 'policy' }),
    });
    const data = await response.json();

    if (!response.ok || data.status !== 'completed') {
      summary.innerHTML = '';
      container.innerHTML = renderError(data.message || 'No se pudieron obtener las alertas.');
      status.textContent = 'La consulta no pudo completarse.';
      return;
    }

    status.textContent = `${data.total_accepted} aceptadas · ${data.total_with_history} con histórico · ${data.total_analyzed} analizadas completas`;
    summary.innerHTML = renderLiveSummary(data);
    container.innerHTML = renderLiveAlerts(data.alerts || []);
  } catch (error) {
    summary.innerHTML = '';
    container.innerHTML = renderError(`Error de conexión: ${error.message}`);
    status.textContent = 'Error de conexión con Guardian.';
  } finally {
    button.disabled = false;
    button.innerHTML = '<span>Consultar alertas</span><span class="btn-arrow">→</span>';
  }
}

function renderLiveSummary(data) {
  return `<div class="summary-grid">
    ${renderSummaryCard('Recibidas', data.total_received, 'Eventos Kipu', 'neutral')}
    ${renderSummaryCard('Evaluadas', data.total_evaluated, 'Política activa', 'blue')}
    ${renderSummaryCard('Aceptadas', data.total_accepted, 'Enviadas a Guardian', 'green')}
    ${renderSummaryCard('Con histórico', data.total_with_history, 'Evidencia disponible', 'purple')}
    ${renderSummaryCard('Analizadas', data.total_analyzed, data.total_analysis_skipped ? `${data.total_analysis_skipped} sin completar` : 'Completas', 'amber')}
  </div>`;
}

function renderSummaryCard(label, value, detail, tone) {
  return `<article class="summary-card ${tone}"><div class="summary-label">${escapeHtml(label)}</div><div class="summary-value">${fmtNum(value)}</div><div class="summary-detail">${escapeHtml(detail)}</div></article>`;
}

function renderLiveAlerts(alerts) {
  if (!alerts.length) {
    return `<div class="empty-state compact"><div class="empty-icon success">✓</div><h3>Sin alertas aceptadas</h3><p>No se encontraron alertas que requieran revisión para la fecha seleccionada.</p></div>`;
  }

  return alerts.map((alert) => {
    const guardian = alert.guardian_analysis || {};
    const conclusions = guardian.conclusions || {};
    const verdict = conclusions.verdict || (guardian.analysis_status === 'no_data' ? 'no_data' : 'requires_review');
    const config = verdictConfig(verdict);
    const history = guardian.history || {};
    const historyMetrics = history.metrics || {};
    const summary = conclusions.summary || (guardian.analysis_status === 'no_data' ? 'No hay datos históricos elegibles suficientes para evaluar la tasa de aceptación.' : guardian.analysis_error ? 'La revisión determinística está disponible, pero la explicación con IA no pudo generarse.' : 'Guardian no generó una conclusión para esta alerta.');

    return `<article class="alert-card">
      <div class="alert-accent ${config.css}"></div>
      <div class="alert-header">
        <div class="merchant-block"><div class="merchant-avatar">${merchantInitial(alert)}</div><div><div class="merchant-name">${escapeHtml(alert.merchant_name || 'Comercio sin nombre')}</div><div class="merchant-meta"><span>${escapeHtml(alert.country || '—')}</span><span class="meta-separator"></span><span>MID ${escapeHtml(alert.merchant_code || '—')}</span></div></div></div>
        <div class="verdict-pill ${config.css}"><span class="verdict-dot"></span>${config.label}</div>
      </div>
      <div class="alert-body">
        <div class="alert-metrics">
          ${renderAlertMetric('Aceptación actual', fmtRate(alert.approval_rate))}
          ${renderAlertMetric('Baseline Kipu', fmtRate(alert.rolling_avg_approval_rate))}
          ${renderAlertMetric('Transacciones', fmtNum(alert.total_transactions))}
          ${renderAlertMetric('Rechazos', fmtNum(alert.declined_count))}
          ${renderAlertMetric('Histórico', historyMetrics.approval_rate_pct != null ? `${Number(historyMetrics.approval_rate_pct).toFixed(1)}%` : '—')}
        </div>
        <div class="guardian-panel">
          <div class="guardian-panel-header"><div><span class="eyebrow">Guardian assessment</span><h4>Conclusión</h4></div><div class="status-chips">${renderStatusChip('Histórico', guardian.history_status)}${renderStatusChip('Análisis', guardian.analysis_status)}</div></div>
          <p class="guardian-summary">${escapeHtml(summary)}</p>
          ${guardian.analysis_error ? renderAnalysisNotice(guardian.analysis_error) : ''}
          ${renderAiResponse(conclusions)}
        </div>
        <details class="technical-details">
          <summary>Ver detalle técnico</summary>
          <div class="technical-grid">
            <div><span>Alert ID</span><strong>${escapeHtml(alert.alert_id || '—')}</strong></div>
            <div><span>Policy</span><strong>${escapeHtml(guardian.policy?.version || '—')}</strong></div>
            <div><span>Hora</span><strong>${formatDate(alert.timestamp || alert.kipu_generated_at)}</strong></div>
            <div><span>Modelo</span><strong>${escapeHtml(guardian.analysis_model || 'Determinístico')}</strong></div>
          </div>
          <pre>${escapeHtml(JSON.stringify(guardian, null, 2))}</pre>
        </details>
      </div>
    </article>`;
  }).join('');
}

function renderAlertMetric(label, value) {
  return `<div class="alert-metric"><span>${escapeHtml(label)}</span><strong>${escapeHtml(String(value))}</strong></div>`;
}

function renderStatusChip(label, status) {
  const value = status || 'not_requested';
  const cls = value === 'completed' ? 'ok' : value === 'unavailable' || value === 'skipped' ? 'warn' : 'neutral';
  const translated = { completed: 'Completo', unavailable: 'No disponible', skipped: 'Omitido', no_data: 'Sin datos', not_requested: 'Pendiente' }[value] || value;
  return `<span class="status-chip ${cls}">${escapeHtml(label)} · ${escapeHtml(translated)}</span>`;
}

function renderAiResponse(conclusions) {
  if (!conclusions || !Object.keys(conclusions).length) return '';

  const findings = Array.isArray(conclusions.findings) ? conclusions.findings : [];
  const limitations = Array.isArray(conclusions.limitations) ? conclusions.limitations : [];
  const nextSteps = Array.isArray(conclusions.next_steps) ? conclusions.next_steps : [];

  return `<div class="ai-response-grid">
    ${findings.length ? renderAiSection(
      'Hallazgos relevantes',
      'Lo que sustenta la evaluación',
      findings.slice(0, 3).map((finding) => ({
        text: finding.observation || '',
        evidence: finding.evidence_ids || [],
      })),
      'finding'
    ) : ''}
    ${limitations.length ? renderAiSection(
      'Limitaciones',
      'Qué no puede concluirse con esta evidencia',
      limitations.slice(0, 3).map((text) => ({ text })),
      'limitation'
    ) : ''}
    ${nextSteps.length ? renderAiSection(
      'Siguiente acción',
      'Paso recomendado para cerrar la revisión',
      nextSteps.slice(0, 1).map((text) => ({ text })),
      'action'
    ) : ''}
  </div>`;
}

function renderAiSection(title, subtitle, items, type) {
  const icons = { finding: '↳', limitation: '!', action: '→' };
  return `<section class="ai-section ${type}">
    <div class="ai-section-header">
      <div><h5>${escapeHtml(title)}</h5><span>${escapeHtml(subtitle)}</span></div>
    </div>
    <div class="ai-section-items">
      ${items.map((item) => `<div class="ai-item">
        <span class="ai-item-icon">${icons[type] || '•'}</span>
        <div class="ai-item-content">
          <p>${escapeHtml(item.text || '')}</p>
          ${item.evidence?.length ? `<div class="evidence-list">${item.evidence.map((id) => `<span>${escapeHtml(formatEvidenceId(id))}</span>`).join('')}</div>` : ''}
        </div>
      </div>`).join('')}
    </div>
  </section>`;
}

function formatEvidenceId(id) {
  if (id === 'alert_0') return 'Alerta Kipu';
  if (id === 'quality_0') return 'Calidad de datos';
  if (/^day_\d+$/.test(id)) return `Histórico · día ${Number(id.split('_')[1]) + 1}`;
  return id;
}

function renderFindings(findings) {
  if (!findings.length) return '';
  return `<div class="findings-list">${findings.slice(0, 3).map((finding) => `<div class="finding-row"><span>↳</span><p>${escapeHtml(finding.observation || '')}</p></div>`).join('')}</div>`;
}

function verdictConfig(verdict) {
  return {
    confirmed: { label: 'Confirmada', css: 'danger' },
    no_data: { label: 'Sin datos', css: 'neutral' },
    requires_review: { label: 'Requiere revisión', css: 'warning' },
  }[verdict] || { label: 'Requiere revisión', css: 'warning' };
}

function renderLoading() {
  return '<div class="loading-state"><span class="spinner large"></span><h3>Analizando alertas</h3><p>Guardian está consultando la fuente Kipu y el histórico transaccional.</p></div>';
}

function renderError(message) {
  return `<div class="error-state"><div class="error-icon">!</div><div><h3>No se pudo completar la consulta</h3><p>${escapeHtml(message)}</p></div></div>`;
}

async function loadThresholds() {
  const container = document.getElementById('thresholds-content');
  if (!healthData) await checkHealth();
  const thresholds = healthData?.thresholds || {};
  container.innerHTML = `<div class="settings-grid">
    <article class="settings-card"><span class="eyebrow">Decision engine</span><h3>Umbrales activos</h3><div class="settings-list">${settingRow('Caída de aceptación', `${thresholds.approval_drop_pp ?? 10} pp`)}${settingRow('Días mínimos', thresholds.min_observed_days ?? 7)}${settingRow('Transacciones mínimas', thresholds.min_total_transactions ?? 100)}</div></article>
    <article class="settings-card"><span class="eyebrow">Runtime</span><h3>Estado del sistema</h3><div class="settings-list">${settingRow('Versión', healthData?.version || '—')}${settingRow('Gemini', healthData?.gemini_configured ? 'Configurado' : 'Sin configurar')}${settingRow('API', 'Online')}</div></article>
  </div>`;
}

function settingRow(label, value) {
  return `<div class="setting-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(String(value))}</strong></div>`;
}

function merchantInitial(alert) {
  const name = alert.merchant_name || alert.merchant_code || 'K';
  return escapeHtml(name.trim().charAt(0).toUpperCase());
}

function fmtRate(value) {
  if (value == null || Number.isNaN(Number(value))) return '—';
  return `${(Number(value) * 100).toFixed(1)}%`;
}

function fmtNum(value) {
  if (value == null || value === '') return '—';
  return Number(value).toLocaleString('es-EC');
}

function formatDate(value) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('es-EC');
}

function escapeHtml(value) {
  return String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#039;');
}

function renderAnalysisNotice(error) {
  const code = error?.code || 'AI_UNAVAILABLE';
  return `<div class="analysis-notice"><span class="analysis-notice-icon">i</span><div><strong>Explicación IA no disponible</strong><span>El veredicto determinístico no depende de este servicio. Código: ${escapeHtml(code)}</span></div></div>`;
}
