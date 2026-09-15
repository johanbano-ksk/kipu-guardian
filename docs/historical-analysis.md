# Guardian: revisión de alertas, histórico transaccional y Gemini

Implementación local: 7 de septiembre de 2026. No configura servicios ni publica
cambios en AWS, Firebase o Git. El filtro operativo de alertas continúa siendo
determinístico y payload-only. El propio agente Python orquesta la investigación
histórica y la conclusión de IA como una etapa adicional de la revisión.

## Preparar el agente

1. En este repositorio, instalar Python 3.12–3.14 y las dependencias:

   ```powershell
   .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
   ```

2. Configurar `.env` local a partir de los nombres en `.env.example`, sin
   sobrescribir configuraciones existentes. Guardar `GEMINI_API_KEY` únicamente
   en el backend. El modelo probado es `gemini-3.1-flash-lite`. No usar variables
   `VITE_*` para secretos, ni incluir la clave en comandos o URLs.

3. Renovar la sesión de Data Lake cuando sea necesario:

   ```powershell
   aws sso login --profile data-core
   ```

El núcleo del agente no necesita navegador, servidor HTTP, Vite ni Firebase.
La vista opcional pertenece exclusivamente al dashboard propio de Guardian;
el panel añadido por error a `acceptance-c-level-dashboard` permanece retirado.

## Vista en el dashboard propio de Guardian

```powershell
Set-Location dashboard
npm run dev
```

Abrir `http://localhost:3000/`. En cada tarjeta de alerta, pulsar **Generar
conclusión**. El resultado se despliega dentro de esa misma tarjeta y conserva
comercio, MID y fecha original. `/guardian` redirige a esta lista.
El servidor de desarrollo ejecuta `alert_reviewer.guardian_view`, que delega al
agente Python. Abrir la lista no consulta Athena ni Gemini automáticamente.
La investigación manual por MID sigue disponible mediante el CLI, pero ya no
es una vista separada del dashboard. Ver el protocolo de evidencia persistente
en **Conclusión junto a cada alerta** más abajo.

El adaptador existe sólo durante desarrollo local. Recibe JSON acotado por stdin
y usa un ejecutable/módulo fijo, sin shell. Valida Host/Origin local y encabezado
de la vista, no habilita CORS y admite una operación a la vez. No expone rutas de
archivos ni credenciales al navegador. No necesita el backend Firebase del otro
dashboard. En Cloudflare `/api/guardian` devuelve explícitamente `LOCAL_ONLY`:
no intenta acceder a Python o a la sesión SSO de este equipo.

## Revisar alertas con Guardian

### Ejecutar una fecha desde el dashboard local

El botón **Ejecutar agente** conserva `POST /api/alerts` con `{date, mode}`. En
desarrollo, el adaptador local lo dirige a `GuardianDailyReview`, sin depender
de las variables ni la base D1 del despliegue Cloudflare. `GET /api/alerts` lee
el último snapshot local; nunca ejecuta consultas por sí solo. El selector
inicia en hoy en Ecuador aunque se esté mostrando una copia antigua.

`GuardianDailyReview` invoca la Lambda existente, no lee DynamoDB directamente.
El extractor desplegado usa `filter_occurrence_snapshot`: día de publicación EventBridge
en UTC−5, última ocurrencia por alerta y después política. Conserva el timestamp
Kipu y el resumen por país. No usa `valid_unique` de la auditoría, no consulta
S3 como fallback y no mezcla el histórico transaccional con esta extracción.

Acceso: perfil `ALERTS_AWS_PROFILE` (`ia-dev-payments-intelligence`), región
`ALERTS_AWS_REGION` (`us-east-1`) y `ALERTS_EXTRACTOR_FUNCTION`
(`kipu-alert-reviewer-manual-extractor`). El backend usa `lambda:InvokeFunction`
en modalidad síncrona, manteniendo el rol `kipu-alert-reviewer-manual-extractor-role`
que ya tiene lectura del archivo. No hay `AssumeRole` ni `Scan` desde este flujo
local, ni fallback al rol del worker o a otra fuente si la Lambda falla.

La autenticación del handler se toma de `AGENT_EXECUTION_KEY` si se configura
localmente; si está vacío, se lee mediante `lambda:GetFunctionConfiguration`
únicamente para esa función. La clave permanece en memoria y se envía dentro
de la invocación autenticada por IAM; no se registra, persiste ni devuelve al
navegador. No se piden logs de la ejecución. No se cambia ninguna política IAM,
configuración ni código de la Lambda. Si AWS rechaza el acceso, el error es
explícito; no se busca otra identidad.

La invocación tiene un timeout de lectura de 75 segundos, sin reintentos
automáticos, y una respuesta máxima de 2 MB. Se validan el estado de la función,
el JSON, fecha/método solicitados y el contrato del snapshot antes de guardarlo.
El scan interno sigue siendo responsabilidad de la Lambda existente (no se le
atribuyen los límites del antiguo lector directo local). Una fecha sin ocurrencias
produce `SNAPSHOT_NOT_FOUND`; una fecha con ocurrencias pero ninguna alerta
aceptada es una ejecución exitosa con cero aceptadas. La retención configurada
del archivo es de 30 días por defecto, no un histórico ilimitado de Slack.

El modo `ai` de ejecución diaria se delega sin cambios al proveedor configurado
en la Lambda; no usa las claves Gemini locales para cambiar la configuración
remota. **Generar conclusión** sigue siendo una operación separada de Guardian
con Gemini local y evidencia histórica persistente, sin modificar la política.

Tras una ejecución válida se guarda el reporte en `reports/guardian-dashboard-runs/`
y se actualiza atómicamente `reports/guardian-dashboard-snapshot.json`; se
conserva el snapshot anterior en el archivo. Ante fecha errónea, falta de permisos
o fallo de IA, no se reemplaza el resultado exitoso previo. El mensaje identifica
la fecha solicitada y deja claro que la pantalla conserva la ejecución anterior.

### CLI independiente

El agente acepta JSON de alertas, listas o envelopes EventBridge/SQS:

```powershell
.\.venv\Scripts\python.exe scripts\run_guardian.py review `
  --source alertas.json --history-days 7 --max-history-queries 5 `
  --output reports\guardian-review.json
```

Flujo: política → MID/fecha → consulta parametrizada Athena → deduplicación CDC
→ métricas anónimas de alerta + histórico → conclusión citada de Gemini. La IA
no recibe herramientas para ejecutar SQL libre ni acceder a los datos crudos.

La salida incluye `accepted_alerts` sin cambios y `reviews` con decisión,
histórico, modelo, conclusión y códigos de error. `history_anchor_at` identifica
el instante usado para calcular la ventana. Se usa el `timestamp` contractual
de la alerta, nunca el reloj de ejecución; fechas ambiguas o futuras se omiten
con un estado explícito. Los siete días completos anteriores son contexto, no
la ventana exacta de la alerta. El límite configurable es 31 días.

Cada lote admite hasta 50 alertas y, por defecto, cinco consultas distintas
MID/rango (máximo configurable de 20); las restantes quedan marcadas
`HISTORY_QUERY_LIMIT`. Las alertas del mismo MID/rango reutilizan la consulta
en ese lote, pero reciben conclusiones individuales. Se conserva la revisión de
política aunque falte histórico, falle Gemini o se alcance el límite. No se
consulta histórico para eventos inválidos o 2.0; su evaluación offline de
política permanece disponible.

Añadir `--policy-only` evita Athena y Gemini. `--history-source reporte.json`
permite reutilizar un histórico guardado, validando MID, fechas y métricas; si no
coincide, no se consulta AWS como fallback. Los errores parciales dejan reporte
y devuelven código de salida 1; ausencia real de filas se informa como `no_data`.

También está conectado al extractor manual existente:

```powershell
.\.venv\Scripts\python.exe scripts\export_manual_review_snapshot.py 2026-09-06 `
  --guardian-history --history-days 7 --history-max-queries 5
```

Este comando conserva el JSON de alertas aceptadas y añade un archivo separado
`*.guardian.json`. El snapshot S3 necesita su propio perfil de lectura Kipu; el
histórico usa `data-core`. Para este replay Guardian usa exclusivamente
`kipu_generated_at` derivado de `notified_at`, nunca el `timestamp` sintético del
replay; si esa hora no está disponible, omite el histórico sin inventarla.

## Investigar un MID desde el agente

Sin una alerta, Guardian también puede consultar histórico y sacar conclusiones:

```powershell
.\.venv\Scripts\python.exe scripts\run_guardian.py history MID_DEL_COMERCIO `
  --days 7 --end 2026-09-06 --output reports\guardian-historico.json
```

Sin `--end`, usa ayer en Ecuador. Añadir `--source reports\historico.json`
reutiliza las métricas guardadas sin un nuevo escaneo. Las entradas y salidas
deben ser archivos diferentes. Tras instalar, `kipu-guardian` es equivalente a
`python scripts/run_guardian.py`.

Prueba de conexión con datos sintéticos:

```powershell
.\.venv\Scripts\python.exe scripts\check_gemini_connection.py
```

Los reportes contienen MID y metadatos internos: se guardan en `reports/`, que
está excluido de Git. No compartirlos públicamente. `.env` también está excluido.
Las claves que se hayan compartido en conversaciones deben rotarse antes de uso
continuado; la nueva clave se configura sólo en el entorno local o gestor de
secretos correspondiente.

## Conclusión junto a cada alerta

El flujo principal está en la lista del dashboard original (`/`). La antigua
ruta `/guardian` redirige allí: no hay que copiar un MID ni identificar el
comercio en otra pantalla. **Generar conclusión** envía al agente el payload
original, no las tasas porcentuales transformadas para presentación.

### Protocolo inline actual: v3

`GuardianCaseReview` aplica `guardian-alert-context-7d-v3`, histórico `3.0` y
analista `guardian-assured-verdict-v1`. Son versiones internas independientes
del contrato Kipu 1.0. El CLI y replay históricos v2 mantienen su contrato anterior;
los archivos de `reports/guardian-alerts/` se conservan sin migrarlos ni reetiquetarlos.

`POST /api/guardian` admite las siguientes operaciones:

```json
{"action":"conclude_alert","alert":{},"metric_profile":"authorizations"}
```

```json
{"action":"refresh_conclusion","alert":{},"metric_profile":"authorizations","expected_revision":"<revision_id de 32 caracteres hexadecimales>"}
```

`alert` debe ser el payload real completo; `{}` sólo ilustra la posición del campo.
El cliente anterior puede omitir `metric_profile` al generar y obtiene
`authorizations`. No se aceptan SQL, fechas, MID independiente, modelo ni ventana.
El servidor fija siete días completos anteriores a `kipu_generated_at` en Ecuador.
Hora ausente, ambigua, futura, contrato inválido o ID de ejemplo impiden la consulta.
La hora del replay nunca sustituye a Kipu.

| Perfil fijo y versionado | Tipos que integran aceptación |
|---|---|
| `sales` — Sólo ventas | SALE, DEFERRED, DEFFERED |
| `authorizations` — Autorizaciones, predeterminado visible | Los anteriores y PREAUTHORIZATION |

Ambos usan exclusivamente APPROVED/DECLINED. CAPTURE no cuenta en ninguno.
El perfil no lo elige Gemini y no acredita que Kipu use el mismo universo.

### Diagnóstico y conservación de evidencia

Una consulta acotada calcula el histórico y los contadores de CDC, llaves faltantes,
versiones reemplazadas, borrados, tipos y estados excluidos. Se comprueba que los
contadores reconcilien. La tarjeta explica seis estados posibles: datos disponibles,
sin filas de origen, llaves inválidas, sin registros vigentes, actividad fuera del
criterio y operaciones sin resultados finales. No confunde cero ventas con cero
actividad. La ingesta máxima del MID/rango es sólo una observación local:
`freshness.status=UNKNOWN` y `completeness.status=UNVERIFIED`.

La evidencia se guarda **antes** de Gemini en
`reports/guardian-evidence/{case_id}/{revision_id}.json`, con un `HEAD.json` que
señala la revisión vigente. La identidad vincula protocolo, perfil, política,
generación y métricas normalizadas. Cambiar perfil/métricas/política crea otro caso;
el replay y los textos descriptivos no invalidan su identidad.

- Volver a generar una conclusión completada reutiliza disco, sin Athena ni Gemini.
- Reintentar Gemini conserva consulta, huella y revisión. No se garantiza que
  el modelo sea determinístico; se conserva la primera respuesta válida.
- **Actualizar evidencia** consulta otra vez y crea una revisión enlazada. No
  modifica los bytes de las anteriores. Si Athena falla, HEAD no cambia.
- `expected_revision` se comprueba antes de consultar. Un cliente obsoleto obtiene
  `REVISION_CONFLICT`/HTTP 409 y debe cargar la última revisión antes de actualizar.
- Se validan esquema, métricas, huella, política, metadatos y contexto de **toda**
  la cadena al releerla; una corrupción no dispara una consulta silenciosa.
- El listado visible contiene metadatos de hasta 20 revisiones, no un navegador
  de conclusiones antiguas. Los JSON anteriores siguen disponibles para auditoría.
- Límites locales: 2 MiB por reporte, 128 revisiones y 32 MiB por cadena. No hay
  borrado automático. Un archivo huérfano tras una interrupción falla de forma
  explícita; no se omite ni se borra para continuar.

El bloqueo exclusivo evita solicitudes concurrentes por caso. Si persiste
`AGENT_BUSY`, comprobar primero si hay una ejecución activa. No eliminar todos
los bloqueos ni evidencias; seguir el [runbook del harness](../.planning/guardian-analyst/RUNBOOK.md).
El almacén detecta inconsistencias, pero no es un registro firmado ni resistente
a un administrador local que reescriba coherentemente todos los archivos.

### Dictamen y comparabilidad

El contrato actual de la alerta no aporta ventana observada y población verificadas.
`build_comparison_context` declara `not_comparable`, no inventa un intervalo desde
la generación y restringe los dictámenes a `requires_review`. El esquema enviado
a Gemini y una comprobación Python independiente rechazan `confirmed` o
`not_supported`, aunque tengan citas válidas. Esta restricción se revisará en G2
cuando exista un contrato de reconciliación probado, no sólo cambiando el prompt.

La tarjeta mantiene comercio, MID y fecha Kipu, muestra un motivo breve y una
única acción. Citas, diagnóstico, métricas y trazabilidad quedan desplegables.
El resumen admite hasta 600 caracteres, de uno a tres hallazgos y exactamente una
acción. Las métricas de entrada y referencias se validan; G1 **no** verifica aún
semánticamente cada cifra o afirmación del texto ni demuestra paridad con analistas.

Sin resultados elegibles no se llama a Gemini. Modelo y fecha de intento sólo
existen cuando realmente se intentó la llamada; la fecha de análisis sólo aparece
si completó. Los errores se normalizan y no se publican respuestas crudas del
proveedor. Nada de esto cambia la criticidad ni las decisiones del worker.

El adaptador funciona sólo con `npm run dev` y Python local. Si D1 no entrega
un snapshot, puede cargar el archivo explícito
`reports/guardian-dashboard-snapshot.json`, mediante `action: "alert_snapshot"`.
Debe ser el contrato completo de una extracción existente; no se reconstruyen
payloads desde las tarjetas de ejemplo. El dashboard etiqueta esta fuente y
su fecha como copia local, no como datos actuales. No cambia el dashboard
compartido ni el despliegue de Cloudflare.

## Qué consulta y qué puede concluir

- Perfil `data-core`, región `us-east-1`, catálogo
  `s3tablescatalog/datalake-prod`, base curada `odl`, tabla `card_transaction`.
  La configuración sigue la skill `kushki-datalake`; el esquema se comprueba en
  Glue antes de consultar. No se acepta SQL libre del usuario ni de Gemini.
- MID y fechas se pasan como parámetros Athena. Se filtra por fecha de creación
  UTC y se agrupa en días de Ecuador. Los timestamps sin zona se interpretan
  como UTC. El intervalo termina al comienzo del día siguiente a `Hasta`.
- Última versión por `merchant_code` y `transaction_code`, ordenada por
  `coalesce(update_timestamp, event_timestamp, etl_job_timestamp, create_timestamp)`
  descendente y después `event_id` descendente. Todos esos campos se verifican
  en Glue antes de consultar. Los borrados, tipos y estados se filtran
  **después** de deduplicar, evitando rescatar una versión anterior elegible.
  No se requiere `ticket_code`: los rechazos sin ticket sí cuentan. Se excluyen
  códigos de transacción nulos o vacíos; no se afirma cobertura de esos registros.
- Tipos de ventas: `SALE`, `DEFERRED`, `DEFFERED`. El perfil inline v3
  `authorizations` agrega `PREAUTHORIZATION`; el CLI histórico v2 sigue siendo
  sólo ventas. Se cuentan intentos por código
  de transacción, no órdenes únicas ni conversión final después de reintentos.
  No se incluyen `CAPTURE`, `REFUND`, `VOID`, `REVERSE`, `CHARGEBACK` ni
  otros tipos ajenos al criterio elegido.
  Estados de aprobación/rechazo `APPROVED` y `DECLINED` fijos para el botón por
  alerta (configurables sólo en el flujo CLI histórico independiente);
  se excluyen `PENDING`, `INITIALIZED` y cualquier estado fuera de esos grupos.
- Aceptación = aprobadas / (aprobadas + rechazadas). `total_transactions` es
  esa suma; `other_transactions` se conserva por compatibilidad con valor cero,
  no significa que no haya otros estados en la fuente. La frontera de IA exige
  esta igualdad y rechaza históricos de versiones anteriores.
  Comparación ponderada entre la
  primera y segunda mitad del rango; si faltan bases, se usa `null`, no cero.
  Los días sin filas no prueban ausencia de actividad ni cobertura completa.
- Es el último estado CDC disponible, no una reconstrucción del estado que
  existía al dispararse una alerta. Kipu puede usar otra ventana o población.
  Ni una aprobación del 100% ni una conclusión de IA bastan para descartar alertas.

Ejemplo temporal: `Desde=2026-08-31`, `Hasta=2026-09-06` son siete días completos
de Ecuador y generan el filtro directo `create_timestamp >= TIMESTAMP
'2026-08-31 05:00:00' AND create_timestamp < TIMESTAMP '2026-09-07 05:00:00'`.
El límite final SQL es exclusivo. No se ha cambiado a días UTC ni al huso del
país del comercio; eso sería otra ventana y requeriría otra comparación.

## Seguridad, costos y errores

El núcleo no expone un servidor web propio. La vista opcional usa un adaptador
del servidor de desarrollo del dashboard, restringido a solicitudes locales.
El acceso depende del usuario local, su sesión AWS y la clave Gemini del entorno
del agente. Nunca se devuelven claves ni cuerpos de errores del proveedor.

Gemini recibe métricas allowlist de la alerta, resultado booleano de política,
conteos diarios, índices relativos de día, tasas, comparación y cobertura.
No recibe MID, fechas exactas, nombres, datos de tarjeta, tickets,
SQL ni credenciales. Se valida la coherencia numérica antes de enviarlos y la
estructura JSON y referencias de evidencia al recibirlos. Las conclusiones no
se ejecutan como código ni cambian la política. La validación estructural no
garantiza que una inferencia sea correcta: requiere revisión humana.

La modalidad gratuita puede usar entradas y salidas para mejorar productos; no
debe recibir información confidencial o personal. Incluso los agregados pueden
estar sujetos a clasificación interna: validar su uso con gobierno de datos
antes de ampliar el piloto. Consultar los
[términos oficiales de Gemini](https://ai.google.dev/gemini-api/terms) y la
[tarifa vigente](https://ai.google.dev/gemini-api/docs/pricing). Athena sí puede
generar costos por datos escaneados, independientemente de Gemini.

El worker SQS y Lambda desplegados no se han modificado para invocar estas
capacidades: el modo enriquecido se ejecuta explícitamente desde Guardian o el
extractor manual. Automatizarlo en producción necesita identidad AWS de servicio,
permisos mínimos, presupuesto/colas de análisis e idempotencia durable; no se
deben desplegar sesiones SSO personales.

SSO vencido pide renovar `aws sso login --profile data-core`. Una consulta
demasiado larga solicita cancelación. Los fallos de Athena se muestran como
error, nunca como cero transacciones. Si Gemini falla, se conservan las métricas
con `analysis_status=unavailable`; no hay fallback silencioso. El proveedor
usa timeout y tamaño de respuesta acotados.

## Verificación realizada

La entrega inline v3 del 2026-09-09 pasó 1.209 pruebas Python (6 omisiones por
symlinks Windows), 34 pruebas Node, TypeScript, build y lint. Se validaron dos
perfiles con Athena real, reintento Gemini sin reextraer, actualización que
conserva bytes anteriores y HTTP 409 frente a una revisión obsoleta. No se hizo
QA visual ni una evaluación adjudicada de calidad de decisiones.
Registro detallado: [harness / VALIDATION](../.planning/guardian-analyst/VALIDATION.md).

### Verificación previa conservada: v2

El 2026-09-09 se validó el SQL v2 con `EXPLAIN (TYPE VALIDATE)` sin escaneo y se
ejecutó el flujo local por alerta: una consulta real de siete días completos,
seguida de análisis Gemini. La evidencia v2 y la anterior se guardan por separado.
El nuevo conteo incorporó rechazos que no aparecían en el histórico anterior;
persisten diferencias de volumen respecto a Kipu que requieren reconciliar sus
universos y ventanas. Un dictamen `not_supported` del modelo no prueba un falso
positivo ni acredita por sí mismo esa reconciliación.

Las pruebas automatizadas cubren
fechas, parámetros, CDC, conteos, SSO, paginación, límites por lote, caché, errores
de proveedores, exclusión de identificadores y citas conjuntas alerta/histórico.
Se ejecutan escenarios CDC sintéticos sobre el SQL generado, incluyendo rechazos
sin ticket, estados pendientes y cambios de tipo/estado posteriores. Los tests
impiden reutilizar evidencia v1 como v2 y validan el denominador antes de Gemini.
No equivalen
a una auditoría de completitud del Data Lake ni a un despliegue productivo.
