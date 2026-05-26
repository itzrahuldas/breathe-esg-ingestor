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
    RejectRowView,
    RowListView,
    SetupView,
    SummaryView,
    UploadView,
)


def health_check(request):
    """Bug 3 fix: root path returns JSON health check instead of 404."""
    return JsonResponse({
        "status": "ok",
        "service": "breathe-esg-backend",
        "version": "1.0.0",
    })


urlpatterns = [
    # Health check — root path
    path("", health_check, name="health-check"),

    # One-time demo bootstrap — creates Client pk=1 and PlantCodes
    path("api/setup/", SetupView.as_view(), name="api-setup"),

    # Danger zone — wipe all client data and reseed demo dataset
    path("api/delete-all/", DeleteAllDataView.as_view(), name="api-delete-all"),

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
