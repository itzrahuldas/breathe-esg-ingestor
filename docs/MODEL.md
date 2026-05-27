# Data Model

## Overview

The Breathe ESG Ingestor converts raw CSV exports from three enterprise source systems (SAP MB51, utility bills, corporate travel bookings) into normalised, scope-classified emission activity records that analysts can review and approve. The data lifecycle is **multi-source ingestion → unit normalisation → GHG scope assignment → analyst review → audit lock**. Every record is tenant-isolated via a `Client` foreign key, every state change is recorded in an append-only audit log, and every original CSV row is preserved verbatim in a JSON field that can never be overwritten.

---

## Models

### 1. Client

```python
class Client(models.Model):
    # PK: BigAutoField (implicit — DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField")
    name       = models.CharField(max_length=255)
    slug       = models.SlugField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
```

**Why it exists — multi-tenancy.**  Every other model carries a `client = ForeignKey(Client, on_delete=CASCADE)` field. Every queryset in `views.py` filters by `client_id` (passed as a query parameter or form field) so that one tenant's data is never visible to another.

In production, `Client` would be linked to an identity provider (SSO/OAuth) and resolved from the authenticated user's organisation, rather than passed as a raw integer query parameter.

---

### 2. PlantCode

```python
class PlantCode(models.Model):
    # PK: BigAutoField (implicit)
    client    = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="plant_codes")
    code      = models.CharField(max_length=10)        # e.g. "IN01", "DE07"
    site_name = models.CharField(max_length=255)       # e.g. "Mumbai Plant"
    country   = models.CharField(max_length=2)         # ISO-3166 alpha-2

    class Meta:
        unique_together = [("client", "code")]
        ordering = ["code"]
```

**Why it exists.**  SAP WERKS codes like `IN01` or `DE07` are meaningless without a lookup table that maps them to a human-readable site name and country. Without this table, an analyst reviewing an ActivityRow would see only a four-character code with no context.

The demo bootstrap (`SetupView` in `views.py`) seeds five plant codes:

| Code | Site Name | Country |
|------|-----------|---------|
| `IN01` | Mumbai Plant | IN |
| `IN02` | Pune Factory | IN |
| `IN03` | Chennai Plant | IN |
| `IN04` | Hyderabad Campus | IN |
| `DE07` | Frankfurt Office | DE |

A real deployment needs this table seeded from the client's SAP T001W (Plant master) table.

---

### 3. RawUpload

```python
class RawUpload(models.Model):
    # PK: BigAutoField (implicit)

    SOURCE_SAP     = "sap_csv"
    SOURCE_UTILITY = "utility_csv"
    SOURCE_TRAVEL  = "travel_csv"
    SOURCE_CHOICES = [
        ("sap_csv",     "SAP CSV (MB51)"),
        ("utility_csv", "Utility Bill CSV"),
        ("travel_csv",  "Travel Booking CSV"),
    ]

    client         = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="raw_uploads")
    uploaded_by_id = models.IntegerField()              # user pk — no FK to keep auth decoupled
    source_system  = models.CharField(max_length=50, choices=SOURCE_CHOICES)
    raw_payload    = models.JSONField()                  # ← immutable after creation
    uploaded_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]
```

**Immutability constraint — `save()` override (verbatim from code):**

```python
def save(self, *args, **kwargs):
    # Enforce immutability of raw_payload once the record exists.
    if self.pk:
        original = RawUpload.objects.filter(pk=self.pk).values("raw_payload").first()
        if original and original["raw_payload"] != self.raw_payload:
            raise ValueError(
                "RawUpload.raw_payload is immutable and cannot be changed after creation."
            )
    super().save(*args, **kwargs)
```

If the record already exists (`self.pk` is truthy), the override fetches the current `raw_payload` from the database and raises `ValueError` if the incoming value differs. This guarantees that the original CSV row — stored as a JSON dict — can never be silently overwritten.

**Why JSONField for `raw_payload`.**  Each source system produces CSV rows with completely different column sets (SAP has WERKS/MATNR/MEINS/MENGE, utility has meter_id/consumption/bill_from/bill_to, travel has booking_id/travel_type/origin/destination). A JSON field accepts any column structure without requiring a separate table per source.

**`source_system` values:**

| Value | Meaning |
|-------|---------|
| `sap_csv` | SAP MB51 material-movement flat-file export |
| `utility_csv` | Electricity meter / utility bill CSV |
| `travel_csv` | Corporate travel booking CSV |

---

### 4. ActivityRow

```python
class ActivityRow(models.Model):
    # PK: BigAutoField (implicit)

    STATUS_PENDING  = "PENDING"
    STATUS_REVIEWED = "REVIEWED"
    STATUS_APPROVED = "APPROVED"
    STATUS_LOCKED   = "LOCKED"
    STATUS_FLAGGED  = "FLAGGED"
    STATUS_REJECTED = "REJECTED"

    STATUS_CHOICES = [
        ("PENDING",  "Pending"),
        ("REVIEWED", "Reviewed"),
        ("APPROVED", "Approved"),
        ("LOCKED",   "Locked"),
        ("FLAGGED",  "Flagged — needs review"),
        ("REJECTED", "Rejected"),
    ]

    # Multi-tenancy
    client          = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="activity_rows")

    # Traceability
    raw_upload      = models.ForeignKey(RawUpload, on_delete=models.PROTECT, related_name="activity_rows")

    # Source identifiers
    plant_code      = models.CharField(max_length=10, blank=True)
    material_number = models.CharField(max_length=50, blank=True)
    description     = models.CharField(max_length=500, blank=True)

    # Dates
    document_date   = models.DateField(null=True, blank=True)
    posting_date    = models.DateField(null=True, blank=True)

    # Normalised quantity
    quantity        = models.DecimalField(max_digits=18, decimal_places=4)
    unit            = models.CharField(max_length=20)            # 'litres', 'kg', 'm3', 'kWh'

    # GHG scope classification
    scope           = models.IntegerField()                      # 1, 2, or 3
    category        = models.CharField(max_length=100)           # e.g. 'stationary_combustion'

    # Emission estimate
    emission_factor = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    co2e_kg         = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True,
                                          help_text="kgCO2e = quantity × emission_factor")

    # Status & quality flags
    status          = models.CharField(max_length=20, choices=STATUS_CHOICES, default="PENDING")
    is_flagged      = models.BooleanField(default=False)
    flag_reason     = models.TextField(blank=True)

    # Review tracking
    reviewed_by_id  = models.IntegerField(null=True, blank=True)  # user pk
    reviewed_at     = models.DateTimeField(null=True, blank=True)

    # Edit tracking
    is_edited         = models.BooleanField(default=False)
    edited_by_id      = models.IntegerField(null=True, blank=True)
    edited_at         = models.DateTimeField(null=True, blank=True)
    original_snapshot = models.JSONField(null=True, blank=True)

    created_at      = models.DateTimeField(auto_now_add=True)
    updated_at      = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
```

**LOCKED guard + edit tracking — `save()` override (verbatim from code):**

```python
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
```

The `save()` override does two things beyond the LOCKED guard:

1. **On first save (creation):** captures `original_snapshot` — a frozen JSON dict of the six core fields (`quantity`, `unit`, `scope`, `category`, `emission_factor`, `co2e_kg`) as string values at ingestion time. This snapshot is never overwritten.
2. **On subsequent saves:** compares each core field against `original_snapshot`. If any field has changed, `is_edited` flips to `True` and `edited_at` records the timestamp. The `edited_by_id` field is set by the caller (view) before calling `.save()`.

Note that `views.py` uses `queryset.update()` (bypassing `save()`) for the approve-and-lock transition itself so that the guard does not block the final lock write.

**`quantity` vs original values.**  The `quantity` and `unit` fields always contain the **normalised** values (e.g. gallons converted to litres, MWh converted to kWh). The original values are preserved in `RawUpload.raw_payload` — there are no separate `quantity_original` or `unit_original` fields on the model. The original can always be recovered by following the `raw_upload` foreign key.

**One RawUpload → multiple ActivityRows (billing split).**  The utility parser's `split_billing_period()` function splits a single billing-period CSV row into multiple ActivityRows when the billing window spans more than one calendar month. The consumption is allocated proportionally by days per month. For example, a bill from 2024-01-18 to 2024-02-21 (35 total days) is split: Jan gets 13/35 of the consumption, Feb gets 22/35. One `RawUpload` is created for the CSV row, and one `ActivityRow` per monthly slice.

#### Scope Assignment Rules

| Source Parser | Rule | Scope | Category |
|---------------|------|-------|----------|
| SAP | `material_number` starts with `FUL-` OR `description` contains any of {fuel, diesel, petrol, lpg, gas} | 1 | `stationary_combustion` |
| SAP | `material_number` starts with `ELEC-` | 2 | `purchased_electricity` |
| SAP | Default (no rule matches) | 3 | `purchased_goods` |
| Utility | All rows — hardcoded | 2 | `purchased_electricity` |
| Travel (FLIGHT) | All flight bookings | 3 | `business_travel_air` |
| Travel (HOTEL) | All hotel bookings | 3 | `business_travel_hotel` |
| Travel (GROUND) | All ground transport bookings | 3 | `business_travel_ground` |
| Travel | Unknown `travel_type` | 3 | `business_travel_unknown` |

#### Unit Normalisation

| Domain | Canonical Unit | Converts From | Multiplier |
|--------|---------------|---------------|------------|
| SAP (fuel/liquid) | `litres` | `L` | ×1.0 |
| SAP (fuel/liquid) | `litres` | `GAL` | ×3.785 |
| SAP (mass) | `kg` | `KG` | ×1.0 |
| SAP (volume) | `m3` | `M3` | ×1.0 |
| SAP (energy) | `kWh` | `KWH` | ×1.0 |
| Utility | `kWh` | `KWH` | ×1.0 |
| Utility | `kWh` | `MWH` | ×1000.0 |
| Travel (flight/ground) | `km` | — | Computed via haversine or raw CSV |
| Travel (hotel) | `nights` | — | Raw from CSV |

---

### 5. AuditLog

```python
class AuditLog(models.Model):
    # PK: BigAutoField (implicit)

    ACTION_UPLOADED = "UPLOADED"
    ACTION_REVIEWED = "REVIEWED"
    ACTION_APPROVED = "APPROVED"
    ACTION_LOCKED   = "LOCKED"
    ACTION_FLAGGED  = "FLAGGED"
    ACTION_REJECTED = "REJECTED"

    ACTION_CHOICES = [
        ("UPLOADED", "Uploaded"),
        ("REVIEWED", "Reviewed"),
        ("APPROVED", "Approved"),
        ("LOCKED",   "Locked"),
        ("FLAGGED",  "Flagged"),
        ("REJECTED", "Rejected"),
    ]

    # Multi-tenancy
    client       = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="audit_logs")

    # What was acted on
    activity_row = models.ForeignKey(ActivityRow, on_delete=models.PROTECT, related_name="audit_logs")

    # Who did it and when
    actor_id     = models.IntegerField()                    # user pk — no FK to keep auth decoupled
    action       = models.CharField(max_length=20, choices=ACTION_CHOICES)
    detail       = models.TextField(blank=True)             # free-form notes
    before_value = models.JSONField(null=True, blank=True)  # snapshot before change
    after_value  = models.JSONField(null=True, blank=True)  # snapshot after change
    timestamp    = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["timestamp"]
```

**Append-only enforcement — `save()` and `delete()` overrides (verbatim from code):**

```python
def save(self, *args, **kwargs):
    if self.pk:
        raise ValueError("AuditLog entries are immutable — create a new entry instead.")
    super().save(*args, **kwargs)

def delete(self, *args, **kwargs):
    raise ValueError("AuditLog entries cannot be deleted.")
```

- **`save()`**: If `self.pk` is truthy (i.e. the record already exists in the database), the override raises `ValueError` — no AuditLog row can ever be updated.
- **`delete()`**: Unconditionally raises `ValueError` — no AuditLog row can ever be deleted via the ORM. (The `DeleteAllDataView` bypasses this by using raw SQL: `DELETE FROM ingestor_auditlog WHERE client_id = %s`.)

**Why `activity_row` uses `on_delete=PROTECT`.**  If an `ActivityRow` is referenced by any `AuditLog` entry, Django will refuse to delete it (raises `ProtectedError`). This prevents orphaned audit records. The `activity_row` FK is **not** nullable — every audit log entry must reference an existing ActivityRow.

**Action values used in the codebase:**

| Action | Written by |
|--------|-----------|
| `UPLOADED` | All three parsers — one entry per ActivityRow created |
| `APPROVED` | `_approve_and_lock()` in views.py — written with `before_value`/`after_value` snapshots |
| `REJECTED` | `RejectRowView.patch()` in views.py — written with reason in `detail` |
| `REVIEWED` | Defined in choices but not written by any current code path |
| `LOCKED` | Defined in choices but not written separately — `APPROVED` action covers the full transition to LOCKED |
| `FLAGGED` | Defined in choices but not written separately — flagging is recorded via `UPLOADED` with flag details in `detail` |

---

## Multi-Tenancy

Multi-tenancy in this codebase is implemented via a **query-parameter-based client filter**, not middleware or subdomain routing.

**How each view gets `client_id`:**

| Endpoint | Method | Client Resolution |
|----------|--------|-------------------|
| `POST /api/upload/` | Form field `client_id` in request body | `Client.objects.get_or_create(pk=client_id, defaults={"name": "Breathe Demo Corp", "slug": "breathe-demo-corp"})` — **auto-creates** the client if it doesn't exist |
| `GET /api/rows/` | Query param `?client_id=` | `qs.filter(client_id=client_id)` — if not provided, **returns all rows across all clients** (no 400 error) |
| `GET /api/summary/` | Query param `?client_id=` (required) | Returns `400 {"error": "client_id query parameter is required."}` if missing |
| `GET /api/audit-log/` | Query param `?client_id=` (required) | Returns `400 {"error": "client_id query parameter is required."}` if missing |
| `PATCH /api/rows/{id}/approve/` | Resolved from the ActivityRow itself (`row.client`) | N/A — operates on a specific row |
| `PATCH /api/rows/{id}/reject/` | Resolved from the ActivityRow itself (`row.client`) | N/A — operates on a specific row |
| `DELETE /api/delete-all/` | Query param `?client_id=` (defaults to `1`) | Returns `404` if client not found |

**Key behaviour:** The `RowListView` does **not** require `client_id` — omitting it silently returns rows from all clients. The `SummaryView` and `AuditLogView` **do** require it, returning HTTP 400 if missing.

---

## Source-of-Truth Chain

```
kgCO2e value (ActivityRow.co2e_kg)
  → ActivityRow.id
    → ActivityRow.raw_upload_id (FK, on_delete=PROTECT)
      → RawUpload.id
        → RawUpload.raw_payload (JSONField, immutable)
          → original CSV row (verbatim key-value pairs)
```

This chain means that for any emission number displayed in the dashboard, an auditor can trace backwards to the exact original CSV cell values. The `PROTECT` delete constraint ensures the `RawUpload` cannot be deleted while any `ActivityRow` references it. The immutable `save()` override ensures the original data cannot be silently altered. This is the core auditability guarantee of the system.

---

## Unit Normalisation

All parsers normalise raw units to a canonical set before persisting to `ActivityRow.quantity` and `ActivityRow.unit`:

| Canonical Unit | Source | Raw Inputs | Conversion |
|----------------|--------|------------|------------|
| `litres` | SAP | `L` (×1.0), `GAL` (×3.785) | `quantity × multiplier` |
| `kg` | SAP | `KG` (×1.0) | Pass-through |
| `m3` | SAP | `M3` (×1.0) | Pass-through |
| `kWh` | SAP, Utility | `KWH` (×1.0), `MWH` (×1000.0) | `quantity × multiplier` |
| `km` | Travel | Haversine-computed (flights) or raw CSV (ground) | — |
| `nights` | Travel | Raw CSV (`hotel_nights`) | — |

The original raw values are always preserved in `RawUpload.raw_payload`. The original SAP MEINS code (e.g. `GAL`) and MENGE value (e.g. `132.5`) remain in the JSON, while the ActivityRow stores the converted values (e.g. `501.3125 litres`).

**Concrete example from the codebase:**

```
Input:   132.5 GAL (SAP MEINS field)
Output:  132.5 × 3.785 = 501.3125 litres
Stored:  ActivityRow.quantity = 501.3125, ActivityRow.unit = "litres"
Original: RawUpload.raw_payload = {"MEINS": "GAL", "MENGE": "132.5", ...}
```

---

## Audit Trail

The AuditLog records every state change in the lifecycle of an ActivityRow. Here is when each action is written:

### `UPLOADED`
**Written by:** All three parsers (`parse_sap_file`, `parse_utility_file`, `parse_travel_file`)
**When:** Immediately after creating each `ActivityRow`. One `AuditLog` entry per `ActivityRow` — if billing-split produces 3 slices, 3 audit entries are created.
**Content:** `detail` contains parser-specific metadata (raw_upload_id, plant/meter/booking info, quantities). If the row was flagged, flag reasons are appended.

### `APPROVED`
**Written by:** `_approve_and_lock()` helper in `views.py`, called from `ApproveRowView.patch()` and `BulkApproveView.post()`
**When:** When an analyst approves a row. The row transitions directly to `LOCKED` status (no intermediate `APPROVED` state is persisted).
**Content:** `before_value = {"status": <old_status>}`, `after_value = {"status": "LOCKED"}`, `detail = "Row approved and locked."`

### `REJECTED`
**Written by:** `RejectRowView.patch()` in `views.py`
**When:** When an analyst rejects a row with a mandatory reason.
**Content:** `before_value = {"status": <old_status>}`, `after_value = {"status": "REJECTED"}`, `detail = <analyst's rejection reason>`

### `REVIEWED`, `LOCKED`, `FLAGGED`
**Defined in:** `AuditLog.ACTION_CHOICES`
**Written by:** No current code path writes these action values. They are reserved for future use. Flagging is recorded via the `UPLOADED` action with flag details in the `detail` field.

---

## Known Limitations and Production Gaps

### 1. Integer PKs vs UUIDs

All models use Django's `BigAutoField` (auto-incrementing 64-bit integer) as the primary key, set globally via:

```python
# settings.py line 162
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
```

**Risk:** Sequential integer PKs are enumerable — an attacker can guess valid IDs and probe endpoints like `/api/rows/1/approve/`. They also leak information about table size and insertion rate.

**Production change:** Replace with `UUIDField(primary_key=True, default=uuid.uuid4, editable=False)` on all models. This would require a migration and updates to all FK references.

### 2. Inline Normalisation vs Separate Pipeline

Unit normalisation, scope assignment, emission factor application, and billing-period splitting are all performed inline within each parser function (`parse_sap_file`, `parse_utility_file`, `parse_travel_file`). This means:

- Normalisation logic is duplicated across parsers where applicable.
- There is no way to re-run normalisation without re-uploading.
- Testing requires database fixtures (parsers touch the DB directly).

In production, these steps would be extracted into a separate normalisation pipeline, with pure-function transforms followed by a single persistence layer.

### 3. Hardcoded Emission Factors

All emission factors are hardcoded as module-level constants. No versioning, no per-client overrides, no effective-date ranges.

**SAP parser (`sap_parser.py`):**

| Constant | Value | Meaning |
|----------|-------|---------|
| `DIESEL_LITRES` | `2.68` | kgCO2e per litre of diesel — DEFRA 2023 |
| `PETROL_LITRES` | `2.31` | kgCO2e per litre of petrol — DEFRA 2023 |
| `LPG_KG` | `1.51` | kgCO2e per kg of LPG — DEFRA 2023 |

**Utility parser (`utility_parser.py`):**

| Constant | Value | Meaning |
|----------|-------|---------|
| `INDIA_GRID_KWH` | `0.716` | kgCO2e per kWh — CEA India Grid 2023 |

**Travel parser (`travel_parser.py`):**

| Constant | Value | Meaning |
|----------|-------|---------|
| `FLIGHT_ECONOMY_KM` | `0.133` | kgCO2e per passenger-km, economy class (incl. RFI) — DEFRA 2023 |
| `FLIGHT_BUSINESS_KM` | `0.295` | kgCO2e per passenger-km, business class — DEFRA 2023 |
| `FLIGHT_FIRST_KM` | `0.430` | kgCO2e per passenger-km, first class — DEFRA 2023 |
| `HOTEL_NIGHT` | `31.0` | kgCO2e per hotel room-night — DEFRA 2023 |
| `TAXI_KM` | `0.149` | kgCO2e per km, taxi/private car — DEFRA 2023 |
| `RAIL_KM` | `0.041` | kgCO2e per km, rail — DEFRA 2023 |

A production system would have a versioned `EmissionFactor` model:

```python
class EmissionFactor(models.Model):
    client         = models.ForeignKey(Client, on_delete=models.CASCADE)
    source         = models.CharField(max_length=50)   # "DEFRA", "CEA", "EPA"
    year           = models.IntegerField()              # 2023, 2024, ...
    factor_key     = models.CharField(max_length=100)   # "diesel_litres", "india_grid_kwh"
    value          = models.DecimalField(max_digits=10, decimal_places=6)
    unit_numerator = models.CharField(max_length=20)    # "kgCO2e"
    unit_denominator = models.CharField(max_length=20)  # "litre", "kWh", "km"
    effective_from = models.DateField()
    effective_to   = models.DateField(null=True, blank=True)
```

### 4. Plant Code Lookup

Plant codes are currently seeded via the `SetupView` endpoint with five hardcoded demo entries. Any SAP row referencing a WERKS code not in this list is flagged with:

```
"Plant code '<code>' not found in reference table for client '<slug>'."
```

In production, this table needs to be populated from the client's SAP T001W master data, ideally via a bulk import or SAP integration. The current approach means any real-world SAP file will flag nearly every row.

### 5. Flight Distance via Hardcoded Airport Coordinates

The travel parser computes flight distances using great-circle haversine with only **10 hardcoded airports** in `AIRPORT_COORDS`:

```
BOM, DEL, LHR, BLR, MAA, HYD, CCU, DXB, SIN, JFK
```

Any IATA code outside this set causes a `ValueError` and the row is flagged. A production system would use a comprehensive airport database or an external API.

### 6. CORS Configuration

```python
# settings.py
CORS_ALLOW_ALL_ORIGINS = config("CORS_ALLOW_ALL", default=True, cast=bool)
```

`CORS_ALLOW_ALL_ORIGINS` defaults to `True` — any origin can call the API. This is acceptable for a prototype but must be restricted in production to specific frontend domains.

### 7. Health Check Endpoint

The root path (`/`) returns a simple JSON health check (defined in `urls.py`):

```python
def health_check(request):
    return JsonResponse({
        "status": "ok",
        "service": "breathe-esg-backend",
        "version": "1.0.0",
    })
```
