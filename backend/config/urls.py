"""
config/urls.py
================
Root URL configuration for Breathe ESG Ingestor.
"""

from django.http import JsonResponse
from django.urls import path

from ingestor.views import (
    AuditLogView,
    ApproveRowView,
    BulkApproveView,
    DeleteAllDataView,
    FixDuplicateClientsView,
    RejectRowView,
    RowListView,
    SetupView,
    RunMigrationsView,
    SeedStatusView,
    SummaryView,
    UploadView,
)


def health_check(request):
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    # Check pending migrations
    try:
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        pending = executor.migration_plan(targets)
        pending_list = [str(m) for m, _ in pending]
    except Exception as e:
        pending_list = [f"ERROR: {str(e)}"]

    # Check DB connection
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        db_status = "ok"
    except Exception as e:
        db_status = f"error: {str(e)}"

    # Check key model counts
    try:
        from ingestor.models import (
            Client, EmissionFactor, ActivityRow,
            RawUpload, AuditLog
        )
        model_counts = {
            "clients": Client.objects.count(),
            "emission_factors": EmissionFactor.objects.count(),
            "activity_rows": ActivityRow.objects.count(),
            "raw_uploads": RawUpload.objects.count(),
            "audit_logs": AuditLog.objects.count(),
        }
    except Exception as e:
        model_counts = {"error": str(e)}

    return JsonResponse({
        "status": "ok",
        "service": "breathe-esg-backend",
        "version": "1.0.0",
        "database": db_status,
        "pending_migrations": pending_list,
        "pending_migration_count": len(pending_list),
        "model_counts": model_counts,
    })


urlpatterns = [
    # Health check — root path
    path("", health_check, name="health-check"),

    # One-time demo bootstrap — creates Client pk=1 and PlantCodes
    path("api/setup/", SetupView.as_view(), name="api-setup"),

    # Run pending migrations (when shell is unavailable)
    path("api/run-migrations/", RunMigrationsView.as_view(), name="api-run-migrations"),

    # Check and reseed data if missing
    path("api/seed-status/", SeedStatusView.as_view(), name="api-seed-status"),

    # Danger zone — wipe all client data and reseed demo dataset
    path("api/delete-all/", DeleteAllDataView.as_view(), name="api-delete-all"),

    # Fix duplicate clients issue (if any)
    path("api/fix-clients/", FixDuplicateClientsView.as_view(), name="api-fix-clients"),

    # Upload
    path("api/upload/", UploadView.as_view(), name="api-upload"),

    # Row listing
    path("api/rows/", RowListView.as_view(), name="api-rows-list"),

    # Bulk approve (must come BEFORE <uuid:pk> routes to avoid collision)
    path("api/rows/bulk-approve/", BulkApproveView.as_view(), name="api-rows-bulk-approve"),

    # Bug 2 fix reverted: ActivityRow uses integer PK (Django default), not UUID.
    # The original <int:pk> was correct. 404s were caused by ALLOWED_HOSTS/DEBUG issue.
    path("api/rows/<int:pk>/approve/", ApproveRowView.as_view(), name="api-rows-approve"),
    path("api/rows/<int:pk>/reject/",  RejectRowView.as_view(),  name="api-rows-reject"),

    # Summary & audit
    path("api/summary/",   SummaryView.as_view(),  name="api-summary"),
    path("api/audit-log/", AuditLogView.as_view(), name="api-audit-log"),
]
