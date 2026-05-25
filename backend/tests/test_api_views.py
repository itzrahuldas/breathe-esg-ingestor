"""
tests/test_api_views.py
=========================
Integration tests for all Phase 3 API endpoints.

Endpoints tested:
  POST   /api/upload/
  GET    /api/rows/
  PATCH  /api/rows/{id}/approve/
  PATCH  /api/rows/{id}/reject/
  POST   /api/rows/bulk-approve/
  GET    /api/summary/
  GET    /api/audit-log/
"""

import io
from datetime import date, timezone
from decimal import Decimal

import pytest
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from ingestor.models import ActivityRow, AuditLog, Client, PlantCode, RawUpload


# ===========================================================================
# Helpers
# ===========================================================================

SAP_CSV = (
    "WERKS,MATNR,MENGE,MEINS,BLDAT,BUDAT,BKTXT\r\n"
    "IN01,FUL-001,500,L,20241231,20250103,Diesel Mumbai\r\n"
    "XX99,FUL-003,200,L,2024-11-15,2024-11-20,Test\r\n"
)

UTILITY_CSV = (
    "meter_id,site_name,bill_from,bill_to,consumption,unit,read_type,amount\r\n"
    "MTR-0042,Mumbai Office,18/01/2024,21/02/2024,3660,kWh,A,54900\r\n"
)

TRAVEL_CSV = (
    "booking_id,travel_type,origin,destination,"
    "travel_date,return_date,cabin_class,"
    "hotel_nights,hotel_city,ground_km,transport_mode\r\n"
    "TRV-001,FLIGHT,BOM,DEL,2024-01-15,2024-01-17,ECONOMY,,,,\r\n"
    "TRV-003,HOTEL,,,2024-01-15,2024-01-17,,2,New Delhi,,\r\n"
)


def _csv_file(content: str, name: str = "test.csv") -> io.BytesIO:
    buf = io.BytesIO(content.encode("utf-8"))
    buf.name = name
    return buf


class ApiTestBase(TestCase):
    """Shared setUp: client tenant, plant code, API client."""

    def setUp(self):
        self.tenant = Client.objects.create(name="Acme", slug="acme-api")
        PlantCode.objects.create(
            client=self.tenant, code="IN01", site_name="Mumbai Plant", country="IN"
        )
        self.api = APIClient()

    def _upload(self, csv_content: str, source_type: str) -> dict:
        resp = self.api.post(
            "/api/upload/",
            data={
                "file": _csv_file(csv_content),
                "source_type": source_type,
                "client_id": self.tenant.pk,
            },
            format="multipart",
        )
        return resp

    def _first_row(self) -> ActivityRow:
        return ActivityRow.objects.filter(client=self.tenant).order_by("pk").first()


# ===========================================================================
# POST /api/upload/
# ===========================================================================


class TestUploadView(ApiTestBase):

    def test_sap_upload_200(self):
        resp = self._upload(SAP_CSV, "SAP")
        self.assertEqual(resp.status_code, 200)

    def test_sap_upload_response_shape(self):
        resp = self._upload(SAP_CSV, "SAP")
        data = resp.json()
        self.assertIn("upload_id", data)
        self.assertIn("rows_created", data)
        self.assertIn("rows_flagged", data)

    def test_sap_rows_created_count(self):
        resp = self._upload(SAP_CSV, "SAP")
        self.assertEqual(resp.json()["rows_created"], 2)

    def test_sap_flagged_count(self):
        """XX99 plant is unknown → 1 flagged row."""
        resp = self._upload(SAP_CSV, "SAP")
        self.assertEqual(resp.json()["rows_flagged"], 1)

    def test_utility_upload_200(self):
        resp = self._upload(UTILITY_CSV, "UTILITY")
        self.assertEqual(resp.status_code, 200)

    def test_utility_splits_to_two_rows(self):
        """18 Jan – 21 Feb spans 2 months → 2 ActivityRows."""
        resp = self._upload(UTILITY_CSV, "UTILITY")
        self.assertEqual(resp.json()["rows_created"], 2)

    def test_travel_upload_200(self):
        resp = self._upload(TRAVEL_CSV, "TRAVEL")
        self.assertEqual(resp.status_code, 200)

    def test_travel_rows_created(self):
        resp = self._upload(TRAVEL_CSV, "TRAVEL")
        self.assertEqual(resp.json()["rows_created"], 2)

    def test_invalid_source_type_400(self):
        resp = self._upload(SAP_CSV, "ORACLE")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("source_type", resp.json()["error"].lower())

    def test_missing_file_400(self):
        resp = self.api.post(
            "/api/upload/",
            data={"source_type": "SAP", "client_id": self.tenant.pk},
            format="multipart",
        )
        self.assertEqual(resp.status_code, 400)

    def test_invalid_client_id_400(self):
        resp = self.api.post(
            "/api/upload/",
            data={
                "file": _csv_file(SAP_CSV),
                "source_type": "SAP",
                "client_id": 999999,
            },
            format="multipart",
        )
        self.assertEqual(resp.status_code, 400)

    def test_non_integer_client_id_400(self):
        resp = self.api.post(
            "/api/upload/",
            data={
                "file": _csv_file(SAP_CSV),
                "source_type": "SAP",
                "client_id": "not-an-int",
            },
            format="multipart",
        )
        self.assertEqual(resp.status_code, 400)


# ===========================================================================
# GET /api/rows/
# ===========================================================================


class TestRowListView(ApiTestBase):

    def setUp(self):
        super().setUp()
        self._upload(SAP_CSV, "SAP")   # 2 rows: 1 PENDING, 1 FLAGGED

    def test_list_returns_200(self):
        resp = self.api.get(f"/api/rows/?client_id={self.tenant.pk}")
        self.assertEqual(resp.status_code, 200)

    def test_list_contains_results_key(self):
        resp = self.api.get(f"/api/rows/?client_id={self.tenant.pk}")
        self.assertIn("results", resp.json())

    def test_list_returns_two_rows(self):
        resp = self.api.get(f"/api/rows/?client_id={self.tenant.pk}")
        self.assertEqual(resp.json()["count"], 2)

    def test_row_shape_has_required_fields(self):
        resp = self.api.get(f"/api/rows/?client_id={self.tenant.pk}")
        row = resp.json()["results"][0]
        for field in [
            "id", "scope", "category", "site_name", "activity_date_start",
            "activity_date_end", "quantity", "unit", "kgco2e",
            "status", "is_flagged", "flag_reason", "source_type",
        ]:
            self.assertIn(field, row, f"Missing field: {field}")

    def test_source_type_in_row(self):
        resp = self.api.get(f"/api/rows/?client_id={self.tenant.pk}")
        row = resp.json()["results"][0]
        self.assertEqual(row["source_type"], "sap_csv")

    def test_filter_by_scope(self):
        resp = self.api.get(f"/api/rows/?client_id={self.tenant.pk}&scope=1")
        for row in resp.json()["results"]:
            self.assertEqual(row["scope"], 1)

    def test_filter_by_status_flagged(self):
        resp = self.api.get(f"/api/rows/?client_id={self.tenant.pk}&status=FLAGGED")
        for row in resp.json()["results"]:
            self.assertEqual(row["status"], "FLAGGED")

    def test_filter_by_is_flagged_true(self):
        resp = self.api.get(f"/api/rows/?client_id={self.tenant.pk}&is_flagged=true")
        data = resp.json()
        self.assertGreater(data["count"], 0)
        for row in data["results"]:
            self.assertTrue(row["is_flagged"])

    def test_filter_by_is_flagged_false(self):
        resp = self.api.get(f"/api/rows/?client_id={self.tenant.pk}&is_flagged=false")
        for row in resp.json()["results"]:
            self.assertFalse(row["is_flagged"])

    def test_filter_by_source_type(self):
        self._upload(TRAVEL_CSV, "TRAVEL")
        resp = self.api.get(f"/api/rows/?client_id={self.tenant.pk}&source_type=SAP")
        for row in resp.json()["results"]:
            self.assertEqual(row["source_type"], "sap_csv")

    def test_pagination_count_present(self):
        resp = self.api.get(f"/api/rows/?client_id={self.tenant.pk}")
        self.assertIn("count", resp.json())
        self.assertIn("next", resp.json())


# ===========================================================================
# PATCH /api/rows/{id}/approve/
# ===========================================================================


class TestApproveRowView(ApiTestBase):

    def setUp(self):
        super().setUp()
        self._upload(SAP_CSV, "SAP")
        self.row = ActivityRow.objects.filter(
            client=self.tenant, is_flagged=False
        ).first()

    def test_approve_returns_200(self):
        resp = self.api.patch(f"/api/rows/{self.row.pk}/approve/")
        self.assertEqual(resp.status_code, 200)

    def test_approve_sets_status_locked(self):
        self.api.patch(f"/api/rows/{self.row.pk}/approve/")
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, ActivityRow.STATUS_LOCKED)

    def test_approve_writes_audit_log(self):
        self.api.patch(f"/api/rows/{self.row.pk}/approve/")
        logs = AuditLog.objects.filter(
            activity_row=self.row, action=AuditLog.ACTION_APPROVED
        )
        self.assertEqual(logs.count(), 1)

    def test_approve_audit_log_has_before_after(self):
        self.api.patch(f"/api/rows/{self.row.pk}/approve/")
        log = AuditLog.objects.get(activity_row=self.row, action=AuditLog.ACTION_APPROVED)
        self.assertEqual(log.before_value["status"], ActivityRow.STATUS_PENDING)
        self.assertEqual(log.after_value["status"], ActivityRow.STATUS_LOCKED)

    def test_approve_already_locked_400(self):
        self.api.patch(f"/api/rows/{self.row.pk}/approve/")
        resp = self.api.patch(f"/api/rows/{self.row.pk}/approve/")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("LOCKED", resp.json()["error"])

    def test_approve_sets_reviewed_at(self):
        self.api.patch(f"/api/rows/{self.row.pk}/approve/")
        self.row.refresh_from_db()
        self.assertIsNotNone(self.row.reviewed_at)

    def test_approve_nonexistent_row_404(self):
        resp = self.api.patch("/api/rows/999999/approve/")
        self.assertEqual(resp.status_code, 404)

    def test_approve_rejected_row_400(self):
        """REJECTED rows cannot be approved."""
        ActivityRow.objects.filter(pk=self.row.pk).update(
            status=ActivityRow.STATUS_REJECTED
        )
        resp = self.api.patch(f"/api/rows/{self.row.pk}/approve/")
        self.assertEqual(resp.status_code, 400)


# ===========================================================================
# PATCH /api/rows/{id}/reject/
# ===========================================================================


class TestRejectRowView(ApiTestBase):

    def setUp(self):
        super().setUp()
        self._upload(SAP_CSV, "SAP")
        self.row = ActivityRow.objects.filter(client=self.tenant).first()

    def test_reject_returns_200(self):
        resp = self.api.patch(
            f"/api/rows/{self.row.pk}/reject/",
            data={"reason": "Wrong data"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)

    def test_reject_sets_status_rejected(self):
        self.api.patch(
            f"/api/rows/{self.row.pk}/reject/",
            data={"reason": "Wrong data"},
            format="json",
        )
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, ActivityRow.STATUS_REJECTED)

    def test_reject_writes_audit_log(self):
        self.api.patch(
            f"/api/rows/{self.row.pk}/reject/",
            data={"reason": "Bad values"},
            format="json",
        )
        logs = AuditLog.objects.filter(
            activity_row=self.row, action=AuditLog.ACTION_REJECTED
        )
        self.assertEqual(logs.count(), 1)

    def test_reject_audit_log_detail_contains_reason(self):
        self.api.patch(
            f"/api/rows/{self.row.pk}/reject/",
            data={"reason": "Duplicate entry"},
            format="json",
        )
        log = AuditLog.objects.get(activity_row=self.row, action=AuditLog.ACTION_REJECTED)
        self.assertIn("Duplicate entry", log.detail)

    def test_reject_locked_row_400(self):
        ActivityRow.objects.filter(pk=self.row.pk).update(
            status=ActivityRow.STATUS_LOCKED
        )
        resp = self.api.patch(
            f"/api/rows/{self.row.pk}/reject/",
            data={"reason": "Test"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("LOCKED", resp.json()["error"])

    def test_reject_missing_reason_400(self):
        resp = self.api.patch(
            f"/api/rows/{self.row.pk}/reject/",
            data={},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_reject_nonexistent_404(self):
        resp = self.api.patch(
            "/api/rows/999999/reject/",
            data={"reason": "gone"},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)


# ===========================================================================
# POST /api/rows/bulk-approve/
# ===========================================================================


class TestBulkApproveView(ApiTestBase):

    def setUp(self):
        super().setUp()
        self._upload(SAP_CSV, "SAP")        # 2 rows
        self._upload(TRAVEL_CSV, "TRAVEL")  # 2 more rows
        self.all_ids = list(
            ActivityRow.objects.filter(client=self.tenant).values_list("pk", flat=True)
        )

    def test_bulk_approve_returns_200(self):
        resp = self.api.post(
            "/api/rows/bulk-approve/",
            data={"row_ids": self.all_ids},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)

    def test_bulk_approve_response_shape(self):
        resp = self.api.post(
            "/api/rows/bulk-approve/",
            data={"row_ids": self.all_ids},
            format="json",
        )
        data = resp.json()
        self.assertIn("approved", data)
        self.assertIn("skipped", data)

    def test_bulk_approve_all_locked(self):
        self.api.post(
            "/api/rows/bulk-approve/",
            data={"row_ids": self.all_ids},
            format="json",
        )
        locked = ActivityRow.objects.filter(
            client=self.tenant, status=ActivityRow.STATUS_LOCKED
        ).count()
        self.assertEqual(locked, len(self.all_ids))

    def test_bulk_approve_skips_already_locked(self):
        # First bulk-approve
        self.api.post(
            "/api/rows/bulk-approve/",
            data={"row_ids": self.all_ids},
            format="json",
        )
        # Second time — all should be skipped
        resp = self.api.post(
            "/api/rows/bulk-approve/",
            data={"row_ids": self.all_ids},
            format="json",
        )
        data = resp.json()
        self.assertEqual(data["approved"], 0)
        self.assertEqual(data["skipped"], len(self.all_ids))

    def test_bulk_approve_audit_logs_written(self):
        self.api.post(
            "/api/rows/bulk-approve/",
            data={"row_ids": self.all_ids},
            format="json",
        )
        approve_logs = AuditLog.objects.filter(
            client=self.tenant, action=AuditLog.ACTION_APPROVED
        ).count()
        self.assertEqual(approve_logs, len(self.all_ids))

    def test_bulk_approve_nonexistent_ids_skipped(self):
        resp = self.api.post(
            "/api/rows/bulk-approve/",
            data={"row_ids": [999998, 999999]},
            format="json",
        )
        data = resp.json()
        self.assertEqual(data["approved"], 0)
        self.assertEqual(data["skipped"], 2)

    def test_bulk_approve_empty_list_400(self):
        resp = self.api.post(
            "/api/rows/bulk-approve/",
            data={"row_ids": []},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)


# ===========================================================================
# GET /api/summary/
# ===========================================================================


class TestSummaryView(ApiTestBase):

    def setUp(self):
        super().setUp()
        self._upload(SAP_CSV, "SAP")        # scope 1: 2 rows (1 pending, 1 flagged)
        self._upload(UTILITY_CSV, "UTILITY")  # scope 2: 2 rows (splits)
        self._upload(TRAVEL_CSV, "TRAVEL")    # scope 3: 2 rows

    def test_summary_200(self):
        resp = self.api.get(f"/api/summary/?client_id={self.tenant.pk}")
        self.assertEqual(resp.status_code, 200)

    def test_summary_shape(self):
        resp = self.api.get(f"/api/summary/?client_id={self.tenant.pk}")
        data = resp.json()
        for key in [
            "total_rows", "pending_review", "flagged",
            "approved", "rejected", "total_kgco2e", "scope_breakdown",
        ]:
            self.assertIn(key, data)

    def test_summary_scope_breakdown_keys(self):
        resp = self.api.get(f"/api/summary/?client_id={self.tenant.pk}")
        breakdown = resp.json()["scope_breakdown"]
        self.assertIn("1", breakdown)
        self.assertIn("2", breakdown)
        self.assertIn("3", breakdown)

    def test_summary_total_rows(self):
        resp = self.api.get(f"/api/summary/?client_id={self.tenant.pk}")
        # SAP→2, UTILITY→2 (split), TRAVEL→2
        self.assertEqual(resp.json()["total_rows"], 6)

    def test_summary_flagged_count(self):
        """Only XX99 SAP row is flagged."""
        resp = self.api.get(f"/api/summary/?client_id={self.tenant.pk}")
        self.assertGreaterEqual(resp.json()["flagged"], 1)

    def test_summary_approved_increments_after_approve(self):
        row = ActivityRow.objects.filter(client=self.tenant, is_flagged=False).first()
        self.api.patch(f"/api/rows/{row.pk}/approve/")
        resp = self.api.get(f"/api/summary/?client_id={self.tenant.pk}")
        self.assertGreaterEqual(resp.json()["approved"], 1)

    def test_summary_missing_client_id_400(self):
        resp = self.api.get("/api/summary/")
        self.assertEqual(resp.status_code, 400)

    def test_summary_total_kgco2e_is_float(self):
        resp = self.api.get(f"/api/summary/?client_id={self.tenant.pk}")
        self.assertIsInstance(resp.json()["total_kgco2e"], float)

    def test_summary_scope_1_positive(self):
        resp = self.api.get(f"/api/summary/?client_id={self.tenant.pk}")
        self.assertGreater(resp.json()["scope_breakdown"]["1"], 0)

    def test_summary_scope_2_positive(self):
        resp = self.api.get(f"/api/summary/?client_id={self.tenant.pk}")
        self.assertGreater(resp.json()["scope_breakdown"]["2"], 0)


# ===========================================================================
# GET /api/audit-log/
# ===========================================================================


class TestAuditLogView(ApiTestBase):

    def setUp(self):
        super().setUp()
        self._upload(SAP_CSV, "SAP")

    def test_audit_log_200(self):
        resp = self.api.get(f"/api/audit-log/?client_id={self.tenant.pk}")
        self.assertEqual(resp.status_code, 200)

    def test_audit_log_has_results(self):
        resp = self.api.get(f"/api/audit-log/?client_id={self.tenant.pk}")
        self.assertGreater(resp.json()["count"], 0)

    def test_audit_log_entry_shape(self):
        resp = self.api.get(f"/api/audit-log/?client_id={self.tenant.pk}")
        entry = resp.json()["results"][0]
        for field in ["id", "activity_row_id", "actor_id", "action", "timestamp"]:
            self.assertIn(field, entry)

    def test_audit_log_filter_by_row_id(self):
        row = ActivityRow.objects.filter(client=self.tenant).first()
        resp = self.api.get(
            f"/api/audit-log/?client_id={self.tenant.pk}&row_id={row.pk}"
        )
        for entry in resp.json()["results"]:
            self.assertEqual(entry["activity_row_id"], row.pk)

    def test_audit_log_includes_approve_entry_after_approve(self):
        row = ActivityRow.objects.filter(client=self.tenant, is_flagged=False).first()
        self.api.patch(f"/api/rows/{row.pk}/approve/")
        resp = self.api.get(
            f"/api/audit-log/?client_id={self.tenant.pk}&row_id={row.pk}"
        )
        actions = [e["action"] for e in resp.json()["results"]]
        self.assertIn("APPROVED", actions)

    def test_audit_log_includes_before_after_on_approve(self):
        row = ActivityRow.objects.filter(client=self.tenant, is_flagged=False).first()
        self.api.patch(f"/api/rows/{row.pk}/approve/")
        resp = self.api.get(
            f"/api/audit-log/?client_id={self.tenant.pk}&row_id={row.pk}"
        )
        approve_entries = [
            e for e in resp.json()["results"] if e["action"] == "APPROVED"
        ]
        self.assertEqual(len(approve_entries), 1)
        entry = approve_entries[0]
        self.assertIsNotNone(entry["before_value"])
        self.assertIsNotNone(entry["after_value"])
        self.assertEqual(entry["after_value"]["status"], "LOCKED")

    def test_audit_log_missing_client_id_400(self):
        resp = self.api.get("/api/audit-log/")
        self.assertEqual(resp.status_code, 400)

    def test_audit_log_ordered_by_timestamp_desc(self):
        """Newest entries come first."""
        row = ActivityRow.objects.filter(client=self.tenant, is_flagged=False).first()
        self.api.patch(f"/api/rows/{row.pk}/approve/")
        resp = self.api.get(f"/api/audit-log/?client_id={self.tenant.pk}")
        timestamps = [e["timestamp"] for e in resp.json()["results"]]
        self.assertEqual(timestamps, sorted(timestamps, reverse=True))
