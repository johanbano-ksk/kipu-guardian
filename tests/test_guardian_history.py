"""Executable SQL/CDC regression tests for the fixed-profile Guardian history."""

import json
import sqlite3
from copy import deepcopy
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from alert_reviewer.datalake_history import AthenaHistoryConfig, HistoryError, HistoryQuery
from alert_reviewer.gemini_review import _history_evidence
from alert_reviewer.guardian_history import (
    DIAGNOSTIC_COUNTERS,
    OPERATION_COUNTERS,
    GuardianHistoryReader,
    build_guardian_history_sql,
    guardian_historical_evidence,
    metric_profile_definition,
    summarize_guardian_rows,
    validate_guardian_history,
)

QUERY = HistoryQuery("20000000104250188000", date(2026, 1, 1), date(2026, 1, 4))
COLUMNS = {
    "merchant_code": "string",
    "transaction_code": "string",
    "ticket_code": "string",
    "transaction_status_type": "string",
    "transaction_type": "string",
    "create_timestamp": "timestamp",
    "event_timestamp": "timestamp",
    "update_timestamp": "timestamp",
    "etl_job_timestamp": "timestamp",
    "event_id": "string",
    "is_deleted": "boolean",
}
NAMES = ["business_date", *DIAGNOSTIC_COUNTERS, *OPERATION_COUNTERS, "last_ingested_at"]


def row(day="2026-01-02", **changes):
    return {
        "business_date": day,
        **dict.fromkeys((*DIAGNOSTIC_COUNTERS, *OPERATION_COUNTERS), "0"),
        "last_ingested_at": "",
        **{key: str(value) if value is not None else "" for key, value in changes.items()},
    }


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
        "etl_job_timestamp": "2026-01-02 12:01:00",
        "event_id": "event-a",
        "is_deleted": None,
        **changes,
    }


def execute_generated_sql(records, *, query=QUERY):
    """Keep production ranking/classification SQL; adapt only Athena date functions."""
    sql, parameters = build_guardian_history_sql(query, COLUMNS, AthenaHistoryConfig())
    sql = sql.replace(
        "CAST(at_timezone(with_timezone(create_timestamp, 'UTC'), 'America/Guayaquil') AS date)",
        "date(create_timestamp, '-5 hours')",
    )

    class CountIf:
        def __init__(self):
            self.count = 0

        def step(self, value):
            self.count += int(bool(value))

        def finalize(self):
            return self.count

    def iso_utc(value):
        return datetime.fromisoformat(value).replace(tzinfo=UTC).isoformat() if value else None

    with sqlite3.connect(":memory:") as database:
        database.create_aggregate("count_if", 1, CountIf)
        database.create_function("with_timezone", 2, lambda value, _zone: value)
        database.create_function("at_timezone", 2, lambda value, _zone: value)
        database.create_function("to_iso8601", 1, iso_utc)
        table_columns = ", ".join(
            f"{name} {'INTEGER' if kind == 'boolean' else 'TEXT'}" for name, kind in COLUMNS.items()
        )
        database.execute(f"CREATE TABLE card_transaction ({table_columns})")
        database.executemany(
            f"INSERT INTO card_transaction ({', '.join(COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in COLUMNS)})",
            [tuple(item[name] for name in COLUMNS) for item in records],
        )
        decoded = [parameters[0][1:-1]] + [
            value.removeprefix("TIMESTAMP '").removesuffix("'") for value in parameters[1:]
        ]
        cursor = database.execute(sql, decoded)
        names = [item[0] for item in cursor.description]
        assert names == NAMES
        return [
            dict(zip(names, (str(v) if v is not None else "" for v in item), strict=True))
            for item in cursor.fetchall()
        ]


def report_for(records, *, metric_profile="authorizations"):
    return summarize_guardian_rows(execute_generated_sql(records), QUERY, {}, metric_profile)


def test_sql_is_single_parameterized_bounded_scan_not_ticket_dependent():
    sql, parameters = build_guardian_history_sql(QUERY, COLUMNS, AthenaHistoryConfig())
    assert QUERY.merchant_code not in sql
    assert parameters == [
        f"'{QUERY.merchant_code}'",
        "TIMESTAMP '2026-01-01 05:00:00'",
        "TIMESTAMP '2026-01-05 05:00:00'",
    ]
    assert sql.count("FROM card_transaction") == 1
    assert "create_timestamp >= ? AND create_timestamp < ?" in sql
    assert "PARTITION BY merchant_code, transaction_code\n" in sql
    assert "ticket_code" not in sql
    assert "LIMIT 32" in sql
    assert "max(ingested_at)" in sql
    assert "WHERE rn" not in sql  # Excluded rows are retained for the diagnostics.


def test_profiles_share_exact_same_extraction_and_never_include_captures():
    records = [
        transaction("sale", status="APPROVED"),
        transaction("preauth-ok", kind="PREAUTHORIZATION", status="APPROVED"),
        transaction("preauth-no", kind="PREAUTHORIZATION"),
        transaction("capture-ok", kind="CAPTURE", status="APPROVED"),
        transaction("capture-no", kind="CAPTURE"),
    ]
    rows = execute_generated_sql(records)
    sales = summarize_guardian_rows(rows, QUERY, {}, "sales")
    auth = summarize_guardian_rows(rows, QUERY, {}, "authorizations")
    assert sales["metrics"]["total_transactions"] == 1
    assert sales["metrics"]["approval_rate"] == 1.0
    assert auth["metrics"]["total_transactions"] == 3
    assert auth["metrics"]["approval_rate"] == pytest.approx(2 / 3)
    assert auth["diagnostic_daily"] == sales["diagnostic_daily"]
    assert auth["diagnostics"]["operation_breakdown"]["captures"] == {
        "approved": 1,
        "declined": 1,
        "other": 0,
        "total": 2,
    }
    assert auth["diagnostics"]["excluded_type_transactions"] == 2
    assert sales["diagnostics"]["excluded_type_transactions"] == 4
    assert auth["metric_profile"] == metric_profile_definition("authorizations")
    assert "CAPTURE" not in auth["metric_profile"]["included_types"]
    assert auth["metrics"]["other_transactions"] == 0


def test_preauthorization_only_merchant_explains_sales_zero_without_false_no_data():
    rows = execute_generated_sql(
        [
            transaction("preauth-ok", kind="PREAUTHORIZATION", status="APPROVED"),
            transaction("preauth-no", kind="PREAUTHORIZATION"),
            transaction("capture", kind="CAPTURE", status="APPROVED"),
        ]
    )
    sales = summarize_guardian_rows(rows, QUERY, {}, "sales")
    assert sales["diagnostics"]["status"] == "outside_scope"
    assert sales["metrics"]["total_transactions"] == 0
    assert sales["metrics"]["approval_rate"] is None
    assert sales["daily"] == []
    auth = summarize_guardian_rows(rows, QUERY, {}, "authorizations")
    assert auth["diagnostics"]["status"] == "available"
    assert auth["metrics"]["approval_rate"] == 0.5


def test_actual_sql_reconciles_every_cdc_row_and_filters_latest_state_only():
    records = [
        transaction("changed-status", status="APPROVED"),
        transaction("changed-status", event_timestamp="2026-01-02 13:00:00"),
        transaction("changed-kind", status="APPROVED"),
        transaction("changed-kind", kind="CAPTURE", event_timestamp="2026-01-02 13:00:00"),
        transaction("changed-pending", status="APPROVED"),
        transaction("changed-pending", status="PENDING", event_timestamp="2026-01-02 13:00:00"),
        transaction("deleted", status="APPROVED"),
        transaction("deleted", is_deleted=True, event_timestamp="2026-01-02 13:00:00"),
        transaction(None),
        transaction(None, status="APPROVED"),
        transaction(""),
        transaction("   "),
        transaction("refund", kind="REFUND"),
        transaction("approved-null-ticket", status="APPROVED"),
        transaction("not-this-merchant", merchant_code="another-mid"),
    ]
    report = report_for(records)
    assert report["diagnostics"]["totals"] == {
        "source_cdc_rows": 14,
        "missing_transaction_code_rows": 4,
        "superseded_rows": 4,
        "deleted_transactions": 1,
        "current_transactions": 5,
    }
    assert report["metrics"]["total_transactions"] == 2
    assert report["metrics"]["approved_transactions"] == 1
    assert report["metrics"]["declined_transactions"] == 1
    assert report["diagnostics"]["non_final_transactions"] == 1
    assert report["diagnostics"]["excluded_type_transactions"] == 2


@pytest.mark.parametrize("kind", ["SALE", "DEFERRED", "DEFFERED", " sale "])
def test_all_verified_sales_spellings_remain_eligible(kind):
    report = report_for([transaction("sale", kind=kind, status=" approved ")])
    assert report["metrics"]["approved_transactions"] == 1


@pytest.mark.parametrize("kind", ["REFUND", "VOID", "REVERSE", "CHARGEBACK", "", None])
def test_other_types_are_diagnosed_not_counted_as_authorizations(kind):
    report = report_for([transaction("other", kind=kind, status="APPROVED")])
    assert report["diagnostics"]["operation_breakdown"]["other_types"]["approved"] == 1
    assert report["diagnostics"]["status"] == "outside_scope"
    assert report["metrics"]["total_transactions"] == 0


@pytest.mark.parametrize("status", ["PENDING", "INITIALIZED", "ERROR", "", None])
def test_non_final_states_have_explicit_diagnosis_and_do_not_enter_denominator(status):
    report = report_for([transaction("non-final", status=status)])
    assert report["diagnostics"]["status"] == "no_final_results"
    assert report["diagnostics"]["non_final_transactions"] == 1
    assert report["metrics"]["approval_rate"] is None
    assert report["daily"] == []


@pytest.mark.parametrize(
    ("records", "status"),
    [
        ([], "no_source_rows"),
        ([transaction(None), transaction(None), transaction(" ")], "invalid_keys"),
        ([transaction("gone", is_deleted=True)], "no_current_records"),
        ([transaction("capture", kind="CAPTURE")], "outside_scope"),
        ([transaction("pending", status="PENDING")], "no_final_results"),
        ([transaction("good")], "available"),
    ],
)
def test_status_precedence_and_coverage_never_claim_zero_activity(records, status):
    report = report_for(records)
    assert report["diagnostics"]["status"] == status
    assert report["completeness"] == {"status": "UNVERIFIED"}
    assert report["freshness"]["status"] == "UNKNOWN"
    assert report["freshness"]["scope"] == "merchant_creation_window"
    assert any("no acredita cero actividad" in item for item in report["limitations"])
    assert validate_guardian_history(report, QUERY) == report


def test_ingestion_max_includes_excluded_and_superseded_rows_but_not_other_mid_or_window():
    report = report_for(
        [
            transaction("sale", etl_job_timestamp="2026-01-02 12:30:00"),
            transaction("sale", event_timestamp="2026-01-02 13:00:00", etl_job_timestamp=None),
            transaction(None, etl_job_timestamp="2026-01-03 23:59:59"),
            transaction(
                "foreign", merchant_code="another", etl_job_timestamp="2026-01-04 23:59:59"
            ),
            transaction(
                "excluded-date",
                create_timestamp="2026-01-05 05:00:00",
                etl_job_timestamp="2026-01-05 23:59:59",
            ),
        ]
    )
    assert report["freshness"]["last_ingested_at"] == "2026-01-03T23:59:59+00:00"
    assert report["freshness"]["status"] == "UNKNOWN"


def test_no_etl_timestamp_is_unknown_not_replaced_with_query_or_event_timestamp():
    report = report_for([transaction("sale", etl_job_timestamp=None)])
    assert report["freshness"]["last_ingested_at"] is None
    assert report["diagnostics"]["status"] == "available"


def test_cdc_order_uses_coalesce_then_event_id_without_resurrecting_old_approval():
    report = report_for(
        [
            transaction(
                "fallback",
                status="APPROVED",
                update_timestamp="2026-01-02 10:00:00",
                event_timestamp="2026-01-02 20:00:00",
            ),
            transaction("fallback", event_timestamp="2026-01-02 11:00:00"),
            transaction("etl-fallback", status="APPROVED"),
            transaction(
                "etl-fallback", event_timestamp=None, etl_job_timestamp="2026-01-02 13:00:00"
            ),
            transaction("create-fallback", status="APPROVED"),
            transaction(
                "create-fallback",
                event_timestamp=None,
                etl_job_timestamp=None,
                create_timestamp="2026-01-02 13:00:00",
            ),
            transaction("tie", status="APPROVED", event_id="a"),
            transaction("tie", event_id="b"),
        ]
    )
    assert report["metrics"]["total_transactions"] == 4
    assert report["metrics"]["declined_transactions"] == 4
    assert report["diagnostics"]["totals"]["superseded_rows"] == 4


def test_utc_bounds_and_weighted_comparison_preserve_gaps_not_synthetic_zero_days():
    report = report_for(
        [
            transaction("before", create_timestamp="2026-01-01 04:59:59"),
            transaction("first", status="APPROVED", create_timestamp="2026-01-01 05:00:00"),
            transaction("last-a", create_timestamp="2026-01-05 04:59:59"),
            transaction("last-b", create_timestamp="2026-01-05 04:59:59"),
            transaction("after", create_timestamp="2026-01-05 05:00:00"),
            transaction(
                "pending-only-day", status="PENDING", create_timestamp="2026-01-03 12:00:00"
            ),
        ]
    )
    assert [row["date"] for row in report["daily"]] == ["2026-01-01", "2026-01-04"]
    assert len(report["diagnostic_daily"]) == 3
    assert report["metrics"]["approval_rate"] == pytest.approx(1 / 3)
    assert report["comparison"]["change_percentage_points"] == -100.0
    evidence = guardian_historical_evidence(report)
    assert [item["evidence_id"] for item in evidence["daily"]] == ["day_0", "day_3"]
    assert evidence["data_quality"]["missing_days"] == 2
    assert _history_evidence(evidence)[0] == evidence
    serialized = json.dumps(evidence)
    for private in [QUERY.merchant_code, "2026-01", "last_ingested_at", "metric_profile", "source"]:
        assert private not in serialized


@pytest.mark.parametrize("profile", ["", "arbitrary", "SALES", None, {}, ["sales"]])
def test_unknown_profile_is_rejected_and_never_becomes_sql(profile):
    with pytest.raises(HistoryError, match="ventas o autorizaciones"):
        GuardianHistoryReader(AthenaHistoryConfig(), metric_profile=profile)


def test_profile_definition_cannot_be_mutated_to_change_future_calls():
    profile = metric_profile_definition("sales")
    profile["included_types"].append("CAPTURE")
    assert "CAPTURE" not in metric_profile_definition("sales")["included_types"]


def test_transaction_ids_remain_strings_and_shared_tickets_do_not_merge_attempts():
    report = report_for(
        [
            transaction("001", status="APPROVED", ticket_code="shared"),
            transaction("1", ticket_code="shared"),
            transaction("without-ticket", ticket_code=None),
            transaction("empty-ticket", ticket_code=""),
        ]
    )
    assert report["metrics"]["total_transactions"] == 4
    assert report["metrics"]["approved_transactions"] == 1
    assert report["metrics"]["approval_rate"] == 0.25
    assert report["diagnostics"]["totals"]["superseded_rows"] == 0


def test_diags_cannot_imply_unreported_or_double_counted_transactions():
    report = report_for(
        [
            transaction("sale-a", status="APPROVED"),
            transaction("sale-d"),
            transaction("preauth-a", kind="PREAUTHORIZATION", status="APPROVED"),
            transaction("preauth-p", kind="PREAUTHORIZATION", status="INITIALIZED"),
            transaction("capture", kind="CAPTURE", status="APPROVED"),
            transaction("other", kind="VOID"),
            transaction("deleted", is_deleted=True),
            transaction(None),
        ]
    )
    diags = report["diagnostics"]
    totals = diags["totals"]
    assert totals["source_cdc_rows"] == (
        totals["missing_transaction_code_rows"]
        + totals["superseded_rows"]
        + totals["deleted_transactions"]
        + totals["current_transactions"]
    )
    assert totals["current_transactions"] == (
        diags["excluded_type_transactions"]
        + diags["non_final_transactions"]
        + report["metrics"]["total_transactions"]
    )
    assert totals["current_transactions"] == sum(
        operation["total"] for operation in diags["operation_breakdown"].values()
    )


def test_ingestion_offset_is_normalized_before_taking_global_max():
    first = row(
        "2026-01-01",
        source_cdc_rows=1,
        current_transactions=1,
        sales_approved=1,
        last_ingested_at="2026-01-04T15:00:00-05:00",
    )
    second = row(
        "2026-01-02",
        source_cdc_rows=1,
        current_transactions=1,
        sales_declined=1,
        last_ingested_at="2026-01-04T19:00:00Z",
    )
    report = summarize_guardian_rows([second, first], QUERY, {})
    assert report["freshness"]["last_ingested_at"] == "2026-01-04T20:00:00+00:00"
    assert report["diagnostic_daily"][0]["last_ingested_at"] == "2026-01-04T20:00:00+00:00"
    assert validate_guardian_history(report, QUERY) == report


@pytest.mark.parametrize("field", ["approved_statuses", "declined_statuses"])
def test_v3_rejects_noncanonical_status_config_without_changing_v2(field):
    config = AthenaHistoryConfig(**{field: ("UNVERIFIED",)})
    with pytest.raises(HistoryError) as caught:
        GuardianHistoryReader(config)
    assert caught.value.code == "INVALID_STATUS_CONFIG"


@pytest.mark.parametrize("field", list(COLUMNS.keys() - {"ticket_code"}))
def test_v3_reuses_fail_closed_catalog_checks(field):
    with pytest.raises(HistoryError) as caught:
        build_guardian_history_sql(
            QUERY, {k: v for k, v in COLUMNS.items() if k != field}, AthenaHistoryConfig()
        )
    assert caught.value.code == "SCHEMA_MISMATCH"


def test_zoned_schema_preserves_timezone_aware_utc_parameters():
    columns = {
        key: "timestamp with time zone" if value == "timestamp" else value
        for key, value in COLUMNS.items()
    }
    sql, params = build_guardian_history_sql(QUERY, columns, AthenaHistoryConfig())
    assert "with_timezone(" not in sql
    assert params[1] == "from_iso8601_timestamp('2026-01-01T05:00:00+00:00')"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda report: report.update(schema_version="2.0"),
        lambda report: report["metric_profile"].update(id="sales"),
        lambda report: report["metric_profile"].update(label="Malicious profile"),
        lambda report: report["metric_profile"]["included_types"].append("CAPTURE"),
        lambda report: report["query"].update(timezone="UTC"),
        lambda report: report["metrics"].update(total_transactions=True),
        lambda report: report["metrics"].update(total_transactions=1.0),
        lambda report: report["daily"][0].update(approved_transactions=1),
        lambda report: report["diagnostics"]["totals"].update(source_cdc_rows=10),
        lambda report: report["diagnostics"].update(status="outside_scope"),
        lambda report: report["diagnostics"].update(non_final_transactions=True),
        lambda report: report["diagnostic_daily"][0].update(source_cdc_rows="1"),
        lambda report: report["diagnostic_daily"][0].update(source_cdc_rows=True),
        lambda report: report["diagnostic_daily"][0].update(sales_declined=2),
        lambda report: report["freshness"].update(status="FRESH"),
        lambda report: report["completeness"].update(status="COMPLETE"),
        lambda report: report["comparison"].update(change_percentage_points=float("nan")),
        lambda report: report.update(unexpected="metadata"),
        lambda report: report["limitations"].clear(),
        lambda report: report["source"].update(database="another-database"),
        lambda report: report["source"].update(retrieved_at="2026-01-05 12:00:00"),
    ],
)
def test_saved_validation_rebuilds_every_derived_field_and_rejects_corruption(mutation):
    report = report_for([transaction("sale")])
    mutation(report)
    with pytest.raises(HistoryError) as caught:
        validate_guardian_history(report, QUERY)
    assert caught.value.code == "INVALID_HISTORY_SOURCE"
    with pytest.raises(HistoryError):
        guardian_historical_evidence(report)


def test_validation_requires_requested_profile_and_returns_independent_evidence():
    report = report_for([transaction("sale")])
    with pytest.raises(HistoryError):
        validate_guardian_history(report, QUERY, "sales")
    copy = validate_guardian_history(report, QUERY, "authorizations")
    copy["diagnostic_daily"][0]["sales_declined"] = 900
    assert report["diagnostic_daily"][0]["sales_declined"] == 1


@pytest.mark.parametrize("change", [{"merchant_code": "another-mid"}, {"date_to": "2026-01-05"}])
def test_saved_evidence_is_bound_to_external_request_not_its_own_declared_query(change):
    report = report_for([transaction("sale")])
    report["query"].update(change)
    with pytest.raises(HistoryError) as caught:
        validate_guardian_history(report, QUERY)
    assert caught.value.code == "INVALID_HISTORY_SOURCE"


@pytest.mark.parametrize(
    "changes",
    [
        {"source_cdc_rows": "1", "current_transactions": "0"},
        {"source_cdc_rows": "1", "current_transactions": "1", "sales_approved": "0"},
        {"source_cdc_rows": "0"},
        {"source_cdc_rows": "1.0"},
        {"source_cdc_rows": "-1"},
        {"source_cdc_rows": "9007199254740992"},
        {"last_ingested_at": "2026-01-02 12:00:00"},
        {"business_date": "2026-01-05"},
        {"business_date": "20260102"},
    ],
)
def test_raw_diagnostics_reject_inconsistent_or_unsafe_counts_and_dates(changes):
    raw = row(source_cdc_rows=1, current_transactions=1, sales_approved=1)
    raw.update(changes)
    with pytest.raises(HistoryError) as caught:
        summarize_guardian_rows([raw], QUERY, {})
    assert caught.value.code == "INVALID_HISTORY"


def test_raw_diagnostics_reject_duplicate_dates_unknown_fields_and_oversized_batches():
    raw = row(source_cdc_rows=1, current_transactions=1, sales_approved=1)
    for rows in ([raw, raw], [{**raw, "extra": "0"}], [raw] * 32):
        with pytest.raises(HistoryError):
            summarize_guardian_rows(rows, QUERY, {})


def test_reader_uses_existing_transport_pagination_without_a_second_diagnostic_query():
    glue = Mock()
    glue.get_table.return_value = {
        "Table": {
            "StorageDescriptor": {
                "Columns": [{"Name": name, "Type": kind} for name, kind in COLUMNS.items()]
            }
        }
    }
    athena = Mock()
    athena.start_query_execution.return_value = {"QueryExecutionId": "synthetic-v3-query"}
    athena.get_query_execution.return_value = {
        "QueryExecution": {
            "Status": {"State": "SUCCEEDED"},
            "Statistics": {"DataScannedInBytes": 512},
        }
    }
    raws = [
        row(day, source_cdc_rows=1, current_transactions=1, preauthorizations_declined=1)
        for day in ("2026-01-01", "2026-01-04")
    ]

    def page(raw, first):
        values = [NAMES, [raw[key] for key in NAMES]] if first else [[raw[key] for key in NAMES]]
        return {
            "ResultSet": {
                "ResultSetMetadata": {"ColumnInfo": [{"Name": name} for name in NAMES]},
                "Rows": [{"Data": [{"VarCharValue": value} for value in item]} for item in values],
            },
            **({"NextToken": "second-page"} if first else {}),
        }

    athena.get_query_results.side_effect = [page(raws[0], True), page(raws[1], False)]
    session = SimpleNamespace(client=lambda name: {"glue": glue, "athena": athena}[name])
    reader = GuardianHistoryReader(AthenaHistoryConfig(timeout_seconds=1), session=session)
    report = reader.read(QUERY)
    assert report["schema_version"] == "3.0"
    assert report["metrics"]["total_transactions"] == 2
    assert report["source"]["data_scanned_bytes"] == 512
    assert report["source"]["query_execution_id"] == "synthetic-v3-query"
    assert validate_guardian_history(report, QUERY) == report
    athena.start_query_execution.assert_called_once()
    options = athena.start_query_execution.call_args.kwargs
    assert options["ResultReuseConfiguration"]["ResultReuseByAgeConfiguration"]["Enabled"] is False
    assert athena.get_query_results.call_args_list[1].kwargs["NextToken"] == "second-page"


def test_empty_history_can_be_replayed_but_is_not_fabricated_as_an_ai_daily_record():
    report = summarize_guardian_rows([], QUERY, {})
    assert validate_guardian_history(deepcopy(report), QUERY) == report
    evidence = guardian_historical_evidence(report)
    assert evidence["daily"] == []
    assert evidence["data_quality"]["observed_days"] == 0
    assert evidence["data_quality"]["missing_days"] == 4
