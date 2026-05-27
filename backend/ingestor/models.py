"""
Core data models for Breathe ESG Ingestor.

Hard constraints enforced here:
  1. RawUpload.raw_payload (JSONField) is NEVER overwritten after creation.
  2. ActivityRow status LOCKED means no further edits — enforced in save().
  3. Every state change writes a row to AuditLog (append-only: no update, no delete).
  4. Every model carries a client ForeignKey for multi-tenancy.
"""

from django.db import models
from django.utils import timezone


# ---------------------------------------------------------------------------
# Tenant / Client
# ---------------------------------------------------------------------------

class Client(models.Model):
    """Represents a tenant organisation using the platform."""

    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


# ---------------------------------------------------------------------------
# Reference: Plant Code lookup
# ---------------------------------------------------------------------------

class PlantCode(models.Model):
    """
    Reference table mapping SAP WERKS codes to human-readable site names.
    Unknown WERKS codes will trigger a flag on the ActivityRow.
    """

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="plant_codes")
    code = models.CharField(max_length=10)          # e.g. "IN01", "DE07"
    site_name = models.CharField(max_length=255)    # e.g. "Mumbai Plant"
    country = models.CharField(max_length=2)        # ISO-3166 alpha-2

    class Meta:
        unique_together = [("client", "code")]
        ordering = ["code"]

    def __str__(self) -> str:
        return f"{self.code} — {self.site_name}"


# ---------------------------------------------------------------------------
# Raw Upload (immutable after creation — constraint 1)
# ---------------------------------------------------------------------------

class RawUpload(models.Model):
    """
    Stores an unmodified snapshot of data as received from the source system.

    HARD CONSTRAINT: raw_payload is written once on creation and NEVER updated.
    """

    SOURCE_SAP     = "sap_csv"
    SOURCE_UTILITY = "utility_csv"
    SOURCE_TRAVEL  = "travel_csv"
    SOURCE_CHOICES = [
        (SOURCE_SAP,     "SAP CSV (MB51)"),
        (SOURCE_UTILITY, "Utility Bill CSV"),
        (SOURCE_TRAVEL,  "Travel Booking CSV"),
    ]

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="raw_uploads")
    uploaded_by_id = models.IntegerField()          # user pk — no FK to keep auth decoupled
    source_system = models.CharField(max_length=50, choices=SOURCE_CHOICES)
    raw_payload = models.JSONField()                 # ← immutable after creation
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def save(self, *args, **kwargs):
        # Enforce immutability of raw_payload once the record exists.
        if self.pk:
            original = RawUpload.objects.filter(pk=self.pk).values("raw_payload").first()
            if original and original["raw_payload"] != self.raw_payload:
                raise ValueError(
                    "RawUpload.raw_payload is immutable and cannot be changed after creation."
                )
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"RawUpload #{self.pk} ({self.source_system}) for {self.client}"


# ---------------------------------------------------------------------------
# Versioned Emission Factors
# ---------------------------------------------------------------------------

class EmissionFactor(models.Model):
    """
    Versioned, source-attributed emission factor lookup.

    client=None  → global default (applies to all tenants).
    client=<obj> → client-specific override that takes precedence.
    """

    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="emission_factors",
        help_text="Null = global default. Set = client-specific override."
    )
    source = models.CharField(max_length=50)
    year = models.IntegerField()
    factor_key = models.CharField(max_length=100)
    value = models.DecimalField(max_digits=10, decimal_places=6)
    unit_numerator = models.CharField(max_length=20)
    unit_denominator = models.CharField(max_length=20)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("client", "factor_key", "effective_from")]
        ordering = ["-effective_from"]

    def __str__(self) -> str:
        return (
            f"{self.factor_key} | {self.source} {self.year} | "
            f"{self.value} kgCO2e/{self.unit_denominator}"
        )


# ---------------------------------------------------------------------------
# Activity Row (the normalized, auditable record)
# ---------------------------------------------------------------------------

class ActivityRow(models.Model):
    """
    A single normalised emission-activity line item.

    Status lifecycle:  PENDING → REVIEWED → APPROVED → LOCKED
    HARD CONSTRAINT: Once status is LOCKED no field may be changed.
    """

    STATUS_PENDING  = "PENDING"
    STATUS_REVIEWED = "REVIEWED"
    STATUS_APPROVED = "APPROVED"
    STATUS_LOCKED   = "LOCKED"
    STATUS_FLAGGED  = "FLAGGED"
    STATUS_REJECTED = "REJECTED"

    STATUS_CHOICES = [
        (STATUS_PENDING,  "Pending"),
        (STATUS_REVIEWED, "Reviewed"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_LOCKED,   "Locked"),
        (STATUS_FLAGGED,  "Flagged — needs review"),
        (STATUS_REJECTED, "Rejected"),
    ]

    # Multi-tenancy
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="activity_rows")

    # Traceability
    raw_upload = models.ForeignKey(
        RawUpload, on_delete=models.PROTECT, related_name="activity_rows"
    )

    # Source identifiers (SAP-centric but generic enough for other parsers)
    plant_code = models.CharField(max_length=10, blank=True)
    material_number = models.CharField(max_length=50, blank=True)
    description = models.CharField(max_length=500, blank=True)

    # Dates
    document_date = models.DateField(null=True, blank=True)
    posting_date = models.DateField(null=True, blank=True)

    # Normalised quantity
    quantity = models.DecimalField(max_digits=18, decimal_places=4)
    unit = models.CharField(max_length=20)          # 'litres', 'kg', 'm3', 'kWh'

    # GHG scope classification
    scope = models.IntegerField()                    # 1, 2, or 3
    category = models.CharField(max_length=100)      # e.g. 'stationary_combustion'

    # Emission estimate (rule-based, pre-computed)
    emission_factor = models.DecimalField(
        max_digits=10, decimal_places=4, null=True, blank=True
    )
    emission_factor_ref = models.ForeignKey(
        "EmissionFactor",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="activity_rows",
        help_text="Versioned EmissionFactor used to compute co2e_kg."
    )
    co2e_kg = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True,
        help_text="kgCO2e = quantity × emission_factor"
    )

    # Status & quality flags
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    is_flagged = models.BooleanField(default=False)
    flag_reason = models.TextField(blank=True)

    # Review tracking (populated on approve/reject)
    reviewed_by_id = models.IntegerField(null=True, blank=True)   # user pk
    reviewed_at = models.DateTimeField(null=True, blank=True)

    # Edit tracking
    is_edited         = models.BooleanField(default=False)
    edited_by_id      = models.IntegerField(null=True, blank=True)
    edited_at         = models.DateTimeField(null=True, blank=True)
    original_snapshot = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    # Core fields tracked for edit detection
    _SNAPSHOT_FIELDS = ("quantity", "unit", "scope", "category", "emission_factor", "co2e_kg")

    def save(self, *args, **kwargs):
        # HARD CONSTRAINT 2: LOCKED rows are immutable.
        if self.pk:
            current_status = (
                ActivityRow.objects.filter(pk=self.pk).values("status").first() or {}
            ).get("status")
            if current_status == self.STATUS_LOCKED:
                raise ValueError(
                    f"ActivityRow #{self.pk} is LOCKED and cannot be modified."
                )

            # Edit detection: compare current core fields against original_snapshot
            if self.original_snapshot:
                for field in self._SNAPSHOT_FIELDS:
                    current_val = getattr(self, field)
                    original_val = self.original_snapshot.get(field)
                    # Decimal / float comparison — compare string representations
                    if str(current_val) != str(original_val):
                        self.is_edited = True
                        self.edited_at = timezone.now()
                        break

        else:
            # First save (creation) — freeze the original values
            self.original_snapshot = {
                field: str(getattr(self, field)) for field in self._SNAPSHOT_FIELDS
            }

        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return (
            f"ActivityRow #{self.pk} [{self.status}] "
            f"{self.plant_code} {self.quantity}{self.unit}"
        )


# ---------------------------------------------------------------------------
# Audit Log (append-only — constraint 3)
# ---------------------------------------------------------------------------

class AuditLog(models.Model):
    """
    Immutable event log.  Every state change MUST create a new row here.
    No updates or deletes are permitted (enforced in save() and delete()).
    """

    # Supported action verbs
    ACTION_UPLOADED = "UPLOADED"
    ACTION_REVIEWED = "REVIEWED"
    ACTION_APPROVED = "APPROVED"
    ACTION_LOCKED = "LOCKED"
    ACTION_FLAGGED = "FLAGGED"
    ACTION_REJECTED = "REJECTED"

    ACTION_CHOICES = [
        (ACTION_UPLOADED, "Uploaded"),
        (ACTION_REVIEWED, "Reviewed"),
        (ACTION_APPROVED, "Approved"),
        (ACTION_LOCKED, "Locked"),
        (ACTION_FLAGGED, "Flagged"),
        (ACTION_REJECTED, "Rejected"),
    ]

    # Multi-tenancy
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="audit_logs")

    # What was acted on
    activity_row = models.ForeignKey(
        ActivityRow, on_delete=models.PROTECT, related_name="audit_logs"
    )

    # Who did it and when
    actor_id = models.IntegerField()                  # user pk — no FK to keep auth decoupled
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    detail = models.TextField(blank=True)             # free-form notes
    before_value = models.JSONField(null=True, blank=True)  # snapshot before change
    after_value = models.JSONField(null=True, blank=True)   # snapshot after change
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["timestamp"]

    # ------------------------------------------------------------------
    # Append-only enforcement
    # ------------------------------------------------------------------

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValueError("AuditLog entries are immutable — create a new entry instead.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("AuditLog entries cannot be deleted.")

    def __str__(self) -> str:
        return f"AuditLog #{self.pk} [{self.action}] row={self.activity_row_id} by={self.actor_id}"
