# Decisions

## Ingestion mechanism choices

### SAP: Flat file CSV (MB51 format)

**Why not IDoc:**
IDoc is SAP's native EDI format. Parsing it requires EDI middleware
and deep SAP system access. No enterprise client can provide IDoc
access in a 4-day onboarding window.

**Why not OData:**
OData requires a live SAP system, OAuth 2.0 setup, and IT approval
from the client's SAP team — typically a 2-4 week process.

**Why flat file CSV:**
Any SAP admin can run MB51 transaction and export to CSV in under
5 minutes. No API access, no middleware, no IT approval needed.
Covers the fuel and procurement use case completely.

**Tradeoff acknowledged:**
Flat file is a point-in-time snapshot. Real-time ingestion would
need OData or a scheduled SFTP pull.

---

### Utility: Portal CSV export

**Why not PDF:**
Utility PDFs have no standard layout. OCR pipelines break every time
a utility redesigns their bill template. Maintenance cost is high.

**Why portal CSV:**
All major Indian utilities (BESCOM, MSEDCL, Tata Power) and most
international utilities offer portal CSV downloads. Facilities teams
already download these for internal reconciliation.

**Tradeoff acknowledged:**
Approximately 15% of utility clients only have PDF access. A PDF
fallback is not built — see TRADEOFFS.md.

---

### Travel: Concur-style CSV export

**Why not live Concur API:**
Concur API access requires OAuth approval from the client's IT
security team. Enterprise procurement typically takes 2-4 weeks.

**Why CSV export:**
CSV export is available to any travel manager today with no IT
involvement. Same data, zero integration overhead for a prototype.

**Tradeoff acknowledged:**
CSV is a manual export. Real-time ingestion would need the Concur
or Navan API with proper OAuth flow.

---

## Data model decisions

### Why RawUpload is immutable

If normalisation has a bug — wrong unit conversion, wrong date parse —
we re-run the parser against the stored `raw_payload` without asking
the client to re-upload. The original is always recoverable.
Enforced in `save()` — raises `ValueError` if `pk` already exists.

### Why JSONField for raw_payload

Each source has different columns. A fixed schema would require
three separate raw tables or dozens of nullable columns.
`JSONField` stores whatever came in, in whatever shape, without
forcing a structure we don't control.

### Why original values are in RawUpload.raw_payload, not ActivityRow

Transparency for the analyst. The original CSV values (e.g. `132.5 GAL`)
are preserved verbatim in `RawUpload.raw_payload` as a JSON dict.
`ActivityRow` stores only the normalised values (e.g. `501.3125 litres`).
An auditor traces back via the `raw_upload` FK to verify the conversion.
This avoids duplicating fields on `ActivityRow` while preserving full
traceability. The `original_snapshot` JSON field captures the normalised
values at ingestion time so edits are also traceable.

### Why integer PKs instead of UUIDs

Prototype simplicity. Integer PKs are simpler to work with in
tests and shell queries. UUID would be better in production
to prevent enumeration attacks — documented in MODEL.md Known Limitations.

### Why LOCKED is not a separate AuditLog action

LOCKED happens inside the same database transaction as APPROVED.
Recording both would duplicate the audit entry for a single user
action. The APPROVED entry records the full transition including
the lock (`before_value={"status": <old>}`, `after_value={"status": "LOCKED"}`).

### Why billing periods are split proportionally

A bill covering 18 Jan – 21 Feb (35 days) is split:
Jan = 13/35 × consumption, Feb = 22/35 × consumption.
This is approximate but transparent, documented, and consistent.
The alternative — holding the full amount in one month — would
misrepresent monthly emissions in dashboard comparisons.
Implementation: `split_billing_period()` in `utility_parser.py`.

---

## Scope assignment rules

| Source | Rule | Scope | GHG Category |
|--------|------|-------|--------------|
| SAP | `material_number` starts with `FUL-` OR description contains any of: fuel, diesel, petrol, lpg, gas | 1 | `stationary_combustion` |
| SAP | `material_number` starts with `ELEC-` | 2 | `purchased_electricity` |
| SAP | Default (no rule matches) | 3 | `purchased_goods` |
| Utility | All rows — hardcoded | 2 | `purchased_electricity` |
| Travel (FLIGHT) | All flight bookings | 3 | `business_travel_air` |
| Travel (HOTEL) | All hotel bookings | 3 | `business_travel_hotel` |
| Travel (GROUND) | All ground transport bookings | 3 | `business_travel_ground` |
| Travel (unknown type) | Fallback | 3 | `business_travel_unknown` |

---

## What I would ask the PM before real deployment

1. **Do any clients only have PDF utility bills?**
   (Determines whether an OCR pipeline is in scope — see TRADEOFFS.md)

2. **Should Scope 3 include supply chain (Category 1) or only travel (Category 6)?**
   (Determines how SAP procurement rows with no `FUL-` prefix are classified.
   Currently they default to `purchased_goods` / Scope 3, which may or may
   not align with the client's GHG inventory boundary.)

3. **Which emission factor database does your audit team accept?**
   (DEFRA 2023 is used throughout. Some Indian regulators require CEA factors
   for electricity, which is already in place. Some clients may require
   EPA, GHG Protocol, or a client-specific government mandate.)

4. **Is multi-user access needed before go-live?**
   (Analyst vs approver vs read-only auditor roles — see TRADEOFFS.md.
   Currently any caller can approve any row with no authentication.)

5. **Should editing a flagged row auto-clear the flag?**
   (Core product question for the future `PATCH /api/rows/{id}/` endpoint.
   The edit infrastructure exists; the policy decision does not.)

6. **What is the expected data volume per client per month?**
   (Determines whether async processing via Celery is needed. The current
   synchronous parser will time out on uploads larger than ~5,000 rows
   in a single request given typical server timeouts.)
