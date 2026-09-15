# Guardian analista — plan de ejecución

Fecha de inicio: 2026-09-09. Alcance: agente Guardian y su dashboard local.

## Objetivo y criterio de éxito

Acercar Guardian al trabajo de un analista de aceptaciones mediante evidencia
comparable, diagnóstico verificable y evaluación humana. No prometer equivalencia
con un analista antes de medirla. El resultado de cada hito debe tener código,
pruebas y registro en [VALIDATION.md](VALIDATION.md).

## Hitos y dependencias

| Hito | Entregable | Estado actual | Criterio de salida |
|---|---|---|---|
| G1 | Evidencia confiable y UX explicativa | Implementado y validado localmente | Perfiles fijos, diagnóstico de exclusiones, límites de comparabilidad, actualización versionada y regresiones |
| G2 | Reconciliación exacta con Kipu | Pendiente de contrato | Ventana observada, universo, zona, corte y versión acordados con productor; contrastes reproducibles |
| G3 | Diagnóstico segmentado | Planificado | Emisor/procesador/marca/canal/motivo y referencia comparable; evidencia de contribución al deterioro |
| G4 | Verificación de conclusiones | Parcial en G1 | Cada cifra/afirmación trazable; abstención obligatoria si falta evidencia; pruebas adversariales |
| G5 | Gestión de incidentes | Planificado | Agrupación, responsable, acción, seguimiento y recuperación; sin acciones de pago automáticas |
| G6 | Eficiencia y operación | Planificado | Presupuesto de consultas, agregados reutilizables, cola, identidad de servicio y observabilidad |
| G7 | Evaluación frente a analistas | Planificado | Casos adjudicados, evaluación temporal reservada, errores críticos, tiempo, costo y derivaciones |

G2 requiere datos que el contrato Kipu actual no publica; no se inventará la
ventana observada a partir de la hora de generación. G5–G7 no autorizan publicar
sitios, modificar IAM, cerrar casos ni cambiar rutas de pagos automáticamente.

## Primera entrega implementable — G1

- [x] G1-01: perfiles `sales` y `authorizations` fijos y versionados; capturas separadas.
- [x] G1-02: única extracción parametrizada con conteos que expliquen cada exclusión.
- [x] G1-03: distinguir actividad excluida, ausencia de filas, llaves inválidas y estados no finales.
- [x] G1-04: mostrar fecha de ingesta observada sin afirmar cobertura/frescura global.
- [x] G1-05: contrato de comparación explícito; no confirmar/descartar con ventana no verificada.
- [x] G1-06: generar y actualizar evidencia en revisiones enlazadas; preservar anteriores.
- [x] G1-07: modelo/fecha de IA sólo cuando se ejecutó; estados vacíos sin dictamen inventado.
- [x] G1-08: dashboard inline con perfil, diagnóstico, trazabilidad y actualización explícita.
- [x] G1-09: pruebas unitarias/semánticas/contrato y comprobación local API → agente → Athena/Gemini.

La prueba de G1-09 no incluyó navegación automatizada ni revisión visual del navegador.
Ver resultados y alcance exacto en [VALIDATION.md](VALIDATION.md). G2–G7 no están
completados por el hecho de finalizar G1; [G2-CONTRACT.md](G2-CONTRACT.md) registra
el acuerdo necesario para el siguiente hito.

## Reglas del harness

1. Leer [STATE.md](STATE.md) al retomar y consultar decisiones antes de cambiar contratos.
2. Trabajar en porciones comprobables; registrar fecha, archivos, prueba y resultado.
3. No marcar completado un criterio sólo porque hay código o compila.
4. Separar prueba sintética, consulta real y validación humana; no intercambiar sus conclusiones.
5. No guardar claves, payloads comerciales ni resultados transaccionales en estos Markdown.
   Las evidencias privadas quedan en `reports/`, excluido de Git.
6. Conservar protocolos anteriores y errores conocidos. No borrar evidencia para pasar pruebas.
7. Revisión final independiente, regresión del worker y verificación del dashboard antes de entrega.

## Fuera del cambio inmediato

Cambios en Kipu, suscripciones/EventBridge, política de criticidad, el dashboard
Acceptance compartido, despliegues, entrenamiento de modelos y operación autónoma.

## Comandos de comprobación

Desde la raíz: `.venv\Scripts\python.exe -m pytest tests -o addopts='' -q`.
Desde `dashboard`: `node --test local/guardian-plugin.test.mjs local/alert-conclusion.test.mjs`,
`npx tsc --noEmit`, `npm run build` y `npm run lint`.
Ruff se ejecuta sobre los archivos Python modificados. Los resultados reales y
limitaciones deben registrarse en VALIDATION antes de actualizar este checklist.
