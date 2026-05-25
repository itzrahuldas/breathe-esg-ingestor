"""
ingestor/parsers/sap_parser.py
================================
Rule-based parser for SAP MB51 flat-file CSV exports.

Handles:
  - German and English SAP column headers
  - Date formats: YYYYMMDD, DD.MM.YYYY, YYYY-MM-DD
  - Unit normalisation (L, GAL, KG, M3, KWH) → internal canonical units
  - GHG scope assignment via material-number prefix / description keywords
  - Unknown plant codes  → flagged row (no exception)
  - Negative quantities  → flagged row (no exception)
  - Blank BUDAT          → falls back to BLDAT

NO ML, NO FUZZY MATCHING.  All logic is deterministic and rule-based.

Hard constraints honoured here:
  1. raw_payload is set once on RawUpload creation and never touched again.
  2. ActivityRow.status starts as PENDING (never LOCKED on creation).
  3. AuditLog row written with action='UPLOADED' for every ActivityRow created.
  4. client ForeignKey always passed in via client_id parameter.
"""

import csv
import io
import logging
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import IO

from ingestor.models import ActivityRow, AuditLog, Client, PlantCode, RawUpload

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hardcoded emission factors (DEFRA 2023, source noted per factor)
# ---------------------------------------------------------------------------

#: kgCO2e per litre of diesel — DEFRA 2023
DIESEL_LITRES: float = 2.68

#: kgCO2e per litre of petrol — DEFRA 2023
PETROL_LITRES: float = 2.31

#: kgCO2e per kg of LPG — DEFRA 2023
LPG_KG: float = 1.51


# ---------------------------------------------------------------------------
# SAP header normalisation map (German & English variants)
# ---------------------------------------------------------------------------

HEADER_MAP: dict[str, str] = {
    "WERKS": "plant_code",  "Werks": "plant_code",
    "MATNR": "material_number", "Matnr": "material_number",
    "MENGE": "quantity",    "Menge": "quantity",
    "MEINS": "unit",        "Meins": "unit",
    "BLDAT": "document_date", "Bldat": "document_date",
    "BUDAT": "posting_date",  "Budat": "posting_date",
    "BKTXT": "description", "Bktxt": "description",
}

# ---------------------------------------------------------------------------
# Unit normalisation table: MEINS → (multiplier, canonical_unit)
# ---------------------------------------------------------------------------

_UNIT_TABLE: dict[str, tuple[float, str]] = {
    "L":   (1.0,   "litres"),
    "GAL": (3.785, "litres"),   # US gallon → litres
    "KG":  (1.0,   "kg"),
    "M3":  (1.0,   "m3"),
    "KWH": (1.0,   "kWh"),
}

# ---------------------------------------------------------------------------
# Scope assignment keywords (all lowercase for case-insensitive matching)
# ---------------------------------------------------------------------------

_FUEL_KEYWORDS: frozenset[str] = frozenset({"fuel", "diesel", "petrol", "lpg", "gas"})


# ===========================================================================
# Public API — pure functions (no DB access)
# ===========================================================================


def parse_sap_date(date_str: str) -> date:
    """
    Parse a SAP date string into a :class:`datetime.date`.

    Supported formats (tried in order):
      1. ``YYYYMMDD``    e.g. ``'20241231'``
      2. ``DD.MM.YYYY``  e.g. ``'31.12.2024'``
      3. ``YYYY-MM-DD``  e.g. ``'2024-12-31'``

    Parameters
    ----------
    date_str:
        Raw string value from the SAP export cell.

    Returns
    -------
    datetime.date

    Raises
    ------
    ValueError
        If ``date_str`` is blank/None or does not match any known format.
    """
    if not date_str or not date_str.strip():
        raise ValueError("SAP date string is empty or blank.")

    date_str = date_str.strip()

    # Format 1: YYYYMMDD (8 digits, no separators)
    if len(date_str) == 8 and date_str.isdigit():
        try:
            return date(
                year=int(date_str[0:4]),
                month=int(date_str[4:6]),
                day=int(date_str[6:8]),
            )
        except ValueError:
            pass  # fall through to next format

    # Format 2: DD.MM.YYYY
    if len(date_str) == 10 and date_str[2] == "." and date_str[5] == ".":
        try:
            day, month, year = date_str.split(".")
            return date(year=int(year), month=int(month), day=int(day))
        except (ValueError, AttributeError):
            pass

    # Format 3: YYYY-MM-DD  (ISO 8601)
    if len(date_str) == 10 and date_str[4] == "-" and date_str[7] == "-":
        try:
            year, month, day = date_str.split("-")
            return date(year=int(year), month=int(month), day=int(day))
        except (ValueError, AttributeError):
            pass

    raise ValueError(
        f"Cannot parse SAP date '{date_str}'. "
        "Expected formats: YYYYMMDD, DD.MM.YYYY, YYYY-MM-DD."
    )


def normalize_sap_unit(meins: str, menge: float) -> tuple[float, str]:
    """
    Convert a SAP unit-of-measure code and quantity into a canonical form.

    Parameters
    ----------
    meins:
        SAP MEINS field value (e.g. ``'L'``, ``'GAL'``, ``'KG'``).
    menge:
        Raw numeric quantity from the SAP row.

    Returns
    -------
    tuple[float, str]
        ``(normalised_quantity, canonical_unit)``
        e.g. ``(501.3225, 'litres')`` for 132.5 GAL.

    Raises
    ------
    ValueError
        If ``meins`` is not in the known unit table.
    """
    key = meins.strip().upper()
    if key not in _UNIT_TABLE:
        raise ValueError(f"Unknown SAP unit: {meins!r}")
    multiplier, canonical = _UNIT_TABLE[key]
    return menge * multiplier, canonical


def normalize_sap_headers(row: dict) -> dict:
    """
    Remap a raw SAP CSV row's keys to internal snake_case field names.

    Handles both German-capitalised (``WERKS``) and mixed-case (``Werks``) variants.
    Keys not present in :data:`HEADER_MAP` are passed through unchanged.

    Parameters
    ----------
    row:
        A single row dict from :class:`csv.DictReader`.

    Returns
    -------
    dict
        New dict with normalised keys.
    """
    return {HEADER_MAP.get(k, k): v for k, v in row.items()}


def assign_scope_from_sap(
    material_number: str, description: str
) -> tuple[int, str]:
    """
    Assign a GHG scope and category based on material number prefix and
    description keywords.  **Rule-based only — no ML or fuzzy matching.**

    Rules (evaluated in priority order):
      1. ``material_number`` starts with ``'FUL-'``
         OR ``description`` contains any of {fuel, diesel, petrol, lpg, gas}
         → Scope 1, ``'stationary_combustion'``
      2. ``material_number`` starts with ``'ELEC-'``
         → Scope 2, ``'purchased_electricity'``
      3. Default
         → Scope 3, ``'purchased_goods'``

    Parameters
    ----------
    material_number:
        Normalised MATNR value (e.g. ``'FUL-001'``).
    description:
        Normalised BKTXT value (e.g. ``'Diesel Mumbai'``).

    Returns
    -------
    tuple[int, str]
        ``(scope_int, category_str)``
    """
    mat = (material_number or "").strip()
    desc = (description or "").strip().lower()

    # Rule 1 — Scope 1: direct fuel combustion
    if mat.upper().startswith("FUL-") or any(kw in desc for kw in _FUEL_KEYWORDS):
        return 1, "stationary_combustion"

    # Rule 2 — Scope 2: purchased electricity
    if mat.upper().startswith("ELEC-"):
        return 2, "purchased_electricity"

    # Default — Scope 3: upstream / purchased goods
    return 3, "purchased_goods"


# ===========================================================================
# DB-touching orchestrator
# ===========================================================================


def parse_sap_file(file_obj: IO[str], client_id: int, user_id: int) -> list[int]:
    """
    Parse an SAP MB51 CSV export and persist the results to the database.

    Steps for each CSV row:
      a. Normalise headers via :func:`normalize_sap_headers`.
      b. Parse ``document_date``; fall back to ``document_date`` if ``posting_date``
         is blank.
      c. Normalise unit and quantity via :func:`normalize_sap_unit`.
      d. Assign GHG scope via :func:`assign_scope_from_sap`.
      e. Look up ``plant_code`` in :class:`~ingestor.models.PlantCode`;
         if not found → set ``is_flagged=True`` and record ``flag_reason``.
      f. If quantity ≤ 0 → set ``is_flagged=True`` and record ``flag_reason``.
      g. Create :class:`~ingestor.models.RawUpload` with ``raw_payload`` =
         original (un-normalised) row dict.  **Never modified after this.**
      h. Create :class:`~ingestor.models.ActivityRow` with all normalised fields.
      i. Create :class:`~ingestor.models.AuditLog` with ``action='UPLOADED'``.

    Parameters
    ----------
    file_obj:
        A file-like object (text mode) wrapping the CSV content.
    client_id:
        PK of the :class:`~ingestor.models.Client` tenant.
    user_id:
        PK of the acting user (stored on RawUpload and AuditLog).

    Returns
    -------
    list[int]
        Ordered list of created :class:`~ingestor.models.ActivityRow` PKs.

    Raises
    ------
    Client.DoesNotExist
        If ``client_id`` does not correspond to a known tenant.
    """
    client = Client.objects.get(pk=client_id)
    created_ids: list[int] = []

    # Wrap raw bytes in a text wrapper if necessary
    if isinstance(file_obj, (bytes, bytearray)):
        file_obj = io.StringIO(file_obj.decode("utf-8-sig"))
    elif hasattr(file_obj, "mode") and "b" in getattr(file_obj, "mode", ""):
        file_obj = io.TextIOWrapper(file_obj, encoding="utf-8-sig")

    reader = csv.DictReader(file_obj)

    for raw_row in reader:
        # Keep original for immutable storage
        original_row: dict = dict(raw_row)

        # ------------------------------------------------------------------ a
        norm = normalize_sap_headers(raw_row)

        # ------------------------------------------------------------------ b  Date resolution
        doc_date: date | None = None
        post_date: date | None = None
        date_flag: str = ""

        raw_bldat = (norm.get("document_date") or "").strip()
        raw_budat = (norm.get("posting_date") or "").strip()

        try:
            doc_date = parse_sap_date(raw_bldat)
        except ValueError as exc:
            date_flag += f"document_date parse error: {exc}. "

        if raw_budat:
            try:
                post_date = parse_sap_date(raw_budat)
            except ValueError as exc:
                date_flag += f"posting_date parse error: {exc}. "
        else:
            # Blank BUDAT → fall back to BLDAT
            post_date = doc_date

        # ------------------------------------------------------------------ c  Unit / quantity
        raw_menge = (norm.get("quantity") or "").strip()
        raw_meins = (norm.get("unit") or "").strip()

        try:
            menge_float = float(raw_menge)
        except (ValueError, TypeError):
            menge_float = 0.0
            date_flag += f"Cannot parse quantity '{raw_menge}'. "

        norm_qty: float = menge_float
        norm_unit: str = raw_meins
        unit_flag: str = ""

        try:
            norm_qty, norm_unit = normalize_sap_unit(raw_meins, menge_float)
        except ValueError as exc:
            unit_flag = str(exc)

        # ------------------------------------------------------------------ d  Scope
        mat_num = (norm.get("material_number") or "").strip()
        description = (norm.get("description") or "").strip()
        scope, category = assign_scope_from_sap(mat_num, description)

        # ------------------------------------------------------------------ e  Plant code lookup
        plant_code_val = (norm.get("plant_code") or "").strip()
        plant_flag: str = ""

        plant_exists = PlantCode.objects.filter(
            client=client, code=plant_code_val
        ).exists()
        if not plant_exists:
            plant_flag = (
                f"Plant code '{plant_code_val}' not found in reference table "
                f"for client '{client.slug}'."
            )

        # ------------------------------------------------------------------ f  Negative quantity
        neg_flag: str = ""
        if menge_float <= 0:
            neg_flag = (
                f"Quantity {menge_float} is zero or negative. "
                "Row may represent a reversal or return."
            )

        # Aggregate flags
        all_flags = " | ".join(
            f for f in [date_flag.strip(), unit_flag, plant_flag, neg_flag] if f
        )
        is_flagged = bool(all_flags)
        row_status = ActivityRow.STATUS_FLAGGED if is_flagged else ActivityRow.STATUS_PENDING

        # ------------------------------------------------------------------ g  Emission estimate
        emission_factor_val: float | None = _estimate_emission_factor(
            mat_num, description, norm_unit
        )
        co2e_kg: float | None = None
        if emission_factor_val is not None and norm_qty > 0:
            co2e_kg = round(norm_qty * emission_factor_val, 4)

        # ------------------------------------------------------------------ g  RawUpload (immutable)
        raw_upload = RawUpload.objects.create(
            client=client,
            uploaded_by_id=user_id,
            source_system=RawUpload.SOURCE_SAP,
            raw_payload=original_row,   # stored once, never updated
        )

        # ------------------------------------------------------------------ h  ActivityRow
        try:
            qty_decimal = Decimal(str(norm_qty))
        except InvalidOperation:
            qty_decimal = Decimal("0")

        activity_row = ActivityRow.objects.create(
            client=client,
            raw_upload=raw_upload,
            plant_code=plant_code_val,
            material_number=mat_num,
            description=description,
            document_date=doc_date,
            posting_date=post_date,
            quantity=qty_decimal,
            unit=norm_unit or raw_meins,
            scope=scope,
            category=category,
            emission_factor=(
                Decimal(str(emission_factor_val))
                if emission_factor_val is not None
                else None
            ),
            co2e_kg=Decimal(str(co2e_kg)) if co2e_kg is not None else None,
            status=row_status,
            is_flagged=is_flagged,
            flag_reason=all_flags,
        )

        # ------------------------------------------------------------------ i  AuditLog
        AuditLog.objects.create(
            client=client,
            activity_row=activity_row,
            actor_id=user_id,
            action=AuditLog.ACTION_UPLOADED,
            detail=(
                f"Parsed from SAP CSV. "
                f"raw_upload_id={raw_upload.pk}. "
                f"plant={plant_code_val}, mat={mat_num}, "
                f"qty={norm_qty:.4f} {norm_unit}."
                + (f" FLAGS: {all_flags}" if all_flags else "")
            ),
        )

        created_ids.append(activity_row.pk)
        logger.info(
            "SAP row processed: ActivityRow#%d client=%s plant=%s flagged=%s",
            activity_row.pk,
            client.slug,
            plant_code_val,
            is_flagged,
        )

    return created_ids


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _estimate_emission_factor(
    material_number: str, description: str, unit: str
) -> float | None:
    """
    Apply hardcoded DEFRA 2023 emission factors based on material and unit.

    Returns ``None`` when no factor can be confidently matched
    (avoids fabricating numbers for unknown material/unit combos).
    """
    desc_lower = description.lower()
    unit_lower = unit.lower()

    if unit_lower == "litres":
        # Petrol keyword takes priority over material-number prefix
        if "petrol" in desc_lower:
            return PETROL_LITRES
        if "diesel" in desc_lower:
            return DIESEL_LITRES
        # FUL- prefix without a specific keyword → diesel as conservative default
        if material_number.upper().startswith("FUL-"):
            return DIESEL_LITRES
        # Other fuel keywords → diesel as conservative default
        if any(kw in desc_lower for kw in _FUEL_KEYWORDS):
            return DIESEL_LITRES

    if unit_lower == "kg" and ("lpg" in desc_lower):
        return LPG_KG

    return None
