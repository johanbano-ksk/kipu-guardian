"""Bounded, parameterized transaction history from Kushki's curated CDC tables."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import BotoCoreError, ClientError

TIMEZONE = "America/Guayaquil"
CATALOG = "s3tablescatalog/datalake-prod"
GLUE_CATALOG = f"956401377419:{CATALOG}"
DATABASE = "odl"
TABLE = "card_transaction"
HISTORY_SCHEMA_VERSION = "2.0"
OUTPUT = (
    "s3://datalake-prod-athena-query-result-956401377419-us-east-1-an/team-queries/kipu-guardian/"
)


class HistoryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class HistoryQuery:
    merchant_code: str
    date_from: date
    date_to: date

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", self.merchant_code):
            raise ValueError("El MID sólo admite letras, números, guion y guion bajo.")
        if type(self.date_from) is not date or type(self.date_to) is not date:
            raise ValueError("Las fechas deben ser fechas completas.")
        if not 0 <= (self.date_to - self.date_from).days < 31:
            raise ValueError("Selecciona un rango de 1 a 31 días.")
        if self.date_to > datetime.now(ZoneInfo(TIMEZONE)).date():
            raise ValueError("No se pueden consultar fechas futuras.")


@dataclass(frozen=True)
class AthenaHistoryConfig:
    profile: str = "data-core"
    region: str = "us-east-1"
    workgroup: str = "primary"
    output_location: str = OUTPUT
    timeout_seconds: float = 90
    # Explicit result semantics, configurable when the source taxonomy is verified.
    approved_statuses: tuple[str, ...] = ("APPROVED",)
    declined_statuses: tuple[str, ...] = ("DECLINED",)

    def __post_init__(self) -> None:
        if not 1 <= self.timeout_seconds <= 180:
            raise ValueError("Athena timeout must be between 1 and 180 seconds")
        statuses = (*self.approved_statuses, *self.declined_statuses)
        if (
            not self.approved_statuses
            or not self.declined_statuses
            or any(not re.fullmatch(r"[A-Z_ ]{1,50}", s) for s in statuses)
        ):
            raise ValueError("Invalid transaction status configuration")
        if set(self.approved_statuses) & set(self.declined_statuses):
            raise ValueError("Approved and declined statuses must be disjoint")


def _aws_error(error: BotoCoreError | ClientError) -> HistoryError:
    name = type(error).__name__
    code = error.response.get("Error", {}).get("Code", "") if isinstance(error, ClientError) else ""
    if "SSO" in name or "Token" in name or code in {"ExpiredToken", "ExpiredTokenException"}:
        return HistoryError(
            "SSO_EXPIRED", "La sesión AWS venció. Ejecuta aws sso login --profile data-core."
        )
    if "AccessDenied" in code or "Unauthorized" in code:
        return HistoryError(
            "ACCESS_DENIED", "Solicita a IT el permiso data-core-payments-intelligence."
        )
    return HistoryError("DATALAKE_UNAVAILABLE", "No fue posible consultar el Data Lake de Kushki.")


def build_history_sql(
    query: HistoryQuery,
    columns: dict[str, str],
    config: AthenaHistoryConfig,
) -> tuple[str, list[str]]:
    """Only catalog-verified identifiers enter SQL; all caller filters are parameters."""
    required = {
        "merchant_code",
        "transaction_code",
        "update_timestamp",
        "event_timestamp",
        "etl_job_timestamp",
        "create_timestamp",
        "event_id",
        "is_deleted",
        "transaction_status_type",
        "transaction_type",
    }
    if not required.issubset(columns):
        raise HistoryError(
            "SCHEMA_MISMATCH", "El esquema de card_transaction no permite deduplicar con seguridad."
        )

    # The creation filter and complete CDC fallback order are part of this contract.
    # Do not silently substitute a different date field or omit a CDC timestamp.
    timestamp_columns = (
        "update_timestamp",
        "event_timestamp",
        "etl_job_timestamp",
        "create_timestamp",
    )
    timestamp_types = [columns[name].strip().lower() for name in timestamp_columns]
    if any(
        not re.fullmatch(r"timestamp(?:\(\d+\))?(?: with time zone)?", value)
        for value in timestamp_types
    ):
        raise HistoryError(
            "SCHEMA_MISMATCH", "Es necesario verificar los timestamps de creación y CDC."
        )
    zoned_types = {"with time zone" in value for value in timestamp_types}
    if len(zoned_types) != 1:
        raise HistoryError(
            "SCHEMA_MISMATCH",
            "Los timestamps de creación y CDC deben tener una zona horaria consistente.",
        )
    text_columns = (
        "merchant_code",
        "transaction_code",
        "event_id",
        "transaction_status_type",
        "transaction_type",
    )
    if columns["is_deleted"].strip().lower() != "boolean" or any(
        not re.fullmatch(r"(?:string|varchar(?:\(\d+\))?|char\(\d+\))", columns[name].lower())
        for name in text_columns
    ):
        raise HistoryError(
            "SCHEMA_MISMATCH", "Es necesario verificar las llaves y los tipos del esquema CDC."
        )

    time_column = "create_timestamp"
    zoned = "with time zone" in columns[time_column].lower()
    # Plain Iceberg timestamps are treated as UTC, explicitly documented in the report.
    utc_timestamp = time_column if zoned else f"with_timezone({time_column}, 'UTC')"
    local_day = f"CAST(at_timezone({utc_timestamp}, '{TIMEZONE}') AS date)"
    order = (
        "coalesce(update_timestamp, event_timestamp, etl_job_timestamp, create_timestamp) "
        "DESC, event_id DESC NULLS LAST"
    )

    bounds = [
        datetime.combine(d, datetime.min.time(), ZoneInfo(TIMEZONE)).astimezone(UTC)
        for d in (query.date_from, query.date_to + timedelta(days=1))
    ]

    def bound(value: datetime) -> str:
        if zoned:
            return f"from_iso8601_timestamp('{value.isoformat()}')"
        return f"TIMESTAMP '{value.strftime('%Y-%m-%d %H:%M:%S')}'"

    approved = ",".join(f"'{s}'" for s in config.approved_statuses)
    declined = ",".join(f"'{s}'" for s in config.declined_statuses)
    sql = f"""WITH versions AS (
    SELECT merchant_code, transaction_code, transaction_status_type, transaction_type,
           is_deleted, {local_day} AS business_date,
           row_number() OVER (
               PARTITION BY merchant_code, transaction_code
               ORDER BY {order}
           ) AS rn
    FROM {TABLE}
    WHERE merchant_code = ?
      AND {time_column} >= ? AND {time_column} < ?
      AND transaction_code IS NOT NULL AND trim(transaction_code) <> ''
), latest AS (
    SELECT business_date, upper(trim(transaction_status_type)) AS status
    FROM versions
    WHERE rn = 1 AND coalesce(is_deleted, false) = false
      AND upper(trim(transaction_type)) IN ('SALE', 'DEFERRED', 'DEFFERED')
      AND upper(trim(transaction_status_type)) IN ({approved},{declined})
)
SELECT CAST(business_date AS varchar) AS business_date,
       count(*) AS total_transactions,
       count_if(status IN ({approved})) AS approved_transactions,
       count_if(status IN ({declined})) AS declined_transactions,
       CAST(0 AS bigint) AS other_transactions
FROM latest
GROUP BY business_date ORDER BY business_date
LIMIT 32"""
    return sql, [f"'{query.merchant_code}'", bound(bounds[0]), bound(bounds[1])]


def summarize_rows(
    rows: list[dict[str, str]],
    query: HistoryQuery,
    source: dict[str, Any],
) -> dict[str, Any]:
    daily: list[dict[str, Any]] = []
    seen: set[date] = set()
    for row in rows:
        try:
            day = date.fromisoformat(row["business_date"])
            counts = {
                k: int(row[k])
                for k in (
                    "total_transactions",
                    "approved_transactions",
                    "declined_transactions",
                    "other_transactions",
                )
            }
        except (KeyError, TypeError, ValueError):
            raise HistoryError("INVALID_HISTORY", "Athena devolvió métricas incompletas.") from None
        if (
            day in seen
            or not query.date_from <= day <= query.date_to
            or any(v < 0 for v in counts.values())
            or counts["other_transactions"] != 0
            or counts["total_transactions"]
            != counts["approved_transactions"] + counts["declined_transactions"]
        ):
            raise HistoryError("INVALID_HISTORY", "Athena devolvió métricas inconsistentes.")
        seen.add(day)
        daily.append(
            {
                "date": day.isoformat(),
                **counts,
                "approval_rate": (
                    counts["approved_transactions"] / counts["total_transactions"]
                    if counts["total_transactions"]
                    else None
                ),
            }
        )
    daily.sort(key=lambda item: item["date"])
    counts = {
        k: sum(row[k] for row in daily)
        for k in (
            "total_transactions",
            "approved_transactions",
            "declined_transactions",
            "other_transactions",
        )
    }
    rate = (
        counts["approved_transactions"] / counts["total_transactions"]
        if counts["total_transactions"]
        else None
    )
    midpoint = query.date_from + timedelta(days=((query.date_to - query.date_from).days + 1) // 2)

    def weighted(part: list[dict[str, Any]]) -> float | None:
        total = sum(row["total_transactions"] for row in part)
        return sum(row["approved_transactions"] for row in part) / total if total else None

    first = weighted([r for r in daily if r["date"] < midpoint.isoformat()])
    second = weighted([r for r in daily if r["date"] >= midpoint.isoformat()])
    return {
        "schema_version": HISTORY_SCHEMA_VERSION,
        "query": {
            "merchant_code": query.merchant_code,
            "date_from": query.date_from.isoformat(),
            "date_to": query.date_to.isoformat(),
            "timezone": TIMEZONE,
        },
        "source": source,
        "metrics": {**counts, "approval_rate": rate, "observed_days": len(daily)},
        "daily": daily,
        "comparison": {
            "first_half_approval_rate": first,
            "second_half_approval_rate": second,
            "change_percentage_points": (second - first) * 100
            if first is not None and second is not None
            else None,
        },
        "limitations": [
            "Este histórico contiene transacciones; no reconstruye alertas Kipu no archivadas.",
            "Último estado CDC por MID y transaction_code; "
            "no el estado conocido al dispararse una alerta.",
            "Se excluyen transaction_code nulos o vacíos y registros borrados; "
            "ticket_code no se requiere ni se usa para contar intentos.",
            "Sólo ventas SALE, DEFERRED y DEFFERED con resultado aprobado o rechazado; "
            "se excluyen CAPTURE, PREAUTHORIZATION y otros tipos o estados no definitivos.",
            "Los timestamps sin zona se interpretan como UTC y se agrupan por America/Guayaquil.",
            "Fechas de entrada inclusivas; create_timestamp se filtra desde medianoche local "
            "hasta la medianoche posterior a la fecha final, exclusiva, convertidas a UTC.",
            "Días sin filas no acreditan cero actividad ni cobertura completa del Data Lake.",
            "Aceptación = aprobadas / (aprobadas + rechazadas); "
            "otros estados no forman parte del denominador. El resultado "
            "puede diferir del universo y ventana de Kipu.",
        ],
    }


class AthenaHistoryReader:
    def __init__(self, config: AthenaHistoryConfig, session: Any = None) -> None:
        self.config = config
        self.session = session

    def _build_sql(
        self, query: HistoryQuery, columns: dict[str, str]
    ) -> tuple[str, list[str]]:
        return build_history_sql(query, columns, self.config)

    def _summarize(
        self, rows: list[dict[str, str]], query: HistoryQuery, source: dict[str, Any]
    ) -> dict[str, Any]:
        report = summarize_rows(rows, query, source)
        report["limitations"].append(
            f"Aprobadas: {', '.join(self.config.approved_statuses)}; rechazadas: "
            f"{', '.join(self.config.declined_statuses)}. "
            "Sólo estos estados definitivos integran el total; otros se excluyen."
        )
        return report

    def read(self, query: HistoryQuery) -> dict[str, Any]:
        try:
            session = self.session or boto3.Session(
                profile_name=self.config.profile, region_name=self.config.region
            )
            glue = session.client("glue")
            metadata = glue.get_table(CatalogId=GLUE_CATALOG, DatabaseName=DATABASE, Name=TABLE)
            columns = {
                c["Name"]: c["Type"] for c in metadata["Table"]["StorageDescriptor"]["Columns"]
            }
            sql, parameters = self._build_sql(query, columns)
            athena = session.client("athena")
            result = athena.start_query_execution(
                QueryString=sql,
                ExecutionParameters=parameters,
                QueryExecutionContext={"Catalog": CATALOG, "Database": DATABASE},
                ResultConfiguration={"OutputLocation": self.config.output_location},
                WorkGroup=self.config.workgroup,
                ResultReuseConfiguration={"ResultReuseByAgeConfiguration": {"Enabled": False}},
            )
            query_id = result["QueryExecutionId"]
            deadline = time.monotonic() + self.config.timeout_seconds
            while True:
                execution = athena.get_query_execution(QueryExecutionId=query_id)["QueryExecution"]
                state = execution["Status"]["State"]
                if state == "SUCCEEDED":
                    break
                if state in {"FAILED", "CANCELLED"}:
                    raise HistoryError(
                        "QUERY_FAILED", "Athena no pudo completar la consulta histórica."
                    )
                if time.monotonic() >= deadline:
                    athena.stop_query_execution(QueryExecutionId=query_id)
                    raise HistoryError(
                        "QUERY_TIMEOUT", "La consulta tardó demasiado y se solicitó cancelarla."
                    )
                time.sleep(1)
            rows: list[dict[str, str]] = []
            names: list[str] = []
            request: dict[str, Any] = {"QueryExecutionId": query_id, "MaxResults": 100}
            seen_tokens: set[str] = set()
            while True:
                if time.monotonic() >= deadline:
                    raise HistoryError(
                        "QUERY_TIMEOUT", "Se agotó el tiempo de lectura de resultados."
                    )
                response = athena.get_query_results(**request)
                page = response["ResultSet"]
                current = [c["Name"] for c in page["ResultSetMetadata"]["ColumnInfo"]]
                if names and names != current:
                    raise HistoryError(
                        "INVALID_HISTORY", "El esquema de resultados cambió durante la consulta."
                    )
                data = page.get("Rows", [])
                # SELECT results have a single header, on the first page only.
                if not names:
                    names = current
                    if data and [d.get("VarCharValue") for d in data[0]["Data"]] == names:
                        data = data[1:]
                for row in data:
                    rows.append(
                        dict(
                            zip(
                                names, [v.get("VarCharValue", "") for v in row["Data"]], strict=True
                            )
                        )
                    )
                if len(rows) > 31:
                    raise HistoryError(
                        "INVALID_HISTORY", "La consulta excedió el límite de resultados."
                    )
                token = response.get("NextToken")
                if not token:
                    break
                if token in seen_tokens:
                    raise HistoryError(
                        "INVALID_HISTORY", "Athena repitió una página de resultados."
                    )
                seen_tokens.add(token)
                request["NextToken"] = token
            return self._summarize(
                rows,
                query,
                {
                    "catalog": CATALOG,
                    "database": DATABASE,
                    "table": TABLE,
                    "query_execution_id": query_id,
                    "data_scanned_bytes": execution.get("Statistics", {}).get(
                        "DataScannedInBytes", 0
                    ),
                    "retrieved_at": datetime.now(UTC).isoformat(),
                },
            )
        except (BotoCoreError, ClientError) as exc:
            raise _aws_error(exc) from None


def historical_evidence(report: dict[str, Any]) -> dict[str, Any]:
    """Minimal numeric evidence for Gemini, without dates or merchant identifiers."""
    if report.get("schema_version") != HISTORY_SCHEMA_VERSION:
        raise HistoryError(
            "INVALID_HISTORY_SOURCE",
            "El histórico usa una metodología anterior. Extrae nuevamente por transaction_code.",
        )
    query = report["query"]
    start = date.fromisoformat(query["date_from"])
    requested_days = (date.fromisoformat(query["date_to"]) - start).days + 1
    daily = []
    for item in report["daily"]:
        index = (date.fromisoformat(item["date"]) - start).days
        daily.append(
            {
                "evidence_id": f"day_{index}",
                "day_index": index,
                "total_transactions": item["total_transactions"],
                "approved_count": item["approved_transactions"],
                "declined_count": item["declined_transactions"],
                "approval_rate_pct": item["approval_rate"] * 100
                if item["approval_rate"] is not None
                else None,
            }
        )
    comparison = report["comparison"]
    return {
        "daily": daily,
        "comparison": {
            "evidence_id": "comparison_0",
            "first_half_approval_rate_pct": comparison["first_half_approval_rate"] * 100
            if comparison["first_half_approval_rate"] is not None
            else None,
            "second_half_approval_rate_pct": comparison["second_half_approval_rate"] * 100
            if comparison["second_half_approval_rate"] is not None
            else None,
            "change_percentage_points": comparison["change_percentage_points"],
        },
        "data_quality": {
            "evidence_id": "quality_0",
            "requested_days": requested_days,
            "observed_days": report["metrics"]["observed_days"],
            "missing_days": requested_days - report["metrics"]["observed_days"],
            "total_transactions": report["metrics"]["total_transactions"],
            "other_transactions": report["metrics"]["other_transactions"],
        },
    }
