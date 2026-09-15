# Registro de validación

Fecha de cierre de G1: 2026-09-09. Alcance validado: piloto local por alerta.

| Fecha | Criterio | Evidencia | Resultado |
|---|---|---|---|
| 2026-09-09 | Línea base anterior | 845 tests Python / 27 tests UI, reportados antes de este hito | REFERENCIA, no validación de G1 |
| 2026-09-09 | G1-01–05 | Tests de SQL v3, perfiles, CDC, diagnóstico, tipos y contexto de Gemini | APROBADO |
| 2026-09-09 | G1-06–07 | Casos/almacén: corrupción HEAD e histórica, timestamps/estado, concurrencia, no_data y reintentos | APROBADO |
| 2026-09-09 | Revisión independiente | Dos hallazgos de integridad corregidos; ADR-008 y regresiones | CERRADOS |
| 2026-09-09 | Regresión Python | `.venv\Scripts\python.exe -m pytest tests -o addopts='' -q` | **1.209 passed, 6 skipped**; 40,68 s |
| 2026-09-09 | Ruff | Seis módulos Python modificados y cinco archivos de pruebas nuevos/modificados | APROBADO |
| 2026-09-09 | G1-08 | `node --test local/guardian-plugin.test.mjs local/alert-conclusion.test.mjs` desde dashboard | **34 passed**, sin omisiones |
| 2026-09-09 | TypeScript | `npx tsc --noEmit` desde dashboard | APROBADO |
| 2026-09-09 | Build/lint | `npm run build` y `npm run lint` desde dashboard | APROBADOS |
| 2026-09-09 | SQL real | EXPLAIN (TYPE VALIDATE) con columnas verificadas en Glue | SUCCEEDED, 0 bytes escaneados |
| 2026-09-09 | G1-09 | API local → agente → Athena; Gemini, reuso y refresh | APROBADO, detalles abajo |

Las 6 omisiones corresponden a symlinks reales que Windows no permite crear
sin privilegios. Se ejecutaron las pruebas equivalentes de junction y controles
de rutas. No se elevaron permisos para cambiar esa condición.

## Matriz cubierta

- Perfiles ventas/autorizaciones; capturas nunca suman a aceptación.
- CDC: claves vacías, borrados, cambios de tipo/estado, orden de versiones y zona horaria.
- Contadores de origen/exclusión y fechas de ingesta consistentes.
- Sin ventana confiable: no se admiten dictámenes definitivos ni entradas falsificadas.
- Evidencia anterior intacta al refrescar; fallo de consulta no sustituye última revisión.
- Reintento Gemini conserva consulta, digest y revisión; no inventa modelo ejecutado.
- Corruptos, enlaces de revisión, concurrencia y doble clic fallan de forma segura.
- Agente/API/UI muestran perfil, motivo del vacío, limitación y revisión seleccionada.
- Regresión completa Python, Node, TypeScript, build y lint.
- Comprobación local de punta a punta separada de pruebas sintéticas.

## Prueba real controlada

Se usó el caso ya aportado por el usuario y siete días completos anteriores a su
alerta. No se copian aquí MID, comercio, payloads ni agregados comerciales; la
evidencia completa queda en `reports/guardian-evidence/`, excluido de Git.

| Operación | ID Athena | Resultado técnico |
|---|---|---|
| EXPLAIN v3 | `d7d3f3b9-87db-4447-bad1-5bf067301bd7` | SQL admitido; 0 bytes |
| Autorizaciones | `af8dfb84-389f-48b8-ac22-d9e2078b0785` | Datos elegibles; preautorizaciones incluidas, capturas excluidas |
| Sólo ventas | `0c313a24-9279-44cf-a7a8-c96413c7e6f6` | `outside_scope` + `no_data`; hay actividad excluida; modelo/intento nulos |
| Refresco de ventas | `bc945d6e-af04-49ea-81e3-bd28fb24cf92` | Nueva revisión con enlace a anterior; bytes anteriores idénticos |

Cada SELECT escaneó 928.324.505 bytes. Se hicieron tres SELECT y un EXPLAIN;
el cambio de perfil aún implica otra extracción. Reutilización entre perfiles
y presupuestos operativos forman parte de G6, no están implementados.

Gemini devolvió `unavailable` en el primer intento (causa original no determinada
a partir del error público normalizado). El reintento completó `requires_review`
sin cambiar revisión, consulta ni huella. No se atribuye ese primer fallo a una
causa específica ni se promete que no vuelva a ocurrir.

El refresh obtuvo HTTP 200, dos revisiones enlazadas y preservación byte a byte.
Repetir `expected_revision` antiguo obtuvo HTTP 409/`REVISION_CONFLICT`. Volver a
cargar autorizaciones obtuvo HTTP 200, `reused=true`, misma revisión/consulta y
dictamen completado, sin otra extracción.

La consulta real usa ExecutionParameters. Sólo el EXPLAIN sustituyó sus tres
parámetros ya validados por literales, porque esa operación de Athena no aplica
ExecutionParameters de la misma forma. No se habilitó SQL libre para clientes.

## Qué no quedó validado

- No se hizo navegación automatizada ni QA visual con navegador; UI comprobada
  por render estático, contrato, tipos, build y lint.
- No se verificó la ventana del incidente ni equivalencia exacta con Kipu (G2).
- Las citas existentes pasan validación estructural; falta vincular/verificar
  semánticamente cada afirmación y cifra, incluidos agregados citados como cobertura (G4).
- No se ejecutó una evaluación adjudicada por analistas, ni prueba de carga o SLO.
- La cadena local no está firmada; no impide una reescritura coherente por un
  administrador. Tampoco conserva un ledger separado de todos los intentos de IA.

## Limitaciones permanentes hasta demostrar lo contrario

No hay prueba de paridad con analistas; no se certifica completitud del Data Lake;
no hay despliegue ni cambio autorizado en Kipu o en operaciones de pago.
