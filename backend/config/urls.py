"""
config/urls.py
================
Root URL configuration for Breathe ESG Ingestor.
"""

from django.urls import path

from ingestor.views import (
    AuditLogView,
    ApproveRowView,
    BulkApproveView,
    RejectRowView,
    RowListView,
    SummaryView,
    UploadView,
)

urlpatterns = [
    # Upload
    path("api/upload/", UploadView.as_view(), name="api-upload"),

    # Row listing
    path("api/rows/", RowListView.as_view(), name="api-rows-list"),

    # Bulk approve (must come BEFORE <int:pk> routes to avoid collision)
    path("api/rows/bulk-approve/", BulkApproveView.as_view(), name="api-rows-bulk-approve"),

    # Per-row actions
    path("api/rows/<int:pk>/approve/", ApproveRowView.as_view(), name="api-rows-approve"),
    path("api/rows/<int:pk>/reject/",  RejectRowView.as_view(),  name="api-rows-reject"),

    # Summary & audit
    path("api/summary/",   SummaryView.as_view(),  name="api-summary"),
    path("api/audit-log/", AuditLogView.as_view(), name="api-audit-log"),
]
