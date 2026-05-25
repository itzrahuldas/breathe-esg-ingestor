"""
ingestor/management/commands/seed_mock_data.py
===============================================
Management command that seeds the database with rich mock data for testing
ALL features of the Breathe ESG Ingestor:

What it creates
---------------
1. Client          -- "Breathe Demo Corp"  (slug: breathe-demo-corp)
2. PlantCode rows  -- IN01 Mumbai, IN02 Pune, IN03 Chennai, IN04 Hyderabad,
                      DE07 Frankfurt.  XX99 intentionally absent → flags.
3. Superuser       -- username="analyst", password="demo1234"
4. ActivityRows    -- parsed from the three mock CSVs:
                        fixtures/mock_sap.csv      → ~20 rows (fuel + electricity + flagged)
                        fixtures/mock_utility.csv  → ~20 rows (multi-site, multi-month)
                        fixtures/mock_travel.csv   → ~20 rows (flights, hotels, ground)
5. Lifecycle sim   -- first 5 approved rows → LOCKED
                      next 3 PENDING rows   → REJECTED (with reason)
                      flagged rows stay FLAGGED

Usage
-----
    python manage.py seed_mock_data
    python manage.py seed_mock_data --reset   # wipe & reload

Hard constraints honoured
--------------------------
* raw_payload is written once and never updated (RawUpload immutability).
* ActivityRow LOCKED guard respected via queryset.update().
* AuditLog is append-only — create-only, no updates.
* All models carry client FK.
"""

import io
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from ingestor.models import ActivityRow, AuditLog, Client, PlantCode, RawUpload
from ingestor.parsers.sap_parser import parse_sap_file
from ingestor.parsers.utility_parser import parse_utility_file
from ingestor.parsers.travel_parser import parse_travel_file

User = get_user_model()

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures"

CLIENT_NAME = "Breathe Demo Corp"
CLIENT_SLUG = "breathe-demo-corp"

PLANT_CODES = [
    {"code": "IN01", "site_name": "Mumbai Plant",       "country": "IN"},
    {"code": "IN02", "site_name": "Pune Factory",       "country": "IN"},
    {"code": "IN03", "site_name": "Chennai Plant",      "country": "IN"},
    {"code": "IN04", "site_name": "Hyderabad Campus",   "country": "IN"},
    {"code": "DE07", "site_name": "Frankfurt Office",   "country": "DE"},
    # XX99 deliberately absent → triggers 'unknown plant' flag
]


class Command(BaseCommand):
    help = (
        "Seed the database with rich mock data for testing all features: "
        "uploads, review, approval, rejection, audit log, and summary dashboard."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help=(
                "Delete all existing ActivityRows, RawUploads, AuditLogs, "
                "PlantCodes, and Clients before loading."
            ),
        )

    # ------------------------------------------------------------------

    def handle(self, *args, **options):
        if not options["reset"] and Client.objects.filter(slug=CLIENT_SLUG).exists():
            self.stdout.write(
                self.style.WARNING(
                    f"[SKIP] Mock data already loaded (Client '{CLIENT_NAME}' exists). "
                    "Run with --reset to reload."
                )
            )
            return

        if options["reset"]:
            self._reset()

        with transaction.atomic():
            client = self._create_client()
            user   = self._create_superuser()
            self._create_plant_codes(client)

            sap_ids = self._parse_sap(client, user)
            uti_ids = self._parse_utility(client, user)
            trv_ids = self._parse_travel(client, user)

            all_ids = sap_ids + uti_ids + trv_ids
            self._simulate_lifecycle(all_ids, client, user)

        self._print_summary(client, sap_ids, uti_ids, trv_ids)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _reset(self):
        self.stdout.write(self.style.WARNING("[RESET] Wiping existing data..."))
        # TRUNCATE with RESTART IDENTITY resets the pk sequence so Client always
        # gets pk=1, matching the hardcoded VITE_CLIENT_ID=1 on the frontend.
        from django.db import connection
        with connection.cursor() as cursor:
            # Truncate leaf tables first to satisfy FK constraints, then root.
            cursor.execute("TRUNCATE TABLE ingestor_auditlog")
            cursor.execute("TRUNCATE TABLE ingestor_activityrow")
            cursor.execute("TRUNCATE TABLE ingestor_rawupload")
            cursor.execute("TRUNCATE TABLE ingestor_plantcode")
            cursor.execute("TRUNCATE TABLE ingestor_client RESTART IDENTITY")
        self.stdout.write("       Cleared + sequences reset: Client pk will restart at 1.")

    # ------------------------------------------------------------------

    def _create_client(self) -> Client:
        client, created = Client.objects.get_or_create(
            slug=CLIENT_SLUG,
            defaults={"name": CLIENT_NAME},
        )
        verb = "Created" if created else "Found existing"
        self.stdout.write(f"[OK]   {verb} client: {client.name} (pk={client.pk})")
        return client

    # ------------------------------------------------------------------

    def _create_superuser(self):
        username = "analyst"
        password = "demo1234"
        if User.objects.filter(username=username).exists():
            user = User.objects.get(username=username)
            self.stdout.write(f"[OK]   Found existing superuser: '{username}' (pk={user.pk})")
        else:
            user = User.objects.create_superuser(
                username=username,
                password=password,
                email="analyst@breathe-esg.example",
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"[OK]   Created superuser '{username}' / password '{password}'"
                )
            )
        return user

    # ------------------------------------------------------------------

    def _create_plant_codes(self, client: Client):
        created_count = 0
        for pc in PLANT_CODES:
            _, created = PlantCode.objects.get_or_create(
                client=client,
                code=pc["code"],
                defaults={"site_name": pc["site_name"], "country": pc["country"]},
            )
            if created:
                created_count += 1
        self.stdout.write(
            f"[OK]   Plant codes: {created_count} created, "
            f"{len(PLANT_CODES) - created_count} already existed."
        )

    # ------------------------------------------------------------------

    def _read_fixture(self, filename: str) -> io.StringIO:
        path = FIXTURES_DIR / filename
        if not path.exists():
            raise CommandError(f"Fixture not found: {path}")
        return io.StringIO(path.read_text(encoding="utf-8-sig"))

    # ------------------------------------------------------------------

    def _parse_sap(self, client: Client, user) -> list[int]:
        self.stdout.write("\n[SAP]     Parsing mock_sap.csv ...")
        file_obj = self._read_fixture("mock_sap.csv")
        row_ids = parse_sap_file(file_obj, client.pk, user.pk)
        flagged = ActivityRow.objects.filter(pk__in=row_ids, is_flagged=True).count()
        self.stdout.write(f"[SAP]     -> {len(row_ids)} rows created, {flagged} flagged.")
        return row_ids

    # ------------------------------------------------------------------

    def _parse_utility(self, client: Client, user) -> list[int]:
        self.stdout.write("\n[UTILITY] Parsing mock_utility.csv ...")
        file_obj = self._read_fixture("mock_utility.csv")
        results  = parse_utility_file(file_obj, client.pk, user.pk)
        row_ids  = [r["activity_row_id"] for r in results]
        flagged  = ActivityRow.objects.filter(pk__in=row_ids, is_flagged=True).count()
        self.stdout.write(
            f"[UTILITY] -> {len(row_ids)} rows created "
            f"(includes month splits if any), {flagged} flagged."
        )
        return row_ids

    # ------------------------------------------------------------------

    def _parse_travel(self, client: Client, user) -> list[int]:
        self.stdout.write("\n[TRAVEL]  Parsing mock_travel.csv ...")
        file_obj = self._read_fixture("mock_travel.csv")
        results  = parse_travel_file(file_obj, client.pk, user.pk)
        row_ids  = [r["activity_row_id"] for r in results]
        flagged  = ActivityRow.objects.filter(pk__in=row_ids, is_flagged=True).count()
        self.stdout.write(f"[TRAVEL]  -> {len(row_ids)} rows created, {flagged} flagged.")
        return row_ids

    # ------------------------------------------------------------------

    def _simulate_lifecycle(self, all_ids: list[int], client: Client, user) -> None:
        """
        Simulate a realistic review lifecycle so the Review and Audit Log
        screens have interesting data to display:

          - First 5 non-flagged PENDING rows  → APPROVED + LOCKED
          - Next  3 non-flagged PENDING rows  → REJECTED (with a reason)
          - All flagged rows remain FLAGGED (no change)
          - Remaining PENDING rows stay PENDING (awaiting review)
        """
        now = timezone.now()
        actor_id = user.pk

        pending_non_flagged = list(
            ActivityRow.objects.filter(
                pk__in=all_ids,
                status=ActivityRow.STATUS_PENDING,
                is_flagged=False,
            ).values_list("pk", flat=True)
        )

        approve_ids = pending_non_flagged[:5]
        reject_ids  = pending_non_flagged[5:8]

        # --- Approve + lock ---
        for pk in approve_ids:
            row = ActivityRow.objects.get(pk=pk)
            before = {"status": row.status}
            ActivityRow.objects.filter(pk=pk).update(
                status=ActivityRow.STATUS_LOCKED,
                reviewed_by_id=actor_id,
                reviewed_at=now,
            )
            AuditLog.objects.create(
                client=client,
                activity_row=row,
                actor_id=actor_id,
                action=AuditLog.ACTION_APPROVED,
                detail="Approved and locked during mock data seed.",
                before_value=before,
                after_value={"status": ActivityRow.STATUS_LOCKED},
            )

        self.stdout.write(
            f"\n[LIFECYCLE] Approved & locked {len(approve_ids)} rows: {approve_ids}"
        )

        # --- Reject ---
        reject_reasons = [
            "Duplicate entry — already captured in previous upload batch.",
            "Quantity appears inconsistent with site consumption records.",
            "Wrong cost centre — belongs to a different client account.",
        ]
        for i, pk in enumerate(reject_ids):
            row = ActivityRow.objects.get(pk=pk)
            before = {"status": row.status}
            reason = reject_reasons[i % len(reject_reasons)]
            ActivityRow.objects.filter(pk=pk).update(
                status=ActivityRow.STATUS_REJECTED,
                reviewed_by_id=actor_id,
                reviewed_at=now,
            )
            AuditLog.objects.create(
                client=client,
                activity_row=row,
                actor_id=actor_id,
                action=AuditLog.ACTION_REJECTED,
                detail=reason,
                before_value=before,
                after_value={"status": ActivityRow.STATUS_REJECTED},
            )

        self.stdout.write(
            f"[LIFECYCLE] Rejected {len(reject_ids)} rows: {reject_ids}"
        )

    # ------------------------------------------------------------------

    def _print_summary(self, client, sap_ids, uti_ids, trv_ids):
        all_ids  = sap_ids + uti_ids + trv_ids
        total    = len(all_ids)
        qs       = ActivityRow.objects.filter(pk__in=all_ids)

        counts = {
            s: qs.filter(status=s).count()
            for s in (
                ActivityRow.STATUS_PENDING,
                ActivityRow.STATUS_FLAGGED,
                ActivityRow.STATUS_LOCKED,
                ActivityRow.STATUS_REJECTED,
            )
        }
        scope_cts = {s: qs.filter(scope=s).count() for s in (1, 2, 3)}
        co2_vals  = qs.filter(co2e_kg__isnull=False).values_list("co2e_kg", flat=True)
        co2_sum   = sum(float(v) for v in co2_vals)

        sep = "-" * 60
        self.stdout.write(f"\n{sep}")
        self.stdout.write(self.style.SUCCESS("  [DONE] Mock data seeded successfully!"))
        self.stdout.write(sep)
        self.stdout.write(f"  Client       : {client.name} (pk={client.pk})")
        self.stdout.write(f"  Superuser    : analyst / demo1234")
        self.stdout.write(f"  ActivityRows : {total} total")
        self.stdout.write(f"    SAP rows   : {len(sap_ids)}")
        self.stdout.write(f"    Utility    : {len(uti_ids)}")
        self.stdout.write(f"    Travel     : {len(trv_ids)}")
        self.stdout.write(f"  Status breakdown:")
        self.stdout.write(f"    PENDING    : {counts[ActivityRow.STATUS_PENDING]}")
        self.stdout.write(f"    FLAGGED    : {counts[ActivityRow.STATUS_FLAGGED]}")
        self.stdout.write(f"    LOCKED     : {counts[ActivityRow.STATUS_LOCKED]}")
        self.stdout.write(f"    REJECTED   : {counts[ActivityRow.STATUS_REJECTED]}")
        self.stdout.write(f"  Scope 1/2/3  : {scope_cts[1]} / {scope_cts[2]} / {scope_cts[3]}")
        self.stdout.write(f"  Total CO2e   : {co2_sum:,.2f} kgCO2e")
        self.stdout.write(sep)
        self.stdout.write(f"  Frontend URL : http://localhost:5173/?client_id={client.pk}")
        self.stdout.write(f"  API base     : http://localhost:8000/api/")
        self.stdout.write(f"  Summary API  : http://localhost:8000/api/summary/?client_id={client.pk}")
        self.stdout.write(sep)
