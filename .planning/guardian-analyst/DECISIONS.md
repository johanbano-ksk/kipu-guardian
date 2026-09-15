# Decisiones de diseño

## ADR-001 — Separar el piloto local de los runtimes existentes

El flujo mejorado vive en módulos de Guardian. Mantener el worker, su política,
el CLI histórico v2 y las evidencias anteriores sin reetiquetarlos. No modificar
el otro dashboard ni desplegar. Razón: limitar regresiones y conservar auditoría.

## ADR-002 — Perfiles explícitos, nunca decididos por Gemini

`sales` incluye SALE/DEFERRED/DEFFERED. `authorizations` agrega PREAUTHORIZATION.
Ambos usan APPROVED/(APPROVED+DECLINED), MID texto y última versión por
MID+transaction_code. CAPTURE no entra en ese denominador. Se cuentan intentos,
no órdenes únicas. La vista inicia en autorizaciones y muestra el criterio;
cambiar perfil crea otra identidad de evidencia. No hay SQL libre en la vista.

## ADR-003 — Diagnosticar exclusiones con la misma extracción

Recuperar agregados diarios y contadores de las etapas de filtrado en una
consulta acotada. No explorar toda la tabla ni transmitir filas crudas a Gemini.
Contadores de CDC no se presentan como pagos únicos. Fechas: Ecuador, límite
SQL final exclusivo. Mantener el filtro directo sobre create_timestamp.

## ADR-004 — Ausencia de ventana implica abstención

El contrato actual no permite verificar la ventana/universo del incidente.
Registrar `not_comparable` y admitir sólo `requires_review` en esta entrega,
aunque Gemini devuelva un dictamen distinto con citas existentes. El modelo
puede explicar el contexto y la siguiente comprobación, no acreditar incidentes.
No tratar fechas arbitrarias del navegador como certificación de comparabilidad.

## ADR-005 — Frescura observada no equivale a cobertura global

La última ingesta de filas del MID/rango sólo describe esa selección. Mostrarla
con estado global UNKNOWN y cobertura UNVERIFIED; no inventar umbrales ni dar
por probado que no hubo actividad si la tabla devuelve cero filas.

## ADR-006 — Refrescar crea una revisión, no altera el pasado

Nueva extracción explícita bajo bloqueo por caso. Persistir evidencia antes de
Gemini. Los reintentos de IA conservan esa evidencia; una actualización crea otro
revision_id con enlace al anterior. Comparar expected_revision antes de consultar
para evitar refrescos duplicados por clics simultáneos. Ante corrupción fallar
cerrado, sin extraer de nuevo automáticamente ni borrar archivos.

## ADR-007 — Calidad estructural no equivale a calidad del juicio

G1 valida esquema, cifras de entrada, perfiles, citas y dictámenes permitidos.
La comprobación semántica de toda cifra en texto y la paridad con analistas son
trabajo posterior. Las pruebas sintéticas no sustituyen una evaluación adjudicada.

## ADR-008 — Validación semántica de toda la cadena y salida cerrada

La revisión independiente detectó que comprobar sólo HEAD dejaba revisiones
anteriores con validación estructural, y que metadatos de análisis requerían
restricciones adicionales. El almacén admite un validador semántico por caso
para cada revisión; el servicio valida campos exactos, política JSON canónica,
modelo, timestamps UTC, estados y errores conocidos. La respuesta pública usa
allowlist. No se considera esto una firma criptográfica ni una protección frente
a un administrador que controle y reescriba todo el almacenamiento.
