# Breathe ESG Ingestor

A prototype ESG (Environmental, Social & Governance) data ingestion and review
platform. Upload carbon-activity CSVs from SAP, utility bills, or travel
bookings — review, flag, approve, and audit every row with full traceability.

![Architecture Diagram](Architecture%20Diagram.png)

---

## Demo

| Field | Value |
|---|---|
| **Backend URL** | `https://breathe-esg-ingestor.onrender.com` |
| **Frontend URL** | `https://breathe-esg-ingestor.vercel.app` |
| **Username** | `analyst` |
| **Password** | `demo1234` |

> **Note:** Render free-tier services spin down after 15 minutes of inactivity.
> The first request after a cold start may take 30–60 seconds.

---

## Stack

| Layer | Technology |
|---|---|
| Backend | Django 4.2 + Django REST Framework |
| Database | PostgreSQL (Render) / SQLite (local dev) |
| Frontend | React 18 + Vite |
| Styling | Vanilla CSS (dark-mode design system) |
| Deployment | Render.com (backend + static frontend + PostgreSQL) |

---

## Project Structure

```
breathe-esg-ingestor/
├── backend/
│   ├── config/
│   │   ├── settings.py       ← Django settings (decouple + whitenoise)
│   │   ├── urls.py           ← Root URL config (7 API endpoints)
│   │   └── wsgi.py           ← Gunicorn entry point
│   ├── ingestor/
│   │   ├── models.py         ← Client, RawUpload, ActivityRow, AuditLog
│   │   ├── serializers.py    ← DRF serializers
│   │   ├── views.py          ← 7 API views
│   │   ├── parsers/
│   │   │   ├── sap_parser.py     ← SAP MB51 CSV parser
│   │   │   ├── utility_parser.py ← Utility bill CSV parser
│   │   │   └── travel_parser.py  ← Travel booking CSV parser
│   │   ├── fixtures/
│   │   │   ├── sample_sap.csv
│   │   │   ├── sample_utility.csv
│   │   │   └── sample_travel.csv
│   │   └── management/commands/
│   │       └── load_sample_data.py
│   ├── tests/
│   │   ├── test_sap_parser.py     (56 tests)
│   │   ├── test_utility_parser.py (54 tests)
│   │   ├── test_travel_parser.py  (57 tests)
│   │   └── test_api_views.py      (63 tests)
│   ├── requirements.txt
│   ├── Procfile
│   └── manage.py
├── frontend/
│   ├── src/
│   │   ├── api.js            ← Central API client
│   │   ├── App.jsx           ← Router + layout
│   │   ├── index.css         ← Design system (dark-mode)
│   │   ├── components/
│   │   │   ├── Navbar.jsx
│   │   │   ├── Toast.jsx
│   │   │   └── RejectModal.jsx
│   │   └── pages/
│   │       ├── Dashboard.jsx  ← Metric cards + scope bar
│   │       ├── Upload.jsx     ← Drag-and-drop zones (SAP/Utility/Travel)
│   │       ├── Review.jsx     ← Filterable table + approve/reject/bulk-approve
│   │       └── AuditLog.jsx   ← Append-only event timeline
│   ├── .env
│   └── vite.config.js
└── render.yaml               ← One-click Render deployment
```

---

## Hard Constraints (always enforced)

1. **Raw data immutability** — `RawUpload.raw_payload` (JSONField) is written once, never updated.
2. **LOCKED rows** — Once an `ActivityRow` reaches `LOCKED` status, no field can be changed.
3. **Append-only audit log** — `AuditLog` has no `update` or `delete` paths; enforced in `save()` and `delete()`.
4. **Multi-tenancy** — Every model carries a `client` ForeignKey.
5. **Rule-based parsing** — No ML, no fuzzy matching. All parsing is deterministic.

---

## Local Development

### Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Run migrations & seed sample data
python manage.py migrate
python manage.py load_sample_data

# Start server
python manage.py runserver   # → http://localhost:8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev   # → http://localhost:5173
```

Create `frontend/.env`:
```
VITE_API_BASE_URL=http://localhost:8000
VITE_CLIENT_ID=1
```

### Run tests (230 tests)

```bash
cd backend
python -m pytest tests/ -v
```

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/upload/` | Upload CSV (SAP/UTILITY/TRAVEL) |
| `GET` | `/api/rows/` | List ActivityRows (paginated, filterable) |
| `PATCH` | `/api/rows/{id}/approve/` | Approve → Lock row |
| `PATCH` | `/api/rows/{id}/reject/` | Reject row (reason required) |
| `POST` | `/api/rows/bulk-approve/` | Bulk approve by ID list |
| `GET` | `/api/summary/` | Aggregate metrics for client |
| `GET` | `/api/audit-log/` | AuditLog entries (append-only) |

---

## Render Deployment

### One-click via render.yaml

1. Push this repo to GitHub.
2. Go to [render.com/dashboard](https://dashboard.render.com/) → **New → Blueprint**.
3. Connect the repo — Render will read `render.yaml` and create:
   - `breathe-esg-backend` (Python web service)
   - `breathe-esg-frontend` (Static site)
   - `breathe-esg-db` (PostgreSQL, free tier)
4. Wait ~5 minutes for the first build. Sample data loads automatically.
5. Visit the frontend URL and log in with `analyst` / `demo1234`.

### Manual environment variables (if not using render.yaml)

| Variable | Value |
|---|---|
| `DJANGO_SECRET_KEY` | Any long random string |
| `DATABASE_URL` | Render PostgreSQL internal URL |
| `DEBUG` | `False` |
| `ALLOWED_HOSTS` | `*` (prototype) |
| `CORS_ALLOW_ALL` | `True` |
| `VITE_API_BASE_URL` | `https://breathe-esg-ingestor.onrender.com` |
| `VITE_CLIENT_ID` | `1` |

---

## Sample Data Summary

After `load_sample_data`, the database contains:

| Source | Rows | Flagged | Intentional flags |
|---|---|---|---|
| SAP | 8 | 2 | `XX99` unknown plant, negative qty return |
| Utility | 11 | 4 | Estimated reads, zero consumption, month splits |
| Travel | 8 | 1 | Ground trip missing origin/destination |
| **Total** | **27** | **7** | **~29,212 kgCO₂e** |

---

## Design Decisions

- **No JWT** — Session + BasicAuth for prototype simplicity.
- **No ML** — All parsing is rule-based and deterministic.
- **No PDF parsing** — Deliberate cut; only CSV ingestion.
- **Month splitting** — Utility bills spanning calendar months are proportionally split by day count into one `ActivityRow` per month for accurate period reporting.
- **Return flight doubling** — `is_return_trip()` checks for a non-blank parseable `return_date`; if true, distance is doubled before applying the emission factor.
