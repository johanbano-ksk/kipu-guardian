# Estado de Guardian analista

Actualizado: 2026-09-09. Estado: G1 implementado y validado localmente. Roadmap completo pendiente.

## Punto de partida

- Históricos v2: sólo ventas, siete días previos, evidencia por transacción.
- Regresión de referencia: 845 pruebas Python, 27 pruebas del dashboard; no son
  una evaluación de calidad contra analistas humanos.
- Diagnóstico confirmado: el flujo de preautorización queda fuera del perfil de
  ventas; el mensaje `no_data` resulta ambiguo y se conserva sin refresco.
- La hora de publicación no identifica por sí sola la ventana observada por Kipu.

## Entrega G1

| Componente | Responsabilidad | Estado |
|---|---|---|
| Histórico v3 | Perfiles, SQL, exclusiones y validación de métricas | Validado con pruebas sintéticas y Athena real |
| Revisiones de evidencia | Bloqueo, persistencia y actualización conservando archivos | Pruebas de cadena/corrupción y refresh real correctos |
| Comparabilidad | Abstención y contexto permitido a Gemini | Dictamen definitivo bloqueado; requires_review real obtenido |
| Integración y UX | Agente local, controles y mensajes | API/SSR/Node/TypeScript/build/lint correctos; sin QA visual |
| Harness | Plan, decisiones, runbook y evidencia de validación | Actualizado; conserva M8 anterior como línea separada |

Regresión final: **1.209 pruebas Python aprobadas, 6 omitidas**, 34 pruebas Node
aprobadas. Las omisiones son pruebas de symlinks reales sin privilegio Windows;
las equivalentes de junction y rutas inseguras sí se ejecutaron. No equivale a
evaluación de calidad de decisiones frente a humanos.

Prueba real: perfiles aislados, diagnóstico de operaciones excluidas, reintento
Gemini sin nueva consulta, refresh con dos revisiones y rechazo HTTP 409 de un
refresco obsoleto. Resultados privados en `reports/`; IDs técnicos en VALIDATION.

## Siguiente punto de reanudación

Preparar G2 con [el contrato pendiente](G2-CONTRACT.md). Acordar la ventana real,
universo, corte y referencia con el productor Kipu antes de habilitar confirmación
o descarte. Luego avanzar a G3 (segmentos) y G4 (validación semántica de conclusiones).
Para retomar/operar, leer [RUNBOOK.md](RUNBOOK.md). No publicar ni modificar
políticas del worker por inferencia.

El servidor local queda disponible en http://127.0.0.1:3000/. No hubo despliegue,
commit, push, cambio IAM, modificación de Kipu ni del dashboard Acceptance compartido.

## Dependencias no resueltas

- G2: contrato y ventana observada verificable del productor Kipu.
- Frescura/cobertura global: no hay un watermark/SLA de ingesta acordado.
- G7: muestra de casos adjudicados y umbrales de calidad acordados con analistas.
