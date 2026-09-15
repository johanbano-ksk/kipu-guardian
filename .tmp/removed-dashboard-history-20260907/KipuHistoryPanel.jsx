import { useEffect, useRef, useState } from 'react';
import { defaultHistoryDates, loadKipuHistory, todayInEcuador } from '../../services/kipuHistory.js';

const formatCount = value => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('es-EC') : '—';
const formatRate = value => typeof value === 'number' && Number.isFinite(value) ? `${(value * 100).toFixed(2)}%` : 'Sin base de cálculo';
const formatChange = value => typeof value === 'number' && Number.isFinite(value) ? `${value > 0 ? '+' : ''}${value.toFixed(2)} pp` : 'No comparable';

function evidenceDate(dateFrom, index) {
  const day = new Date(`${dateFrom}T00:00:00Z`);
  day.setUTCDate(day.getUTCDate() + index);
  return day.toISOString().slice(0, 10);
}

function evidenceLabel(id, dateFrom) {
  const day = /^day_(\d+)$/.exec(id);
  if (day && Number(day[1]) < 31) return `${evidenceDate(dateFrom, Number(day[1]))} · ${id}`;
  if (id === 'comparison_0') return 'Comparación de períodos · comparison_0';
  if (id === 'quality_0') return 'Cobertura de datos · quality_0';
  return id;
}

function HistoryMetric({ label, value }) {
  return (
    <div className="rounded-lg bg-slate-50 px-4 py-3">
      <dt className="text-[10px] font-semibold uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className="mt-1 text-xl font-bold text-[#023365] tabular-nums">{value}</dd>
    </div>
  );
}

function HistoryReport({ report }) {
  const { query, metrics, daily, source, comparison, conclusions } = report;
  const hasNoData = metrics.total_transactions === 0;
  return (
    <div className="mt-5 space-y-5" aria-live="polite">
      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-slate-100 pt-5">
        <div>
          <h4 className="text-sm font-semibold text-slate-700">MID <span className="font-mono">{query.merchant_code}</span></h4>
          <p className="mt-1 text-xs text-slate-500">{query.date_from} al {query.date_to} · America/Guayaquil · {formatCount(metrics.observed_days)} días con transacciones</p>
        </div>
        <span className="rounded-full bg-slate-100 px-3 py-1 text-[10px] font-semibold text-slate-500">Datos consultados en Athena</span>
      </div>

      {hasNoData && (
        <p className="rounded-lg border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-600">
          La consulta terminó sin transacciones para este MID y rango. No hay base para calcular aprobación ni generar conclusiones con Gemini.
        </p>
      )}

      <dl className="grid grid-cols-2 gap-3 lg:grid-cols-5">
        <HistoryMetric label="Transacciones" value={formatCount(metrics.total_transactions)} />
        <HistoryMetric label="Aprobadas" value={formatCount(metrics.approved_transactions)} />
        <HistoryMetric label="Rechazadas" value={formatCount(metrics.declined_transactions)} />
        <HistoryMetric label="Otros estados" value={formatCount(metrics.other_transactions)} />
        <HistoryMetric label="Aprobación" value={formatRate(metrics.approval_rate)} />
      </dl>

      <div id="history-evidence-comparison_0" className="rounded-lg border border-slate-200 px-4 py-3">
        <p className="text-xs font-semibold text-slate-600">Comparación dentro del rango</p>
        <p className="mt-1 text-sm text-slate-600">
          Primera mitad: <strong>{formatRate(comparison.first_half_approval_rate)}</strong>
          {' · '}Segunda mitad: <strong>{formatRate(comparison.second_half_approval_rate)}</strong>
          {' · '}Cambio: <strong>{formatChange(comparison.change_percentage_points)}</strong>
        </p>
        <p className="mt-1 font-mono text-[10px] text-slate-400">comparison_0</p>
      </div>

      <div className="overflow-x-auto rounded-lg border border-slate-200">
        <table className="w-full text-left text-xs">
          <caption className="border-b border-slate-100 px-4 py-3 text-left font-semibold text-slate-600">Detalle diario · aprobación = aprobadas / total de transacciones</caption>
          <thead className="bg-slate-50 text-[10px] uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-4 py-3" scope="col">Fecha / evidencia</th>
              <th className="px-3 py-3 text-right" scope="col">Total</th>
              <th className="px-3 py-3 text-right" scope="col">Aprobadas</th>
              <th className="px-3 py-3 text-right" scope="col">Rechazadas</th>
              <th className="px-3 py-3 text-right" scope="col">Otros</th>
              <th className="px-4 py-3 text-right" scope="col">Aprobación</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 text-slate-600">
            {daily.map(day => {
              const evidenceId = `day_${Math.round((Date.parse(`${day.date}T00:00:00Z`) - Date.parse(`${query.date_from}T00:00:00Z`)) / 86_400_000)}`;
              return (
              <tr key={day.date} id={`history-evidence-${evidenceId}`}>
                <th className="px-4 py-3 font-medium" scope="row">{day.date}<span className="mt-0.5 block font-mono text-[9px] text-slate-400">{evidenceId}</span></th>
                <td className="px-3 py-3 text-right tabular-nums">{formatCount(day.total_transactions)}</td>
                <td className="px-3 py-3 text-right tabular-nums">{formatCount(day.approved_transactions)}</td>
                <td className="px-3 py-3 text-right tabular-nums">{formatCount(day.declined_transactions)}</td>
                <td className="px-3 py-3 text-right tabular-nums">{formatCount(day.other_transactions)}</td>
                <td className="px-4 py-3 text-right font-semibold tabular-nums">{formatRate(day.approval_rate)}</td>
              </tr>
              );
            })}
            {daily.length === 0 && <tr><td className="px-4 py-5 text-center text-slate-400" colSpan={6}>Sin registros diarios en la respuesta.</td></tr>}
          </tbody>
        </table>
      </div>

      <div className="rounded-xl border border-slate-200 px-4 py-4">
        <h4 className="flex items-center gap-2 text-sm font-semibold text-[#023365]"><span className="material-symbols-rounded text-[19px]">auto_awesome</span>Conclusiones de Gemini</h4>
        {report.analysis_status === 'not_requested' && <p className="mt-2 text-xs text-slate-500">Análisis no solicitado. Pulsa “Analizar con Gemini” para consultar este rango y enviar sus métricas agregadas al modelo.</p>}
        {report.analysis_status === 'no_data' && <p className="mt-2 text-xs text-slate-500">No se ejecutó Gemini: no hay transacciones suficientes para analizar.</p>}
        {report.analysis_status === 'unavailable' && <p className="mt-2 text-xs text-amber-700">{report.analysis_error?.message || 'El análisis no está disponible. Revisa la configuración de Gemini en el backend local.'} Los datos consultados siguen disponibles arriba.</p>}
        {report.analysis_status === 'completed' && conclusions && (
          <div className="mt-3 space-y-4 text-sm text-slate-600">
            <p className="whitespace-pre-wrap">{conclusions.summary}</p>
            {conclusions.findings.length > 0 && (
              <ul className="space-y-3">
                {conclusions.findings.map((finding, index) => (
                  <li key={index} className="rounded-lg bg-slate-50 px-3 py-3">
                    <p className="whitespace-pre-wrap">{finding.observation}</p>
                    <div className="mt-2 flex flex-wrap gap-1.5" aria-label="Referencias de evidencia">
                      {finding.evidence_ids.map(id => <span key={id} className="break-all rounded bg-white px-2 py-1 text-[10px] text-[#023365] ring-1 ring-slate-200">{evidenceLabel(id, query.date_from)}</span>)}
                    </div>
                  </li>
                ))}
              </ul>
            )}
            {conclusions.next_steps.length > 0 && <div><h5 className="text-xs font-semibold text-slate-700">Siguientes pasos sugeridos</h5><ul className="mt-2 list-disc space-y-1 pl-5 text-xs">{conclusions.next_steps.map((item, index) => <li key={index}>{item}</li>)}</ul></div>}
            {conclusions.limitations.length > 0 && <div><h5 className="text-xs font-semibold text-slate-700">Limitaciones del análisis</h5><ul className="mt-2 list-disc space-y-1 pl-5 text-xs">{conclusions.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul></div>}
            <p className="text-[11px] text-slate-400">Conclusiones generadas por IA a partir de métricas agregadas; requieren revisión operativa y no prueban la causa de un incidente.</p>
          </div>
        )}
      </div>

      <details id="history-evidence-quality_0" className="text-xs text-slate-500" open>
        <summary className="cursor-pointer font-semibold text-slate-600">Fuente y limitaciones</summary>
        <div className="mt-2 space-y-1 break-words">
          <p>Fuente: <code>{source.catalog}.{source.database}.{source.table}</code> · perfil data-core</p>
          <p>Consulta Athena: <code className="break-all">{source.query_execution_id}</code></p>
          <p>Consultado: {source.retrieved_at} · datos leídos: {formatCount(source.data_scanned_bytes)} bytes</p>
          <p>Cobertura: {formatCount(metrics.observed_days)} días con transacciones en el rango · <code>quality_0</code></p>
          {report.limitations.length > 0 && <ul className="list-disc space-y-1 pl-5 pt-2">{report.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul>}
        </div>
      </details>
    </div>
  );
}

export const KipuHistoryPanel = ({ user, prefill }) => {
  const [merchantCode, setMerchantCode] = useState(prefill?.merchant_code || '');
  const [dates, setDates] = useState(defaultHistoryDates);
  const [report, setReport] = useState(null);
  const [loading, setLoading] = useState(null);
  const [error, setError] = useState('');
  const requestRef = useRef(null);
  const merchantInputRef = useRef(null);

  useEffect(() => {
    if (prefill?.merchant_code) merchantInputRef.current?.focus({ preventScroll: true });
    return () => requestRef.current?.abort();
  }, [prefill]);

  const runHistory = async analyze => {
    requestRef.current?.abort();
    const controller = new AbortController();
    requestRef.current = controller;
    setLoading(analyze ? 'analysis' : 'query');
    setError('');
    setReport(null);
    try {
      const result = await loadKipuHistory(user, { merchant_code: merchantCode, ...dates, analyze }, { signal: controller.signal });
      if (!controller.signal.aborted) setReport(result);
    } catch (err) {
      if (!controller.signal.aborted) setError(err instanceof Error ? err.message : 'No fue posible consultar el histórico en el backend local.');
    } finally {
      if (!controller.signal.aborted) setLoading(null);
    }
  };

  return (
    <section id="kipu-history-panel" aria-labelledby="kipu-history-title" className="mt-8 scroll-mt-6 rounded-xl bg-white p-5 kushki-shadow">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 id="kipu-history-title" className="text-lg font-bold text-[#023365]">Histórico transaccional</h3>
          <p className="mt-1 max-w-3xl text-xs leading-relaxed text-slate-500">Consulta transacciones de tarjeta del comercio (card_transaction) en el Data Lake; no es un archivo de alertas Kipu. Gemini recibe únicamente métricas agregadas sin identificadores, cuando lo solicitas.</p>
        </div>
        <span className="rounded-full bg-slate-100 px-3 py-1 text-[10px] font-semibold uppercase tracking-wide text-slate-500">Sólo local · data-core</span>
      </div>

      <form className="mt-5" onSubmit={event => { event.preventDefault(); runHistory(false); }}>
        <fieldset disabled={Boolean(loading)} className="flex flex-col gap-3 lg:flex-row lg:items-end">
          <label className="block flex-1 text-xs font-semibold text-slate-600">
            MID del comercio
            <input ref={merchantInputRef} required value={merchantCode} onChange={event => setMerchantCode(event.target.value)} placeholder="Ej. 10001" autoComplete="off" className="mt-1.5 h-10 w-full rounded-lg border border-slate-200 px-3 text-sm font-normal outline-none focus:border-[#023365] disabled:bg-slate-50" />
          </label>
          <label className="block text-xs font-semibold text-slate-600">
            Desde
            <input required type="date" value={dates.date_from} max={dates.date_to || todayInEcuador()} onChange={event => setDates(previous => ({ ...previous, date_from: event.target.value }))} className="mt-1.5 h-10 w-full rounded-lg border border-slate-200 px-3 text-sm font-normal outline-none focus:border-[#023365] disabled:bg-slate-50" />
          </label>
          <label className="block text-xs font-semibold text-slate-600">
            Hasta
            <input required type="date" value={dates.date_to} min={dates.date_from || undefined} max={todayInEcuador()} onChange={event => setDates(previous => ({ ...previous, date_to: event.target.value }))} className="mt-1.5 h-10 w-full rounded-lg border border-slate-200 px-3 text-sm font-normal outline-none focus:border-[#023365] disabled:bg-slate-50" />
          </label>
          <button type="submit" disabled={Boolean(loading)} className="h-10 rounded-lg border border-[#023365] px-4 text-xs font-semibold text-[#023365] hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50">{loading === 'query' ? 'Consultando…' : 'Consultar histórico'}</button>
          <button type="button" disabled={Boolean(loading)} onClick={() => runHistory(true)} className="flex h-10 items-center justify-center gap-2 rounded-lg bg-[#023365] px-4 text-xs font-semibold text-white hover:bg-[#112b45] disabled:cursor-not-allowed disabled:opacity-50"><span className={`material-symbols-rounded text-[17px] ${loading === 'analysis' ? 'animate-spin' : ''}`}>{loading === 'analysis' ? 'progress_activity' : 'auto_awesome'}</span>{loading === 'analysis' ? 'Analizando…' : 'Analizar con Gemini'}</button>
        </fieldset>
        <p className="mt-2 text-[11px] leading-relaxed text-slate-400">Máximo 31 días, ambas fechas incluidas · zona horaria Ecuador · últimos 7 días completos por defecto. Se reutilizan consultas durante 5 minutos. Athena puede generar costos; el análisis también envía agregados a Gemini.</p>
      </form>

      <div role="status" className="mt-3 text-xs text-slate-500">{loading ? 'Consultando el Data Lake; el proceso puede tardar unos momentos. No cierres el backend local.' : !report && !error ? 'Listo para consultar. Requiere backend local en 127.0.0.1:8765 y sesión AWS SSO de data-core; la clave de Gemini se configura sólo en el servidor.' : null}</div>
      {error && <div role="alert" className="mt-4 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800"><p className="font-semibold">No se pudo consultar el histórico</p><p className="mt-1">{error}</p><p className="mt-2 text-xs">No se muestran ceros ni conclusiones como sustituto de una consulta fallida.</p></div>}
      {report && <HistoryReport report={report} />}
    </section>
  );
};
