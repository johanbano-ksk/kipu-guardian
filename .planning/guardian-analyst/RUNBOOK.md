# Guardian analista — operación y continuidad local

## Retomar el desarrollo

1. Leer STATE, PLAN, DECISIONS y el último resultado de VALIDATION en este directorio.
2. Confirmar cambios locales antes de editar; no restaurar ni publicar trabajo ajeno.
3. Elegir un criterio pendiente con evidencia de salida; no cambiar contratos por inferencia.
4. Implementar una porción, añadir regresiones y ejecutar los comandos de PLAN.
5. Registrar resultado, limitaciones y siguiente paso. Actualizar el checklist sólo después.

## Probar desde la tarjeta

Con el servidor local del dashboard iniciado (`npm run dev -- --host 127.0.0.1
--port 3000` desde `dashboard`), abrir la lista de alertas:

1. Elegir **Autorizaciones** o **Sólo ventas** junto a la alerta. El criterio aparece
   explícito y no cambia sus filtros ni la criticidad.
2. Pulsar **Generar conclusión**. Se usa el MID y fecha Kipu del payload original.
3. Abrir el sustento: contrastar tipos incluidos/excluidos, denominador, período,
   consulta, huella y revisión. Frescura/cobertura global siguen sin verificar.
4. **Actualizar evidencia** hace otra consulta y conserva la anterior. Puede
   generar costo Athena y una nueva llamada a Gemini. No usarlo como sondeo continuo.
5. Si aparece conflicto, **Cargar última revisión** antes de decidir otro refresco.

La sesión AWS del histórico es `data-core`; la extracción diaria usa su perfil
separado. Una clave Gemini se configura sólo en el entorno local del backend.
No poner claves, resultados reales, nombres comerciales ni MID en estos archivos.

## Diagnóstico seguro

| Estado | Interpretación y siguiente comprobación |
|---|---|
| `outside_scope` | Sí hay actividad vigente, pero no tipos del perfil elegido. Revisar desglose; no afirmar falta de datos del comercio. |
| `no_source_rows` | No se observaron filas para MID/rango. Validar cobertura y fuente; no acredita cero transacciones. |
| `invalid_keys` | Las filas observadas carecen de claves utilizables. Escalar calidad del dato; no contar filas como intentos. |
| `no_current_records` | No quedan operaciones vigentes después del tratamiento CDC. Revisar contadores y borrados. |
| `no_final_results` | Hay operaciones del criterio, pero sin resultado final aprobado/rechazado. No usar pendientes en aceptación. |
| `SSO_EXPIRED` | Renovar sesión `aws sso login --profile data-core`; no cambiar roles ni pedir claves por chat. |
| `ACCESS_DENIED` | Revisar permiso concreto con IT; no sustituir la fuente ni cambiar IAM automáticamente. |
| `REVISION_CONFLICT` | Cargar HEAD vigente; no repetir el refresco con un ID antiguo. |
| `AGENT_BUSY` | Esperar si hay proceso activo. Si persiste, identificar caso y proceso antes de intervenir. |
| `EVIDENCE_INVALID` | Detener cambios del caso, conservar archivos y revisar el error; no borrar para forzar otra extracción. |

Ante bloqueo huérfano, corrupción, revisión huérfana o límite de almacenamiento,
identificar los archivos exactos de `reports/guardian-evidence/{case_id}` mediante
lectura. Preservar una copia verificable antes de una recuperación autorizada.
No existe reparación automática ni comando general de borrado. La edición manual
del ledger no equivale a evidencia original válida.

## Artefactos y fronteras

- `reports/guardian-evidence/`: datos privados y revisiones v3, excluidos de Git.
- `reports/guardian-alerts/`: evidencia inline anterior, conservada sin migrar.
- `guardian_history.py`: consulta fija y validación numérica.
- `guardian_assurance.py`: contexto de comparación y frontera de Gemini.
- `guardian_evidence_store.py`: revisiones, bloqueos y validación de cadena.
- `guardian_cases.py`: coordinación local por alerta y salida pública.
- `guardian_view.py`: contrato de operaciones; `dashboard/local/guardian-plugin.ts`:
  frontera HTTP local. El dashboard presenta resultados; no decide el universo SQL.

Los tests son sintéticos y no llaman a AWS/Gemini. Las verificaciones reales se
registran por separado en VALIDATION. No activar la integración en Lambda/SQS,
publicar ni desplegar como consecuencia implícita de una prueba local.
