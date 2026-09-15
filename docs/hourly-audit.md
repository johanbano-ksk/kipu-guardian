# Archivo horario de ocurrencias del reviewer

## Límite de integración

Phase 08 no modifica Kipu. No agrega metadatos a sus payloads, no cambia su
frecuencia, almacenamiento, notificaciones ni deduplicación. El reviewer registra
en su propia tabla DynamoDB los eventos versionados que recibe desde EventBridge.

La frecuencia horaria del productor no convierte este archivo en un registro de
sus ejecuciones internas. Si una detección no se publica en EventBridge, el
reviewer no puede observarla ni reconstruirla. La captura tampoco confirma la
entrega de Slack ni la existencia de un incidente real.

## Captura antes de la decisión

El procesamiento mantiene dos registros independientes dentro de la tabla del
reviewer:

1. Se valida el JSON, `source`, `detail-type`, correspondencia con
   `schema_version` y contrato Kipu `1.0` o `2.0`.
2. Para un envelope estructuralmente válido se construye una identidad de
   ocurrencia:
   - `occurrence#event:<event-id>` cuando EventBridge entrega `id`;
   - `occurrence#sha256:<hash-del-envelope>` como fallback determinista.
3. La ocurrencia se inserta condicionalmente antes de reclamar una decisión.
4. La evaluación usa la misma identidad bajo otro namespace:
   `decision#event:<event-id>` o `decision#sha256:<hash-del-envelope>`.
5. El filtro decide exclusivamente con el payload. Los aceptados se publican
   como `acceptance.reviewer / Anomaly Validated v1`; los descartes de negocio se
   reconocen sin publicación.

Un retry del mismo evento reutiliza la ocurrencia y la decisión. Dos envelopes
distintos que comparten `alert_id` se conservan como eventos separados. Si un ID
de EventBridge ya registrado reaparece con otro payload, el reviewer falla por
colisión en lugar de sobrescribir evidencia.

La captura guarda el payload original, su SHA-256, source, detail type, versión,
`alert_id`, timestamp contractual, tiempo de publicación, instante de registro y
TTL. El valor por defecto es `720` horas y se configura con
`ALERT_OCCURRENCE_TTL_HOURS`. Para `2.0` también conserva `observation_date`; no
lo inventa para `1.0`.

Antes de escribir, el reviewer aplica un límite conservador de `350 KiB` al
item serializado para mantener margen bajo el máximo de DynamoDB. Un evento que
lo exceda falla de forma explícita antes del filtro y permanece para
reintento/DLQ; no se guarda truncado.

Un JSON, envelope o contrato malformado falla antes de la captura y de la
decisión. El mensaje queda sin ACK para reintento y redrive a la DLQ. Si DynamoDB
no puede guardar la ocurrencia, también se detiene antes del filtro y del ACK.

## Semántica temporal

La base `publication` usa `time` del envelope EventBridge convertido a la zona
IANA solicitada. Sólo cuando ese tiempo no está disponible usa el timestamp
contractual de la alerta (`timestamp` en `1.0` o `published_at` en `2.0`).
`recorded_at` indica cuándo DynamoDB aceptó el registro y no redefine la fecha de
publicación.

La base `observation` utiliza exclusivamente `observation_date` explícito en el
contrato `2.0`. No deriva una fecha transaccional desde timestamps `1.0`. Las
ocurrencias sin ese campo se contabilizan en el resumen y quedan fuera de un
corte por observación.

## Exportar un día para revisión manual

Por día local de publicación en Ecuador:

```powershell
.\.venv\Scripts\python.exe scripts\export_hourly_alert_audit.py 2026-08-18 `
  --date-basis publication `
  --timezone America/Guayaquil `
  --profile ia-dev-payments-intelligence
```

Por fecha transaccional explícita de eventos `2.0`:

```powershell
.\.venv\Scripts\python.exe scripts\export_hourly_alert_audit.py 2026-08-18 `
  --date-basis observation `
  --profile ia-dev-payments-intelligence
```

Para probar sin AWS, `--source` acepta registros decodificados o una respuesta
JSON de DynamoDB:

```powershell
.\.venv\Scripts\python.exe scripts\export_hourly_alert_audit.py 2026-08-18 `
  --source examples\reviewer-occurrences.example.json `
  --output-dir reports\demo-reviewer-occurrences
```

El comando escribe cinco archivos:

- `all.json`: todas las ocurrencias primarias v1/v2 del corte;
- `unique.json`: la última ocurrencia por `alert_id`, aceptada o rechazada;
- `valid.json`: todas las ocurrencias de `all.json` aceptadas por la política;
- `valid_unique.json`: la última ocurrencia aceptada por `alert_id`, calculada
  desde `valid.json` para que un rechazo posterior no elimine una aceptación;
- `summary.json`: contrato `1.1`, base temporal, zona, versiones, fuente y
  conteos explícitos, incluidos `valid_occurrence_count`,
  `valid_unique_alert_count`, `excluded_transition_count` y
  `excluded_unsupported_count`.

Las copias transitorias `1.0` marcadas como reemplazadas por `2.0` se capturan,
pero no entran en ninguno de los cuatro conjuntos de alertas; el resumen las
contabiliza en `excluded_transition_count`. Sources, detail types o parejas
schema/type no soportadas se contabilizan en `excluded_unsupported_count`.
`valid.json` y `valid_unique.json` usan el mismo filtro payload-only que el worker
y la skill; no consultan Elastic, Slack, dashboards ni APIs.

El script `export_manual_review_snapshot.py` sigue disponible para el snapshot
diario histórico de Kipu. Esa fuente es independiente, no tiene identidad
EventBridge por ocurrencia y no debe mezclarse con el archivo del reviewer.

## Estado de despliegue

La implementación de Phase 08 aún no se ha desplegado. Los registros aparecen
únicamente después de arrancar un worker actualizado y no pueden reconstruirse
retroactivamente. La suite local, Ruff, skill y ejemplo de exportación pasaron;
el build Docker y el E2E LocalStack actuales permanecen pendientes porque el
daemon no está iniciado.
