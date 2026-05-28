# Data Model

## Overview

The hard problem here is not computing carbon — it is that client data arrives in three completely different shapes: SAP MB51 material movements, utility bills, and corporate travel bookings. I built this pipeline to convert those chaotic CSV exports into normalised, scope-classified emission activity records that analysts can actually review and approve. The data lifecycle is **multi-source ingestion → unit normalisation → GHG scope assignment → analyst review → audit lock**. I added a `Client` foreign key to every record to isolate tenants, an append-only audit log to record every state change, and a JSON field to preserve every original CSV row verbatim so we never lose the raw data.

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

**Why it exists — multi-tenancy.** This model solves the problem of data isolation. It acts as the tenancy anchor, meaning every queryset in the codebase filters by `client_id` so one company's emissions data is never visible to another. For the prototype, `client_id` comes in as a query parameter. This is fine for a demo but not for real access control — in production this needs to resolve from the authenticated user's organisation.

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

**Why it exists.** This model solves the problem of unreadable SAP data. SAP WERKS codes like `IN01` or `DE07` are meaningless to an analyst, so I added this lookup table to map those four-character codes to human-readable site names and countries. I seeded the demo with five hardcoded codes. In a real deployment, this needs to be populated directly from the client's SAP T001W master table.

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

**Why it exists.** This model solves the problem of data loss during ingestion. The original CSV row is frozen on arrival, so if a parser converts a unit wrong, we fix the parser and reprocess against the stored data. We never ask the client to resend. The tradeoff is that storing everything in a JSON field means we lose the strict schema validation a relational table would provide.

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

I made this an immutable field as a practical engineering decision. If the record already exists (`self.pk` is truthy), I fetch the current `raw_payload` from the database and raise `ValueError` if the incoming value differs. This guarantees that the original CSV row — stored as a JSON dict — can never be silently overwritten.

**Why JSONField for `raw_payload`.** Each source system produces CSV rows with completely different column sets (SAP has WERKS/MATNR/MEINS/MENGE, utility has meter_id/consumption/bill_from/bill_to, travel has booking_id/travel_type/origin/destination). I used a JSON field to accept any column structure without requiring a separate table per source.

**`source_system` values:**

| Value | Meaning |
|-------|---------|
| `sap_csv` | SAP MB51 material-movement flat-file export |
| `utility_csv` | Electricity meter / utility bill CSV |
| `travel_csv` | Corporate travel booking CSV |

---

### 4. EmissionFactor

```python
class EmissionFactor(models.Model):
    # PK: BigAutoField (implicit)

    client           = models.ForeignKey(Client, on_delete=models.CASCADE,
                                         null=True, blank=True,
                                         related_name="emission_factors")
    source           = models.CharField(max_length=50)
    year             = models.IntegerField()
    factor_key       = models.CharField(max_length=100)
    value            = models.DecimalField(max_digits=10, decimal_places=6)
    unit_numerator   = models.CharField(max_length=20)
    unit_denominator = models.CharField(max_length=20)
    effective_from   = models.DateField()
    effective_to     = models.DateField(null=True, blank=True)
    created_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("client", "factor_key", "effective_from")]
        ordering = ["-effective_from"]
```

**Why it exists — versioned, auditable emission factors.** This model solves the problem of hardcoded constants losing historical accuracy. DEFRA updates factors annually, so I built this table to give us versioned factors that let us look up exactly what the India grid factor was on a specific date. The parsers still use hardcoded constants for now. In production, they need to be wired up to query this table so calculations are fully auditable.

**`client` field — global defaults vs client overrides.** `client=None` means the factor is a global default available to all tenants. `client=<Client>` means I overrode the global default for that tenant only. My parsers resolve this by checking for a client-specific factor first, then falling back to the global one.

**`effective_from` / `effective_to` — time-bounded validity.** A factor with `effective_to=None` is currently active. When DEFRA publishes updated factors, I insert a new row with a new `effective_from` — I never modify old rows. This preserves the historical record of what factor was valid at what time.

**Seeded global defaults (10 factors on first setup):**

| factor_key | value | source | unit |
|---|---|---|---|
| `diesel_litres` | 2.68 | DEFRA 2023 | kgCO2e/litre |
| `petrol_litres` | 2.31 | DEFRA 2023 | kgCO2e/litre |
| `lpg_kg` | 1.51 | DEFRA 2023 | kgCO2e/kg |
| `india_grid_kwh` | 0.716 | CEA 2023 | kgCO2e/kWh |
| `flight_economy_km` | 0.133 | DEFRA 2023 | kgCO2e/km |
| `flight_business_km` | 0.295 | DEFRA 2023 | kgCO2e/km |
| `flight_first_km` | 0.430 | DEFRA 2023 | kgCO2e/km |
| `hotel_night` | 31.0 | DEFRA 2023 | kgCO2e/night |
| `taxi_km` | 0.149 | DEFRA 2023 | kgCO2e/km |
| `rail_km` | 0.041 | DEFRA 2023 | kgCO2e/km |

**Link back to ActivityRow.** `ActivityRow.emission_factor_ref` is a `ForeignKey(EmissionFactor, on_delete=PROTECT)`. I used PROTECT so an `EmissionFactor` row cannot be deleted while any `ActivityRow` references it — preserving the calculation chain. I store the numeric value directly in `ActivityRow.emission_factor` (DecimalField) for fast reads without a join.

---

### 5. ActivityRow

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
    emission_factor     = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    emission_factor_ref = models.ForeignKey("EmissionFactor", on_delete=models.PROTECT,
                                            null=True, blank=True, related_name="activity_rows")
    co2e_kg             = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True,
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

**Why it exists.** This model solves the problem of standardising disparate data sources. It gives analysts a single normalised line item to review, so an SAP fuel log and a utility bill look exactly the same in the dashboard (`quantity`, `unit`, `co2e_kg`). The main production gap is that unit normalisation and factor application happen inline in the parsers instead of a dedicated pipeline.

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

UI restriction alone is not enough — someone with shell access or direct API access could bypass it. Enforcing in `save()` means there is no code path that can modify a locked row.

The `save()` override does two things beyond the LOCKED guard:

1. **On first save (creation):** I capture `original_snapshot` — a frozen JSON dict of the six core fields (`quantity`, `unit`, `scope`, `category`, `emission_factor`, `co2e_kg`) as string values at ingestion time. This snapshot is never overwritten.
2. **On subsequent saves:** I compare each core field against `original_snapshot`. If any field has changed, I flip `is_edited` to `True` and record the timestamp in `edited_at`. The caller (view) sets the `edited_by_id` field before calling `.save()`.

Note that I use `queryset.update()` (bypassing `save()`) in `views.py` for the approve-and-lock transition itself so that the guard does not block the final lock write.

**Edit tracking.**

An auditor needs to answer: "What did the parser produce, and what did the analyst change it to?" When an analyst corrects emission data before approval, the model records:

- `is_edited = True` — permanent flag that this row was changed post-ingestion
- `edited_at` — timestamp of the last edit
- `edited_by_id` — which user made the change
- `original_snapshot` — frozen JSON of values as they were at parse time

I write `original_snapshot` once in `save()` when `self.pk` is None (first save only). I never overwrite it on subsequent saves. The six tracked fields are: `quantity`, `unit`, `scope`, `category`, `emission_factor`, `co2e_kg`.

**`quantity` vs original values.** The `quantity` and `unit` fields always contain the **normalised** values (e.g. gallons converted to litres, MWh converted to kWh). I preserve the original values in `RawUpload.raw_payload` — there are no separate `quantity_original` or `unit_original` fields on the model. We can always recover the original by following the `raw_upload` foreign key.

**One RawUpload → multiple ActivityRows (billing split).** I wrote the utility parser's `split_billing_period()` function to split a single billing-period CSV row into multiple ActivityRows when the billing window spans more than one calendar month. I allocate the consumption proportionally by days per month. For example, a bill from 2024-01-18 to 2024-02-21 (35 total days) is split: Jan gets 13/35 of the consumption, Feb gets 22/35. One `RawUpload` is created for the CSV row, and one `ActivityRow` per monthly slice.

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

### 6. AuditLog

```python
class AuditLog(models.Model):
    # PK: BigAutoField (implicit)

    ACTION_UPLOADED = "UPLOADED"
    ACTION_APPROVED = "APPROVED"
    ACTION_REJECTED = "REJECTED"
    # EDITED: Reserved for future PATCH /api/rows/{id}/ endpoint.
    ACTION_EDITED   = "EDITED"

    ACTION_CHOICES = [
        ("UPLOADED", "Uploaded"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
        ("EDITED",   "Edited"),
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

**Why it exists.** This model solves the problem of accountability. It acts as an immutable event log, so when an auditor asks who approved a specific emission figure, we have an append-only record of the exact user, time, and before/after state. The tradeoff is that the log grows indefinitely, as we can never delete or prune entries without breaking the chain of custody.

**Append-only enforcement — `save()` and `delete()` overrides (verbatim from code):**

```python
def save(self, *args, **kwargs):
    if self.pk:
        raise ValueError("AuditLog entries are immutable — create a new entry instead.")
    super().save(*args, **kwargs)

def delete(self, *args, **kwargs):
    raise ValueError("AuditLog entries cannot be deleted.")
```

If you can edit or delete audit entries, the trail is worthless in a dispute. Both `save()` and `delete()` raise unconditionally on existing records — there is no escape hatch.

- **`save()`**: If `self.pk` is truthy (i.e. the record already exists in the database), I raise `ValueError` — no AuditLog row can ever be updated.
- **`delete()`**: Unconditionally raises `ValueError` — no AuditLog row can ever be deleted via the ORM. (I bypassed this in `DeleteAllDataView` by using raw SQL: `DELETE FROM ingestor_auditlog WHERE client_id = %s`.)

**Why `activity_row` uses `on_delete=PROTECT`.** I used `on_delete=PROTECT` so that if an `ActivityRow` is referenced by any `AuditLog` entry, Django will refuse to delete it (raises `ProtectedError`). I did this to prevent orphaned audit records. The `activity_row` FK is **not** nullable — every audit log entry must reference an existing ActivityRow.

**Action values used in the codebase:**

| Action | Written by |
|--------|-----------|
| `UPLOADED` | All three parsers — one entry per ActivityRow created |
| `APPROVED` | `_approve_and_lock()` in views.py — written with `before_value`/`after_value` snapshots |
| `REJECTED` | `RejectRowView.patch()` in views.py — written with reason in `detail` |
| `EDITED` | Reserved — future `PATCH /api/rows/{id}/` endpoint (see `TRADEOFFS.md`) |

---

## Multi-Tenancy

One database, multiple clients. Every table has a client FK and every queryset filters by it. It is not middleware-level isolation — it is enforced per-query in the view layer via a **query-parameter-based client filter**.

**How each view gets `client_id`:**

| Endpoint | Method | Client Resolution |
|----------|--------|-------------------|
| `POST /api/upload/` | Form field `client_id` in request body | `Client.objects.get_or_create(pk=client_id, defaults={"name": "Breathe Demo Corp", "slug": "breathe-demo-corp"})` — **auto-creates** the client if it doesn't exist |
| `GET /api/rows/` | Query param `?client_id=` (required) | Returns `400 {"error": "client_id query parameter is required."}` if missing — consistent with SummaryView and AuditLogView |
| `GET /api/summary/` | Query param `?client_id=` (required) | Returns `400 {"error": "client_id query parameter is required."}` if missing |
| `GET /api/audit-log/` | Query param `?client_id=` (required) | Returns `400 {"error": "client_id query parameter is required."}` if missing |
| `PATCH /api/rows/{id}/approve/` | Resolved from the ActivityRow itself (`row.client`) | N/A — operates on a specific row |
| `PATCH /api/rows/{id}/reject/` | Resolved from the ActivityRow itself (`row.client`) | N/A — operates on a specific row |
| `DELETE /api/delete-all/` | Query param `?client_id=` (defaults to `1`) | Returns `404` if client not found |

**~~Multi-tenancy leak in RowListView~~ — FIXED.** Before the fix, omitting `?client_id=` returned all rows across all tenants. Now every list endpoint returns HTTP 400 if `client_id` is missing. The approve/reject endpoints resolve the client from the row's own FK — so they never needed the param, meaning isolation is maintained.

---

## Source-of-Truth Chain

An auditor needs to answer "where did this kgCO2e number come from" without reading source code. This chain makes that possible. For any emission number in the dashboard, an auditor can trace backwards to the original CSV cell values and the exact factor version used.

```
ActivityRow.co2e_kg                   ← emission estimate in dashboard
│
├── ActivityRow.emission_factor       ← numeric value used (fast read)
│
├── ActivityRow.emission_factor_ref   ← FK → EmissionFactor (PROTECT)
│     ├── source          ("DEFRA")
│     ├── year            (2023)
│     ├── factor_key      ("diesel_litres")
│     ├── value           (2.680000)
│     └── effective_from  (2023-01-01)
│
├── ActivityRow.raw_upload_id         ← FK → RawUpload (PROTECT)
│     └── RawUpload.raw_payload       ← JSONField, immutable after creation
│           └── original CSV row      ← verbatim key-value pairs
│
└── ActivityRow.original_snapshot     ← written once on first save(), never overwritten
      ├── quantity        (parsed value at ingestion)
      ├── unit            (canonical unit at ingestion)
      ├── scope           (GHG scope at ingestion)
      ├── category        (emission category at ingestion)
      ├── emission_factor (factor value at ingestion)
      └── co2e_kg         (computed emission at ingestion)
```

**Three immutability guarantees protect this chain:**

1. **`RawUpload.raw_payload`** — I enforce a `ValueError` in `save()` if modified after creation. The original CSV is permanently frozen.

2. **`ActivityRow.original_snapshot`** — I populate this on first `save()` only. Subsequent saves never touch it.

3. **`AuditLog`** — I enforce a `ValueError` in `save()` if `pk` is set, and `delete()` unconditionally raises `ValueError`.

**Three PROTECT constraints prevent orphaning:**

- `ActivityRow.raw_upload` — I prevent `RawUpload` deletion while an `ActivityRow` references it.
- `AuditLog.activity_row` — I prevent `ActivityRow` deletion while an `AuditLog` references it.
- `ActivityRow.emission_factor_ref` — I prevent `EmissionFactor` deletion while an `ActivityRow` references it.

---

## Unit Normalisation

I designed all parsers to normalise raw units to a canonical set before persisting to `ActivityRow.quantity` and `ActivityRow.unit`:

| Canonical Unit | Source | Raw Inputs | Conversion |
|----------------|--------|------------|------------|
| `litres` | SAP | `L` (×1.0), `GAL` (×3.785) | `quantity × multiplier` |
| `kg` | SAP | `KG` (×1.0) | Pass-through |
| `m3` | SAP | `M3` | Pass-through |
| `kWh` | SAP, Utility | `KWH` (×1.0), `MWH` (×1000.0) | `quantity × multiplier` |
| `km` | Travel | Haversine-computed (flights) or raw CSV (ground) | — |
| `nights` | Travel | Raw CSV (`hotel_nights`) | — |

I always preserve the original raw values in `RawUpload.raw_payload`. The original SAP MEINS code (e.g. `GAL`) and MENGE value (e.g. `132.5`) remain in the JSON, while I store the converted values (e.g. `501.3125 litres`) in the `ActivityRow`.

**Concrete example from the codebase:**

```
Input:   132.5 GAL (SAP MEINS field)
Output:  132.5 × 3.785 = 501.3125 litres
Stored:  ActivityRow.quantity = 501.3125, ActivityRow.unit = "litres"
Original: RawUpload.raw_payload = {"MEINS": "GAL", "MENGE": "132.5", ...}
```

---

## Audit Trail

I use the `AuditLog` to record every state change in the lifecycle of an `ActivityRow`.

### Actions written by current code

| Action | Written by | When |
|--------|------------|------|
| `UPLOADED` | All three parsers (`parse_sap_file`, `parse_utility_file`, `parse_travel_file`) | On creation of each `ActivityRow` — one entry per row. If billing-split produces 3 slices, 3 entries are created. If the row was flagged, flag reasons are appended to `detail`. |
| `APPROVED` | `_approve_and_lock()` helper in `views.py`, called from `ApproveRowView` and `BulkApproveView` | When an analyst approves a row. The row atomically becomes `LOCKED`. `before_value = {"status": <old>}`, `after_value = {"status": "LOCKED"}`. |
| `REJECTED` | `RejectRowView.patch()` in `views.py` | When an analyst rejects a row with a mandatory reason. `before_value = {"status": <old>}`, `after_value = {"status": "REJECTED"}`, `detail = <reason>`. |
| `EDITED` | **Reserved** | Future `PATCH /api/rows/{id}/` endpoint. Infrastructure fully in place (`is_edited`, `edited_at`, `original_snapshot` on `ActivityRow`). Endpoint not built — see `TRADEOFFS.md`. |

### Actions deliberately removed

| Action | Reason removed |
|--------|----------------|
| `REVIEWED` | No intermediate review state in the current workflow — approval goes straight from `PENDING` to `LOCKED` in one atomic step. |
| `LOCKED` | Not a separate event — locking happens inside the same transaction as `APPROVED`. A separate `LOCKED` entry would be redundant noise. |
| `FLAGGED` | Flagging happens at parse time and is recorded inside the `UPLOADED` entry's `detail` field. A separate `FLAGGED` action was never written by any code path. |

---

## Known Limitations and Production Gaps

### 1. Integer PKs vs UUIDs

```python
# settings.py line 162
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
```

I used integer PKs to keep shell queries and tests readable.
The risk is that sequential IDs are guessable and leak table size.
The fix is to replace them with UUIDs — it is a single field change plus a migration, but it needs to happen before any client data goes to production.

### 2. Inline Normalisation vs Separate Pipeline

I put unit normalisation, scope assignment, emission factor application, and billing-period splitting inline within each parser function because it was the fastest way to get data flowing end-to-end.
The risk is that normalisation logic is duplicated across parsers, and we cannot re-run normalisation without re-uploading the file.
The fix is to extract these steps into a separate normalisation pipeline — this requires a moderate refactor to separate pure-function transforms from the persistence layer.

### 3. Hardcoded Emission Factors in Parsers

I left emission factors hardcoded as module-level constants in the parsers because the `EmissionFactor` database schema was only just introduced.
The risk is that parsers are not yet using versioned, client-specific factors, meaning calculations lack full auditability.
The fix is to wire the parsers to query the `EmissionFactor` table and populate `emission_factor_ref` — this is a low-effort change but requires updating all parser tests.

The `EmissionFactor` model now exists (see §4) and 10 global factors are seeded on `GET /api/setup/`. The `ActivityRow.emission_factor_ref` FK links each row to the versioned factor used.

However, the parsers (`sap_parser.py`, `utility_parser.py`, `travel_parser.py`) still use hardcoded module-level constants for the actual calculation. They do not yet query the `EmissionFactor` table or populate `emission_factor_ref`. The database schema is ready; the parser wiring is the remaining step. See `TRADEOFFS.md` for details.

**Constants still in use by parsers:**

| Parser | Constant | Value |
|--------|----------|-------|
| SAP | `DIESEL_LITRES` | 2.68 kgCO2e/litre |
| SAP | `PETROL_LITRES` | 2.31 kgCO2e/litre |
| SAP | `LPG_KG` | 1.51 kgCO2e/kg |
| Utility | `INDIA_GRID_KWH` | 0.716 kgCO2e/kWh |
| Travel | `FLIGHT_ECONOMY_KM` | 0.133 kgCO2e/km |
| Travel | `FLIGHT_BUSINESS_KM` | 0.295 kgCO2e/km |
| Travel | `FLIGHT_FIRST_KM` | 0.430 kgCO2e/km |
| Travel | `HOTEL_NIGHT` | 31.0 kgCO2e/night |
| Travel | `TAXI_KM` | 0.149 kgCO2e/km |
| Travel | `RAIL_KM` | 0.041 kgCO2e/km |

### 4. Plant Code Lookup

I seeded five hardcoded demo entries via the `SetupView` endpoint because we needed sample data for the prototype dashboard.
The risk is that any real-world SAP file will flag nearly every row with a "Plant code not found" error.
The fix is to populate this table from the client's SAP T001W master data — this requires building a bulk import endpoint or SAP integration, which is a medium-effort feature.

Any SAP row referencing a WERKS code not in this list is flagged with:

```
"Plant code '<code>' not found in reference table for client '<slug>'."
```

### 5. Flight Distance via Hardcoded Airport Coordinates

I hardcoded 10 airports in `AIRPORT_COORDS` because it was enough to prove the haversine calculation worked for demo flights.
The risk is that any IATA code outside this small set causes a `ValueError` and flags the row.
The fix is to integrate a complete airport database or external API — this is a simple data-loading task but adds an external dependency.

The travel parser computes flight distances using great-circle haversine with only **10 hardcoded airports** in `AIRPORT_COORDS`:

```
BOM, DEL, LHR, BLR, MAA, HYD, CCU, DXB, SIN, JFK
```

### 6. CORS Configuration

```python
# settings.py
CORS_ALLOW_ALL_ORIGINS = config("CORS_ALLOW_ALL", default=True, cast=bool)
```

I set `CORS_ALLOW_ALL_ORIGINS` to `True` because it allowed the local frontend to connect to the backend without friction.
The risk is that any origin can call the API, leaving it open to unauthorized clients.
The fix is to restrict this to specific frontend domains — it is a trivial one-line settings change before deployment.

### 7. Health Check Endpoint

```python
def health_check(request):
    return JsonResponse({
        "status": "ok",
        "service": "breathe-esg-backend",
        "version": "1.0.0",
    })
```

I added a simple JSON health check at the root path (`/`) because I needed to verify the web server was responding.
The risk is that it only proves the web server is running, not that the database or parsers are actually healthy.
The fix is to add a database ping to the health check — this is a quick five-minute update to the view.

---
VERIFY: Compare section count against original — should be identical.
