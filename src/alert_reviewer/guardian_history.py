"""Versioned Guardian history with fixed metric profiles and reconciled diagnostics.

This is deliberately separate from the standalone v2 sales-history contract. All
profiles share one bounded extraction; neither a caller nor the model supplies SQL.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import UTC, date, datetime
from typing import Any

from alert_reviewer.datalake_history import (
    HISTORY_SCHEMA_VERSION as LEGACY_SCHEMA_VERSION,
)
from alert_reviewer.datalake_history import (
    TIMEZONE,
    AthenaHistoryConfig,
    AthenaHistoryReader,
    HistoryError,
    HistoryQuery,
    build_history_sql,
    historical_evidence,
    summarize_rows,
)

GUARDIAN_HISTORY_SCHEMA_VERSION = "3.0"
PROFILE_VERSION = "1"
_SALES_TYPES = ("SALE", "DEFERRED", "DEFFERED")
_PROFILES = {
    "sales": ("Aceptación de ventas", _SALES_TYPES),
    "authorizations": (
        "Aceptación de autorizaciones",
        (*_SALES_TYPES, "PREAUTHORIZATION"),
    ),
}
OPERATION_GROUPS = ("sales", "preauthorizations", "captures", "other_types")
DIAGNOSTIC_COUNTERS = (
    "source_cdc_rows",
    "missing_transaction_code_rows",
    "superseded_rows",
    "deleted_transactions",
    "current_transactions",
)
OPERATION_COUNTERS = tuple(
    f"{group}_{status}"
    for group in OPERATION_GROUPS
    for status in ("approved", "declined", "other")
)
_ROW_FIELDS = frozenset(
    ("business_date", "last_ingested_at", *DIAGNOSTIC_COUNTERS, *OPERATION_COUNTERS)
)
_MAX_COUNT = 2**53 - 1


def metric_profile_definition(metric_profile: str) -> dict[str, Any]:
    """Return a fresh, server-owned metric definition, never an inferred profile."""
    if not isinstance(metric_profile, str) or metric_profile not in _PROFILES:
        raise HistoryError("INVALID_METRIC_PROFILE", "Selecciona ventas o autorizaciones.")
    label, kinds = _PROFILES[metric_profile]
    return {
        "id": metric_profile,
        "label": label,
        "version": PROFILE_VERSION,
        "included_types": list(kinds),
    }


def _canonical_config(config: AthenaHistoryConfig) -> None:
    if config.approved_statuses != ("APPROVED",) or config.declined_statuses != ("DECLINED",):
        raise HistoryError(
            "INVALID_STATUS_CONFIG",
            "Los perfiles Guardian usan exclusivamente APPROVED y DECLINED.",
        )


def build_guardian_history_sql(
    query: HistoryQuery,
    columns: dict[str, str],
    config: AthenaHistoryConfig,
) -> tuple[str, list[str]]:
    """One MID/date-bounded scan, preserving excluded rows for quality diagnostics."""
    _canonical_config(config)
    # Reuse the existing fail-closed catalog/type checks and UTC-bound parameters.
    # The v2 SELECT itself is not executed or used as evidence for this contract.
    _, parameters = build_history_sql(query, columns, config)
    zoned = "with time zone" in columns["create_timestamp"].lower()
    creation = "create_timestamp" if zoned else "with_timezone(create_timestamp, 'UTC')"
    ingested = "etl_job_timestamp" if zoned else "with_timezone(etl_job_timestamp, 'UTC')"
    local_day = f"CAST(at_timezone({creation}, '{TIMEZONE}') AS date)"
    order = (
        "coalesce(update_timestamp, event_timestamp, etl_job_timestamp, create_timestamp) "
        "DESC, event_id DESC NULLS LAST"
    )
    aggregates = []
    for group in OPERATION_GROUPS:
        for status in ("approved", "declined", "other"):
            condition = (
                f"status = '{status.upper()}'"
                if status != "other"
                else "(status IS NULL OR status NOT IN ('APPROVED', 'DECLINED'))"
            )
            aggregates.append(
                "count_if(row_category = 'current' "
                f"AND operation_group = '{group}' AND {condition}) AS {group}_{status}"
            )
    operation_sql = ",\n       ".join(aggregates)
    # Invalid keys are classified before looking at rn: every invalid CDC row is
    # counted as missing, never collapsed into a fictional transaction/attempt.
    sql = f"""WITH versions AS (
    SELECT {local_day} AS business_date,
           upper(trim(transaction_status_type)) AS status,
           upper(trim(transaction_type)) AS operation_type,
           transaction_code IS NOT NULL AND trim(transaction_code) <> '' AS valid_key,
           is_deleted, {ingested} AS ingested_at,
           row_number() OVER (
               PARTITION BY merchant_code, transaction_code
               ORDER BY {order}
           ) AS rn
    FROM card_transaction
    WHERE merchant_code = ?
      AND create_timestamp >= ? AND create_timestamp < ?
), classified AS (
    SELECT business_date, status, ingested_at,
           CASE WHEN NOT valid_key THEN 'missing_key'
                WHEN rn > 1 THEN 'superseded'
                WHEN coalesce(is_deleted, false) THEN 'deleted'
                ELSE 'current' END AS row_category,
           CASE WHEN operation_type IN ('SALE', 'DEFERRED', 'DEFFERED') THEN 'sales'
                WHEN operation_type = 'PREAUTHORIZATION' THEN 'preauthorizations'
                WHEN operation_type = 'CAPTURE' THEN 'captures'
                ELSE 'other_types' END AS operation_group
    FROM versions
)
SELECT CAST(business_date AS varchar) AS business_date,
       count(*) AS source_cdc_rows,
       count_if(row_category = 'missing_key') AS missing_transaction_code_rows,
       count_if(row_category = 'superseded') AS superseded_rows,
       count_if(row_category = 'deleted') AS deleted_transactions,
       count_if(row_category = 'current') AS current_transactions,
       {operation_sql},
       to_iso8601(at_timezone(max(ingested_at), 'UTC')) AS last_ingested_at
FROM classified
GROUP BY business_date ORDER BY business_date
LIMIT 32"""
    return sql, parameters


def _utc_timestamp(value: Any, *, optional: bool = True) -> str | None:
    if optional and value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Invalid timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp requires a timezone")
    return parsed.astimezone(UTC).isoformat()


def _count(value: Any) -> int:
    # Athena values are decimal strings. Saved reports are checked separately as
    # actual integers, excluding bool, to prevent False == 0 accepting corruption.
    if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]{0,15}", value):
        raise ValueError("Invalid count")
    result = int(value)
    if result > _MAX_COUNT:
        raise ValueError("Count exceeds exact JSON integer range")
    return result


def _validate_source(source: Any) -> dict[str, Any]:
    if not isinstance(source, dict) or set(source) - {
        "catalog",
        "database",
        "table",
        "query_execution_id",
        "data_scanned_bytes",
        "retrieved_at",
    }:
        raise ValueError("Invalid source metadata")
    for field, expected in (
        ("catalog", "s3tablescatalog/datalake-prod"),
        ("database", "odl"),
        ("table", "card_transaction"),
    ):
        if field in source and source[field] != expected:
            raise ValueError("Unexpected historical source")
    if "query_execution_id" in source and (
        not isinstance(source["query_execution_id"], str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", source["query_execution_id"])
    ):
        raise ValueError("Invalid query identifier")
    if "data_scanned_bytes" in source and (
        type(source["data_scanned_bytes"]) is not int
        or not 0 <= source["data_scanned_bytes"] <= _MAX_COUNT
    ):
        raise ValueError("Invalid scanned bytes")
    if "retrieved_at" in source:
        _utc_timestamp(source["retrieved_at"], optional=False)
    return deepcopy(source)


def summarize_guardian_rows(
    rows: list[dict[str, str]],
    query: HistoryQuery,
    source: dict[str, Any],
    metric_profile: str = "authorizations",
) -> dict[str, Any]:
    """Reconcile every CDC row and compute a fixed acceptance profile in Python."""
    profile = metric_profile_definition(metric_profile)
    selected = ("sales", "preauthorizations") if metric_profile == "authorizations" else ("sales",)
    try:
        source = _validate_source(source)
        if not isinstance(rows, list) or len(rows) > 31:
            raise ValueError("Invalid diagnostic rows")
        diagnostics_daily = []
        eligible_rows = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != _ROW_FIELDS:
                raise ValueError("Unexpected diagnostic fields")
            day = date.fromisoformat(row["business_date"])
            if day.isoformat() != row["business_date"] or day in seen:
                raise ValueError("Duplicate or noncanonical day")
            if not query.date_from <= day <= query.date_to:
                raise ValueError("Out of range day")
            seen.add(day)
            counts = {key: _count(row[key]) for key in (*DIAGNOSTIC_COUNTERS, *OPERATION_COUNTERS)}
            if counts["source_cdc_rows"] == 0 or counts["source_cdc_rows"] != sum(
                counts[key] for key in DIAGNOSTIC_COUNTERS if key != "source_cdc_rows"
            ):
                raise ValueError("Unreconciled CDC rows")
            if counts["current_transactions"] != sum(counts[key] for key in OPERATION_COUNTERS):
                raise ValueError("Unreconciled operation counts")
            diagnostics_daily.append(
                {
                    "date": day.isoformat(),
                    **counts,
                    "last_ingested_at": _utc_timestamp(row["last_ingested_at"]),
                }
            )
            approved = sum(counts[f"{group}_approved"] for group in selected)
            declined = sum(counts[f"{group}_declined"] for group in selected)
            if approved + declined:
                eligible_rows.append(
                    {
                        "business_date": day.isoformat(),
                        "total_transactions": str(approved + declined),
                        "approved_transactions": str(approved),
                        "declined_transactions": str(declined),
                        "other_transactions": "0",
                    }
                )
        diagnostics_daily.sort(key=lambda row: row["date"])
        totals = {key: sum(item[key] for item in diagnostics_daily) for key in DIAGNOSTIC_COUNTERS}
        if any(value > _MAX_COUNT for value in totals.values()):
            raise ValueError("Aggregate count exceeds exact JSON integer range")
        operations = {}
        for group in OPERATION_GROUPS:
            values = {
                status: sum(item[f"{group}_{status}"] for item in diagnostics_daily)
                for status in ("approved", "declined", "other")
            }
            operations[group] = {**values, "total": sum(values.values())}
        report = summarize_rows(eligible_rows, query, source)
        selected_current = sum(operations[group]["total"] for group in selected)
        if report["metrics"]["total_transactions"]:
            status = "available"
        elif not totals["source_cdc_rows"]:
            status = "no_source_rows"
        elif totals["missing_transaction_code_rows"] == totals["source_cdc_rows"]:
            status = "invalid_keys"
        elif not totals["current_transactions"]:
            status = "no_current_records"
        elif not selected_current:
            status = "outside_scope"
        else:
            status = "no_final_results"
        ingested_values = [
            datetime.fromisoformat(row["last_ingested_at"])
            for row in diagnostics_daily
            if row["last_ingested_at"] is not None
        ]
        report.update(
            schema_version=GUARDIAN_HISTORY_SCHEMA_VERSION,
            metric_profile=profile,
            diagnostic_daily=diagnostics_daily,
            diagnostics={
                "status": status,
                "totals": totals,
                "excluded_type_transactions": totals["current_transactions"] - selected_current,
                "non_final_transactions": sum(operations[group]["other"] for group in selected),
                "operation_breakdown": operations,
            },
            freshness={
                "status": "UNKNOWN",
                "last_ingested_at": max(ingested_values).isoformat() if ingested_values else None,
                "scope": "merchant_creation_window",
            },
            completeness={"status": "UNVERIFIED"},
            limitations=[
                "Este histórico contiene transacciones; no reconstruye alertas Kipu no archivadas.",
                "Último estado CDC por MID y transaction_code dentro de la ventana de creación; "
                "no el estado conocido al dispararse una alerta.",
                "Los registros sin transaction_code y los borrados se diagnostican por separado; "
                "ticket_code no se requiere ni se usa para contar intentos.",
                f"Perfil fijo {profile['id']} v{profile['version']}: "
                f"{', '.join(profile['included_types'])}. Sólo APPROVED y DECLINED son elegibles.",
                "CAPTURE se muestra por separado y nunca integra el denominador de aceptación; "
                "se cuentan intentos, no órdenes únicas ni capturas de fondos.",
                "Timestamps sin zona interpretados como UTC; días America/Guayaquil (UTC-5). "
                "Fechas inclusivas convertidas a inicio UTC y fin UTC exclusivo del día posterior.",
                "Ausencia de filas no acredita cero actividad ni cobertura completa del Data Lake.",
                "last_ingested_at es el máximo ETL observado para este MID/rango; no certifica "
                "la frescura global ni que todos los eventos hayan llegado. "
                "Cobertura no verificada.",
                "Aceptación = aprobadas / (aprobadas + rechazadas). Tipos excluidos y estados "
                "no definitivos no forman parte del denominador; Kipu puede usar otro universo.",
            ],
        )
        return report
    except (ValueError, TypeError, KeyError, OverflowError):
        raise HistoryError(
            "INVALID_HISTORY", "El diagnóstico histórico contiene métricas inconsistentes."
        ) from None


def validate_guardian_history(
    report: Any, query: HistoryQuery, metric_profile: str | None = None
) -> dict[str, Any]:
    """Rebuild every derived field; never promote old or modified evidence to v3."""
    try:
        if (
            not isinstance(report, dict)
            or report.get("schema_version") != GUARDIAN_HISTORY_SCHEMA_VERSION
        ):
            raise ValueError("Wrong history version")
        profile = report["metric_profile"]
        if not isinstance(profile, dict) or profile != metric_profile_definition(profile.get("id")):
            raise ValueError("Invalid metric profile")
        if metric_profile is not None and profile["id"] != metric_profile:
            raise ValueError("Metric profile mismatch")
        rows = []
        daily = report["diagnostic_daily"]
        if not isinstance(daily, list) or len(daily) > 31:
            raise ValueError("Invalid diagnostic rows")
        for row in daily:
            if not isinstance(row, dict) or set(row) != (_ROW_FIELDS - {"business_date"}) | {
                "date"
            }:
                raise ValueError("Invalid diagnostic shape")
            counts = {}
            for key in (*DIAGNOSTIC_COUNTERS, *OPERATION_COUNTERS):
                if type(row[key]) is not int:
                    raise ValueError("Invalid stored count")
                counts[key] = str(row[key])
            rows.append(
                {
                    "business_date": row["date"],
                    **counts,
                    "last_ingested_at": row["last_ingested_at"],
                }
            )
        rebuilt = summarize_guardian_rows(rows, query, report["source"], profile["id"])
        # JSON's bool/int equality is too permissive. Canonical serialized data
        # also rejects NaN, infinities, extra fields, and changed numeric types.
        if json.dumps(report, sort_keys=True, allow_nan=False) != json.dumps(
            rebuilt, sort_keys=True, allow_nan=False
        ):
            raise ValueError("Stored history does not match its evidence")
        return deepcopy(rebuilt)
    except (ValueError, TypeError, KeyError, HistoryError, OverflowError):
        raise HistoryError(
            "INVALID_HISTORY_SOURCE",
            "El histórico no corresponde al perfil/MID/rango o tiene métricas inconsistentes. "
            "Conserva la evidencia anterior y genera una extracción nueva.",
        ) from None


def guardian_historical_evidence(report: dict[str, Any]) -> dict[str, Any]:
    """Return only the established anonymous daily/comparison/quality allowlist."""
    try:
        query = HistoryQuery(
            report["query"]["merchant_code"],
            date.fromisoformat(report["query"]["date_from"]),
            date.fromisoformat(report["query"]["date_to"]),
        )
        verified = validate_guardian_history(report, query)
    except (KeyError, TypeError, ValueError):
        raise HistoryError("INVALID_HISTORY_SOURCE", "El histórico Guardian es inválido.") from None
    # Only the already revalidated numerical v2-compatible fields cross this
    # adapter. This never relabels a stored v2 report or bypasses v3 validation.
    numerical = {key: verified[key] for key in ("query", "metrics", "daily", "comparison")}
    return historical_evidence({"schema_version": LEGACY_SCHEMA_VERSION, **numerical})


class GuardianHistoryReader(AthenaHistoryReader):
    """Use shared Athena pagination/timeouts, with the isolated v3 extraction contract."""

    def __init__(
        self,
        config: AthenaHistoryConfig,
        *,
        metric_profile: str = "authorizations",
        session: Any = None,
    ) -> None:
        metric_profile_definition(metric_profile)
        _canonical_config(config)
        super().__init__(config, session)
        self.metric_profile = metric_profile

    def _build_sql(self, query: HistoryQuery, columns: dict[str, str]) -> tuple[str, list[str]]:
        return build_guardian_history_sql(query, columns, self.config)

    def _summarize(
        self, rows: list[dict[str, str]], query: HistoryQuery, source: dict[str, Any]
    ) -> dict[str, Any]:
        return summarize_guardian_rows(rows, query, source, self.metric_profile)
