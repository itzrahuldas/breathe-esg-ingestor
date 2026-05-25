"""
ingestor/views.py
==================
DRF API views for Breathe ESG Ingestor Phase 3.

Endpoints implemented:
  POST   /api/upload/
  GET    /api/rows/
  PATCH  /api/rows/{id}/approve/
  PATCH  /api/rows/{id}/reject/
  POST   /api/rows/bulk-approve/
  GET    /api/summary/
  GET    /api/audit-log/
"""

import io
import uuid
import logging
from datetime import datetime, timezone

from django.db import transaction
from django.db.models import Q, Sum
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ActivityRow, AuditLog, Client, PlantCode, RawUpload
from .parsers.sap_parser import parse_sap_file
from .parsers.utility_parser import parse_utility_file
from .parsers.travel_parser import parse_travel_file
from .serializers import (
    ActivityRowSerializer,
    AuditLogSerializer,
    BulkApproveBodySerializer,
    RejectBodySerializer,
    SummarySerializer,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SOURCE_TYPE_MAP = {
    "SAP":     ("sap_csv",     parse_sap_file),
    "UTILITY": ("utility_csv", parse_utility_file),
    "TRAVEL":  ("travel_csv",  parse_travel_file),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _actor_id(request: Request) -> int:
    """Return the user pk, or 0 for anonymous/unauthenticated."""
    user = getattr(request, "user", None)
    if user and user.is_authenticated:
        return user.pk
    return 0


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _approve_and_lock(row: ActivityRow, actor_id: int, client: Client) -> None:
    """
    Approve then immediately lock a single ActivityRow in one save cycle.

    Writes a single AuditLog entry capturing the full APPROVED→LOCKED
    transition so the spec's 'write before_value/after_value' requirement
    is satisfied in one atomic step.
    """
    before = {"status": row.status}
    now = _now()

    # Use queryset.update() to bypass the LOCKED guard in save() for the
    # two-step transition — we're doing it in one shot here.
    ActivityRow.objects.filter(pk=row.pk).update(
        status=ActivityRow.STATUS_LOCKED,
        reviewed_by_id=actor_id,
        reviewed_at=now,
    )

    AuditLog.objects.create(
        client=client,
        activity_row=row,
        actor_id=actor_id,
        action=AuditLog.ACTION_APPROVED,
        detail="Row approved and locked.",
        before_value=before,
        after_value={"status": ActivityRow.STATUS_LOCKED},
    )


# ===========================================================================
# POST /api/upload/
# ===========================================================================

class UploadView(APIView):
    """
    Accept a CSV file and route it to the correct parser.

    Request (multipart/form-data):
      file        — the CSV file
      source_type — 'SAP' | 'UTILITY' | 'TRAVEL'
      client_id   — integer PK of the Client tenant

    Response 200:
      { "upload_id": "<uuid>", "rows_created": <int>, "rows_flagged": <int> }
    """

    parser_classes = [MultiPartParser, FormParser]

    def post(self, request: Request) -> Response:
        source_type = (request.data.get("source_type") or "").strip().upper()
        client_id_raw = request.data.get("client_id", "")
        uploaded_file = request.FILES.get("file")

        # --- Validation ---
        if not uploaded_file:
            return Response(
                {"error": "No file provided. Include a 'file' field."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if source_type not in _SOURCE_TYPE_MAP:
            return Response(
                {
                    "error": (
                        f"Invalid source_type '{source_type}'. "
                        "Must be one of: SAP, UTILITY, TRAVEL."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            client_id = int(client_id_raw)
        except (TypeError, ValueError):
            return Response(
                {"error": f"client_id must be an integer, got '{client_id_raw}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Auto-create the demo client if it doesn't exist yet
        # (handles cold-start on Render before seed_mock_data has run)
        client, _ = Client.objects.get_or_create(
            pk=client_id,
            defaults={"name": "Breathe Demo Corp", "slug": "breathe-demo-corp"},
        )

        # --- Parse ---
        _, parser_fn = _SOURCE_TYPE_MAP[source_type]

        try:
            text_file = io.TextIOWrapper(uploaded_file, encoding="utf-8-sig")
            result = parser_fn(text_file, client.pk, _actor_id(request))
        except Exception as exc:  # noqa: BLE001
            logger.exception("CSV parse failed for source_type=%s", source_type)
            return Response(
                {"error": f"CSV parse failed: {exc}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Normalise result: sap_parser returns list[int], others return list[dict]
        if result and isinstance(result[0], dict):
            row_ids = [r["activity_row_id"] for r in result]
        else:
            row_ids = list(result)

        flagged_count = ActivityRow.objects.filter(
            pk__in=row_ids, is_flagged=True
        ).count()

        return Response(
            {
                "upload_id": str(uuid.uuid4()),  # logical batch ID for this upload
                "rows_created": len(row_ids),
                "rows_flagged": flagged_count,
            },
            status=status.HTTP_200_OK,
        )


# ===========================================================================
# GET /api/rows/
# ===========================================================================

class RowListView(APIView):
    """
    Return a paginated, filtered list of ActivityRow objects.

    Query params (all optional):
      source_type  — 'SAP' | 'UTILITY' | 'TRAVEL'
      scope        — 1 | 2 | 3
      status       — PENDING | FLAGGED | APPROVED | LOCKED | REJECTED
      is_flagged   — 'true' | 'false'
      date_from    — YYYY-MM-DD (filters on document_date)
      date_to      — YYYY-MM-DD (filters on document_date)
      client_id    — integer PK
    """

    def get(self, request: Request) -> Response:
        qs = ActivityRow.objects.select_related("raw_upload").all()

        # --- Filters ---
        client_id = request.query_params.get("client_id")
        if client_id:
            qs = qs.filter(client_id=client_id)

        source_type = (request.query_params.get("source_type") or "").upper()
        if source_type and source_type in _SOURCE_TYPE_MAP:
            source_system, _ = _SOURCE_TYPE_MAP[source_type]
            qs = qs.filter(raw_upload__source_system=source_system)

        scope = request.query_params.get("scope")
        if scope:
            qs = qs.filter(scope=scope)

        row_status = request.query_params.get("status")
        if row_status:
            qs = qs.filter(status=row_status.upper())

        is_flagged = request.query_params.get("is_flagged")
        if is_flagged is not None:
            qs = qs.filter(is_flagged=(is_flagged.lower() == "true"))

        date_from = request.query_params.get("date_from")
        if date_from:
            qs = qs.filter(document_date__gte=date_from)

        date_to = request.query_params.get("date_to")
        if date_to:
            qs = qs.filter(document_date__lte=date_to)

        # --- Pagination ---
        paginator = PageNumberPagination()
        paginator.page_size = 50
        page = paginator.paginate_queryset(qs, request)
        serializer = ActivityRowSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


# ===========================================================================
# PATCH /api/rows/{id}/approve/
# ===========================================================================

class ApproveRowView(APIView):
    """
    Approve and immediately lock a single ActivityRow.

    Transition: any non-LOCKED status → LOCKED
    AuditLog: before_value={'status': <old>}, after_value={'status': 'LOCKED'}
    Error 400: if row is already LOCKED or REJECTED
    """

    def patch(self, request: Request, pk: int) -> Response:
        try:
            row = ActivityRow.objects.select_related("client").get(pk=pk)
        except ActivityRow.DoesNotExist:
            return Response(
                {"error": f"ActivityRow {pk} not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if row.status in (ActivityRow.STATUS_LOCKED, ActivityRow.STATUS_REJECTED):
            return Response(
                {"error": f"Row {pk} is already {row.status} and cannot be approved."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        actor = _actor_id(request)
        with transaction.atomic():
            _approve_and_lock(row, actor, row.client)

        row.refresh_from_db()
        return Response(ActivityRowSerializer(row).data, status=status.HTTP_200_OK)


# ===========================================================================
# PATCH /api/rows/{id}/reject/
# ===========================================================================

class RejectRowView(APIView):
    """
    Reject an ActivityRow with a mandatory reason.

    Body: { "reason": "<string>" }
    Error 400: if row is already LOCKED
    """

    def patch(self, request: Request, pk: int) -> Response:
        body = RejectBodySerializer(data=request.data)
        if not body.is_valid():
            return Response(body.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            row = ActivityRow.objects.select_related("client").get(pk=pk)
        except ActivityRow.DoesNotExist:
            return Response(
                {"error": f"ActivityRow {pk} not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if row.status == ActivityRow.STATUS_LOCKED:
            return Response(
                {"error": f"Row {pk} is LOCKED and cannot be rejected."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        reason = body.validated_data["reason"]
        before = {"status": row.status}
        actor = _actor_id(request)

        with transaction.atomic():
            ActivityRow.objects.filter(pk=pk).update(
                status=ActivityRow.STATUS_REJECTED,
                reviewed_by_id=actor,
                reviewed_at=_now(),
            )
            AuditLog.objects.create(
                client=row.client,
                activity_row=row,
                actor_id=actor,
                action=AuditLog.ACTION_REJECTED,
                detail=reason,
                before_value=before,
                after_value={"status": ActivityRow.STATUS_REJECTED},
            )

        row.refresh_from_db()
        return Response(ActivityRowSerializer(row).data, status=status.HTTP_200_OK)


# ===========================================================================
# POST /api/rows/bulk-approve/
# ===========================================================================

class BulkApproveView(APIView):
    """
    Approve and lock a batch of ActivityRows in a single DB transaction.

    Body:    { "row_ids": [<int>, ...] }
    Response: { "approved": <int>, "skipped": <int> }

    Rows already LOCKED or REJECTED are silently skipped (counted in 'skipped').
    """

    def post(self, request: Request) -> Response:
        body = BulkApproveBodySerializer(data=request.data)
        if not body.is_valid():
            return Response(body.errors, status=status.HTTP_400_BAD_REQUEST)

        row_ids = body.validated_data["row_ids"]
        actor = _actor_id(request)
        approved = 0
        skipped = 0

        with transaction.atomic():
            rows = ActivityRow.objects.select_related("client").filter(pk__in=row_ids)
            found_ids = set(rows.values_list("pk", flat=True))

            # Count IDs that weren't found at all as skipped
            skipped += len(set(row_ids) - found_ids)

            for row in rows:
                if row.status in (ActivityRow.STATUS_LOCKED, ActivityRow.STATUS_REJECTED):
                    skipped += 1
                    continue
                _approve_and_lock(row, actor, row.client)
                approved += 1

        return Response(
            {"approved": approved, "skipped": skipped},
            status=status.HTTP_200_OK,
        )


# ===========================================================================
# GET /api/summary/
# ===========================================================================

class SummaryView(APIView):
    """
    Aggregate summary for a client.

    Query param: client_id (required)

    Response:
      {
        "total_rows": int,
        "pending_review": int,
        "flagged": int,
        "approved": int,
        "rejected": int,
        "total_kgco2e": float,
        "scope_breakdown": {"1": float, "2": float, "3": float}
      }
    """

    def get(self, request: Request) -> Response:
        client_id = request.query_params.get("client_id")
        if not client_id:
            return Response(
                {"error": "client_id query parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        qs = ActivityRow.objects.filter(client_id=client_id)

        total_rows = qs.count()
        pending_review = qs.filter(
            status__in=[ActivityRow.STATUS_PENDING, ActivityRow.STATUS_REVIEWED]
        ).count()
        flagged = qs.filter(is_flagged=True).count()
        approved = qs.filter(
            status__in=[ActivityRow.STATUS_APPROVED, ActivityRow.STATUS_LOCKED]
        ).count()
        rejected = qs.filter(status=ActivityRow.STATUS_REJECTED).count()

        total_kgco2e = float(
            qs.aggregate(total=Sum("co2e_kg"))["total"] or 0
        )

        scope_breakdown: dict[str, float] = {}
        for scope_val in (1, 2, 3):
            scope_total = qs.filter(scope=scope_val).aggregate(
                total=Sum("co2e_kg")
            )["total"]
            scope_breakdown[str(scope_val)] = float(scope_total or 0)

        data = {
            "total_rows": total_rows,
            "pending_review": pending_review,
            "flagged": flagged,
            "approved": approved,
            "rejected": rejected,
            "total_kgco2e": total_kgco2e,
            "scope_breakdown": scope_breakdown,
        }
        serializer = SummarySerializer(data)
        return Response(serializer.data, status=status.HTTP_200_OK)


# ===========================================================================
# GET /api/audit-log/
# ===========================================================================

class AuditLogView(APIView):
    """
    Return AuditLog entries, newest first.

    Query params:
      client_id — required
      row_id    — optional, filter by ActivityRow pk
    """

    def get(self, request: Request) -> Response:
        client_id = request.query_params.get("client_id")
        if not client_id:
            return Response(
                {"error": "client_id query parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        qs = AuditLog.objects.filter(client_id=client_id).order_by("-timestamp")

        row_id = request.query_params.get("row_id")
        if row_id:
            qs = qs.filter(activity_row_id=row_id)

        paginator = PageNumberPagination()
        paginator.page_size = 100
        page = paginator.paginate_queryset(qs, request)
        serializer = AuditLogSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


# ===========================================================================
# GET /api/setup/  — one-time demo bootstrap (idempotent)
# ===========================================================================

_DEMO_PLANT_CODES = [
    {"code": "IN01", "site_name": "Mumbai Plant",     "country": "IN"},
    {"code": "IN02", "site_name": "Pune Factory",     "country": "IN"},
    {"code": "IN03", "site_name": "Chennai Plant",    "country": "IN"},
    {"code": "IN04", "site_name": "Hyderabad Campus", "country": "IN"},
    {"code": "DE07", "site_name": "Frankfurt Office", "country": "DE"},
]


class SetupView(APIView):
    """
    Idempotent bootstrap endpoint. Creates the demo Client (pk=1) and
    PlantCode reference rows if they don't already exist.

    GET /api/setup/
    Safe to call repeatedly - never overwrites existing data.
    """

    def get(self, request: Request) -> Response:
        client, client_created = Client.objects.get_or_create(
            pk=1,
            defaults={"name": "Breathe Demo Corp", "slug": "breathe-demo-corp"},
        )

        pc_created = 0
        for pc in _DEMO_PLANT_CODES:
            _, created = PlantCode.objects.get_or_create(
                client=client,
                code=pc["code"],
                defaults={"site_name": pc["site_name"], "country": pc["country"]},
            )
            if created:
                pc_created += 1

        return Response({
            "client_id": client.pk,
            "client_name": client.name,
            "client_created": client_created,
            "plant_codes_created": pc_created,
            "message": "Demo client ready. You can now upload CSV files.",
        }, status=status.HTTP_200_OK)
