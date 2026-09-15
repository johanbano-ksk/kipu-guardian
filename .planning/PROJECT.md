# Kipu Alert Reviewer — Project Harness

## Core value

Evitar que Hub muestre alertas que no sean críticas o que no estén respaldadas
por impacto, severidad o deterioro cuantitativo material en la información
estructurada que Kipu ya incluye en cada evento.

## Integration boundary

```text
Kipu
  -> EventBridge acceptance.kipu / Anomaly Detected v1
  -> SQS reviewer input
  -> DynamoDB reviewer occurrence# archive
  -> critical-only payload filter
  -> DynamoDB reviewer decision# state
  -> EventBridge acceptance.reviewer / Anomaly Validated v1
  -> Hub
  -> Slack
```

## Constraints

- No consultar Elastic, OpenSearch, Kibana, dashboards ni APIs para decidir.
- Preservar el payload aceptado, especialmente `alert_id`, `batch_id` y
  `group_name`.
- No enviar descartes de negocio a la DLQ.
- No perder mensajes malformados o fallos técnicos: dejarlos para redrive.
- Capturar únicamente envelopes versionados y estructuralmente válidos antes de
  decidir; los malformados permanecen para DLQ y no entran al archivo.
- Mantener evidencia `occurrence#…` y decisiones `decision#…` en namespaces
  independientes, idempotentes por ID EventBridge o hash del envelope.
- Tratar la entrega de salida como at-least-once.
- Exigir criticidad `Critica` o su forma acentuada equivalente como condición
  necesaria, pero nunca como evidencia suficiente por sí sola.
- Mantener todos los umbrales en una política versionada compartida por el
  worker y la skill del repositorio.

## Source contract

- Event bus development: `acceptance-intelligence-bus-dev`
- Source: `acceptance.kipu`
- Detail type activo: `Anomaly Detected v1`
- Schema version activa: `1.0`
- Required identity: `alert_id`, `merchant_code`, `merchant_name`, `country`
- Reviewer policy activa: `2026-08-13.1`
- Global business gates: critical severity, at least 20 transactions and at
  least 20 declines
- Critical signals: four observed impact/deterioration branches and two
  predictive branches with a minimum volume of 50 and material margins
- Producer: Kipu `origin/main@d692b1b` y snapshot local `../kipu-review-3171c38`
- Consumer: Hub notification processor

## Current milestone

M8 — Reviewer-owned occurrence archive, suite local aprobada, runtime
Docker/LocalStack pendiente y sin despliegue AWS.
