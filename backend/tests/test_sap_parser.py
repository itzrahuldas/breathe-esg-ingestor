"""
tests/test_sap_parser.py
=========================
Unit tests for ingestor/parsers/sap_parser.py

Coverage:
  Pure-function tests (no DB):
    - parse_sap_date: all 3 formats + blank/invalid
    - normalize_sap_unit: all known units + unknown
    - normalize_sap_headers: German & English variants
    - assign_scope_from_sap: FUL- prefix, desc keyword, ELEC-, default

  Integration tests (Django DB):
    - parse_sap_file with the 4 exact sample rows from the spec:
        Row 1 — normal diesel purchase → PENDING, emission factor applied
        Row 2 — GAL unit, German date, blank BUDAT → PENDING, GAL converted
        Row 3 — unknown plant code → FLAGGED
        Row 4 — negative quantity → FLAGGED
    - AuditLog written for every row
    - RawUpload.raw_payload immutability guard
    - ActivityRow LOCKED guard
"""

import io
import textwrap
from datetime import date
from decimal import Decimal

import pytest
from django.test import TestCase

from ingestor.models import ActivityRow, AuditLog, Client, PlantCode, RawUpload
from ingestor.parsers.sap_parser import (
    DIESEL_LITRES,
    PETROL_LITRES,
    assign_scope_from_sap,
    normalize_sap_headers,
    normalize_sap_unit,
    parse_sap_date,
    parse_sap_file,
)


# ===========================================================================
# Helpers
# ===========================================================================


def _make_csv(rows: list[str]) -> io.StringIO:
    """Build a StringIO CSV from a header + data rows."""
    header = "WERKS,MATNR,MENGE,MEINS,BLDAT,BUDAT,BKTXT"
    body = "\n".join([header] + rows)
    return io.StringIO(body)


# ===========================================================================
# Pure-function tests (no DB required)
# ===========================================================================


class TestParseSapDate:
    """parse_sap_date — all supported formats and error cases."""

    def test_format_yyyymmdd(self):
        assert parse_sap_date("20241231") == date(2024, 12, 31)

    def test_format_dd_mm_yyyy(self):
        assert parse_sap_date("31.12.2024") == date(2024, 12, 31)

    def test_format_iso(self):
        assert parse_sap_date("2024-11-15") == date(2024, 11, 15)

    def test_yyyymmdd_row1_bldat(self):
        """Row 1 spec: BLDAT=20241231"""
        assert parse_sap_date("20241231") == date(2024, 12, 31)

    def test_yyyymmdd_row1_budat(self):
        """Row 1 spec: BUDAT=20250103"""
        assert parse_sap_date("20250103") == date(2025, 1, 3)

    def test_german_date_row2(self):
        """Row 2 spec: BLDAT=31.12.2024"""
        assert parse_sap_date("31.12.2024") == date(2024, 12, 31)

    def test_iso_date_row3(self):
        """Row 3 spec: BLDAT=2024-11-15"""
        assert parse_sap_date("2024-11-15") == date(2024, 11, 15)

    def test_iso_date_row3_budat(self):
        """Row 3 spec: BUDAT=2024-11-20"""
        assert parse_sap_date("2024-11-20") == date(2024, 11, 20)

    def test_yyyymmdd_row4(self):
        """Row 4 spec: BLDAT=20241001"""
        assert parse_sap_date("20241001") == date(2024, 10, 1)

    def test_blank_raises(self):
        with pytest.raises(ValueError, match="empty or blank"):
            parse_sap_date("")

    def test_none_like_raises(self):
        with pytest.raises(ValueError, match="empty or blank"):
            parse_sap_date("   ")

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="Cannot parse SAP date"):
            parse_sap_date("not-a-date")

    def test_invalid_month_raises(self):
        with pytest.raises(ValueError):
            parse_sap_date("20241399")  # month 13 — invalid


class TestNormalizeSapUnit:
    """normalize_sap_unit — unit conversion accuracy."""

    def test_litres_passthrough(self):
        qty, unit = normalize_sap_unit("L", 500.0)
        assert qty == 500.0
        assert unit == "litres"

    def test_gallons_to_litres(self):
        """Row 2: 132.5 GAL → 132.5 × 3.785 litres"""
        qty, unit = normalize_sap_unit("GAL", 132.5)
        assert abs(qty - 132.5 * 3.785) < 1e-6
        assert unit == "litres"

    def test_kg(self):
        qty, unit = normalize_sap_unit("KG", 100.0)
        assert qty == 100.0
        assert unit == "kg"

    def test_m3(self):
        qty, unit = normalize_sap_unit("M3", 50.0)
        assert qty == 50.0
        assert unit == "m3"

    def test_kwh(self):
        qty, unit = normalize_sap_unit("KWH", 1000.0)
        assert qty == 1000.0
        assert unit == "kWh"

    def test_case_insensitive(self):
        """MEINS values should match regardless of case."""
        qty, unit = normalize_sap_unit("gal", 10.0)
        assert abs(qty - 10.0 * 3.785) < 1e-6
        assert unit == "litres"

    def test_unknown_unit_raises(self):
        with pytest.raises(ValueError, match="Unknown SAP unit"):
            normalize_sap_unit("BBL", 100.0)

    def test_unknown_unit_message_includes_code(self):
        with pytest.raises(ValueError, match="BBL"):
            normalize_sap_unit("BBL", 100.0)


class TestNormalizeSapHeaders:
    """normalize_sap_headers — German and English header mapping."""

    def test_english_uppercase(self):
        row = {"WERKS": "IN01", "MATNR": "FUL-001", "MENGE": "500",
               "MEINS": "L", "BLDAT": "20241231", "BUDAT": "20250103",
               "BKTXT": "Diesel Mumbai"}
        result = normalize_sap_headers(row)
        assert result["plant_code"] == "IN01"
        assert result["material_number"] == "FUL-001"
        assert result["quantity"] == "500"
        assert result["unit"] == "L"
        assert result["document_date"] == "20241231"
        assert result["posting_date"] == "20250103"
        assert result["description"] == "Diesel Mumbai"

    def test_german_mixed_case(self):
        row = {"Werks": "DE07", "Matnr": "FUL-002", "Menge": "132.5",
               "Meins": "GAL", "Bldat": "31.12.2024", "Budat": "",
               "Bktxt": "Petrol Frankfurt"}
        result = normalize_sap_headers(row)
        assert result["plant_code"] == "DE07"
        assert result["material_number"] == "FUL-002"
        assert result["unit"] == "GAL"

    def test_unknown_keys_pass_through(self):
        """Keys not in HEADER_MAP must be preserved unchanged."""
        row = {"WERKS": "IN01", "CUSTOM_FIELD": "abc"}
        result = normalize_sap_headers(row)
        assert "CUSTOM_FIELD" in result
        assert result["CUSTOM_FIELD"] == "abc"


class TestAssignScopeFromSap:
    """assign_scope_from_sap — deterministic scope/category assignment."""

    def test_ful_prefix_scope1(self):
        scope, cat = assign_scope_from_sap("FUL-001", "Diesel Mumbai")
        assert scope == 1
        assert cat == "stationary_combustion"

    def test_ful_prefix_scope1_case_insensitive(self):
        scope, cat = assign_scope_from_sap("ful-002", "Petrol Frankfurt")
        assert scope == 1
        assert cat == "stationary_combustion"

    def test_desc_diesel_scope1(self):
        scope, cat = assign_scope_from_sap("MAT-999", "diesel for generator")
        assert scope == 1
        assert cat == "stationary_combustion"

    def test_desc_petrol_scope1(self):
        scope, cat = assign_scope_from_sap("MAT-999", "Petrol Frankfurt")
        assert scope == 1
        assert cat == "stationary_combustion"

    def test_desc_lpg_scope1(self):
        scope, cat = assign_scope_from_sap("MAT-999", "LPG heating")
        assert scope == 1
        assert cat == "stationary_combustion"

    def test_desc_gas_scope1(self):
        scope, cat = assign_scope_from_sap("MAT-999", "natural gas boiler")
        assert scope == 1
        assert cat == "stationary_combustion"

    def test_elec_prefix_scope2(self):
        scope, cat = assign_scope_from_sap("ELEC-001", "Grid electricity")
        assert scope == 2
        assert cat == "purchased_electricity"

    def test_elec_prefix_scope2_case_insensitive(self):
        scope, cat = assign_scope_from_sap("elec-010", "")
        assert scope == 2
        assert cat == "purchased_electricity"

    def test_default_scope3(self):
        scope, cat = assign_scope_from_sap("MAT-777", "Office supplies")
        assert scope == 3
        assert cat == "purchased_goods"

    def test_empty_strings_default_scope3(self):
        scope, cat = assign_scope_from_sap("", "")
        assert scope == 3
        assert cat == "purchased_goods"

    def test_ful_prefix_takes_priority_over_elec(self):
        """FUL- rule evaluated before ELEC-."""
        scope, cat = assign_scope_from_sap("FUL-001", "electricity")
        assert scope == 1

    def test_row3_unknown_plant(self):
        """Row 3 — MATNR=FUL-003 → Scope 1 regardless of unknown plant."""
        scope, cat = assign_scope_from_sap("FUL-003", "Test")
        assert scope == 1
        assert cat == "stationary_combustion"

    def test_row4_negative_qty(self):
        """Row 4 — MATNR=FUL-004 → Scope 1 (negative qty handled elsewhere)."""
        scope, cat = assign_scope_from_sap("FUL-004", "Return")
        assert scope == 1
        assert cat == "stationary_combustion"


# ===========================================================================
# Integration tests (Django TestCase — uses SQLite in-memory)
# ===========================================================================


class TestParseSapFile(TestCase):
    """parse_sap_file — full pipeline through DB."""

    def setUp(self):
        self.client_obj = Client.objects.create(name="Acme Corp", slug="acme")
        # Register known plant codes (IN01, IN02, DE07 only — XX99 deliberately absent)
        PlantCode.objects.create(
            client=self.client_obj, code="IN01", site_name="Mumbai Plant", country="IN"
        )
        PlantCode.objects.create(
            client=self.client_obj, code="IN02", site_name="Delhi Plant", country="IN"
        )
        PlantCode.objects.create(
            client=self.client_obj, code="DE07", site_name="Frankfurt Plant", country="DE"
        )
        self.user_id = 42

    # ------------------------------------------------------------------
    # Row 1: normal diesel purchase
    # ------------------------------------------------------------------

    def test_row1_created_successfully(self):
        csv_io = _make_csv([
            "IN01,FUL-001,500,L,20241231,20250103,Diesel Mumbai"
        ])
        ids = parse_sap_file(csv_io, self.client_obj.pk, self.user_id)
        assert len(ids) == 1
        row = ActivityRow.objects.get(pk=ids[0])
        assert row.plant_code == "IN01"
        assert row.material_number == "FUL-001"
        assert row.quantity == Decimal("500")
        assert row.unit == "litres"
        assert row.scope == 1
        assert row.category == "stationary_combustion"
        assert row.document_date == date(2024, 12, 31)
        assert row.posting_date == date(2025, 1, 3)
        assert row.is_flagged is False
        assert row.status == ActivityRow.STATUS_PENDING

    def test_row1_emission_factor_applied(self):
        csv_io = _make_csv([
            "IN01,FUL-001,500,L,20241231,20250103,Diesel Mumbai"
        ])
        ids = parse_sap_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=ids[0])
        assert row.emission_factor == Decimal(str(DIESEL_LITRES))
        expected_co2e = Decimal(str(round(500.0 * DIESEL_LITRES, 4)))
        assert row.co2e_kg == expected_co2e

    def test_row1_raw_upload_immutable(self):
        csv_io = _make_csv([
            "IN01,FUL-001,500,L,20241231,20250103,Diesel Mumbai"
        ])
        ids = parse_sap_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=ids[0])
        ru = row.raw_upload
        assert ru.raw_payload["WERKS"] == "IN01"
        assert ru.raw_payload["MATNR"] == "FUL-001"
        # Attempt to mutate raw_payload must raise
        ru.raw_payload = {"tampered": True}
        with self.assertRaises(ValueError, msg="RawUpload.raw_payload is immutable"):
            ru.save()

    def test_row1_audit_log_written(self):
        csv_io = _make_csv([
            "IN01,FUL-001,500,L,20241231,20250103,Diesel Mumbai"
        ])
        ids = parse_sap_file(csv_io, self.client_obj.pk, self.user_id)
        logs = AuditLog.objects.filter(activity_row_id=ids[0])
        assert logs.count() == 1
        assert logs.first().action == AuditLog.ACTION_UPLOADED
        assert logs.first().actor_id == self.user_id

    # ------------------------------------------------------------------
    # Row 2: GAL unit, German date, blank BUDAT
    # ------------------------------------------------------------------

    def test_row2_gal_converted(self):
        csv_io = _make_csv([
            "DE07,FUL-002,132.5,GAL,31.12.2024,,Petrol Frankfurt"
        ])
        ids = parse_sap_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=ids[0])
        expected_litres = Decimal(str(round(132.5 * 3.785, 4)))
        assert abs(row.quantity - expected_litres) < Decimal("0.0001")
        assert row.unit == "litres"

    def test_row2_blank_budat_falls_back_to_bldat(self):
        csv_io = _make_csv([
            "DE07,FUL-002,132.5,GAL,31.12.2024,,Petrol Frankfurt"
        ])
        ids = parse_sap_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=ids[0])
        assert row.document_date == date(2024, 12, 31)
        assert row.posting_date == date(2024, 12, 31)  # fallback

    def test_row2_not_flagged(self):
        csv_io = _make_csv([
            "DE07,FUL-002,132.5,GAL,31.12.2024,,Petrol Frankfurt"
        ])
        ids = parse_sap_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=ids[0])
        assert row.is_flagged is False

    def test_row2_petrol_emission_factor(self):
        csv_io = _make_csv([
            "DE07,FUL-002,132.5,GAL,31.12.2024,,Petrol Frankfurt"
        ])
        ids = parse_sap_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=ids[0])
        assert row.emission_factor == Decimal(str(PETROL_LITRES))

    # ------------------------------------------------------------------
    # Row 3: unknown plant code → FLAGGED
    # ------------------------------------------------------------------

    def test_row3_unknown_plant_flagged(self):
        csv_io = _make_csv([
            "XX99,FUL-003,200,L,2024-11-15,2024-11-20,Test"
        ])
        ids = parse_sap_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=ids[0])
        assert row.is_flagged is True
        assert row.status == ActivityRow.STATUS_FLAGGED
        assert "XX99" in row.flag_reason
        assert "not found" in row.flag_reason.lower()

    def test_row3_iso_dates_parsed(self):
        csv_io = _make_csv([
            "XX99,FUL-003,200,L,2024-11-15,2024-11-20,Test"
        ])
        ids = parse_sap_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=ids[0])
        assert row.document_date == date(2024, 11, 15)
        assert row.posting_date == date(2024, 11, 20)

    def test_row3_audit_log_written_even_when_flagged(self):
        csv_io = _make_csv([
            "XX99,FUL-003,200,L,2024-11-15,2024-11-20,Test"
        ])
        ids = parse_sap_file(csv_io, self.client_obj.pk, self.user_id)
        assert AuditLog.objects.filter(activity_row_id=ids[0]).count() == 1

    # ------------------------------------------------------------------
    # Row 4: negative quantity → FLAGGED
    # ------------------------------------------------------------------

    def test_row4_negative_quantity_flagged(self):
        csv_io = _make_csv([
            "IN02,FUL-004,-50,L,20241001,20241005,Return"
        ])
        ids = parse_sap_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=ids[0])
        assert row.is_flagged is True
        assert row.status == ActivityRow.STATUS_FLAGGED
        assert "negative" in row.flag_reason.lower() or "-50" in row.flag_reason

    def test_row4_quantity_stored_as_negative(self):
        csv_io = _make_csv([
            "IN02,FUL-004,-50,L,20241001,20241005,Return"
        ])
        ids = parse_sap_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=ids[0])
        assert row.quantity == Decimal("-50")

    def test_row4_audit_log_written_even_when_flagged(self):
        csv_io = _make_csv([
            "IN02,FUL-004,-50,L,20241001,20241005,Return"
        ])
        ids = parse_sap_file(csv_io, self.client_obj.pk, self.user_id)
        assert AuditLog.objects.filter(activity_row_id=ids[0]).count() == 1

    # ------------------------------------------------------------------
    # Multi-row batch
    # ------------------------------------------------------------------

    def test_all_four_rows_batch(self):
        csv_io = _make_csv([
            "IN01,FUL-001,500,L,20241231,20250103,Diesel Mumbai",
            "DE07,FUL-002,132.5,GAL,31.12.2024,,Petrol Frankfurt",
            "XX99,FUL-003,200,L,2024-11-15,2024-11-20,Test",
            "IN02,FUL-004,-50,L,20241001,20241005,Return",
        ])
        ids = parse_sap_file(csv_io, self.client_obj.pk, self.user_id)
        assert len(ids) == 4

        rows = ActivityRow.objects.filter(pk__in=ids).order_by("pk")
        statuses = [r.status for r in rows]
        flags = [r.is_flagged for r in rows]

        assert statuses[0] == ActivityRow.STATUS_PENDING   # Row 1 — clean
        assert statuses[1] == ActivityRow.STATUS_PENDING   # Row 2 — clean
        assert statuses[2] == ActivityRow.STATUS_FLAGGED   # Row 3 — unknown plant
        assert statuses[3] == ActivityRow.STATUS_FLAGGED   # Row 4 — negative qty

        assert flags == [False, False, True, True]

        # 4 RawUploads, 4 AuditLog entries
        assert RawUpload.objects.filter(client=self.client_obj).count() == 4
        assert AuditLog.objects.filter(client=self.client_obj).count() == 4

    # ------------------------------------------------------------------
    # Hard constraint guards
    # ------------------------------------------------------------------

    def test_locked_activity_row_cannot_be_edited(self):
        csv_io = _make_csv([
            "IN01,FUL-001,500,L,20241231,20250103,Diesel Mumbai"
        ])
        ids = parse_sap_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=ids[0])

        # Manually force to LOCKED (simulating approval flow)
        ActivityRow.objects.filter(pk=row.pk).update(status=ActivityRow.STATUS_LOCKED)

        row.refresh_from_db()
        row.description = "tampered description"
        with pytest.raises(ValueError, match="LOCKED"):
            row.save()

    def test_audit_log_entry_cannot_be_updated(self):
        csv_io = _make_csv([
            "IN01,FUL-001,500,L,20241231,20250103,Diesel Mumbai"
        ])
        ids = parse_sap_file(csv_io, self.client_obj.pk, self.user_id)
        log = AuditLog.objects.filter(activity_row_id=ids[0]).first()
        log.detail = "tampered"
        with pytest.raises(ValueError, match="immutable"):
            log.save()

    def test_audit_log_entry_cannot_be_deleted(self):
        csv_io = _make_csv([
            "IN01,FUL-001,500,L,20241231,20250103,Diesel Mumbai"
        ])
        ids = parse_sap_file(csv_io, self.client_obj.pk, self.user_id)
        log = AuditLog.objects.filter(activity_row_id=ids[0]).first()
        with pytest.raises(ValueError, match="cannot be deleted"):
            log.delete()

    def test_multi_tenancy_client_fk_set(self):
        csv_io = _make_csv([
            "IN01,FUL-001,500,L,20241231,20250103,Diesel Mumbai"
        ])
        ids = parse_sap_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=ids[0])
        assert row.client_id == self.client_obj.pk
        assert row.raw_upload.client_id == self.client_obj.pk
        log = AuditLog.objects.filter(activity_row_id=ids[0]).first()
        assert log.client_id == self.client_obj.pk
