# Worker Kipu: EventBridge → SQS → Hub

## Recursos

[`infra/sqs-worker.yaml`](../infra/sqs-worker.yaml) conecta el bus existente de
Kipu y crea:

- una regla para `acceptance.kipu` con `Anomaly Detected v1`;
- una cola de entrada cifrada con long polling;
- una DLQ para fallos de entrega y redrive de procesamiento;
- una tabla DynamoDB con TTL y `idempotency_key` como clave primaria para
  ocurrencias `occurrence#…` y decisiones `decision#…` del reviewer;
- un rol restringido que puede asumir el perfil local del MVP.

El worker necesita estos outputs/configuración:

```dotenv
AWS_REGION=us-east-1
SQS_INPUT_QUEUE_URL=<InputQueueUrl>
SQS_IDEMPOTENCY_TABLE=<IdempotencyTableName>
EVENTBRIDGE_OUTPUT_BUS_NAME=<EventBusName>
FILTER_POLICY_PATH=config/filter_policy.yaml
ALERT_OCCURRENCE_TTL_HOURS=720
```

## Contratos de entrada

La cola activa admite exactamente el contrato final
`detail-type = Anomaly Detected v1` y `schema_version = 1.0`. No consume la
copia legacy sin versión ni el contrato 2.0 propuesto anteriormente.

### Contrato final 1.0

```json
{
  "version": "0",
  "id": "event-001",
  "source": "acceptance.kipu",
  "detail-type": "Anomaly Detected v1",
  "detail": {
    "schema_version": "1.0",
    "alert_id": "alert-001",
    "merchant_code": "merchant-001",
    "merchant_name": "Example Merchant",
    "country": "Ecuador",
    "timestamp": "2026-07-30T12:00:00Z",
    "criticality": "Critica",
    "approval_rate": 0.20,
    "rolling_avg_approval_rate": 0.25,
    "total_transactions": 100,
    "declined_count": 80
  }
}
```

La versión `1.0` mantiene las seis ramas critical-only de `2026-08-13.1` y sus
gates de 20 transacciones y 20 rechazos. Kipu final aplica internamente
`alert_min_volume = 20` y un guard de caída contra baseline, pero el evento no
expone todas las métricas usadas por ese gate. El reviewer no las infiere desde
textos. La comparación exacta está en
[`kipu-final-contract.md`](kipu-final-contract.md).

El evaluador 2.0 y su fixture permanecen únicamente para reproducibilidad de
evidencia histórica. No forman parte de la regla EventBridge desplegable.

### Errores de contrato

El parser rechaza otros productores, detail types no versionados, combinaciones
incompatibles de detail type/schema y alertas incompletas. `alert_id`,
`merchant_code`, `merchant_name` y `country` deben existir y no estar vacíos.
Los mensajes que incumplen el contrato quedan disponibles para el redrive a
DLQ.

Las tasas, conteos, thresholds y métricas deben ser números JSON reales: valores
como `"100"` o `"0.20"` no se convierten silenciosamente. La criticidad se
normaliza sin distinguir mayúsculas ni el acento de `Critica`. Los timestamps
deben ser strings ISO 8601 con fecha y hora; epochs numéricos y fechas sin hora
son inválidos.

Los comandos para ejecutar el filtro y el recorrido LocalStack con `1.0` están
en [Probar el filtro directamente](../README.md#probar-el-filtro-directamente)
y [Prueba local de extremo a extremo](../README.md#prueba-local-de-extremo-a-extremo).

## Contrato de salida

Sólo las alertas que superan los criterios se publican al mismo bus
con:

```json
{
  "Source": "acceptance.reviewer",
  "DetailType": "Anomaly Validated v1",
  "EventBusName": "acceptance-intelligence-bus-dev",
  "Detail": "<payload Kipu original>"
}
```

La salida conserva `Anomaly Validated v1`. El payload original se reenvía sin
mutarlo.

## Captura e idempotencia del reviewer

Phase 08 no cambia la deduplicación ni el almacenamiento de Kipu. Después de
validar el envelope y su contrato versionado, el worker guarda el detalle
original en su propia tabla antes de evaluar señales:

- `occurrence#event:<event-id>` identifica la ocurrencia cuando existe el ID de
  EventBridge;
- `occurrence#sha256:<hash-del-envelope>` es el fallback cuando falta ese ID;
- `decision#event:<event-id>` y `decision#sha256:<hash-del-envelope>` separan el
  outcome idempotente de la evidencia recibida.

La inserción de la ocurrencia es condicional. Repetir la misma identidad con el
mismo SHA-256 es un retry; reutilizarla con otro payload es una colisión y falla
sin sobrescribir el registro. Un fallo de captura detiene la decisión y el ACK.
El archivo conserva las ocurrencias `720` horas por defecto y rechaza antes de
DynamoDB cualquier item estimado por encima de `350 KiB`; nunca trunca el
payload silenciosamente.

La fecha de publicación usa `time` del envelope EventBridge y recurre al
timestamp contractual sólo cuando falta. La fecha de observación se conserva
únicamente cuando `2.0` declara `observation_date`; no se infiere para `1.0`.

Los eventos `Alta`, `Media` o `Baja`, y los críticos sin impacto o señal
material, se capturan como ocurrencias válidas de transporte, pero son descartes
de negocio: su decisión se marca `COMPLETED` y se eliminan de SQS sin
publicación. Una copia transitoria `1.0` con
`superseded_by_schema_version = 2.0` también se captura y después se descarta
como `SUPERSEDED_BY_V2`. Los contratos inválidos fallan antes de crear
`occurrence#` o `decision#` y permanecen para DLQ. El worker renueva
periódicamente la visibilidad SQS y el lock de decisión mientras procesa.

## Credenciales

Usar credenciales temporales mediante el perfil AWS configurado con
`source_profile` SSO y el rol emitido por la plantilla. No guardar access keys en
archivos del proyecto.

## Observabilidad

Monitorear:

- mensajes visibles en la DLQ;
- edad del mensaje más antiguo y mensajes no visibles sostenidos;
- `sqs_poll_failed` y `queued_review_failed`;
- `sqs_visibility_extension_failed`;
- `idempotency_lock_extension_failed`;
- throttling de DynamoDB, SQS y EventBridge.

Hub debe suscribirse únicamente a:

```json
{
  "source": ["acceptance.reviewer"],
  "detail-type": ["Anomaly Validated v1"]
}
```

## Activación de una política

El YAML se carga una sola vez al iniciar el worker. Después de actualizar
`config/filter_policy.yaml`, reiniciar el proceso para activar la nueva versión.
Los outcomes `READY` persistidos bajo `decision#…` conservan su decisión y los
`COMPLETED` no se recalculan. Los registros `occurrence#…` son independientes y
preservan que el evento fue recibido. La publicación continúa siendo
at-least-once y Hub mantiene su propia deduplicación.

No confundir los identificadores:

- `version = 2026-08-13.1` se conserva en las decisiones reviewer `1.0`;
- `v2_version = 2026-08-18.1` identifica las decisiones reviewer `2.0`;
- `policy_version = kipu-main-2026-08-13.1` identifica los umbrales con los que
  Kipu generó un evento `2.0`.

El reviewer exige que `policy_thresholds` coincida con sus valores canónicos;
no adopta umbrales arbitrarios enviados por el productor.

## Orden de activación

1. Desplegar la regla y el worker limitados a `Anomaly Detected v1`.
2. Ejecutar el periodo shadow y observar DLQ, rechazos de contrato y decisiones.
3. Hacer que Hub consuma `Anomaly Validated v1` y retirar su suscripción directa
   a los eventos originales sólo después de validar los resultados.
4. Negociar un contrato nuevo antes de aceptar otro detail type o schema.

La captura de Phase 08 es una evolución exclusiva del reviewer: después de su
verificación se despliega reiniciando o reemplazando el worker. No requiere otro
cambio en Kipu y no reconstruye eventos recibidos antes de activarse.

El template conserva `acceptance-intelligence-bus-dev` como valor predeterminado.
Para PROD hay que desplegarlo en la cuenta del bus
`acceptance-intelligence-bus-prod` o configurar forwarding cross-account con
políticas explícitas. La alineación del contrato no autoriza ni realiza ese cambio.
