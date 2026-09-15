# G2 — contrato pendiente para reconciliar una alerta con Kipu

Estado: propuesta para acordar con el productor, **no implementada ni verificada**.
No son campos que hoy se deban asumir existentes en el payload.

## Datos mínimos necesarios

| Dato a acordar | Por qué hace falta |
|---|---|
| Inicio y fin exclusivo de la ventana observada | La hora de publicación no reconstruye qué transacciones evaluó Kipu. |
| Zona y campo temporal utilizado | Crear, completar, actualizar y publicar no son fechas intercambiables. |
| Universo versionado de tipos/estados | Ventas, preautorizaciones y capturas no deben mezclarse por accidente. |
| Llave de intento y política de reintentos/CDC | Contar transacciones, tickets u órdenes produce denominadores distintos. |
| Fuente y corte de datos usados por Kipu | El último CDC disponible hoy puede diferir del estado cuando se disparó. |
| Filtros por MID y segmentos | Emisor, procesador, marca, país o canal pueden acotar la señal. |
| Aprobadas, rechazadas y denominador exacto | Comparar una tasa redondeada sin su base no permite reconciliar. |
| Referencia de normalidad, versión y umbral | Siete días previos no equivalen necesariamente al baseline del detector. |
| Frescura/cobertura de origen | Sin corte de ingesta verificable no se distingue ausencia de retraso. |

## Prueba de salida

1. Obtener una muestra autorizada de alertas reales con esos datos del productor.
2. Consultar la misma ventana y población con SQL fijo/parametrizado, sin que Gemini
   decida los filtros. Conservar consulta, perfil, corte y métricas por caso.
3. Conciliar intentos/aprobadas/rechazadas y documentar cada diferencia. Acordar
   tolerancias sólo si hay una justificación medida; no inventar un porcentaje.
4. Incluir cambios CDC tardíos, límites horarios, tipos de operación, ventanas
   vacías y datos incompletos. Mantener `not_comparable` en los casos sin contrato.
5. Definir y probar la regla que habilitaría otros dictámenes antes de ampliar la
   lista permitida. Una cita válida o el texto de Gemini no bastan para habilitarlos.

Hasta entonces, G1 aporta contexto histórico explicable, no confirmación de incidentes.
