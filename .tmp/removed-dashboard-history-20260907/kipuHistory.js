const API_PATH = '/api/kipu-history';
const ECUADOR_TIME_ZONE = 'America/Guayaquil';
const DAY_MS = 86_400_000;
const LOCAL_BACKEND_ERROR = 'No se pudo conectar con el backend local de históricos. Inícialo en 127.0.0.1:8765 y mantén Vite en ejecución.';

const isObject = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const isCount = value => Number.isSafeInteger(value) && value >= 0;
const isRate = value => value === null || (typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1);
const isStrings = value => Array.isArray(value) && value.every(item => typeof item === 'string');

function dateTimestamp(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return NaN;
  const timestamp = Date.parse(`${value}T00:00:00Z`);
  return Number.isFinite(timestamp) && new Date(timestamp).toISOString().slice(0, 10) === value
    ? timestamp : NaN;
}

export function todayInEcuador(now = new Date()) {
  const parts = new Intl.DateTimeFormat('en', {
    timeZone: ECUADOR_TIME_ZONE, year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(now);
  const value = type => parts.find(part => part.type === type)?.value;
  return `${value('year')}-${value('month')}-${value('day')}`;
}

export function defaultHistoryDates(now = new Date()) {
  const today = dateTimestamp(todayInEcuador(now));
  return {
    date_from: new Date(today - 7 * DAY_MS).toISOString().slice(0, 10),
    date_to: new Date(today - DAY_MS).toISOString().slice(0, 10),
  };
}

export function validateHistoryRequest(query, now = new Date()) {
  const merchantCode = typeof query?.merchant_code === 'string' ? query.merchant_code.trim() : '';
  if (!merchantCode) throw new Error('Ingresa el MID del comercio.');
  const from = dateTimestamp(query.date_from);
  const to = dateTimestamp(query.date_to);
  if (!Number.isFinite(from) || !Number.isFinite(to)) {
    throw new Error('Ingresa fechas válidas en formato AAAA-MM-DD.');
  }
  if (from > to) throw new Error('La fecha inicial no puede ser posterior a la fecha final.');
  if ((to - from) / DAY_MS + 1 > 31) throw new Error('El rango máximo es de 31 días, incluyendo ambas fechas.');
  if (to > dateTimestamp(todayInEcuador(now))) throw new Error('No se pueden consultar fechas futuras en Ecuador.');
  if (query.analyze !== undefined && typeof query.analyze !== 'boolean') {
    throw new Error('La opción de análisis debe ser verdadera o falsa.');
  }
  return { merchant_code: merchantCode, date_from: query.date_from, date_to: query.date_to, analyze: query.analyze ?? false };
}

function validMetrics(metrics) {
  if (!isObject(metrics)) return false;
  const counts = ['total_transactions', 'approved_transactions', 'declined_transactions', 'other_transactions'];
  return counts.every(key => isCount(metrics[key])) && isRate(metrics.approval_rate)
    && metrics.total_transactions === metrics.approved_transactions + metrics.declined_transactions + metrics.other_transactions
    && (metrics.total_transactions !== 0 || metrics.approval_rate === null);
}

export function validateKipuHistoryReport(payload) {
  const report = payload?.report ?? payload;
  const invalid = () => { throw new Error('El histórico devolvió un formato de datos incompatible.'); };
  if (!isObject(report) || report.schema_version !== '1.0') invalid();
  const { query, source, metrics, daily, comparison, conclusions, analysis_status: status, analysis_error: analysisError } = report;
  if (!isObject(query) || typeof query.merchant_code !== 'string' || !query.merchant_code.trim()
    || !Number.isFinite(dateTimestamp(query.date_from)) || !Number.isFinite(dateTimestamp(query.date_to))
    || query.date_from > query.date_to || query.timezone !== ECUADOR_TIME_ZONE) invalid();
  if (!isObject(source) || !['catalog', 'database', 'table', 'query_execution_id', 'retrieved_at'].every(key => typeof source[key] === 'string' && source[key])
    || !isCount(source.data_scanned_bytes)) invalid();
  if (!validMetrics(metrics) || !isCount(metrics.observed_days)
    || !Array.isArray(daily) || !daily.every(item => validMetrics(item) && Number.isFinite(dateTimestamp(item.date)))) invalid();
  if (!isObject(comparison) || !isRate(comparison.first_half_approval_rate) || !isRate(comparison.second_half_approval_rate)
    || !(comparison.change_percentage_points === null || (typeof comparison.change_percentage_points === 'number' && Number.isFinite(comparison.change_percentage_points)))) invalid();
  if (!['not_requested', 'completed', 'unavailable', 'no_data'].includes(status) || !isStrings(report.limitations)) invalid();
  if (!(analysisError === null || (isObject(analysisError) && typeof analysisError.code === 'string' && typeof analysisError.message === 'string'))) invalid();
  if (conclusions !== null && (!isObject(conclusions) || typeof conclusions.summary !== 'string'
    || !Array.isArray(conclusions.findings) || !conclusions.findings.every(finding => isObject(finding) && typeof finding.observation === 'string' && isStrings(finding.evidence_ids))
    || !isStrings(conclusions.limitations) || !isStrings(conclusions.next_steps))) invalid();
  if (status === 'completed' && conclusions === null) invalid();
  return report;
}

async function requestHistory(user, { method, body, signal }) {
  if (typeof user?.getIdToken !== 'function') throw new Error('Debes iniciar sesión para consultar el histórico.');
  const token = await user.getIdToken();
  if (typeof token !== 'string' || !token.trim()) throw new Error('La sesión no es válida. Inicia sesión nuevamente para consultar el histórico.');
  let response;
  try {
    response = await fetch(method === 'GET' ? `${API_PATH}/status` : API_PATH, {
      method,
      headers: { Authorization: `Bearer ${token}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
      ...(body ? { body: JSON.stringify(body) } : {}),
      ...(signal ? { signal } : {}),
    });
  } catch (error) {
    if (error?.name === 'AbortError') throw error;
    throw new Error(LOCAL_BACKEND_ERROR, { cause: error });
  }
  const payload = await response.json().catch(() => null);
  if (!payload) throw new Error(LOCAL_BACKEND_ERROR);
  if (!response.ok) {
    const error = new Error(typeof payload.error === 'string' ? payload.error : LOCAL_BACKEND_ERROR);
    if (typeof payload.code === 'string') error.code = payload.code;
    throw error;
  }
  return payload;
}

export async function loadKipuHistory(user, query, { signal } = {}) {
  const body = validateHistoryRequest(query);
  return validateKipuHistoryReport(await requestHistory(user, { method: 'POST', body, signal }));
}

export async function loadKipuHistoryStatus(user, { signal } = {}) {
  const status = await requestHistory(user, { method: 'GET', signal });
  if (typeof status.gemini_configured !== 'boolean' || typeof status.model !== 'string'
    || status.profile !== 'data-core' || !isCount(status.max_days)) {
    throw new Error('El backend local devolvió un estado incompatible.');
  }
  return status;
}
