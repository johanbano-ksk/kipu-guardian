# Criterios de aceptación critical-only

## Estado del contrato productor

El runtime final verificado de Kipu (`origin/main` en `d692b1b`) publica sólo
`Anomaly Detected v1` con `schema_version = 1.0`. La sección 2.0 se conserva
para reproducibilidad histórica de una propuesta anterior no integrada; no
describe la regla EventBridge activa ni autoriza tráfico 2.0 en producción.

`FilterPolicy` mantiene dos versiones de decisión durante la transición de
Kipu:

- `schema_version = 1.0`: `version = 2026-08-13.1`, sin cambiar la decisión
  histórica;
- `schema_version = 2.0`: verificación de la detección descriptiva publicada
  por Kipu `main`, con `v2_version = 2026-08-18.1`; el payload declara aparte
  la política productora `kipu-main-2026-08-13.1`.

Una alerta sólo se muestra cuando supera las validaciones de su propia versión.
No se completan campos ni se trasladan señales entre contratos.

## Invariantes comunes

- `alert_id`, `merchant_code`, `merchant_name` y `country`: textos no vacíos.
- Los timestamps contractuales: cadenas ISO 8601 con fecha y hora; no se
  aceptan epochs numéricos ni fechas sin hora.
- `criticality`: valor reconocido y, para aceptar, exactamente `Critica` o su
  forma acentuada equivalente.
- `approval_rate`: número JSON entre `0` y `1`; no se aceptan cadenas numéricas.
- Los conteos: enteros JSON no negativos; no se aceptan booleanos, decimales ni
  cadenas numéricas.
- Las métricas restantes: números JSON finitos y dentro de su dominio.
- Los conteos y la tasa deben ser internamente coherentes.
- Los textos, incluso `alert_summary`, `anomaly_type`, `cluster_profile` y
  `anomaly_reasons`, son informativos y nunca demuestran una señal.
- `signal_codes` tampoco demuestra una señal: sólo identifica ramas que deben
  poder recalcularse desde métricas o booleanos contractuales.
- La aceptación significa "respaldada por este payload". No confirma una causa
  raíz ni un incidente operativo externo.

## Contrato 1.0: compatibilidad conservadora

El modo heredado de `2026-08-13.1` conserva el comportamiento del MVP anterior.
Exige simultáneamente:

- `schema_version = 1.0`;
- `criticality = Critica`;
- `total_transactions >= 20`;
- `declined_count >= 20`.

Una copia de transición `1.0` puede declarar exclusivamente
`superseded_by_schema_version = 2.0`. Esa copia no compite contra las señales
heredadas: se descarta como `SUPERSEDED_BY_V2` y se completa bajo una clave
idempotente separada, para no bloquear el evento `2.0` del mismo `alert_id`.
Otro valor del marcador es un contrato inválido; un evento `1.0` sin marcador
conserva exactamente la decisión histórica.

Además:

- `declined_count <= total_transactions`;
- `round(total_transactions * approval_rate) + declined_count` no puede superar
  el total por más de una transacción de tolerancia;
- las tasas opcionales deben estar entre `0` y `1`;
- los cuantiles presentes deben respetar `q10 <= q50 <= q90`;
- `priority_score`, si existe, debe estar entre `0` y `100`;
- `predicted_dc_q90`, si existe, debe ser no negativo.

La alerta se acepta cuando cumple al menos una rama. Los límites de esta lista
son inclusivos:

1. **Impacto masivo:** `total_transactions >= 100` y
   `approval_rate <= 0.15`.
2. **Severidad con deterioro:** `total_transactions >= 50`,
   `approval_rate <= 0.20` y
   `rolling_avg_approval_rate - approval_rate >= 0.05`.
3. **Tasa extrema con caída:** `total_transactions >= 20`,
   `approval_rate <= 0.10` y
   `rolling_avg_approval_rate - approval_rate >= 0.10`.
4. **Colapso de alto volumen:** `total_transactions >= 100`,
   `approval_rate <= 0.50` y
   `rolling_avg_approval_rate - approval_rate >= 0.10`.
5. **Brecha predictiva de aprobación:** `total_transactions >= 50` y
   `predicted_ar_q10 - approval_rate >= 0.05`.
6. **Exceso predictivo de rechazos:** `total_transactions >= 50` y
   `declined_count - predicted_dc_q90 >= max(5, total_transactions * 0.05)`.

Las ramas predictivas se conservan únicamente para compatibilidad. El motor
descriptivo actual de Kipu no las utiliza ni debe simularlas desde textos.

## Contrato 2.0: evidencia verificable de Kipu main

### Identidad, tiempo y versión

Exigir:

- `schema_version = 2.0`;
- `detection_engine = descriptive_v1`;
- `policy_version = kipu-main-2026-08-13.1`;
- `observation_date` como fecha ISO `YYYY-MM-DD`;
- `published_at` y su alias transitorio `timestamp` como datetimes ISO 8601
  coherentes;
- `analysis_run_id` y `analysis_window_end`, cuando aparezcan, como un par
  atómico: el identificador no puede estar vacío, debe coincidir con `batch_id`
  cuando éste exista, y el fin de ventana debe ser UTC, tener zona horaria y no
  ser posterior a `published_at`;
- `approved_count`, `declined_count`, `unknown_status_count` y
  `total_transactions` como conteos válidos;
- `approved_count + declined_count + unknown_status_count = total_transactions`
  exactamente y coherencia de `approved_count / total_transactions` con
  `approval_rate` dentro de la tolerancia contractual;
- `adaptive_window` como entero positivo y las dos banderas ML como booleanos;
  `isolation_forest_merchant_score` e `isolation_forest_global_score`, cuando
  existen, deben ser números finitos, y el score correspondiente es obligatorio
  cuando una bandera o código ML declara anomalía;
- `priority_score` entre `0` y `100`;
- `criticality = Critica` y `priority_score >= 70`.

`cluster_profile` identifica el clúster asignado, no la causa de la alerta.
Los metadatos `analysis_run_id` y `analysis_window_end` identifican una corrida
horaria para auditoría, pero nunca aumentan la confianza ni activan una rama.
`observation_date` corresponde al día transaccional analizado y puede ser
anterior al día local o UTC de publicación cuando existe rezago T+1.
`total_approved_amount`, cuando existe, debe ser un número no negativo y se usa
para comprobar el componente de volumen del score.
`historical_avg_approval_rate`, cuando existe, representa el baseline anterior
a la observación; se valida como tasa pero no sustituye ninguna rama del gate.

### Umbrales canónicos

`policy_thresholds` debe contener los valores de la política conocida por el
reviewer. El productor no puede modificar la decisión enviando umbrales
arbitrarios:

| Campo | Valor |
|---|---:|
| `ta_low_threshold` | `0.65` |
| `ta_critical_threshold` | `0.55` |
| `zscore_mad_threshold` | `3.0` |
| `alert_min_criteria` | `2` |
| `alert_min_volume` | `5` |
| `rejection_doubling_min_mean` | `5.0` |
| `rejection_doubling_multiplier` | `2.0` |
| `volume_spike_baseline_weeks` | `4` |
| `volume_spike_min_target` | `50` |
| `volume_spike_min_avg_hist` | `10.0` |
| `volume_spike_min_ratio` | `20.0` |
| `tpv_low_threshold` | `1000.0` |
| `tpv_mid_threshold` | `10000.0` |
| `tpv_high_threshold` | `100000.0` |
| `criticality_critica_threshold` | `70.0` |
| `criticality_alta_threshold` | `50.0` |
| `criticality_media_threshold` | `30.0` |
| `isolation_forest_decision_threshold` | `0.0` |

### Cuatro criterios descriptivos

Recalcular los criterios desde las métricas. Los operadores estrictos son
intencionales:

1. `LOW_APPROVAL_RATE` (`c1`): `approval_rate < 0.65`.
2. `APPROVAL_RATE_MAD_DROP` (`c2`): `zscore_ta < -3.0`.
3. `DECLINES_ABOVE_P95` (`c3`): `zscore_rechazos > 3.0` y
   `declined_count > rolling_q95_rejections`.
4. `DECLINES_DOUBLED` (`c4`): `rolling_avg_rejections > 5.0` y
   `declined_count >= 2 * rolling_avg_rejections`.

`criteria_count` debe coincidir con el número de criterios verdaderos entre
`c1` y `c4`. Cuando `rolling_avg_rejections > 5.0`,
`rejection_change_pct` es obligatorio y debe ser coherente con
`(declined_count - rolling_avg_rejections) / rolling_avg_rejections * 100`; con
un promedio `<= 5.0` debe estar ausente.

Los códigos `ML_MERCHANT_ANOMALY` y `ML_GLOBAL_ANOMALY` deben coincidir,
respectivamente, con estas parejas:

- `is_anomaly_merchant` e `isolation_forest_merchant_score < 0.0`;
- `is_anomaly_global` e `isolation_forest_global_score < 0.0`.

Flag, código y score deben ser coherentes. Las señales ML verificadas sólo
pueden confirmar al menos un criterio descriptivo; nunca bastan por sí solas.
La ausencia de un score sólo es válida cuando su bandera es falsa y su código
ML no está declarado.

### Pico de volumen

Si aparece cualquiera de `volume_ratio`, `baseline_weekday_avg` u
`oldest_weekday_activity`, deben aparecer los tres y el ratio debe ser coherente
con `total_transactions / (baseline_weekday_avg + 0.1)`.

`VOLUME_SPIKE` es una rama independiente sólo cuando
`is_volume_spike = true`, el código también está declarado y se demuestra todo
lo siguiente:

- `total_transactions >= 50`;
- `baseline_weekday_avg >= 10.0`;
- `volume_ratio >= 20.0`;
- `oldest_weekday_activity >= 1`;
- el ratio ya superó la comprobación de coherencia anterior.

El baseline corresponde al mismo día de la semana durante las cuatro semanas
anteriores. Un código o una bandera sin estas métricas no prueba el pico.

### Gate de detección

Fuera de un pico de volumen verificado, exigir `total_transactions >= 5` y una
de estas combinaciones:

- al menos dos de `c1` a `c4`;
- al menos uno de `c1` a `c4` y alguna bandera ML verdadera;
- al menos uno de `c1` a `c4` y `approval_rate < 0.55`.

Una alerta que no supera este gate se rechaza aunque declare códigos, score o
criticidad suficientes.

### Coherencia del priority score

Recalcular siempre los tres componentes y exigir que su suma sea igual a
`priority_score`, con el redondeo contractual a un decimal.
`priority_components` es obligatorio y sus tres valores también deben coincidir
con el recálculo:

1. `volume_score`: máximo del bucket por transacciones y, si existe
   `total_approved_amount`, del bucket por TPV.
   - transacciones: `>10000 -> 30`, `>1000 -> 20`, `>100 -> 10`, resto `5`;
   - TPV: `>100000 -> 30`, `>10000 -> 20`, `>1000 -> 10`, resto `5`.
2. `approval_rate_score`:
   `<0.50 -> 40`, `<0.65 -> 30`, `<0.75 -> 15`, resto `5`.
3. `anomaly_score`: suma de puntos demostrados, limitada a `30`:
   `c3 +15`, `c1 +10`, `c2 +10`, `approval_rate < 0.55 +15`,
   `is_anomaly_merchant +12`, `is_anomaly_global +8` y pico de volumen
   verificado `+15`. `c4` participa en el gate, pero no añade puntos en la
   implementación actual de Kipu.

El score coherente confirma la clasificación `Critica`, pero no reemplaza el
gate de detección.

### Códigos admitidos

`signal_codes` sólo puede contener:

- `LOW_APPROVAL_RATE`;
- `APPROVAL_RATE_MAD_DROP`;
- `DECLINES_ABOVE_P95`;
- `DECLINES_DOUBLED`;
- `ML_MERCHANT_ANOMALY`;
- `ML_GLOBAL_ANOMALY`;
- `VOLUME_SPIKE`.

La lista debe coincidir exactamente con las condiciones recalculadas: no puede
declarar una señal falsa ni omitir una que los campos estructurados demuestran.
Un texto descriptivo parecido, un código desconocido o una lista de códigos sin
métricas no permite aceptar la alerta.

## Resultado

Para cualquiera de los dos contratos, el filtro devuelve la alerta original
sin enriquecerla. Un payload estructuralmente válido pero sin señal suficiente
se descarta como decisión de negocio; un contrato contradictorio o incompleto
es inválido y debe seguir el manejo técnico del consumidor.
