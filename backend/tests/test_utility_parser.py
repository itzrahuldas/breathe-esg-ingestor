"""
tests/test_utility_parser.py
==============================
Unit tests for ingestor/parsers/utility_parser.py

Coverage:
  Pure-function tests (no DB):
    - parse_utility_date: all 3 formats + blank/invalid
    - normalize_utility_unit: kWh, MWh, unknown
    - split_billing_period: same month, spec example (Row 1), MWh row (Row 3)

  Integration tests (Django DB):
    - Row 1 — normal, billing period spans 2 months → 2 ActivityRows, PENDING
    - Row 2 — estimated read (read_type=E) → FLAGGED
    - Row 3 — MWh unit normalised to kWh → PENDING
    - Row 4 — zero consumption → FLAGGED
    - AuditLog written for every ActivityRow
    - RawUpload.raw_payload immutability guard
    - Period > 45 days → FLAGGED
    - Scope=2, category='purchased_electricity' always
"""

import io
from datetime import date
from decimal import Decimal

import pytest
from django.test import TestCase

from ingestor.models import ActivityRow, AuditLog, Client, RawUpload
from ingestor.parsers.utility_parser import (
    INDIA_GRID_KWH,
    MAX_BILLING_DAYS,
    normalize_utility_unit,
    parse_utility_date,
    parse_utility_file,
    split_billing_period,
)


# ===========================================================================
# Helpers
# ===========================================================================


def _make_csv(rows: list[str]) -> io.StringIO:
    header = "meter_id,site_name,bill_from,bill_to,consumption,unit,read_type,amount"
    body = "\n".join([header] + rows)
    return io.StringIO(body)


# ===========================================================================
# Pure-function tests
# ===========================================================================


class TestParseUtilityDate:
    """parse_utility_date — all supported formats and error cases."""

    def test_dd_slash_mm_slash_yyyy(self):
        assert parse_utility_date("18/01/2024") == date(2024, 1, 18)

    def test_dd_slash_mm_slash_yyyy_row1_from(self):
        assert parse_utility_date("18/01/2024") == date(2024, 1, 18)

    def test_dd_slash_mm_slash_yyyy_row1_to(self):
        assert parse_utility_date("21/02/2024") == date(2024, 2, 21)

    def test_iso_format(self):
        assert parse_utility_date("2024-01-18") == date(2024, 1, 18)

    def test_dd_dash_mm_dash_yyyy(self):
        assert parse_utility_date("18-01-2024") == date(2024, 1, 18)

    def test_row3_from(self):
        assert parse_utility_date("01/01/2024") == date(2024, 1, 1)

    def test_row3_to(self):
        assert parse_utility_date("31/01/2024") == date(2024, 1, 31)

    def test_blank_raises(self):
        with pytest.raises(ValueError, match="empty or blank"):
            parse_utility_date("")

    def test_whitespace_raises(self):
        with pytest.raises(ValueError, match="empty or blank"):
            parse_utility_date("   ")

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Cannot parse utility date"):
            parse_utility_date("not-a-date")

    def test_invalid_month_raises(self):
        with pytest.raises(ValueError):
            parse_utility_date("18/13/2024")   # month 13


class TestNormalizeUtilityUnit:
    """normalize_utility_unit — unit conversion."""

    def test_kwh_passthrough(self):
        qty, unit = normalize_utility_unit(3660.0, "kWh")
        assert qty == 3660.0
        assert unit == "kWh"

    def test_kwh_uppercase(self):
        qty, unit = normalize_utility_unit(3660.0, "KWH")
        assert qty == 3660.0
        assert unit == "kWh"

    def test_mwh_to_kwh(self):
        """Row 3: 2.3 MWh → 2300 kWh"""
        qty, unit = normalize_utility_unit(2.3, "MWh")
        assert abs(qty - 2300.0) < 1e-9
        assert unit == "kWh"

    def test_mwh_uppercase(self):
        qty, unit = normalize_utility_unit(1.0, "MWH")
        assert qty == 1000.0
        assert unit == "kWh"

    def test_unknown_unit_raises(self):
        with pytest.raises(ValueError, match="Unknown utility unit"):
            normalize_utility_unit(100.0, "BTU")

    def test_unknown_unit_message_includes_code(self):
        with pytest.raises(ValueError, match="BTU"):
            normalize_utility_unit(100.0, "BTU")


class TestSplitBillingPeriod:
    """split_billing_period — monthly proportional split."""

    def test_same_month_single_slice(self):
        result = split_billing_period(750.0, date(2024, 1, 18), date(2024, 1, 21))
        assert len(result) == 1
        assert result[0]["consumption"] == 750.0
        assert result[0]["month_start"] == date(2024, 1, 18)
        assert result[0]["month_end"] == date(2024, 1, 21)

    def test_row3_same_month(self):
        """Row 3: 01/01/2024 – 31/01/2024 → single slice."""
        result = split_billing_period(2300.0, date(2024, 1, 1), date(2024, 1, 31))
        assert len(result) == 1
        assert result[0]["consumption"] == 2300.0

    def test_row1_two_month_split(self):
        """
        Row 1 spec: 18 Jan – 21 Feb, 3660 kWh, 35 total days.
        Jan share = 13/35, Feb share = 22/35.
        """
        result = split_billing_period(3660.0, date(2024, 1, 18), date(2024, 2, 21))
        assert len(result) == 2

        total_days = 35
        jan_days = 13
        feb_days = 22

        jan_slice = result[0]
        feb_slice = result[1]

        expected_jan = round(3660.0 * jan_days / total_days, 6)
        expected_feb = round(3660.0 * feb_days / total_days, 6)

        assert jan_slice["month_start"] == date(2024, 1, 18)
        assert jan_slice["month_end"] == date(2024, 1, 31)
        assert abs(jan_slice["consumption"] - expected_jan) < 0.001

        assert feb_slice["month_start"] == date(2024, 2, 1)
        assert feb_slice["month_end"] == date(2024, 2, 21)
        assert abs(feb_slice["consumption"] - expected_feb) < 0.001

    def test_row1_slices_sum_to_total(self):
        result = split_billing_period(3660.0, date(2024, 1, 18), date(2024, 2, 21))
        total = sum(s["consumption"] for s in result)
        assert abs(total - 3660.0) < 0.01

    def test_three_month_span(self):
        """Jan 15 – Mar 20 should yield 3 slices."""
        result = split_billing_period(9000.0, date(2024, 1, 15), date(2024, 3, 20))
        assert len(result) == 3
        total = sum(s["consumption"] for s in result)
        assert abs(total - 9000.0) < 0.01

    def test_bill_to_before_bill_from_raises(self):
        with pytest.raises(ValueError, match="must not be before"):
            split_billing_period(100.0, date(2024, 2, 1), date(2024, 1, 1))

    def test_single_day_period(self):
        result = split_billing_period(24.0, date(2024, 3, 15), date(2024, 3, 15))
        assert len(result) == 1
        assert result[0]["consumption"] == 24.0


# ===========================================================================
# Integration tests (Django TestCase)
# ===========================================================================


class TestParseUtilityFile(TestCase):
    """parse_utility_file — full pipeline through DB."""

    def setUp(self):
        self.client_obj = Client.objects.create(
            name="Acme Corp", slug="acme-util"
        )
        self.user_id = 7

    # ------------------------------------------------------------------
    # Row 1: normal, billing period spans 2 months → 2 ActivityRows
    # ------------------------------------------------------------------

    def test_row1_produces_two_activity_rows(self):
        csv_io = _make_csv([
            "MTR-0042,Mumbai Office,18/01/2024,21/02/2024,3660,kWh,A,54900"
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        assert len(results) == 2

    def test_row1_slices_cover_correct_months(self):
        csv_io = _make_csv([
            "MTR-0042,Mumbai Office,18/01/2024,21/02/2024,3660,kWh,A,54900"
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        assert results[0]["month_start"] == date(2024, 1, 18)
        assert results[0]["month_end"] == date(2024, 1, 31)
        assert results[1]["month_start"] == date(2024, 2, 1)
        assert results[1]["month_end"] == date(2024, 2, 21)

    def test_row1_consumption_split_sums_to_total(self):
        csv_io = _make_csv([
            "MTR-0042,Mumbai Office,18/01/2024,21/02/2024,3660,kWh,A,54900"
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        total = sum(r["consumption_kwh"] for r in results)
        assert abs(total - 3660.0) < 0.01

    def test_row1_not_flagged(self):
        csv_io = _make_csv([
            "MTR-0042,Mumbai Office,18/01/2024,21/02/2024,3660,kWh,A,54900"
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        assert all(not r["is_flagged"] for r in results)

    def test_row1_scope_2_electricity(self):
        csv_io = _make_csv([
            "MTR-0042,Mumbai Office,18/01/2024,21/02/2024,3660,kWh,A,54900"
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        for r in results:
            row = ActivityRow.objects.get(pk=r["activity_row_id"])
            assert row.scope == 2
            assert row.category == "purchased_electricity"

    def test_row1_emission_factor_applied(self):
        csv_io = _make_csv([
            "MTR-0042,Mumbai Office,18/01/2024,21/02/2024,3660,kWh,A,54900"
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        for r in results:
            row = ActivityRow.objects.get(pk=r["activity_row_id"])
            assert row.emission_factor == Decimal(str(INDIA_GRID_KWH))
            expected = Decimal(str(round(r["consumption_kwh"] * INDIA_GRID_KWH, 4)))
            assert row.co2e_kg == expected

    def test_row1_two_audit_logs_written(self):
        csv_io = _make_csv([
            "MTR-0042,Mumbai Office,18/01/2024,21/02/2024,3660,kWh,A,54900"
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        assert len(results) == 2
        for r in results:
            logs = AuditLog.objects.filter(activity_row_id=r["activity_row_id"])
            assert logs.count() == 1
            assert logs.first().action == AuditLog.ACTION_UPLOADED

    def test_row1_one_raw_upload_for_two_rows(self):
        """One CSV row → one RawUpload, but two ActivityRows."""
        csv_io = _make_csv([
            "MTR-0042,Mumbai Office,18/01/2024,21/02/2024,3660,kWh,A,54900"
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        assert len(results) == 2
        ru_ids = {
            ActivityRow.objects.get(pk=r["activity_row_id"]).raw_upload_id
            for r in results
        }
        assert len(ru_ids) == 1   # both slices share the same RawUpload

    def test_row1_raw_payload_immutable(self):
        csv_io = _make_csv([
            "MTR-0042,Mumbai Office,18/01/2024,21/02/2024,3660,kWh,A,54900"
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=results[0]["activity_row_id"])
        ru = row.raw_upload
        assert ru.raw_payload["meter_id"] == "MTR-0042"
        ru.raw_payload = {"tampered": True}
        with pytest.raises(ValueError):
            ru.save()

    # ------------------------------------------------------------------
    # Row 2: estimated read → FLAGGED
    # ------------------------------------------------------------------

    def test_row2_estimated_read_flagged(self):
        csv_io = _make_csv([
            "MTR-0043,Mumbai Office,18/01/2024,21/02/2024,750,kWh,E,11250"
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        assert all(r["is_flagged"] for r in results)

    def test_row2_flag_reason_mentions_estimated(self):
        csv_io = _make_csv([
            "MTR-0043,Mumbai Office,18/01/2024,21/02/2024,750,kWh,E,11250"
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=results[0]["activity_row_id"])
        assert "estimated" in row.flag_reason.lower()

    def test_row2_status_flagged(self):
        csv_io = _make_csv([
            "MTR-0043,Mumbai Office,18/01/2024,21/02/2024,750,kWh,E,11250"
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        for r in results:
            row = ActivityRow.objects.get(pk=r["activity_row_id"])
            assert row.status == ActivityRow.STATUS_FLAGGED

    def test_row2_audit_log_written_when_flagged(self):
        csv_io = _make_csv([
            "MTR-0043,Mumbai Office,18/01/2024,21/02/2024,750,kWh,E,11250"
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        for r in results:
            assert AuditLog.objects.filter(activity_row_id=r["activity_row_id"]).count() == 1

    # ------------------------------------------------------------------
    # Row 3: MWh unit normalised to kWh, same month → 1 ActivityRow
    # ------------------------------------------------------------------

    def test_row3_mwh_normalised(self):
        csv_io = _make_csv([
            "MTR-0089,Pune Factory,01/01/2024,31/01/2024,2.3,MWh,A,345000"
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        assert len(results) == 1
        row = ActivityRow.objects.get(pk=results[0]["activity_row_id"])
        assert row.unit == "kWh"
        assert abs(float(row.quantity) - 2300.0) < 0.01

    def test_row3_not_flagged(self):
        csv_io = _make_csv([
            "MTR-0089,Pune Factory,01/01/2024,31/01/2024,2.3,MWh,A,345000"
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        assert not results[0]["is_flagged"]

    def test_row3_single_activity_row(self):
        """Same month → no split → exactly 1 ActivityRow."""
        csv_io = _make_csv([
            "MTR-0089,Pune Factory,01/01/2024,31/01/2024,2.3,MWh,A,345000"
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        assert len(results) == 1

    def test_row3_emission_factor_on_kwh_qty(self):
        csv_io = _make_csv([
            "MTR-0089,Pune Factory,01/01/2024,31/01/2024,2.3,MWh,A,345000"
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=results[0]["activity_row_id"])
        expected_co2e = Decimal(str(round(2300.0 * INDIA_GRID_KWH, 4)))
        assert row.co2e_kg == expected_co2e

    # ------------------------------------------------------------------
    # Row 4: zero consumption → FLAGGED
    # ------------------------------------------------------------------

    def test_row4_zero_consumption_flagged(self):
        csv_io = _make_csv([
            "MTR-0010,Delhi Office,01/01/2024,31/01/2024,0,kWh,A,0"
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        assert results[0]["is_flagged"] is True

    def test_row4_flag_reason_mentions_zero(self):
        csv_io = _make_csv([
            "MTR-0010,Delhi Office,01/01/2024,31/01/2024,0,kWh,A,0"
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=results[0]["activity_row_id"])
        assert "zero" in row.flag_reason.lower() or "negative" in row.flag_reason.lower()

    def test_row4_status_flagged(self):
        csv_io = _make_csv([
            "MTR-0010,Delhi Office,01/01/2024,31/01/2024,0,kWh,A,0"
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=results[0]["activity_row_id"])
        assert row.status == ActivityRow.STATUS_FLAGGED

    def test_row4_no_emission_factor_for_zero(self):
        csv_io = _make_csv([
            "MTR-0010,Delhi Office,01/01/2024,31/01/2024,0,kWh,A,0"
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=results[0]["activity_row_id"])
        assert row.co2e_kg is None

    # ------------------------------------------------------------------
    # Period > 45 days flag
    # ------------------------------------------------------------------

    def test_period_exceeding_45_days_flagged(self):
        """A 60-day billing window should trigger the period flag."""
        csv_io = _make_csv([
            "MTR-0099,Test Site,01/01/2024,01/03/2024,5000,kWh,A,75000"
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        assert all(r["is_flagged"] for r in results)

    def test_period_exceeding_45_days_flag_reason(self):
        csv_io = _make_csv([
            "MTR-0099,Test Site,01/01/2024,01/03/2024,5000,kWh,A,75000"
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=results[0]["activity_row_id"])
        assert str(MAX_BILLING_DAYS) in row.flag_reason or "45" in row.flag_reason

    def test_exactly_45_days_not_flagged(self):
        """Exactly 45-day period is fine."""
        # 01 Jan to 14 Feb = 45 days (31-1 + 14 = 44 days delta + 1 = 45)
        csv_io = _make_csv([
            "MTR-0099,Test Site,01/01/2024,14/02/2024,2000,kWh,A,30000"
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        flag_reasons = [
            ActivityRow.objects.get(pk=r["activity_row_id"]).flag_reason
            for r in results
        ]
        # Should not contain the period flag
        assert not any("exceeds 45" in fr for fr in flag_reasons)

    # ------------------------------------------------------------------
    # Multi-row batch
    # ------------------------------------------------------------------

    def test_all_four_rows_batch(self):
        csv_io = _make_csv([
            "MTR-0042,Mumbai Office,18/01/2024,21/02/2024,3660,kWh,A,54900",
            "MTR-0043,Mumbai Office,18/01/2024,21/02/2024,750,kWh,E,11250",
            "MTR-0089,Pune Factory,01/01/2024,31/01/2024,2.3,MWh,A,345000",
            "MTR-0010,Delhi Office,01/01/2024,31/01/2024,0,kWh,A,0",
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        # Row1→2 slices, Row2→2 slices, Row3→1 slice, Row4→1 slice = 6 total
        assert len(results) == 6

        # 4 RawUploads (one per CSV row)
        assert RawUpload.objects.filter(client=self.client_obj).count() == 4
        # 6 AuditLog entries (one per ActivityRow)
        assert AuditLog.objects.filter(client=self.client_obj).count() == 6

    def test_all_four_rows_scope_2_throughout(self):
        csv_io = _make_csv([
            "MTR-0042,Mumbai Office,18/01/2024,21/02/2024,3660,kWh,A,54900",
            "MTR-0043,Mumbai Office,18/01/2024,21/02/2024,750,kWh,E,11250",
            "MTR-0089,Pune Factory,01/01/2024,31/01/2024,2.3,MWh,A,345000",
            "MTR-0010,Delhi Office,01/01/2024,31/01/2024,0,kWh,A,0",
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        for r in results:
            row = ActivityRow.objects.get(pk=r["activity_row_id"])
            assert row.scope == 2
            assert row.category == "purchased_electricity"

    # ------------------------------------------------------------------
    # Hard constraint guards
    # ------------------------------------------------------------------

    def test_audit_log_cannot_be_updated(self):
        csv_io = _make_csv([
            "MTR-0042,Mumbai Office,18/01/2024,21/02/2024,3660,kWh,A,54900"
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        log = AuditLog.objects.filter(
            activity_row_id=results[0]["activity_row_id"]
        ).first()
        log.detail = "tampered"
        with pytest.raises(ValueError, match="immutable"):
            log.save()

    def test_audit_log_cannot_be_deleted(self):
        csv_io = _make_csv([
            "MTR-0042,Mumbai Office,18/01/2024,21/02/2024,3660,kWh,A,54900"
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        log = AuditLog.objects.filter(
            activity_row_id=results[0]["activity_row_id"]
        ).first()
        with pytest.raises(ValueError, match="cannot be deleted"):
            log.delete()

    def test_multi_tenancy_client_fk_set(self):
        csv_io = _make_csv([
            "MTR-0042,Mumbai Office,18/01/2024,21/02/2024,3660,kWh,A,54900"
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        for r in results:
            row = ActivityRow.objects.get(pk=r["activity_row_id"])
            assert row.client_id == self.client_obj.pk
            assert row.raw_upload.client_id == self.client_obj.pk
            log = AuditLog.objects.filter(activity_row_id=r["activity_row_id"]).first()
            assert log.client_id == self.client_obj.pk

    def test_meter_id_stored_on_activity_row(self):
        csv_io = _make_csv([
            "MTR-0042,Mumbai Office,18/01/2024,21/02/2024,3660,kWh,A,54900"
        ])
        results = parse_utility_file(csv_io, self.client_obj.pk, self.user_id)
        assert results[0]["meter_id"] == "MTR-0042"
        row = ActivityRow.objects.get(pk=results[0]["activity_row_id"])
        assert row.plant_code == "MTR-0042"
