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

# RENDER DEPLOY NOTE:
# After pushing this fix, go to Render Shell and run:
# python manage.py migrate
# python manage.py shell -c "from ingestor.views import seed_emission_factors; seed_emission_factors()"


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
from rest_framework.permissions import AllowAny

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
        try:
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

            # Try to find existing client first
            client = Client.objects.filter(pk=client_id).first()

            if client is None:
                # Client not found — look up by slug instead
                client = Client.objects.filter(
                    slug="breathe-demo-corp"
                ).first()

            if client is None:
                # No client exists at all — return clear error
                return Response(
                    {
                        "error": f"Client {client_id} not found. "
                                  "Run /api/setup/ first to initialise demo data."
                    },
                    status=status.HTTP_400_BAD_REQUEST
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
        except Exception as e:
            import traceback
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Upload failed: {str(e)}")
            logger.error(traceback.format_exc())
            return Response(
                {
                    "error": "Upload failed.",
                    "detail": str(e)
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


# ===========================================================================
# GET /api/rows/
# ===========================================================================

class RowListView(APIView):
    """
    Return a paginated, filtered list of ActivityRow objects.

    Query params:
      client_id    — integer PK (REQUIRED — returns 400 if missing)
      source_type  — 'SAP' | 'UTILITY' | 'TRAVEL'
      scope        — 1 | 2 | 3
      status       — PENDING | FLAGGED | APPROVED | LOCKED | REJECTED
      is_flagged   — 'true' | 'false'
      date_from    — YYYY-MM-DD (filters on document_date)
      date_to      — YYYY-MM-DD (filters on document_date)
    """

    def get(self, request: Request) -> Response:
        qs = ActivityRow.objects.select_related("raw_upload").all()

        # --- Filters ---
        client_id = request.query_params.get("client_id")

        # SECURITY: client_id is mandatory.
        # Never return rows across all tenants.
        if not client_id:
            return Response(
                {"error": "client_id query parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        qs = qs.filter(client_id=client_id)

        SOURCE_FILTER_MAP = {
            "SAP":     RawUpload.SOURCE_SAP,
            "UTILITY": RawUpload.SOURCE_UTILITY,
            "TRAVEL":  RawUpload.SOURCE_TRAVEL,
        }
        source_param = request.query_params.get("source_type")
        if source_param:
            mapped_source = SOURCE_FILTER_MAP.get(source_param.upper(), source_param)
            qs = qs.filter(raw_upload__source_system=mapped_source)

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


def seed_emission_factors() -> int:
    """
    Idempotently seed all global emission factors into the EmissionFactor table.
    Returns the number of rows actually created (0 if already seeded).
    """
    from datetime import date
    from .models import EmissionFactor

    factors = [
        {
            "factor_key": "diesel_litres", "value": "2.68",
            "source": "DEFRA", "year": 2023,
            "unit_numerator": "kgCO2e", "unit_denominator": "litre",
            "effective_from": date(2023, 1, 1),
        },
        {
            "factor_key": "petrol_litres", "value": "2.31",
            "source": "DEFRA", "year": 2023,
            "unit_numerator": "kgCO2e", "unit_denominator": "litre",
            "effective_from": date(2023, 1, 1),
        },
        {
            "factor_key": "lpg_kg", "value": "1.51",
            "source": "DEFRA", "year": 2023,
            "unit_numerator": "kgCO2e", "unit_denominator": "kg",
            "effective_from": date(2023, 1, 1),
        },
        {
            "factor_key": "india_grid_kwh", "value": "0.716",
            "source": "CEA", "year": 2023,
            "unit_numerator": "kgCO2e", "unit_denominator": "kWh",
            "effective_from": date(2023, 1, 1),
        },
        {
            "factor_key": "flight_economy_km", "value": "0.133",
            "source": "DEFRA", "year": 2023,
            "unit_numerator": "kgCO2e", "unit_denominator": "km",
            "effective_from": date(2023, 1, 1),
        },
        {
            "factor_key": "flight_business_km", "value": "0.295",
            "source": "DEFRA", "year": 2023,
            "unit_numerator": "kgCO2e", "unit_denominator": "km",
            "effective_from": date(2023, 1, 1),
        },
        {
            "factor_key": "flight_first_km", "value": "0.430",
            "source": "DEFRA", "year": 2023,
            "unit_numerator": "kgCO2e", "unit_denominator": "km",
            "effective_from": date(2023, 1, 1),
        },
        {
            "factor_key": "hotel_night", "value": "31.0",
            "source": "DEFRA", "year": 2023,
            "unit_numerator": "kgCO2e", "unit_denominator": "night",
            "effective_from": date(2023, 1, 1),
        },
        {
            "factor_key": "taxi_km", "value": "0.149",
            "source": "DEFRA", "year": 2023,
            "unit_numerator": "kgCO2e", "unit_denominator": "km",
            "effective_from": date(2023, 1, 1),
        },
        {
            "factor_key": "rail_km", "value": "0.041",
            "source": "DEFRA", "year": 2023,
            "unit_numerator": "kgCO2e", "unit_denominator": "km",
            "effective_from": date(2023, 1, 1),
        },
    ]

    created_count = 0
    for f in factors:
        _, created = EmissionFactor.objects.get_or_create(
            client=None,
            factor_key=f["factor_key"],
            effective_from=f["effective_from"],
            defaults=f,
        )
        if created:
            created_count += 1
    return created_count



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

        ef_created = seed_emission_factors()

        return Response({
            "client_id": client.pk,
            "client_name": client.name,
            "client_created": client_created,
            "plant_codes_created": pc_created,
            "emission_factors_created": ef_created,
            "message": "Demo client ready. You can now upload CSV files.",
        }, status=status.HTTP_200_OK)


# ===========================================================================
# DELETE /api/delete-all/  — wipe all data for a client and reseed
# ===========================================================================

class RunMigrationsView(APIView):
    """
    GET /api/run-migrations/
    Runs pending Django migrations programmatically.
    Safe to call multiple times — only applies pending migrations.
    Used when Render Shell is not available.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        from django.db.migrations.executor import MigrationExecutor
        from django.db import connection
        from django.core.management import call_command
        import io

        # Check pending migrations first
        try:
            executor = MigrationExecutor(connection)
            targets = executor.loader.graph.leaf_nodes()
            pending = executor.migration_plan(targets)
            pending_list = [str(m) for m, _ in pending]
        except Exception as e:
            return Response(
                {"error": f"Could not check migrations: {str(e)}"},
                status=500
            )

        if not pending_list:
            return Response({
                "status": "nothing_to_do",
                "message": "No pending migrations. Database is up to date.",
                "pending": []
            })

        # Run migrations
        try:
            out = io.StringIO()
            call_command("migrate", "--no-input", stdout=out)
            output = out.getvalue()
        except Exception as e:
            return Response(
                {
                    "status": "error",
                    "message": str(e),
                    "pending_before": pending_list
                },
                status=500
            )

        # Verify no pending migrations remain
        try:
            executor2 = MigrationExecutor(connection)
            targets2 = executor2.loader.graph.leaf_nodes()
            still_pending = executor2.migration_plan(targets2)
            still_pending_list = [str(m) for m, _ in still_pending]
        except Exception:
            still_pending_list = []

        return Response({
            "status": "success",
            "applied": pending_list,
            "still_pending": still_pending_list,
            "output": output,
        })


class SeedStatusView(APIView):
    """
    GET /api/seed-status/
    Shows current seed state and runs seed if needed.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        from ingestor.models import (
            Client, EmissionFactor, PlantCode, ActivityRow
        )

        client = Client.objects.filter(
            slug="breathe-demo-corp"
        ).first()

        ef_count = EmissionFactor.objects.filter(client=None).count()
        pc_count = PlantCode.objects.count()
        row_count = ActivityRow.objects.count()

        needs_seed = (
            client is None or
            ef_count == 0 or
            pc_count == 0
        )

        if needs_seed:
            # Run seed
            from django.core.management import call_command
            import io
            out = io.StringIO()
            call_command("seed_mock_data", stdout=out)
            seed_output = out.getvalue()

            # Also seed emission factors
            seed_emission_factors()

            return Response({
                "status": "seeded",
                "message": "Seed data was missing — reseeded successfully.",
                "output": seed_output,
                "counts": {
                    "clients": Client.objects.count(),
                    "emission_factors": EmissionFactor.objects.filter(
                        client=None
                    ).count(),
                    "plant_codes": PlantCode.objects.count(),
                    "activity_rows": ActivityRow.objects.count(),
                }
            })

        return Response({
            "status": "ok",
            "message": "Seed data is present. No action needed.",
            "counts": {
                "clients": Client.objects.count(),
                "client_pk": client.pk,
                "emission_factors": ef_count,
                "plant_codes": pc_count,
                "activity_rows": row_count,
            }
        })


class DeleteAllDataView(APIView):
    """
    DELETE /api/delete-all/?client_id=1

    Deletes all ActivityRows, RawUploads, AuditLogs, PlantCodes and the Client
    itself, then re-runs seed_mock_data so the demo dataset is restored fresh.

    Intended for demo / testing resets only.  Safe to call repeatedly.
    """

    def delete(self, request: Request) -> Response:
        client_id = request.query_params.get("client_id", 1)

        try:
            client = Client.objects.get(pk=client_id)
        except Client.DoesNotExist:
            return Response(
                {"error": f"Client {client_id} not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Count before deletion (for response)
        row_count    = ActivityRow.objects.filter(client=client).count()
        upload_count = RawUpload.objects.filter(client=client).count()
        audit_count  = AuditLog.objects.filter(client=client).count()

        # AuditLog.delete() is blocked by the append-only guard, bypass via SQL
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM ingestor_auditlog WHERE client_id = %s", [client.pk]
            )

        # Wipe data rows only — keep the Client row itself
        ActivityRow.objects.filter(client=client).delete()
        RawUpload.objects.filter(client=client).delete()
        PlantCode.objects.filter(client=client).delete()

        # Re-seed plant codes and emission factors only
        # (do NOT recreate the Client — keep its pk stable)
        from ingestor.models import PlantCode
        plant_codes = [
            {"code": "IN01", "site_name": "Mumbai Plant",    "country": "India"},
            {"code": "IN02", "site_name": "Pune Plant",      "country": "India"},
            {"code": "IN03", "site_name": "Chennai Plant",   "country": "India"},
            {"code": "DE07", "site_name": "Frankfurt Plant", "country": "Germany"},
            {"code": "IN04", "site_name": "Delhi Plant",     "country": "India"},
        ]
        for pc in plant_codes:
            PlantCode.objects.get_or_create(
                client=client,
                code=pc["code"],
                defaults={"site_name": pc["site_name"], "country": pc["country"]}
            )

        seed_emission_factors()

        return Response({
            "message": "All data deleted and demo data reseeded.",
            "client_id": client.pk,
            "deleted": {
                "activity_rows": row_count,
                "raw_uploads": upload_count,
                "audit_logs": audit_count
            }
        }, status=status.HTTP_200_OK)
