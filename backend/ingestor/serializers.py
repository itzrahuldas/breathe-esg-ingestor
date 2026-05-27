"""
ingestor/serializers.py
=========================
DRF serializers for all API endpoints.
"""

from rest_framework import serializers

from .models import ActivityRow, AuditLog, RawUpload


class ActivityRowSerializer(serializers.ModelSerializer):
    """
    Full representation of an ActivityRow used in GET /api/rows/.

    Extra read-only computed fields:
      - source_type: human-readable source system from the related RawUpload
      - site_name: alias for description (matches spec field name)
      - activity_date_start: alias for document_date
      - activity_date_end: alias for posting_date
      - kgco2e: alias for co2e_kg
    """

    source_type = serializers.SerializerMethodField()
    site_name = serializers.CharField(source="description", read_only=True)
    activity_date_start = serializers.DateField(source="document_date", read_only=True)
    activity_date_end = serializers.DateField(source="posting_date", read_only=True)
    kgco2e = serializers.DecimalField(
        source="co2e_kg", max_digits=18, decimal_places=4,
        read_only=True, allow_null=True
    )

    class Meta:
        model = ActivityRow
        fields = [
            "id",
            "scope",
            "category",
            "site_name",
            "activity_date_start",
            "activity_date_end",
            "quantity",
            "unit",
            "kgco2e",
            "status",
            "is_flagged",
            "flag_reason",
            "source_type",
            "plant_code",
            "material_number",
            "emission_factor",
            "reviewed_by_id",
            "reviewed_at",
            "is_edited",
            "edited_by_id",
            "edited_at",
            "original_snapshot",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_source_type(self, obj: ActivityRow) -> str:
        return obj.raw_upload.source_system


class AuditLogSerializer(serializers.ModelSerializer):
    """Serializer for GET /api/audit-log/."""

    class Meta:
        model = AuditLog
        fields = [
            "id",
            "activity_row_id",
            "actor_id",
            "action",
            "detail",
            "before_value",
            "after_value",
            "timestamp",
        ]
        read_only_fields = fields


class UploadResponseSerializer(serializers.Serializer):
    """Response shape for POST /api/upload/."""
    upload_id = serializers.CharField()
    rows_created = serializers.IntegerField()
    rows_flagged = serializers.IntegerField()


class RejectBodySerializer(serializers.Serializer):
    """Request body for PATCH /api/rows/{id}/reject/."""
    reason = serializers.CharField(allow_blank=False)


class BulkApproveBodySerializer(serializers.Serializer):
    """Request body for POST /api/rows/bulk-approve/."""
    row_ids = serializers.ListField(
        child=serializers.IntegerField(),
        allow_empty=False,
    )


class BulkApproveResponseSerializer(serializers.Serializer):
    """Response shape for POST /api/rows/bulk-approve/."""
    approved = serializers.IntegerField()
    skipped = serializers.IntegerField()


class SummarySerializer(serializers.Serializer):
    """Response shape for GET /api/summary/."""
    total_rows = serializers.IntegerField()
    pending_review = serializers.IntegerField()
    flagged = serializers.IntegerField()
    approved = serializers.IntegerField()
    rejected = serializers.IntegerField()
    total_kgco2e = serializers.FloatField()
    scope_breakdown = serializers.DictField(child=serializers.FloatField())
