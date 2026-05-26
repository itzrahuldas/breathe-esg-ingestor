"""
ingestor/parsers/travel_parser.py
====================================
Rule-based parser for corporate travel booking CSV exports.

Handles three travel types:
  FLIGHT — great-circle distance via haversine + cabin-class emission factor
  HOTEL  — per-night emission factor
  GROUND — per-km emission factor by transport mode (TAXI, RAIL, …)

All travel rows → scope=3 (upstream / business travel).

NO ML, NO FUZZY MATCHING, NO EXTERNAL API CALLS.
Distance lookup is purely from the hardcoded AIRPORT_COORDS dict.

Hard constraints honoured:
  1. raw_payload stored once on RawUpload, never mutated.
  2. ActivityRow status starts PENDING or FLAGGED — never LOCKED on creation.
  3. AuditLog row written with action='UPLOADED' per ActivityRow.
  4. client ForeignKey always passed via client_id parameter.
"""

import csv
import io
import logging
import math
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import IO

from ingestor.models import ActivityRow, AuditLog, Client, RawUpload

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hardcoded airport coordinates (lat, lon) — no API calls ever
# ---------------------------------------------------------------------------

AIRPORT_COORDS: dict[str, tuple[float, float]] = {
    "BOM": (19.0896, 72.8656),
    "DEL": (28.5562, 77.1000),
    "LHR": (51.4775, -0.4614),
    "BLR": (13.1986, 77.7066),
    "MAA": (12.9941, 80.1709),
    "HYD": (17.2403, 78.4294),
    "CCU": (22.6520, 88.4463),
    "DXB": (25.2528, 55.3644),
    "SIN": (1.3644, 103.9915),
    "JFK": (40.6413, -73.7781),
}

# ---------------------------------------------------------------------------
# Hardcoded emission factors (DEFRA 2023, with RFI applied to flights)
# ---------------------------------------------------------------------------

#: kgCO2e per passenger-km — economy class, includes RFI — DEFRA 2023
FLIGHT_ECONOMY_KM: float = 0.133

#: kgCO2e per passenger-km — business class — DEFRA 2023
FLIGHT_BUSINESS_KM: float = 0.295

#: kgCO2e per passenger-km — first class — DEFRA 2023
FLIGHT_FIRST_KM: float = 0.430

#: kgCO2e per hotel room-night — DEFRA 2023
HOTEL_NIGHT: float = 31.0

#: kgCO2e per km — taxi / private car — DEFRA 2023
TAXI_KM: float = 0.149

#: kgCO2e per km — rail — DEFRA 2023
RAIL_KM: float = 0.041

# Cabin-class → emission factor lookup
_CABIN_FACTORS: dict[str, float] = {
    "ECONOMY":       FLIGHT_ECONOMY_KM,
    "ECONOMY PLUS":  FLIGHT_ECONOMY_KM,    # treat as economy
    "PREMIUM":       FLIGHT_BUSINESS_KM,   # treat as business
    "BUSINESS":      FLIGHT_BUSINESS_KM,
    "FIRST":         FLIGHT_FIRST_KM,
}

# Transport-mode → emission factor lookup
_GROUND_FACTORS: dict[str, float] = {
    "TAXI":  TAXI_KM,
    "CAR":   TAXI_KM,
    "RAIL":  RAIL_KM,
    "TRAIN": RAIL_KM,
}


# ===========================================================================
# Public API — pure functions (no DB access)
# ===========================================================================


def haversine_km(coord1: tuple[float, float], coord2: tuple[float, float]) -> float:
    """
    Compute the great-circle distance between two points using the
    haversine formula.

    Parameters
    ----------
    coord1, coord2:
        ``(latitude, longitude)`` in decimal degrees.

    Returns
    -------
    float
        Distance in kilometres.
    """
    R = 6371  # Earth's mean radius in km
    lat1 = math.radians(coord1[0])
    lon1 = math.radians(coord1[1])
    lat2 = math.radians(coord2[0])
    lon2 = math.radians(coord2[1])

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.asin(math.sqrt(a))


def get_flight_distance_km(origin_iata: str, destination_iata: str) -> float:
    """
    Return the one-way haversine distance in km between two IATA airport codes.

    Both codes are looked up in :data:`AIRPORT_COORDS`. If either is absent,
    a ``ValueError`` is raised — **no external API is ever called**.

    Parameters
    ----------
    origin_iata:
        3-letter IATA code of the departure airport (case-insensitive).
    destination_iata:
        3-letter IATA code of the arrival airport (case-insensitive).

    Returns
    -------
    float
        Great-circle distance in kilometres.

    Raises
    ------
    ValueError
        If either IATA code is not present in :data:`AIRPORT_COORDS`.
    """
    origin = (origin_iata or "").strip().upper()
    destination = (destination_iata or "").strip().upper()

    if origin not in AIRPORT_COORDS or destination not in AIRPORT_COORDS:
        raise ValueError(
            f"Unknown IATA code: {origin_iata!r} or {destination_iata!r}. "
            "Supported codes: " + ", ".join(sorted(AIRPORT_COORDS.keys()))
        )
    return haversine_km(AIRPORT_COORDS[origin], AIRPORT_COORDS[destination])


def is_return_trip(return_date_val: str | None) -> bool:
    """
    Determine whether a booking includes a return leg.

    A booking is considered a return trip when ``return_date_val`` is a
    non-empty string that can be parsed as a date (``YYYY-MM-DD``).

    Parameters
    ----------
    return_date_val:
        Raw string from the ``return_date`` CSV field.

    Returns
    -------
    bool
        ``True`` if a parseable return date is present, ``False`` otherwise.
    """
    if not return_date_val or not str(return_date_val).strip():
        return False
    s = str(return_date_val).strip()
    # Accept YYYY-MM-DD only (the format used in this CSV)
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        try:
            year, month, day = s.split("-")
            date(int(year), int(month), int(day))  # validate
            return True
        except ValueError:
            pass
    return False


# ---------------------------------------------------------------------------
# Private date parser (travel CSV uses YYYY-MM-DD throughout)
# ---------------------------------------------------------------------------

def _parse_travel_date(date_str: str) -> date | None:
    """Parse a YYYY-MM-DD travel date; return None if blank/invalid."""
    if not date_str or not date_str.strip():
        return None
    s = date_str.strip()
    try:
        year, month, day = s.split("-")
        return date(int(year), int(month), int(day))
    except (ValueError, AttributeError):
        return None


# ===========================================================================
# DB-touching orchestrator
# ===========================================================================


def parse_travel_file(file_obj: IO[str], client_id: int, user_id: int) -> list[dict]:
    """
    Parse a corporate travel booking CSV and persist results to the database.

    Each CSV row produces exactly **one** ``ActivityRow`` regardless of travel
    type.  A return flight doubles the distance before computing emissions.

    Processing per travel type
    --------------------------
    **FLIGHT**
      a. ``get_flight_distance_km(origin, destination)``
      b. If ``return_date`` present: distance × 2
      c. Pick emission factor from ``cabin_class``
      d. ``co2e_kg = distance × factor``
      e. ``category = 'business_travel_air'``
      f. Flag if IATA code not in :data:`AIRPORT_COORDS`

    **HOTEL**
      a. ``co2e_kg = hotel_nights × HOTEL_NIGHT``
      b. ``category = 'business_travel_hotel'``
      c. Flag if ``hotel_nights`` is missing or ≤ 0

    **GROUND**
      a. ``co2e_kg = ground_km × factor`` (by ``transport_mode``)
      b. ``category = 'business_travel_ground'``
      c. Flag if ``ground_km`` is missing/≤ 0 or ``transport_mode`` unknown

    All rows → ``scope = 3``

    Parameters
    ----------
    file_obj:
        File-like object (text mode) wrapping the CSV content.
    client_id:
        PK of the :class:`~ingestor.models.Client` tenant.
    user_id:
        PK of the acting user.

    Returns
    -------
    list[dict]
        One entry per created ``ActivityRow``:
        ``{'activity_row_id': int, 'booking_id': str, 'travel_type': str,
           'co2e_kg': float | None, 'is_flagged': bool}``

    Raises
    ------
    Client.DoesNotExist
        If ``client_id`` does not correspond to a known tenant.
    """
    client = Client.objects.get(pk=client_id)
    results: list[dict] = []

    if isinstance(file_obj, (bytes, bytearray)):
        file_obj = io.StringIO(file_obj.decode("utf-8-sig"))
    elif hasattr(file_obj, "mode") and "b" in getattr(file_obj, "mode", ""):
        file_obj = io.TextIOWrapper(file_obj, encoding="utf-8-sig")

    reader = csv.DictReader(file_obj)

    for raw_row in reader:
        original_row: dict = dict(raw_row)

        def _s(key: str) -> str:
            return (raw_row.get(key) or "").strip()

        booking_id   = _s("booking_id")
        travel_type  = _s("travel_type").upper()
        travel_date  = _parse_travel_date(_s("travel_date"))
        return_date  = _s("return_date")

        flags: list[str] = []
        co2e_kg: float | None = None
        emission_factor_val: float | None = None
        quantity: float = 0.0
        unit: str = ""
        category: str = ""
        description: str = ""
        plant_code: str = booking_id

        # ==================================================================
        if travel_type == "FLIGHT":
            origin      = _s("origin").upper()
            destination = _s("destination").upper()
            cabin       = _s("cabin_class").upper() or "ECONOMY"
            category    = "business_travel_air"
            description = f"{origin}→{destination} ({cabin})"
            unit        = "km"

            # a. Distance
            distance_km: float = 0.0
            try:
                distance_km = get_flight_distance_km(origin, destination)
            except ValueError as exc:
                flags.append(str(exc))

            # b. Return trip
            is_return = is_return_trip(return_date)
            if is_return and distance_km > 0:
                distance_km *= 2

            quantity = distance_km

            # c. Cabin class factor
            ef = _CABIN_FACTORS.get(cabin)
            if ef is None:
                flags.append(
                    f"Unknown cabin class '{cabin}'. "
                    f"Supported: {', '.join(_CABIN_FACTORS.keys())}."
                )
            else:
                emission_factor_val = ef

            # d. Emissions
            if distance_km > 0 and ef is not None:
                co2e_kg = round(distance_km * ef, 4)

        # ==================================================================
        elif travel_type == "HOTEL":
            hotel_city   = _s("hotel_city")
            category     = "business_travel_hotel"
            description  = hotel_city or "Hotel stay"
            unit         = "nights"

            raw_nights = _s("hotel_nights")
            try:
                hotel_nights = float(raw_nights)
            except (ValueError, TypeError):
                hotel_nights = 0.0
                flags.append(f"Cannot parse hotel_nights '{raw_nights}'.")

            if hotel_nights <= 0:
                flags.append("Zero or missing hotel nights.")
            else:
                emission_factor_val = HOTEL_NIGHT
                co2e_kg = round(hotel_nights * HOTEL_NIGHT, 4)

            quantity = hotel_nights

        # ==================================================================
        elif travel_type == "GROUND":
            transport_mode = _s("transport_mode").upper()
            category       = "business_travel_ground"
            description    = f"{_s('origin')}→{_s('destination')} ({transport_mode})"
            unit           = "km"

            raw_km = _s("ground_km")
            try:
                ground_km = float(raw_km)
            except (ValueError, TypeError):
                ground_km = 0.0
                flags.append(f"Cannot parse ground_km '{raw_km}'.")

            if ground_km <= 0:
                flags.append("Zero or missing ground distance (km).")

            ef_ground = _GROUND_FACTORS.get(transport_mode)
            if ef_ground is None:
                flags.append(
                    f"Unknown transport_mode '{transport_mode}'. "
                    f"Supported: {', '.join(_GROUND_FACTORS.keys())}."
                )
            else:
                emission_factor_val = ef_ground

            if ground_km > 0 and ef_ground is not None:
                co2e_kg = round(ground_km * ef_ground, 4)

            quantity = ground_km

        # ==================================================================
        else:
            flags.append(f"Unknown travel_type '{travel_type}'.")
            category = "business_travel_unknown"
            description = f"Booking {booking_id}"

        # Aggregate flags
        flag_reason = " | ".join(f for f in flags if f)
        is_flagged = bool(flag_reason)
        row_status = ActivityRow.STATUS_FLAGGED if is_flagged else ActivityRow.STATUS_PENDING

        # ------------------------------------------------------------------ RawUpload (immutable)
        raw_upload = RawUpload.objects.create(
            client=client,
            uploaded_by_id=user_id,
            source_system=RawUpload.SOURCE_TRAVEL,
            raw_payload=original_row,
        )

        # ------------------------------------------------------------------ ActivityRow
        try:
            qty_decimal = Decimal(str(quantity))
        except InvalidOperation:
            qty_decimal = Decimal("0")

        activity_row = ActivityRow.objects.create(
            client=client,
            raw_upload=raw_upload,
            plant_code=plant_code,
            material_number="",
            description=description,
            document_date=travel_date,
            posting_date=travel_date,
            quantity=qty_decimal,
            unit=unit,
            scope=3,                              # All travel → Scope 3
            category=category,
            emission_factor=(
                Decimal(str(emission_factor_val))
                if emission_factor_val is not None
                else None
            ),
            co2e_kg=Decimal(str(co2e_kg)) if co2e_kg is not None else None,
            status=row_status,
            is_flagged=is_flagged,
            flag_reason=flag_reason,
        )

        # ------------------------------------------------------------------ AuditLog
        AuditLog.objects.create(
            client=client,
            activity_row=activity_row,
            actor_id=user_id,
            action=AuditLog.ACTION_UPLOADED,
            detail=(
                f"Parsed from travel CSV. "
                f"raw_upload_id={raw_upload.pk}. "
                f"booking={booking_id}, type={travel_type}, "
                f"qty={quantity:.4f} {unit}, co2e={co2e_kg} kgCO2e."
                + (f" FLAGS: {flag_reason}" if flag_reason else "")
            ),
        )

        results.append(
            {
                "activity_row_id": activity_row.pk,
                "booking_id": booking_id,
                "travel_type": travel_type,
                "co2e_kg": co2e_kg,
                "is_flagged": is_flagged,
            }
        )

        logger.info(
            "Travel row processed: ActivityRow#%d client=%s booking=%s type=%s flagged=%s",
            activity_row.pk, client.slug, booking_id, travel_type, is_flagged,
        )

    return results
