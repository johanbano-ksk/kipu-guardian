# Contrato final de Kipu consumido por el reviewer

## Fuente verificada

El contrato se contrastó contra Kipu `origin/main` en
`d692b1b2c38e3de28912ad16e12489437b9b8ecc` y contra el snapshot de revisión
`3171c38` disponible localmente. Ambos exponen el mismo límite de integración:

- `source = acceptance.kipu`;
- `detail-type = Anomaly Detected v1`;
- `detail.schema_version = 1.0`.

Kipu también publica temporalmente el detail type no versionado
`Anomaly Detected`. El reviewer no se suscribe a esa copia para evitar procesar
dos veces la misma publicación lógica.

El contrato final no publica `Anomaly Detected v2`. El soporte 2.0 que existe en
el código y en artefactos históricos del reviewer corresponde a una propuesta
anterior no integrada y no forma parte de la regla EventBridge activa.

## Campos publicados

El detalle final contiene:

- `schema_version`, `alert_id`, `merchant_code`, `merchant_name`, `country`;
- `criticality`, `anomaly_type`;
- `approval_rate`, `rolling_avg_approval_rate`;
- `total_transactions`, `declined_count`;
- `top_rejections`, `alert_summary`, `timestamp`;
- opcionalmente `batch_id` y `group_name`.

El reviewer conserva el detalle original y sólo usa campos estructurados para
decidir. Los textos son informativos.

## Cambios internos de la versión final

La versión final de Kipu elevó `alert_min_volume` de 5 a 20 transacciones y
habilitó un guard para los caminos de un solo criterio. Ese guard exige una
señal accionable: caída estadística, una diferencia estrictamente mayor a 10
puntos frente a `rolling_avg_approval_rate`, o una anomalía de rechazos.

Esos criterios internos, flags ML, z-scores y componentes del
`priority_score` no se incluyen en el evento 1.0. Por tanto, el reviewer no los
reconstruye desde `alert_summary` ni afirma haber verificado el gate interno de
Kipu. Aplica su política critical-only independiente `2026-08-13.1` usando sólo
la evidencia que sí está presente.

## Consecuencia operativa

La regla EventBridge del reviewer acepta exclusivamente la pareja final
`Anomaly Detected v1`/`1.0`. Un cambio futuro de schema o detail type requiere
un contrato productor-consumidor explícito, un fixture generado por Kipu y una
ventana shadow antes de ampliar la suscripción.
