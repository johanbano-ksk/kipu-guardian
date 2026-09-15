import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  defaultHistoryDates,
  loadKipuHistory,
  loadKipuHistoryStatus,
  todayInEcuador,
  validateHistoryRequest,
  validateKipuHistoryReport,
} from './kipuHistory.js';

const NOW = new Date('2026-09-07T03:00:00Z');
const QUERY = {
  merchant_code: '10001',
  date_from: '2026-08-30',
  date_to: '2026-09-05',
};

function historyReport() {
  return {
    schema_version: '1.0',
    query: { ...QUERY, timezone: 'America/Guayaquil' },
    source: {
      catalog: 'AwsDataCatalog',
      database: 'odl',
      table: 'transactions',
      query_execution_id: 'query-123',
      data_scanned_bytes: 1024,
      retrieved_at: '2026-09-07T02:59:00Z',
    },
    metrics: {
      total_transactions: 100,
      approved_transactions: 70,
      declined_transactions: 20,
      other_transactions: 10,
      approval_rate: 0.7,
      observed_days: 2,
    },
    daily: [
      {
        date: '2026-08-30', total_transactions: 60, approved_transactions: 42,
        declined_transactions: 12, other_transactions: 6, approval_rate: 0.7,
      },
      {
        date: '2026-08-31', total_transactions: 40, approved_transactions: 28,
        declined_transactions: 8, other_transactions: 4, approval_rate: 0.7,
      },
    ],
    comparison: {
      first_half_approval_rate: 0.7,
      second_half_approval_rate: null,
      change_percentage_points: null,
    },
    conclusions: null,
    analysis_status: 'not_requested',
    analysis_error: null,
    limitations: ['Sólo se muestran días con transacciones observadas.'],
  };
}

const authenticatedUser = () => ({ getIdToken: vi.fn().mockResolvedValue('firebase-token') });
const jsonResponse = (payload, ok = true) => ({
  ok,
  json: vi.fn().mockResolvedValue(payload),
});

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('fechas del histórico en Ecuador', () => {
  it('usa America/Guayaquil aunque UTC ya sea el día siguiente', () => {
    expect(todayInEcuador(NOW)).toBe('2026-09-06');
    expect(todayInEcuador()).toBe('2026-09-06');
    expect(todayInEcuador(new Date('2026-09-07T05:00:00Z'))).toBe('2026-09-07');
  });

  it('propone los últimos siete días completos, sin incluir hoy', () => {
    expect(defaultHistoryDates(NOW)).toEqual({
      date_from: '2026-08-30',
      date_to: '2026-09-05',
    });
    expect(defaultHistoryDates()).toEqual({
      date_from: '2026-08-30',
      date_to: '2026-09-05',
    });
  });

  it('calcula correctamente el rango que cruza el inicio de año', () => {
    expect(defaultHistoryDates(new Date('2026-01-01T15:00:00Z'))).toEqual({
      date_from: '2025-12-25',
      date_to: '2025-12-31',
    });
  });
});

describe('validateHistoryRequest', () => {
  it('recorta el MID y mantiene el análisis desactivado por defecto', () => {
    expect(validateHistoryRequest({ ...QUERY, merchant_code: '  10001  ' }, NOW))
      .toEqual({ ...QUERY, analyze: false });
  });

  it('conserva la activación explícita del análisis', () => {
    expect(validateHistoryRequest({ ...QUERY, analyze: true }, NOW))
      .toEqual({ ...QUERY, analyze: true });
  });

  it('acepta 31 días inclusivos y rechaza 32', () => {
    expect(validateHistoryRequest({
      ...QUERY, date_from: '2026-08-01', date_to: '2026-08-31',
    }, NOW)).toMatchObject({ date_from: '2026-08-01', date_to: '2026-08-31' });
    expect(() => validateHistoryRequest({
      ...QUERY, date_from: '2026-08-01', date_to: '2026-09-01',
    }, NOW)).toThrow();
  });

  it('acepta hoy en Ecuador como consulta de un solo día', () => {
    expect(validateHistoryRequest({
      ...QUERY, date_from: '2026-09-06', date_to: '2026-09-06',
    }, NOW)).toMatchObject({ date_from: '2026-09-06', date_to: '2026-09-06' });
  });

  it('rechaza mañana en Ecuador aunque sea hoy en UTC', () => {
    expect(() => validateHistoryRequest({ ...QUERY, date_to: '2026-09-07' }, NOW))
      .toThrow();
  });

  it.each([
    ['MID vacío', { merchant_code: '   ' }],
    ['MID ausente', { merchant_code: undefined }],
    ['rango invertido', { date_from: '2026-09-05', date_to: '2026-08-30' }],
    ['fecha inexistente', { date_from: '2026-02-30', date_to: '2026-03-01' }],
    ['fecha inicial sin ceros', { date_from: '2026-8-30' }],
    ['fecha final sin ceros', { date_to: '2026-9-5' }],
    ['timestamp en vez de fecha', { date_to: '2026-09-05T00:00:00Z' }],
    ['fecha ausente', { date_from: undefined }],
    ['análisis no booleano', { analyze: 'true' }],
  ])('rechaza %s', (_label, changes) => {
    expect(() => validateHistoryRequest({ ...QUERY, ...changes }, NOW)).toThrow();
  });
});

describe('validateKipuHistoryReport', () => {
  it('acepta el contrato 1.0 directo o dentro de report sin alterar sus valores', () => {
    const report = historyReport();

    expect(validateKipuHistoryReport(report)).toBe(report);
    expect(validateKipuHistoryReport({ report })).toBe(report);
    expect(report.metrics.approval_rate).toBe(0.7);
    expect(report.comparison.second_half_approval_rate).toBeNull();
    expect(report.comparison.change_percentage_points).toBeNull();
  });

  it('preserva las tasas desconocidas como null, sin convertirlas en cero', () => {
    const report = historyReport();
    report.metrics.approval_rate = null;
    report.daily[0].approval_rate = null;
    report.comparison.first_half_approval_rate = null;

    const validated = validateKipuHistoryReport(report);

    expect(validated.metrics.approval_rate).toBeNull();
    expect(validated.daily[0].approval_rate).toBeNull();
    expect(validated.comparison).toEqual({
      first_half_approval_rate: null,
      second_half_approval_rate: null,
      change_percentage_points: null,
    });
  });

  it('acepta un reporte sin transacciones y tasa desconocida', () => {
    const report = historyReport();
    report.metrics = {
      total_transactions: 0,
      approved_transactions: 0,
      declined_transactions: 0,
      other_transactions: 0,
      approval_rate: null,
      observed_days: 0,
    };
    report.daily = [];
    report.comparison.first_half_approval_rate = null;
    report.analysis_status = 'no_data';

    expect(validateKipuHistoryReport(report)).toBe(report);
    report.metrics.approval_rate = 0;
    expect(() => validateKipuHistoryReport(report)).toThrow();
  });

  it('no acepta cero como tasa de un día sin transacciones', () => {
    const report = historyReport();
    report.daily.push({
      date: '2026-09-01', total_transactions: 0, approved_transactions: 0,
      declined_transactions: 0, other_transactions: 0, approval_rate: null,
    });

    expect(validateKipuHistoryReport(report).daily[2].approval_rate).toBeNull();
    report.daily[2].approval_rate = 0;
    expect(() => validateKipuHistoryReport(report)).toThrow();
  });

  it('acepta conclusiones estructuradas con referencias a evidencia', () => {
    const report = historyReport();
    report.analysis_status = 'completed';
    report.conclusions = {
      summary: 'La tasa observada fue de 70%.',
      findings: [{
        observation: 'Los dos días observados tuvieron la misma tasa.',
        evidence_ids: ['day_0', 'day_1', 'comparison_0'],
      }],
      limitations: ['La segunda mitad del período no tiene observaciones.'],
      next_steps: ['Contrastar con los días del próximo período.'],
    };

    expect(validateKipuHistoryReport({ report }).conclusions).toEqual(report.conclusions);
  });

  it('mantiene los datos determinísticos cuando el análisis no está disponible', () => {
    const report = historyReport();
    report.analysis_status = 'unavailable';
    report.analysis_error = { code: 'GEMINI_UNAVAILABLE', message: 'El modelo no respondió.' };

    expect(validateKipuHistoryReport(report)).toMatchObject({
      metrics: report.metrics,
      conclusions: null,
      analysis_status: 'unavailable',
      analysis_error: report.analysis_error,
    });
  });

  it.each([null, undefined, [], {}, { report: null }, { metrics: {} }])(
    'rechaza respuestas vacías o incompletas (%j)',
    payload => {
      expect(() => validateKipuHistoryReport(payload)).toThrow();
    },
  );

  it.each([
    ['versión desconocida', report => { report.schema_version = '2.0'; }],
    ['MID vacío', report => { report.query.merchant_code = ''; }],
    ['zona horaria distinta', report => { report.query.timezone = 'UTC'; }],
    ['fecha inexistente', report => { report.query.date_from = '2026-02-30'; }],
    ['rango invertido', report => { report.query.date_to = '2026-08-01'; }],
    ['fuente ausente', report => { delete report.source; }],
    ['ID de consulta vacío', report => { report.source.query_execution_id = ''; }],
    ['bytes negativos', report => { report.source.data_scanned_bytes = -1; }],
    ['conteo negativo', report => { report.metrics.approved_transactions = -1; }],
    ['conteo fraccionario', report => { report.metrics.total_transactions = 100.5; }],
    ['conteo serializado como texto', report => { report.metrics.total_transactions = '100'; }],
    ['conteos incompatibles', report => { report.metrics.total_transactions = 101; }],
    ['días observados fraccionarios', report => { report.metrics.observed_days = 1.5; }],
    ['tasa fuera de rango', report => { report.metrics.approval_rate = 70; }],
    ['tasa desconocida en texto', report => { report.metrics.approval_rate = 'unknown'; }],
    ['tasa ausente', report => { delete report.metrics.approval_rate; }],
    ['tasa NaN', report => { report.metrics.approval_rate = NaN; }],
    ['lista diaria ausente', report => { delete report.daily; }],
    ['fecha diaria inválida', report => { report.daily[0].date = '2026-02-30'; }],
    ['tasa diaria fuera de rango', report => { report.daily[0].approval_rate = -0.1; }],
    ['comparación ausente', report => { delete report.comparison; }],
    ['tasa de comparación en texto', report => { report.comparison.second_half_approval_rate = 'unknown'; }],
    ['variación infinita', report => { report.comparison.change_percentage_points = Infinity; }],
    ['estado de análisis desconocido', report => { report.analysis_status = 'success'; }],
    ['conclusiones faltantes al completar', report => { report.analysis_status = 'completed'; }],
    ['conclusiones de texto libre', report => { report.conclusions = 'Todo está bien.'; }],
    ['error de análisis incompleto', report => { report.analysis_error = { code: 'UNAVAILABLE' }; }],
    ['limitaciones no estructuradas', report => { report.limitations = 'Sin datos'; }],
    ['hallazgo sin referencias válidas', report => {
      report.conclusions = {
        summary: 'Resumen', findings: [{ observation: 'Hallazgo', evidence_ids: [1] }],
        limitations: [], next_steps: [],
      };
    }],
  ])('rechaza %s', (_label, mutate) => {
    const report = historyReport();
    mutate(report);

    expect(() => validateKipuHistoryReport(report)).toThrow();
  });
});

describe('loadKipuHistory', () => {
  it.each([false, true])('envía MID, fechas y analyze=%s con un token Firebase', async analyze => {
    const user = authenticatedUser();
    const report = historyReport();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ report })));
    const query = { ...QUERY, merchant_code: '  10001  ', ...(analyze ? { analyze: true } : {}) };

    await expect(loadKipuHistory(user, query)).resolves.toEqual(report);
    expect(user.getIdToken).toHaveBeenCalledOnce();
    expect(fetch).toHaveBeenCalledOnce();
    expect(fetch).toHaveBeenCalledWith('/api/kipu-history', expect.objectContaining({
      method: 'POST',
      headers: {
        Authorization: 'Bearer firebase-token',
        'Content-Type': 'application/json',
      },
    }));
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ ...QUERY, analyze });
  });

  it('valida el reporte recibido incluso cuando HTTP responde correctamente', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ report: { schema_version: '1.0' } })));

    await expect(loadKipuHistory(authenticatedUser(), QUERY)).rejects.toThrow();
  });
});

describe('autenticación del histórico', () => {
  it.each([null, {}, { getIdToken: 'no-es-una-función' }])(
    'impide consultas y diagnósticos sin un usuario autenticado (%j)',
    async user => {
      const fetchMock = vi.fn();
      vi.stubGlobal('fetch', fetchMock);

      await expect(loadKipuHistory(user, QUERY)).rejects.toThrow();
      await expect(loadKipuHistoryStatus(user)).rejects.toThrow();
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );

  it('no envía una consulta si Firebase no puede emitir el token', async () => {
    const user = { getIdToken: vi.fn().mockRejectedValue(new Error('Sesión expirada.')) };
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    await expect(loadKipuHistory(user, QUERY)).rejects.toThrow();
    await expect(loadKipuHistoryStatus(user)).rejects.toThrow();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('no envía un token vacío', async () => {
    const user = { getIdToken: vi.fn().mockResolvedValue('') };
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    await expect(loadKipuHistory(user, QUERY)).rejects.toThrow();
    await expect(loadKipuHistoryStatus(user)).rejects.toThrow();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('no consulta el backend si el rango es inválido', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    await expect(loadKipuHistory(authenticatedUser(), { ...QUERY, date_to: '2026-09-07' }))
      .rejects.toThrow();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('errores de red y del backend', () => {
  it.each([
    ['consulta', user => loadKipuHistory(user, QUERY)],
    ['diagnóstico', user => loadKipuHistoryStatus(user)],
  ])('explica que falta el backend local cuando falla la red: %s', async (_label, load) => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));

    await expect(load(authenticatedUser())).rejects.toThrow(/backend local/i);
  });

  it.each([true, false])('identifica respuestas no JSON del proxy (ok=%s)', async ok => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok,
      json: vi.fn().mockRejectedValue(new SyntaxError('Unexpected token <')),
    }));

    await expect(loadKipuHistory(authenticatedUser(), QUERY)).rejects.toThrow(/backend local/i);
    await expect(loadKipuHistoryStatus(authenticatedUser())).rejects.toThrow(/backend local/i);
  });

  it('conserva el mensaje y código de error JSON del servidor', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({
      error: 'La sesión de AWS SSO expiró.',
      code: 'SSO_SESSION_EXPIRED',
    }, false)));

    await expect(loadKipuHistory(authenticatedUser(), QUERY)).rejects.toMatchObject({
      message: 'La sesión de AWS SSO expiró.',
      code: 'SSO_SESSION_EXPIRED',
    });
  });
});

describe('loadKipuHistoryStatus', () => {
  it('consulta el estado por GET con el token de Firebase', async () => {
    const user = authenticatedUser();
    const status = {
      gemini_configured: true, model: 'gemini-2.5-flash', profile: 'data-core', max_days: 31,
    };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(status)));

    await expect(loadKipuHistoryStatus(user)).resolves.toEqual(status);
    expect(user.getIdToken).toHaveBeenCalledOnce();
    expect(fetch).toHaveBeenCalledWith('/api/kipu-history/status', expect.objectContaining({
      method: 'GET',
      headers: expect.objectContaining({ Authorization: 'Bearer firebase-token' }),
    }));
    expect(fetch.mock.calls[0][1].body).toBeUndefined();
  });
});
