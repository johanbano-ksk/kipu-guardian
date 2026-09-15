import json
import sqlite3
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from alert_reviewer import datalake_history as history
from alert_reviewer.datalake_history import (
    HISTORY_SCHEMA_VERSION,
    AthenaHistoryConfig,
    AthenaHistoryReader,
    HistoryError,
    HistoryQuery,
    build_history_sql,
    historical_evidence,
    summarize_rows,
)

QUERY = HistoryQuery("synthetic_mid", date(2026, 1, 1), date(2026, 1, 4))
COLUMNS = {
    "merchant_code": "string",
    "ticket_code": "string",
    "transaction_code": "string",
    "event_timestamp": "timestamp",
    "update_timestamp": "timestamp",
    "etl_job_timestamp": "timestamp",
    "event_id": "string",
    "create_timestamp": "timestamp",
    "is_deleted": "boolean",
    "transaction_status_type": "string",
    "transaction_type": "string",
}
NAMES = [
    "business_date",
    "total_transactions",
    "approved_transactions",
    "declined_transactions",
    "other_transactions",
]


def row(day="2026-01-01", total=10, approved=9, declined=1, other=0):
    return dict(zip(NAMES, map(str, (day, total, approved, declined, other)), strict=True))


def page(rows=(), *, header=False, token=None):
    values = [NAMES] if header else []
    values += [list(item.values()) for item in rows]
    result = {
        "ResultSet": {
            "ResultSetMetadata": {"ColumnInfo": [{"Name": name} for name in NAMES]},
            "Rows": [{"Data": [{"VarCharValue": value} for value in item]} for item in values],
        },
    }
    if token:
        result["NextToken"] = token
    return result


def reader_and_client():
    glue = Mock()
    glue.get_table.return_value = {
        "Table": {
            "StorageDescriptor": {
                "Columns": [{"Name": name, "Type": value} for name, value in COLUMNS.items()],
            }
        },
    }
    athena = Mock()
    athena.start_query_execution.return_value = {"QueryExecutionId": "synthetic-query-id"}
    athena.get_query_execution.return_value = {
        "QueryExecution": {
            "Status": {"State": "SUCCEEDED"},
            "Statistics": {"DataScannedInBytes": 512},
        },
    }
    session = SimpleNamespace(client=lambda name: {"glue": glue, "athena": athena}[name])
    return AthenaHistoryReader(AthenaHistoryConfig(timeout_seconds=1), session), athena


def test_query_uses_ecuador_today_and_inclusive_31_day_limit(monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 7, 3, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(history, "datetime", FrozenDateTime)
    HistoryQuery("MID", date(2026, 8, 7), date(2026, 9, 6))
    with pytest.raises(ValueError, match="futuras"):
        HistoryQuery("MID", date(2026, 9, 7), date(2026, 9, 7))
    with pytest.raises(ValueError, match="31"):
        HistoryQuery("MID", date(2026, 8, 6), date(2026, 9, 6))
    with pytest.raises(ValueError, match="31"):
        HistoryQuery("MID", date(2026, 9, 6), date(2026, 8, 6))


@pytest.mark.parametrize("mid", ["", "  ", "mid' OR 1=1 --", "mid\n", "a" * 101])
def test_query_rejects_invalid_mid_before_sql(mid):
    with pytest.raises(ValueError):
        HistoryQuery(mid, QUERY.date_from, QUERY.date_to)


def test_sql_filters_dates_with_parameters_and_deduplicates_before_tombstones():
    sql, parameters = build_history_sql(QUERY, COLUMNS, AthenaHistoryConfig())
    versions, latest = sql.split("), latest AS (")
    assert "synthetic_mid" not in sql
    assert parameters == [
        "'synthetic_mid'",
        "TIMESTAMP '2026-01-01 05:00:00'",
        "TIMESTAMP '2026-01-05 05:00:00'",
    ]
    assert "merchant_code = ?" in versions
    assert "create_timestamp >= ? AND create_timestamp < ?" in versions
    assert "PARTITION BY merchant_code, transaction_code\n" in versions
    assert (
        "ORDER BY coalesce(update_timestamp, event_timestamp, etl_job_timestamp, "
        "create_timestamp) DESC" in sql
    )
    assert "event_id DESC NULLS LAST" in versions
    assert "transaction_code IS NOT NULL AND trim(transaction_code) <> ''" in versions
    assert "ticket_code" not in sql
    assert "coalesce(is_deleted" not in versions
    assert "transaction_status_type)) IN" not in versions
    assert "transaction_type)) IN" not in versions
    assert "WHERE rn = 1 AND coalesce(is_deleted, false) = false" in latest
    assert "('SALE', 'DEFERRED', 'DEFFERED')" in latest
    assert "transaction_status_type)) IN ('APPROVED','DECLINED')" in latest
    assert "CAPTURE" not in sql
    assert "CAST(0 AS bigint) AS other_transactions" in sql
    assert "with_timezone(create_timestamp, 'UTC')" in versions
    assert "America/Guayaquil" in versions
    assert "LIMIT 32" in sql


def test_zoned_creation_timestamp_uses_zoned_parameters_without_guessing_units():
    columns = {
        key: "timestamp with time zone" if value == "timestamp" else value
        for key, value in COLUMNS.items()
    }
    sql, parameters = build_history_sql(QUERY, columns, AthenaHistoryConfig())
    assert "with_timezone(create_timestamp" not in sql
    assert parameters[1] == "from_iso8601_timestamp('2026-01-01T05:00:00+00:00')"


@pytest.mark.parametrize(
    "missing",
    [
        "transaction_code",
        "is_deleted",
        "update_timestamp",
        "event_timestamp",
        "etl_job_timestamp",
        "create_timestamp",
        "event_id",
    ],
)
def test_sql_rejects_missing_dedup_metadata(missing):
    columns = {key: value for key, value in COLUMNS.items() if key != missing}
    with pytest.raises(HistoryError) as caught:
        build_history_sql(QUERY, columns, AthenaHistoryConfig())
    assert caught.value.code == "SCHEMA_MISMATCH"


def test_sql_does_not_require_ticket_code_or_use_creation_datetime_fallback():
    without_ticket = {key: value for key, value in COLUMNS.items() if key != "ticket_code"}
    sql, _ = build_history_sql(QUERY, without_ticket, AthenaHistoryConfig())
    assert "ticket_code" not in sql
    without_creation = {key: value for key, value in COLUMNS.items() if key != "create_timestamp"}
    with pytest.raises(HistoryError, match="esquema"):
        build_history_sql(
            QUERY, {**without_creation, "create_datetime": "timestamp"}, AthenaHistoryConfig()
        )


@pytest.mark.parametrize(
    "column", ["create_timestamp", "update_timestamp", "event_timestamp", "etl_job_timestamp"]
)
@pytest.mark.parametrize("invalid_type", ["bigint", "string", "timestamp_unverified"])
def test_sql_does_not_guess_numeric_or_string_timestamp_formats(column, invalid_type):
    with pytest.raises(HistoryError) as caught:
        build_history_sql(QUERY, {**COLUMNS, column: invalid_type}, AthenaHistoryConfig())
    assert caught.value.code == "SCHEMA_MISMATCH"


def test_sql_rejects_mixed_zoned_and_unzoned_cdc_timestamps():
    with pytest.raises(HistoryError) as caught:
        build_history_sql(
            QUERY, {**COLUMNS, "event_timestamp": "timestamp with time zone"}, AthenaHistoryConfig()
        )
    assert caught.value.code == "SCHEMA_MISMATCH"


@pytest.mark.parametrize(
    ("column", "invalid_type"),
    [
        ("merchant_code", "bigint"),
        ("transaction_code", "bigint"),
        ("event_id", "array<string>"),
        ("transaction_status_type", "bigint"),
        ("transaction_type", "bigint"),
        ("is_deleted", "string"),
    ],
)
def test_sql_verifies_string_keys_statuses_and_boolean_tombstones(column, invalid_type):
    with pytest.raises(HistoryError) as caught:
        build_history_sql(QUERY, {**COLUMNS, column: invalid_type}, AthenaHistoryConfig())
    assert caught.value.code == "SCHEMA_MISMATCH"


@pytest.mark.parametrize("field", ["approved_statuses", "declined_statuses"])
def test_status_configuration_requires_both_final_result_buckets(field):
    with pytest.raises(ValueError, match="status configuration"):
        AthenaHistoryConfig(**{field: ()})


def test_summary_weights_volume_and_preserves_missing_days_and_anonymous_day_indexes():
    report = summarize_rows(
        [
            row("2026-01-03", total=90, approved=9, declined=81),
            row(),
        ],
        QUERY,
        {"query_execution_id": "synthetic-query-id"},
    )
    assert report["schema_version"] == HISTORY_SCHEMA_VERSION == "2.0"
    assert report["metrics"]["total_transactions"] == 100
    assert report["metrics"]["approval_rate"] == pytest.approx(0.18)
    assert report["comparison"] == {
        "first_half_approval_rate": 0.9,
        "second_half_approval_rate": 0.1,
        "change_percentage_points": -80.0,
    }
    assert [item["date"] for item in report["daily"]] == ["2026-01-01", "2026-01-03"]
    evidence = historical_evidence(report)
    assert [item["evidence_id"] for item in evidence["daily"]] == ["day_0", "day_2"]
    assert evidence["data_quality"]["missing_days"] == 2
    assert evidence["data_quality"]["observed_days"] == 2
    serialized = json.dumps(evidence)
    for private in ["synthetic_mid", "2026-01", "synthetic-query-id", "merchant_code"]:
        assert private not in serialized


def test_empty_rows_are_unknown_rates_not_zero_or_synthetic_daily_rows():
    report = summarize_rows([], QUERY, {})
    assert report["metrics"]["total_transactions"] == 0
    assert report["metrics"]["approval_rate"] is None
    assert report["daily"] == []
    assert all(value is None for value in report["comparison"].values())
    assert historical_evidence(report)["data_quality"]["missing_days"] == 4


@pytest.mark.parametrize("schema_version", ["1.0", None, "3.0"])
def test_historical_evidence_rejects_legacy_or_unknown_methodology_even_with_final_counts(
    schema_version,
):
    report = summarize_rows([row()], QUERY, {})
    report["schema_version"] = schema_version
    with pytest.raises(HistoryError) as caught:
        historical_evidence(report)
    assert caught.value.code == "INVALID_HISTORY_SOURCE"


@pytest.mark.parametrize(
    "rows",
    [
        [row(), row()],
        [row("2026-01-05")],
        [row(total=11)],
        [row(approved=-1, declined=11)],
        [row(total=11, other=1)],
    ],
)
def test_summary_rejects_duplicate_out_of_range_or_inconsistent_rows(rows):
    with pytest.raises(HistoryError) as caught:
        summarize_rows(rows, QUERY, {})
    assert caught.value.code == "INVALID_HISTORY"


def test_reader_paginates_without_dropping_first_data_row_of_second_page():
    reader, athena = reader_and_client()
    athena.get_query_results.side_effect = [
        page([row()], header=True, token="next"),
        page([row("2026-01-03")]),
    ]
    report = reader.read(QUERY)
    assert report["metrics"]["total_transactions"] == 20
    assert report["source"]["query_execution_id"] == "synthetic-query-id"
    assert report["source"]["data_scanned_bytes"] == 512
    assert athena.get_query_results.call_args_list[1].kwargs["NextToken"] == "next"
    options = athena.start_query_execution.call_args.kwargs
    assert options["QueryExecutionContext"] == {
        "Catalog": "s3tablescatalog/datalake-prod",
        "Database": "odl",
    }
    assert options["ResultReuseConfiguration"]["ResultReuseByAgeConfiguration"] == {
        "Enabled": False,
    }


def test_reader_cancels_running_query_after_deadline(monkeypatch):
    reader, athena = reader_and_client()
    athena.get_query_execution.return_value = {
        "QueryExecution": {"Status": {"State": "RUNNING"}},
    }
    monkeypatch.setattr(history.time, "monotonic", Mock(side_effect=[0, 2]))
    monkeypatch.setattr(history.time, "sleep", lambda _seconds: pytest.fail("unexpected wait"))
    with pytest.raises(HistoryError) as caught:
        reader.read(QUERY)
    assert caught.value.code == "QUERY_TIMEOUT"
    athena.stop_query_execution.assert_called_once_with(QueryExecutionId="synthetic-query-id")
    athena.get_query_results.assert_not_called()


@pytest.mark.parametrize("tokens", [("A", "A"), ("A", "B", "A")])
def test_reader_rejects_pagination_token_cycles_even_when_pages_are_empty(tokens):
    reader, athena = reader_and_client()
    athena.get_query_results.side_effect = [
        *(page(header=index == 0, token=token) for index, token in enumerate(tokens)),
        AssertionError("Pagination must stop before revisiting a token"),
    ]
    with pytest.raises(HistoryError) as caught:
        reader.read(QUERY)
    assert caught.value.code == "INVALID_HISTORY"


def transaction(code, *, status="DECLINED", kind="SALE", **changes):
    return {
        "merchant_code": QUERY.merchant_code,
        "transaction_code": code,
        "ticket_code": None,
        "transaction_status_type": status,
        "transaction_type": kind,
        "create_timestamp": "2026-01-02 10:00:00",
        "update_timestamp": None,
        "event_timestamp": "2026-01-02 12:00:00",
        "etl_job_timestamp": None,
        "event_id": "event-a",
        "is_deleted": None,
        **changes,
    }


def execute_generated_sql(records, *, query=QUERY, config=None):
    """Execute production filtering/ranking SQL, adapting only Athena's date/count syntax.

    SQLite's CAST(... AS date) does not extract a calendar date. Replace that single
    timezone expression by its equivalent for Ecuador (UTC-05), and register the
    Athena count_if aggregate. Window functions, CTEs, filters and bounds stay intact.
    """
    sql, parameters = build_history_sql(query, COLUMNS, config or AthenaHistoryConfig())
    sql = sql.replace(
        "CAST(at_timezone(with_timezone(create_timestamp, 'UTC'), 'America/Guayaquil') AS date)",
        "date(create_timestamp, '-5 hours')",
    )

    class CountIf:
        def __init__(self):
            self.count = 0

        def step(self, condition):
            self.count += int(bool(condition))

        def finalize(self):
            return self.count

    with sqlite3.connect(":memory:") as database:
        database.create_aggregate("count_if", 1, CountIf)
        table_columns = ", ".join(
            f"{name} {'INTEGER' if kind == 'boolean' else 'TEXT'}" for name, kind in COLUMNS.items()
        )
        database.execute(f"CREATE TABLE card_transaction ({table_columns})")
        insert = (
            f"INSERT INTO card_transaction ({', '.join(COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in COLUMNS)})"
        )
        database.executemany(insert, [tuple(item[name] for name in COLUMNS) for item in records])
        decoded_parameters = [parameters[0][1:-1]] + [
            value.removeprefix("TIMESTAMP '").removesuffix("'") for value in parameters[1:]
        ]
        cursor = database.execute(sql, decoded_parameters)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, map(str, item), strict=True)) for item in cursor.fetchall()]


def test_generated_sql_counts_declines_without_tickets_and_distinct_attempts_per_ticket():
    rows = execute_generated_sql(
        [
            transaction("001", status="APPROVED", ticket_code="shared-ticket"),
            transaction("1", ticket_code="shared-ticket"),
            transaction("declined-null-ticket"),
            transaction("declined-empty-ticket", ticket_code=""),
            transaction(None, status="APPROVED"),
            transaction("", status="APPROVED"),
            transaction("   ", status="APPROVED"),
            transaction("foreign-merchant", merchant_code="another-merchant", status="APPROVED"),
        ]
    )
    assert rows == [row("2026-01-02", total=4, approved=1, declined=3)]
    report = summarize_rows(rows, QUERY, {})
    assert report["metrics"]["approval_rate"] == 0.25
    assert report["metrics"]["other_transactions"] == 0


def test_generated_sql_filters_latest_type_status_and_tombstone_after_deduplication():
    records = [
        transaction("repeat", status="APPROVED", event_id="old"),
        transaction("repeat", event_timestamp="2026-01-02 13:00:00", event_id="new"),
    ]
    for code, changed in [
        ("changed-to-capture", {"transaction_type": "CAPTURE"}),
        ("changed-to-pending", {"transaction_status_type": "PENDING"}),
        ("changed-to-deleted", {"is_deleted": True}),
    ]:
        records.extend(
            [
                transaction(code, status="APPROVED"),
                transaction(
                    code, status="APPROVED", event_timestamp="2026-01-02 13:00:00", **changed
                ),
            ]
        )
    assert execute_generated_sql(records) == [row("2026-01-02", total=1, approved=0, declined=1)]


@pytest.mark.parametrize(
    "kind", ["CAPTURE", "REFUND", "VOID", "REVERSE", "CHARGEBACK", "PREAUTHORIZATION", None]
)
def test_generated_sql_excludes_non_sales_even_if_approved(kind):
    assert execute_generated_sql([transaction("excluded", kind=kind, status="APPROVED")]) == []


@pytest.mark.parametrize("status", ["PENDING", "INITIALIZED", "ERROR", None, ""])
def test_generated_sql_excludes_non_final_states_from_denominator(status):
    assert execute_generated_sql([transaction("excluded", status=status)]) == []


@pytest.mark.parametrize("kind", ["SALE", "DEFERRED", "DEFFERED"])
def test_generated_sql_includes_sales_and_legacy_deffered_spelling(kind):
    assert execute_generated_sql([transaction("eligible", kind=kind, status="APPROVED")]) == [
        row("2026-01-02", total=1, approved=1, declined=0)
    ]


def test_generated_sql_uses_first_non_null_timestamp_not_lexicographic_columns_or_greatest():
    assert execute_generated_sql(
        [
            transaction(
                "fallback",
                status="APPROVED",
                update_timestamp="2026-01-02 10:00:00",
                event_timestamp="2026-01-02 20:00:00",
            ),
            transaction("fallback", event_timestamp="2026-01-02 11:00:00"),
        ]
    ) == [row("2026-01-02", total=1, approved=0, declined=1)]


@pytest.mark.parametrize(
    "newer",
    [
        {"event_timestamp": None, "etl_job_timestamp": "2026-01-02 13:00:00"},
        {
            "event_timestamp": None,
            "etl_job_timestamp": None,
            "create_timestamp": "2026-01-02 13:00:00",
        },
    ],
)
def test_generated_sql_falls_back_to_etl_then_creation_when_prior_timestamps_are_null(newer):
    assert execute_generated_sql(
        [transaction("fallback", status="APPROVED"), transaction("fallback", **newer)]
    ) == [row("2026-01-02", total=1, approved=0, declined=1)]


def test_generated_sql_breaks_equal_cdc_timestamp_ties_with_event_id():
    assert execute_generated_sql(
        [
            transaction("tie", event_id="event-b"),
            transaction("tie", status="APPROVED", event_id="event-a"),
        ]
    ) == [row("2026-01-02", total=1, approved=0, declined=1)]


def test_generated_sql_uses_inclusive_ecuador_dates_and_exclusive_utc_end_boundary():
    rows = execute_generated_sql(
        [
            transaction("before-start", create_timestamp="2026-01-01 04:59:59"),
            transaction("at-start", create_timestamp="2026-01-01 05:00:00", status="APPROVED"),
            transaction("before-end", create_timestamp="2026-01-05 04:59:59"),
            transaction("at-end", create_timestamp="2026-01-05 05:00:00"),
        ]
    )
    assert rows == [
        row("2026-01-01", total=1, approved=1, declined=0),
        row("2026-01-04", total=1, approved=0, declined=1),
    ]


def test_generated_sql_and_summary_preserve_unknown_rate_for_empty_eligible_universe():
    rows = execute_generated_sql([transaction("pending", status="PENDING")])
    assert rows == []
    report = summarize_rows(rows, QUERY, {})
    assert report["metrics"]["total_transactions"] == 0
    assert report["metrics"]["approval_rate"] is None


def test_generated_sql_retains_explicit_verified_status_taxonomy_for_standalone_reader():
    rows = execute_generated_sql(
        [
            transaction("success", status="SUCCESS"),
            transaction("failed", status="FAILED"),
            transaction("not-selected", status="APPROVED"),
        ],
        config=AthenaHistoryConfig(approved_statuses=("SUCCESS",), declined_statuses=("FAILED",)),
    )
    assert rows == [row("2026-01-02", total=2, approved=1, declined=1)]
