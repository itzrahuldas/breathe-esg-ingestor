# Sources Research

## Source 1 — SAP Flat File (MB51 format)

**Format researched:**
SAP MB51 transaction export — flat file CSV with SAP technical column names.
This transaction lists material documents and is the standard way SAP admins
extract fuel and procurement history.

**What I learned about the real-world format:**

*Column names used (from `HEADER_MAP` in `sap_parser.py`):*

| SAP column (EN) | SAP column (DE/mixed) | Internal field |
|-----------------|----------------------|----------------|
| `WERKS` | `Werks` | `plant_code` |
| `MATNR` | `Matnr` | `material_number` |
| `MENGE` | `Menge` | `quantity` |
| `MEINS` | `Meins` | `unit` |
| `BLDAT` | `Bldat` | `document_date` |
| `BUDAT` | `Budat` | `posting_date` |
| `BKTXT` | `Bktxt` | `description` |

*Date formats handled by `parse_sap_date()` (tried in order):*
1. `YYYYMMDD` — e.g. `20241231` (compact SAP export format)
2. `DD.MM.YYYY` — e.g. `31.12.2024` (German locale SAP format)
3. `YYYY-MM-DD` — e.g. `2024-12-31` (ISO 8601)

Blank `BUDAT` (posting date) falls back to `BLDAT` (document date) rather
than failing.

*Unit codes handled by `normalize_sap_unit()`:*

| MEINS code | Multiplier | Canonical unit |
|------------|------------|----------------|
| `L` | ×1.0 | `litres` |
| `GAL` | ×3.785 | `litres` (US gallon) |
| `KG` | ×1.0 | `kg` |
| `M3` | ×1.0 | `m3` |
| `KWH` | ×1.0 | `kWh` |

All other MEINS codes raise `ValueError` and the row is flagged.

*German and English header variants handled by `normalize_sap_headers()`:*
Both uppercase (`WERKS`) and mixed-case (`Werks`) SAP variants are mapped to
the same internal snake_case field. Keys not in `HEADER_MAP` are passed through
unchanged.

*Plant code lookup:*
For each row, the parser checks `PlantCode.objects.filter(client=client, code=plant_code_val).exists()`.
If the code is not found in the reference table for that client, `is_flagged=True` and
`flag_reason` records:
`"Plant code '<code>' not found in reference table for client '<slug>'."`

*Emission factor assignment (`_estimate_emission_factor()`):*
- Unit `litres` + description contains "petrol" → `PETROL_LITRES = 2.31` kgCO2e/litre
- Unit `litres` + description contains "diesel" → `DIESEL_LITRES = 2.68` kgCO2e/litre
- Unit `litres` + `FUL-` prefix or other fuel keyword → `DIESEL_LITRES = 2.68` (conservative default)
- Unit `kg` + description contains "lpg" → `LPG_KG = 1.51` kgCO2e/kg
- All other combinations → `None` (no fabricated number)

*Flag conditions in `parse_sap_file()`:*
1. Unknown plant code (`plant_code` not in `PlantCode` table)
2. Quantity ≤ 0 (`"Quantity N is zero or negative. Row may represent a reversal or return."`)
3. Date parse failure (either `BLDAT` or `BUDAT` cannot be parsed)
4. Unknown unit code (not in `_UNIT_TABLE`)

**What my sample data (`sample_sap.csv`) looks like and why:**

| Row | Plant | Material | Qty | Unit | Notes |
|-----|-------|----------|-----|------|-------|
| 1 | IN01 | FUL-001 | 500 L | litres | Clean diesel row → Scope 1 |
| 2 | DE07 | FUL-002 | 132.5 GAL | → 501.3 litres | GAL conversion test; blank BUDAT → falls back to BLDAT |
| 3 | IN02 | FUL-003 | 2000 KG | kg | LPG row → Scope 1; ISO date format test |
| 4 | IN01 | FUL-004 | 180 L | litres | Second clean diesel row |
| 5 | **XX99** | FUL-005 | 200 L | litres | **Flagged** — unknown plant code |
| 6 | IN02 | FUL-006 | **-50** L | litres | **Flagged** — negative quantity (reversal) |
| 7 | IN03 | FUL-007 | 350 L | litres | Clean diesel, different plant |
| 8 | DE07 | FUL-008 | 90 M3 | m3 | Natural gas; no emission factor assigned (M3 has no factor rule) |

**What would break in a real deployment:**
1. **Plant code lookup table needs seeding from SAP T001W.** The prototype seeds 5 demo
   codes (IN01, IN02, IN03, IN04, DE07). A real client may have hundreds of plant codes —
   nearly every row would be flagged until the table is populated.
2. **Material number to fuel type mapping is rule-based** (`FUL-` prefix and description
   keywords). Real SAP configs use client-specific material groups (e.g. `ZMTL-DIESEL-IND`).
   The prefix rule will miss or misclassify some real materials.
3. **Natural gas (M3) has no emission factor assigned** — `_estimate_emission_factor()`
   returns `None` for M3 units. A real deployment needs a kgCO2e/m3 factor per gas type.
4. **Currency conversion not handled** — WRBTR is not a column in our parser. Multi-currency
   clients need FX rates if cost data is required.

---

## Source 2 — Utility Portal CSV

**Format researched:**
Portal CSV export from utility billing portals (BESCOM, MSEDCL, Tata Power, and equivalent
international utilities). Facilities teams download this monthly for internal reconciliation.

**What I learned about the real-world format:**

*Column names expected (from `parse_utility_file()` in `utility_parser.py`):*

| Column | Used for |
|--------|----------|
| `meter_id` | Stored as `plant_code` on ActivityRow |
| `account_no` | Present in sample but not used in parser logic |
| `site_name` | Stored as `description` on ActivityRow |
| `bill_from` | Start of billing period |
| `bill_to` | End of billing period |
| `consumption` | kWh or MWh value |
| `unit` | `kWh` or `MWh` |
| `read_type` | `A` (actual) or `E` (estimated) |
| `tariff_code` | Present in sample but not used in parser logic |
| `amount` | Present in sample but not used in emission calc |
| `currency` | Present in sample but not used in emission calc |

*Unit codes handled by `normalize_utility_unit()`:*

| Raw unit | Multiplier | Canonical unit |
|----------|------------|----------------|
| `KWH` (case-insensitive) | ×1.0 | `kWh` |
| `MWH` (case-insensitive) | ×1000.0 | `kWh` |

All other units raise `ValueError` and the row is flagged.

*Date formats handled by `parse_utility_date()` (tried in order):*
1. `DD/MM/YYYY` — e.g. `18/01/2024`
2. `YYYY-MM-DD` — e.g. `2024-01-18` (ISO 8601)
3. `DD-MM-YYYY` — e.g. `18-01-2024`

*Estimated read flag:*
If `read_type == 'E'` (case-insensitive comparison after `.upper()`), the row is flagged:
`"Estimated meter reading — verify with actual bill."`

*Billing period split — `split_billing_period()`:*
Yes, this function exists. It proportionally splits total consumption across calendar months
when the billing window spans more than one month.
- Day counting: `total_days = (bill_to - bill_from).days + 1`
- Per-month share: `days_in_month / total_days × consumption`
- Last segment absorbs any rounding remainder
- Single-month windows return one slice with no split

*Flag conditions in `parse_utility_file()`:*
1. `read_type == 'E'` → estimated read flag
2. `consumption <= 0` → `"Zero or negative consumption."`
3. Billing period > 45 days → `"Billing period exceeds 45 days (N days) — possible estimated read."`
4. Date parse failure (`bill_from` or `bill_to`)
5. Unknown unit code (not `KWH` or `MWH`)

**CEA emission factor:**
`INDIA_GRID_KWH = 0.716` kgCO2e/kWh
Source: Central Electricity Authority (CEA), CO2 Baseline Database for the Indian Power
Sector, Version 18, 2023. This is the location-based grid emission factor for India.

**What my sample data (`sample_utility.csv`) looks like and why:**

| Row | Meter | Site | From | To | Consumption | Read | Notes |
|-----|-------|------|------|----|-------------|------|-------|
| 1 | MTR-0042 | Mumbai Office | 18/01/2024 | 21/02/2024 | 3660 kWh | A | **Split** → Jan + Feb slices |
| 2 | MTR-0043 | Mumbai Office | 18/01/2024 | 21/02/2024 | 750 kWh | **E** | **Flagged** — estimated read, also splits |
| 3 | MTR-0089 | Pune Factory | 01/01/2024 | 31/01/2024 | 2.3 **MWh** | A | MWh conversion test → 2300 kWh |
| 4 | MTR-0010 | Delhi Office | 01/01/2024 | 31/01/2024 | **0** kWh | A | **Flagged** — zero consumption |
| 5 | MTR-0011 | Delhi Office | 01/02/2024 | 29/02/2024 | 1240 kWh | A | Feb-only single-month, no split |
| 6 | MTR-0050 | Bangalore HQ | 01/01/2024 | 31/01/2024 | 8900 kWh | A | Clean row |
| 7 | MTR-0051 | Bangalore HQ | 01/01/2024 | 31/01/2024 | 1200 kWh | **E** | **Flagged** — estimated read |
| 8 | MTR-0099 | Chennai Plant | 05/12/2023 | 10/01/2024 | 4500 kWh | A | **Split** — Dec 2023 + Jan 2024 |

**What would break in a real deployment:**
1. Billing period split is proportional by days — approximate. Actual consumption within
   a period is not uniformly distributed (e.g. seasonal variation, site shutdowns).
2. Some utilities only provide PDF bills. Portal CSV is not universally available.
3. `amount` and `currency` columns are read but not used in any calculation. Multi-currency
   clients cannot use cost data without FX rates.
4. The emission factor `INDIA_GRID_KWH = 0.716` is a national average. Some clients may
   require state-specific or time-of-use grid factors.

---

## Source 3 — Corporate Travel (Concur-style CSV)

**Format researched:**
CSV export from Concur Travel & Expense, the most widely deployed enterprise travel
management platform. Used by most Fortune 500 companies for booking and expense management.

**What I learned about the real-world format:**

*Column names expected (from `parse_travel_file()` in `travel_parser.py`):*

| Column | Used for |
|--------|----------|
| `booking_id` | Stored as `plant_code` and in audit `detail` |
| `travel_type` | `FLIGHT`, `HOTEL`, or `GROUND` |
| `origin` | IATA code (FLIGHT) or city name (GROUND) |
| `destination` | IATA code (FLIGHT) or city name (GROUND) |
| `travel_date` | `YYYY-MM-DD` — stored as `document_date` and `posting_date` |
| `return_date` | `YYYY-MM-DD` — if present, distance is doubled for FLIGHT |
| `cabin_class` | `ECONOMY`, `ECONOMY PLUS`, `PREMIUM`, `BUSINESS`, `FIRST` |
| `hotel_nights` | Number of nights (HOTEL rows) |
| `hotel_city` | Stored as `description` for HOTEL rows |
| `ground_km` | Distance in km (GROUND rows) |
| `transport_mode` | `TAXI`, `CAR`, `RAIL`, `TRAIN` |
| `amount` | Present in sample but not used in emission calc |
| `currency` | Present in sample but not used in emission calc |

*Travel types handled:*
- `FLIGHT` → scope 3, category `business_travel_air`, unit `km`
- `HOTEL` → scope 3, category `business_travel_hotel`, unit `nights`
- `GROUND` → scope 3, category `business_travel_ground`, unit `km`
- Any other value → scope 3, category `business_travel_unknown`, flagged

*IATA codes in hardcoded `AIRPORT_COORDS` dict (10 airports):*
`BOM` (Mumbai), `DEL` (Delhi), `LHR` (London Heathrow), `BLR` (Bengaluru),
`MAA` (Chennai), `HYD` (Hyderabad), `CCU` (Kolkata), `DXB` (Dubai),
`SIN` (Singapore), `JFK` (New York JFK)

*Cabin classes handled with emission factors:*

| `cabin_class` value | Factor applied |
|---------------------|----------------|
| `ECONOMY` | `FLIGHT_ECONOMY_KM = 0.133` kgCO2e/km |
| `ECONOMY PLUS` | `FLIGHT_ECONOMY_KM = 0.133` (treated as economy) |
| `PREMIUM` | `FLIGHT_BUSINESS_KM = 0.295` (treated as business) |
| `BUSINESS` | `FLIGHT_BUSINESS_KM = 0.295` kgCO2e/km |
| `FIRST` | `FLIGHT_FIRST_KM = 0.430` kgCO2e/km |

*Ground transport modes handled:*

| `transport_mode` value | Factor applied |
|------------------------|----------------|
| `TAXI` | `TAXI_KM = 0.149` kgCO2e/km |
| `CAR` | `TAXI_KM = 0.149` (same as taxi) |
| `RAIL` | `RAIL_KM = 0.041` kgCO2e/km |
| `TRAIN` | `RAIL_KM = 0.041` (same as rail) |

*Return trips:*
Detected by `is_return_trip(return_date_val)` — returns `True` when `return_date`
is a non-empty parseable `YYYY-MM-DD` string. If detected, `distance_km` is multiplied
by 2 before computing emissions. One `ActivityRow` is created for the entire
outbound+return journey.

*Distance calculation:*
`get_flight_distance_km(origin, destination)` calls `haversine_km()` with hardcoded
coordinates. Returns great-circle distance (Earth radius = 6371 km). If either IATA
code is absent from `AIRPORT_COORDS`, raises `ValueError` and the row is flagged.

**Emission factors used (all DEFRA 2023, with RFI applied to flights):**

| Factor | Value | Unit | Source |
|--------|-------|------|--------|
| `FLIGHT_ECONOMY_KM` | 0.133 | kgCO2e/km | DEFRA 2023 (includes RFI uplift) |
| `FLIGHT_BUSINESS_KM` | 0.295 | kgCO2e/km | DEFRA 2023 |
| `FLIGHT_FIRST_KM` | 0.430 | kgCO2e/km | DEFRA 2023 |
| `HOTEL_NIGHT` | 31.0 | kgCO2e/night | DEFRA 2023 |
| `TAXI_KM` | 0.149 | kgCO2e/km | DEFRA 2023 |
| `RAIL_KM` | 0.041 | kgCO2e/km | DEFRA 2023 |

**What RFI (Radiative Forcing Index) means:**
Flights emit CO2 at altitude which has a warming effect 2-3× stronger than ground-level
emissions due to contrails and cirrus cloud formation. DEFRA's flight factors already
include an RFI uplift factor. This is why `FLIGHT_ECONOMY_KM` (0.133) is comparable to
`TAXI_KM` (0.149) per km even though planes carry more passengers per litre of fuel.

**What my sample data (`sample_travel.csv`) looks like and why:**

| Row | Booking | Type | Route/Detail | Return? | Notes |
|-----|---------|------|-------------|---------|-------|
| 1 | TRV-001 | FLIGHT | BOM→DEL, ECONOMY | Yes (17 Jan) | Return trip — distance ×2 |
| 2 | TRV-002 | FLIGHT | DEL→LHR, BUSINESS | Yes (10 Feb) | Business class factor; return ×2 |
| 3 | TRV-003 | HOTEL | 2 nights, New Delhi | — | Hotel emission test |
| 4 | TRV-004 | GROUND | Pune→Mumbai, 148 km | — | **TAXI** mode test; ground_km in CSV |
| 5 | TRV-005 | FLIGHT | BLR→BOM, ECONOMY | No | One-way flight |
| 6 | TRV-006 | FLIGHT | BOM→SIN, ECONOMY | Yes (18 Feb) | Long-haul return |
| 7 | TRV-007 | GROUND | 212 km | — | **RAIL** mode test |
| 8 | TRV-008 | HOTEL | 3 nights, Singapore | — | Second hotel row |

All IATA codes in the sample (BOM, DEL, LHR, BLR, SIN) are present in `AIRPORT_COORDS`.
No row tests an unknown IATA code — unknown-IATA flagging is covered in `test_travel_parser.py`.

**What would break in a real deployment:**
1. `AIRPORT_COORDS` has only **10 airports**. Real deployment needs the full IATA database
   (~10,000 airports). Any booking outside these 10 is flagged.
2. Distance is great-circle (shortest geometric path). Actual flight paths are 5-15%
   longer due to air traffic control routing and wind avoidance.
3. Some Concur exports split return trips as **two separate booking rows** rather than one
   row with a `return_date`. The parser handles only the single-row-with-return_date format.
4. The `hotel_night` factor (31.0 kgCO2e/night) is a global average. Actual hotel
   emissions vary significantly by country, star rating, and property.
5. Ground transport `amount` and `currency` columns are read but not used. Cost-per-km
   analysis is not possible without FX conversion.
