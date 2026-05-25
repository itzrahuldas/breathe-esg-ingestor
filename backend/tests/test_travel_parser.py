"""
tests/test_travel_parser.py
=============================
Unit tests for ingestor/parsers/travel_parser.py

Coverage:
  Pure-function tests (no DB):
    - haversine_km: known pairs (BOM-DEL, DEL-LHR, same point)
    - get_flight_distance_km: known pairs + unknown IATA raises
    - is_return_trip: populated date, blank, unparseable

  Integration tests (Django DB):
    - Row 1 — economy domestic flight (BOM→DEL), return trip
    - Row 2 — business class international (DEL→LHR), return trip
    - Row 3 — hotel stay, 2 nights
    - Row 4 — taxi ground transport, 148 km
    - Unknown IATA code → FLAGGED
    - Unknown cabin class → FLAGGED
    - Unknown transport_mode → FLAGGED
    - Zero hotel_nights → FLAGGED
    - Zero ground_km → FLAGGED
    - All rows scope=3
    - AuditLog written per row
    - RawUpload.raw_payload immutability
    - Multi-tenancy client FK
"""

import io
import math
from datetime import date
from decimal import Decimal

import pytest
from django.test import TestCase

from ingestor.models import ActivityRow, AuditLog, Client, RawUpload
from ingestor.parsers.travel_parser import (
    AIRPORT_COORDS,
    FLIGHT_BUSINESS_KM,
    FLIGHT_ECONOMY_KM,
    HOTEL_NIGHT,
    RAIL_KM,
    TAXI_KM,
    get_flight_distance_km,
    haversine_km,
    is_return_trip,
    parse_travel_file,
)


# ===========================================================================
# Helpers
# ===========================================================================

HEADER = (
    "booking_id,travel_type,origin,destination,"
    "travel_date,return_date,cabin_class,"
    "hotel_nights,hotel_city,ground_km,transport_mode"
)


def _make_csv(rows: list[str]) -> io.StringIO:
    body = "\n".join([HEADER] + rows)
    return io.StringIO(body)


# ===========================================================================
# Pure-function tests
# ===========================================================================


class TestHaversineKm:
    """haversine_km — geometric correctness."""

    def test_same_point_is_zero(self):
        coord = AIRPORT_COORDS["BOM"]
        assert haversine_km(coord, coord) == pytest.approx(0.0, abs=1e-9)

    def test_bom_del_approx(self):
        """BOM→DEL: roughly 1150 km straight-line."""
        dist = haversine_km(AIRPORT_COORDS["BOM"], AIRPORT_COORDS["DEL"])
        assert 1100 < dist < 1250

    def test_del_lhr_approx(self):
        """DEL→LHR: roughly 6700 km."""
        dist = haversine_km(AIRPORT_COORDS["DEL"], AIRPORT_COORDS["LHR"])
        assert 6500 < dist < 6900

    def test_symmetry(self):
        """Distance A→B must equal B→A."""
        d1 = haversine_km(AIRPORT_COORDS["BOM"], AIRPORT_COORDS["LHR"])
        d2 = haversine_km(AIRPORT_COORDS["LHR"], AIRPORT_COORDS["BOM"])
        assert d1 == pytest.approx(d2, rel=1e-9)

    def test_known_coords_manually(self):
        """
        Validate formula against a hand-computed value.
        North Pole → Equator/Prime-Meridian should be ~10007 km (quarter-earth).
        """
        north_pole = (90.0, 0.0)
        equator    = (0.0, 0.0)
        dist = haversine_km(north_pole, equator)
        assert dist == pytest.approx(10007.5, abs=5)

    def test_returns_float(self):
        dist = haversine_km(AIRPORT_COORDS["BOM"], AIRPORT_COORDS["DEL"])
        assert isinstance(dist, float)


class TestGetFlightDistanceKm:
    """get_flight_distance_km — IATA lookup + haversine."""

    def test_bom_del_distance(self):
        dist = get_flight_distance_km("BOM", "DEL")
        assert 1100 < dist < 1250

    def test_del_lhr_distance(self):
        dist = get_flight_distance_km("DEL", "LHR")
        assert 6500 < dist < 6900

    def test_case_insensitive(self):
        dist1 = get_flight_distance_km("bom", "del")
        dist2 = get_flight_distance_km("BOM", "DEL")
        assert dist1 == pytest.approx(dist2)

    def test_unknown_origin_raises(self):
        with pytest.raises(ValueError, match="Unknown IATA code"):
            get_flight_distance_km("XYZ", "DEL")

    def test_unknown_destination_raises(self):
        with pytest.raises(ValueError, match="Unknown IATA code"):
            get_flight_distance_km("BOM", "ZZZ")

    def test_both_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown IATA code"):
            get_flight_distance_km("AAA", "BBB")

    def test_matches_direct_haversine(self):
        direct = haversine_km(AIRPORT_COORDS["BOM"], AIRPORT_COORDS["DEL"])
        via_fn = get_flight_distance_km("BOM", "DEL")
        assert via_fn == pytest.approx(direct)


class TestIsReturnTrip:
    """is_return_trip — return-date detection."""

    def test_valid_iso_date_is_return(self):
        assert is_return_trip("2024-01-17") is True

    def test_blank_string_is_not_return(self):
        assert is_return_trip("") is False

    def test_none_is_not_return(self):
        assert is_return_trip(None) is False

    def test_whitespace_is_not_return(self):
        assert is_return_trip("   ") is False

    def test_invalid_date_is_not_return(self):
        assert is_return_trip("not-a-date") is False

    def test_row1_return_date(self):
        """Row 1 spec: return_date=2024-01-17 → True"""
        assert is_return_trip("2024-01-17") is True

    def test_row2_return_date(self):
        """Row 2 spec: return_date=2024-02-10 → True"""
        assert is_return_trip("2024-02-10") is True

    def test_row4_no_return_date(self):
        """Row 4 spec: return_date= (blank) → False"""
        assert is_return_trip("") is False


# ===========================================================================
# Integration tests (Django TestCase)
# ===========================================================================


class TestParseTravelFile(TestCase):
    """parse_travel_file — full pipeline through DB."""

    def setUp(self):
        self.client_obj = Client.objects.create(
            name="Acme Corp", slug="acme-travel"
        )
        self.user_id = 99

    # ------------------------------------------------------------------
    # Row 1: economy domestic flight BOM→DEL, return trip
    # ------------------------------------------------------------------

    def test_row1_created(self):
        csv_io = _make_csv([
            "TRV-001,FLIGHT,BOM,DEL,2024-01-15,2024-01-17,ECONOMY,,,,",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        assert len(results) == 1

    def test_row1_not_flagged(self):
        csv_io = _make_csv([
            "TRV-001,FLIGHT,BOM,DEL,2024-01-15,2024-01-17,ECONOMY,,,,",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        assert results[0]["is_flagged"] is False

    def test_row1_return_doubles_distance(self):
        """Return trip → distance × 2 before applying EF."""
        one_way = get_flight_distance_km("BOM", "DEL")
        expected_co2e = round(one_way * 2 * FLIGHT_ECONOMY_KM, 4)
        csv_io = _make_csv([
            "TRV-001,FLIGHT,BOM,DEL,2024-01-15,2024-01-17,ECONOMY,,,,",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        assert results[0]["co2e_kg"] == pytest.approx(expected_co2e, rel=1e-4)

    def test_row1_quantity_is_return_distance(self):
        one_way = get_flight_distance_km("BOM", "DEL")
        csv_io = _make_csv([
            "TRV-001,FLIGHT,BOM,DEL,2024-01-15,2024-01-17,ECONOMY,,,,",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=results[0]["activity_row_id"])
        assert float(row.quantity) == pytest.approx(one_way * 2, rel=1e-4)

    def test_row1_scope_3(self):
        csv_io = _make_csv([
            "TRV-001,FLIGHT,BOM,DEL,2024-01-15,2024-01-17,ECONOMY,,,,",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=results[0]["activity_row_id"])
        assert row.scope == 3
        assert row.category == "business_travel_air"

    def test_row1_travel_date_stored(self):
        csv_io = _make_csv([
            "TRV-001,FLIGHT,BOM,DEL,2024-01-15,2024-01-17,ECONOMY,,,,",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=results[0]["activity_row_id"])
        assert row.document_date == date(2024, 1, 15)

    def test_row1_economy_emission_factor(self):
        csv_io = _make_csv([
            "TRV-001,FLIGHT,BOM,DEL,2024-01-15,2024-01-17,ECONOMY,,,,",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=results[0]["activity_row_id"])
        assert row.emission_factor == Decimal(str(FLIGHT_ECONOMY_KM))

    def test_row1_audit_log_written(self):
        csv_io = _make_csv([
            "TRV-001,FLIGHT,BOM,DEL,2024-01-15,2024-01-17,ECONOMY,,,,",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        logs = AuditLog.objects.filter(activity_row_id=results[0]["activity_row_id"])
        assert logs.count() == 1
        assert logs.first().action == AuditLog.ACTION_UPLOADED

    def test_row1_raw_payload_immutable(self):
        csv_io = _make_csv([
            "TRV-001,FLIGHT,BOM,DEL,2024-01-15,2024-01-17,ECONOMY,,,,",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=results[0]["activity_row_id"])
        ru = row.raw_upload
        assert ru.raw_payload["booking_id"] == "TRV-001"
        ru.raw_payload = {"tampered": True}
        with pytest.raises(ValueError):
            ru.save()

    # ------------------------------------------------------------------
    # Row 2: business class international DEL→LHR, return trip
    # ------------------------------------------------------------------

    def test_row2_business_class_ef(self):
        csv_io = _make_csv([
            "TRV-002,FLIGHT,DEL,LHR,2024-02-03,2024-02-10,BUSINESS,,,,",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=results[0]["activity_row_id"])
        assert row.emission_factor == Decimal(str(FLIGHT_BUSINESS_KM))

    def test_row2_return_trip_co2e(self):
        one_way = get_flight_distance_km("DEL", "LHR")
        expected = round(one_way * 2 * FLIGHT_BUSINESS_KM, 4)
        csv_io = _make_csv([
            "TRV-002,FLIGHT,DEL,LHR,2024-02-03,2024-02-10,BUSINESS,,,,",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        assert results[0]["co2e_kg"] == pytest.approx(expected, rel=1e-4)

    def test_row2_not_flagged(self):
        csv_io = _make_csv([
            "TRV-002,FLIGHT,DEL,LHR,2024-02-03,2024-02-10,BUSINESS,,,,",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        assert results[0]["is_flagged"] is False

    def test_row2_category_air(self):
        csv_io = _make_csv([
            "TRV-002,FLIGHT,DEL,LHR,2024-02-03,2024-02-10,BUSINESS,,,,",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=results[0]["activity_row_id"])
        assert row.category == "business_travel_air"

    # ------------------------------------------------------------------
    # Row 3: hotel stay, 2 nights
    # ------------------------------------------------------------------

    def test_row3_hotel_co2e(self):
        """2 nights × 31.0 kgCO2e = 62.0 kgCO2e"""
        csv_io = _make_csv([
            "TRV-003,HOTEL,,,2024-01-15,2024-01-17,,2,New Delhi,,",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        assert results[0]["co2e_kg"] == pytest.approx(62.0, rel=1e-6)

    def test_row3_hotel_emission_factor(self):
        csv_io = _make_csv([
            "TRV-003,HOTEL,,,2024-01-15,2024-01-17,,2,New Delhi,,",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=results[0]["activity_row_id"])
        assert row.emission_factor == Decimal(str(HOTEL_NIGHT))

    def test_row3_not_flagged(self):
        csv_io = _make_csv([
            "TRV-003,HOTEL,,,2024-01-15,2024-01-17,,2,New Delhi,,",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        assert results[0]["is_flagged"] is False

    def test_row3_category_hotel(self):
        csv_io = _make_csv([
            "TRV-003,HOTEL,,,2024-01-15,2024-01-17,,2,New Delhi,,",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=results[0]["activity_row_id"])
        assert row.category == "business_travel_hotel"
        assert row.scope == 3

    def test_row3_quantity_is_nights(self):
        csv_io = _make_csv([
            "TRV-003,HOTEL,,,2024-01-15,2024-01-17,,2,New Delhi,,",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=results[0]["activity_row_id"])
        assert row.quantity == Decimal("2")
        assert row.unit == "nights"

    # ------------------------------------------------------------------
    # Row 4: taxi ground transport, 148 km
    # ------------------------------------------------------------------

    def test_row4_taxi_co2e(self):
        """148 km × 0.149 = 22.052 kgCO2e"""
        expected = round(148 * TAXI_KM, 4)
        csv_io = _make_csv([
            "TRV-004,GROUND,Pune,Mumbai,2024-01-20,,,,,148,TAXI",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        assert results[0]["co2e_kg"] == pytest.approx(expected, rel=1e-6)

    def test_row4_taxi_emission_factor(self):
        csv_io = _make_csv([
            "TRV-004,GROUND,Pune,Mumbai,2024-01-20,,,,,148,TAXI",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=results[0]["activity_row_id"])
        assert row.emission_factor == Decimal(str(TAXI_KM))

    def test_row4_not_flagged(self):
        csv_io = _make_csv([
            "TRV-004,GROUND,Pune,Mumbai,2024-01-20,,,,,148,TAXI",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        assert results[0]["is_flagged"] is False

    def test_row4_category_ground(self):
        csv_io = _make_csv([
            "TRV-004,GROUND,Pune,Mumbai,2024-01-20,,,,,148,TAXI",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=results[0]["activity_row_id"])
        assert row.category == "business_travel_ground"
        assert row.scope == 3

    def test_row4_quantity_is_km(self):
        csv_io = _make_csv([
            "TRV-004,GROUND,Pune,Mumbai,2024-01-20,,,,,148,TAXI",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=results[0]["activity_row_id"])
        assert row.quantity == Decimal("148")
        assert row.unit == "km"

    # ------------------------------------------------------------------
    # Flag cases
    # ------------------------------------------------------------------

    def test_unknown_iata_flagged(self):
        csv_io = _make_csv([
            "TRV-X01,FLIGHT,ZZZ,DEL,2024-01-15,2024-01-17,ECONOMY,,,,",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        assert results[0]["is_flagged"] is True
        row = ActivityRow.objects.get(pk=results[0]["activity_row_id"])
        assert "Unknown IATA" in row.flag_reason

    def test_unknown_cabin_class_flagged(self):
        csv_io = _make_csv([
            "TRV-X02,FLIGHT,BOM,DEL,2024-01-15,2024-01-17,SUPERSONIC,,,,",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        assert results[0]["is_flagged"] is True
        row = ActivityRow.objects.get(pk=results[0]["activity_row_id"])
        assert "SUPERSONIC" in row.flag_reason

    def test_unknown_transport_mode_flagged(self):
        csv_io = _make_csv([
            "TRV-X03,GROUND,Mumbai,Pune,2024-01-20,,,,,148,RICKSHAW",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        assert results[0]["is_flagged"] is True
        row = ActivityRow.objects.get(pk=results[0]["activity_row_id"])
        assert "RICKSHAW" in row.flag_reason

    def test_zero_hotel_nights_flagged(self):
        csv_io = _make_csv([
            "TRV-X04,HOTEL,,,2024-01-15,2024-01-17,,0,Mumbai,,",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        assert results[0]["is_flagged"] is True

    def test_zero_ground_km_flagged(self):
        csv_io = _make_csv([
            "TRV-X05,GROUND,A,B,2024-01-20,,,,,0,TAXI",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        assert results[0]["is_flagged"] is True

    def test_rail_transport_mode(self):
        """RAIL uses RAIL_KM factor."""
        expected = round(200 * RAIL_KM, 4)
        csv_io = _make_csv([
            "TRV-R01,GROUND,Delhi,Agra,2024-03-01,,,,,200,RAIL",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        assert results[0]["is_flagged"] is False
        assert results[0]["co2e_kg"] == pytest.approx(expected, rel=1e-6)

    def test_one_way_flight_no_return(self):
        """No return_date → one-way distance only."""
        one_way = get_flight_distance_km("BOM", "DEL")
        expected = round(one_way * FLIGHT_ECONOMY_KM, 4)
        csv_io = _make_csv([
            "TRV-OW1,FLIGHT,BOM,DEL,2024-01-15,,ECONOMY,,,,",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        assert results[0]["co2e_kg"] == pytest.approx(expected, rel=1e-4)

    # ------------------------------------------------------------------
    # Multi-row batch
    # ------------------------------------------------------------------

    def test_all_four_spec_rows_batch(self):
        csv_io = _make_csv([
            "TRV-001,FLIGHT,BOM,DEL,2024-01-15,2024-01-17,ECONOMY,,,,",
            "TRV-002,FLIGHT,DEL,LHR,2024-02-03,2024-02-10,BUSINESS,,,,",
            "TRV-003,HOTEL,,,2024-01-15,2024-01-17,,2,New Delhi,,",
            "TRV-004,GROUND,Pune,Mumbai,2024-01-20,,,,,148,TAXI",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        assert len(results) == 4
        assert RawUpload.objects.filter(client=self.client_obj).count() == 4
        assert AuditLog.objects.filter(client=self.client_obj).count() == 4

    def test_all_four_scope_3(self):
        csv_io = _make_csv([
            "TRV-001,FLIGHT,BOM,DEL,2024-01-15,2024-01-17,ECONOMY,,,,",
            "TRV-002,FLIGHT,DEL,LHR,2024-02-03,2024-02-10,BUSINESS,,,,",
            "TRV-003,HOTEL,,,2024-01-15,2024-01-17,,2,New Delhi,,",
            "TRV-004,GROUND,Pune,Mumbai,2024-01-20,,,,,148,TAXI",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        for r in results:
            row = ActivityRow.objects.get(pk=r["activity_row_id"])
            assert row.scope == 3

    def test_all_four_not_flagged(self):
        csv_io = _make_csv([
            "TRV-001,FLIGHT,BOM,DEL,2024-01-15,2024-01-17,ECONOMY,,,,",
            "TRV-002,FLIGHT,DEL,LHR,2024-02-03,2024-02-10,BUSINESS,,,,",
            "TRV-003,HOTEL,,,2024-01-15,2024-01-17,,2,New Delhi,,",
            "TRV-004,GROUND,Pune,Mumbai,2024-01-20,,,,,148,TAXI",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        assert all(not r["is_flagged"] for r in results)

    # ------------------------------------------------------------------
    # Hard constraint guards
    # ------------------------------------------------------------------

    def test_audit_log_cannot_be_updated(self):
        csv_io = _make_csv([
            "TRV-001,FLIGHT,BOM,DEL,2024-01-15,2024-01-17,ECONOMY,,,,",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        log = AuditLog.objects.filter(
            activity_row_id=results[0]["activity_row_id"]
        ).first()
        log.detail = "tampered"
        with pytest.raises(ValueError, match="immutable"):
            log.save()

    def test_audit_log_cannot_be_deleted(self):
        csv_io = _make_csv([
            "TRV-001,FLIGHT,BOM,DEL,2024-01-15,2024-01-17,ECONOMY,,,,",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        log = AuditLog.objects.filter(
            activity_row_id=results[0]["activity_row_id"]
        ).first()
        with pytest.raises(ValueError, match="cannot be deleted"):
            log.delete()

    def test_multi_tenancy_client_fk_set(self):
        csv_io = _make_csv([
            "TRV-001,FLIGHT,BOM,DEL,2024-01-15,2024-01-17,ECONOMY,,,,",
        ])
        results = parse_travel_file(csv_io, self.client_obj.pk, self.user_id)
        row = ActivityRow.objects.get(pk=results[0]["activity_row_id"])
        assert row.client_id == self.client_obj.pk
        assert row.raw_upload.client_id == self.client_obj.pk
        log = AuditLog.objects.filter(
            activity_row_id=results[0]["activity_row_id"]
        ).first()
        assert log.client_id == self.client_obj.pk
