"""
ingestor/management/commands/load_sample_data.py
==================================================
Management command that seeds the database with demonstration data for the
Breathe ESG Ingestor prototype.

What it creates
---------------
1. Client          -- "Acme Corp Ltd"
2. PlantCode rows  -- IN01 (Mumbai), IN02 (Pune), IN03 (Chennai), DE07 (Frankfurt)
                      XX99 is intentionally *absent* so that row flags.
3. Django superuser -- username="analyst", password="demo1234"
4. ActivityRows    -- parsed from the three fixture CSVs:
                        ingestor/fixtures/sample_sap.csv      -> 8 SAP rows
                        ingestor/fixtures/sample_utility.csv  -> 9+ rows (month splits)
                        ingestor/fixtures/sample_travel.csv   -> 8 travel rows

Usage
-----
    python manage.py load_sample_data
    python manage.py load_sample_data --reset   # wipe & reload

Hard constraints honoured
-------------------------
* raw_payload is never overwritten -- each CSV row creates a fresh RawUpload.
* ActivityRow is never edited once LOCKED.
* AuditLog receives one UPLOADED entry per ActivityRow.
* Every model carries client FK.
"""

import io
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from ingestor.models import ActivityRow, AuditLog, Client, PlantCode, RawUpload
from ingestor.parsers.sap_parser import parse_sap_file
from ingestor.parsers.utility_parser import parse_utility_file
from ingestor.parsers.travel_parser import parse_travel_file

User = get_user_model()

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures"

# ---------------------------------------------------------------------------
# Plant code reference data
# ---------------------------------------------------------------------------
PLANT_CODES = [
    {"code": "IN01", "site_name": "Mumbai Plant",     "country": "IN"},
    {"code": "IN02", "site_name": "Pune Plant",       "country": "IN"},
    {"code": "IN03", "site_name": "Chennai Plant",    "country": "IN"},
    {"code": "DE07", "site_name": "Frankfurt Office", "country": "DE"},
    # XX99 deliberately omitted -> triggers 'unknown plant' flag on FUL-005
]


class Command(BaseCommand):
    help = (
        "Seed the database with sample client, plant codes, superuser, "
        "and activity rows parsed from the three fixture CSVs."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help=(
                "Delete all existing ActivityRows, RawUploads, AuditLogs, "
                "PlantCodes, and Clients before loading. "
                "The superuser is kept."
            ),
        )

    # ------------------------------------------------------------------

    def handle(self, *args, **options):
        # Bug 4 fix: idempotent guard — skip if data already loaded
        if not options["reset"] and Client.objects.filter(name="Acme Corp Ltd").exists():
            self.stdout.write(
                self.style.WARNING(
                    "[SKIP] Sample data already loaded (Client 'Acme Corp Ltd' exists). "
                    "Run with --reset to reload."
                )
            )
            return

        if options["reset"]:
            self._reset()

        with transaction.atomic():
            client  = self._create_client()
            user    = self._create_superuser()
            self._create_plant_codes(client)
            sap_ids = self._parse_sap(client, user)
            uti_ids = self._parse_utility(client, user)
            trv_ids = self._parse_travel(client, user)

        self._print_summary(client, sap_ids, uti_ids, trv_ids)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _reset(self):
        self.stdout.write(self.style.WARNING("[RESET] Wiping existing data..."))
        AuditLog.objects.all().delete()      # must go first (FK -> ActivityRow)
        ActivityRow.objects.all().delete()
        RawUpload.objects.all().delete()
        PlantCode.objects.all().delete()
        Client.objects.all().delete()
        self.stdout.write("       Cleared: ActivityRows, RawUploads, AuditLogs, PlantCodes, Clients.")

    # ------------------------------------------------------------------

    def _create_client(self) -> Client:
        client, created = Client.objects.get_or_create(
            slug="acme-corp-ltd",
            defaults={"name": "Acme Corp Ltd"},
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
        self.stdout.write("\n[SAP]     Parsing sample_sap.csv ...")
        file_obj = self._read_fixture("sample_sap.csv")
        row_ids = parse_sap_file(file_obj, client.pk, user.pk)
        flagged = ActivityRow.objects.filter(pk__in=row_ids, is_flagged=True).count()
        self.stdout.write(
            f"[SAP]     -> {len(row_ids)} ActivityRows created, {flagged} flagged."
        )
        return row_ids

    # ------------------------------------------------------------------

    def _parse_utility(self, client: Client, user) -> list[int]:
        self.stdout.write("\n[UTILITY] Parsing sample_utility.csv ...")
        file_obj = self._read_fixture("sample_utility.csv")
        results  = parse_utility_file(file_obj, client.pk, user.pk)
        row_ids  = [r["activity_row_id"] for r in results]
        flagged  = ActivityRow.objects.filter(pk__in=row_ids, is_flagged=True).count()
        self.stdout.write(
            f"[UTILITY] -> {len(row_ids)} ActivityRows created "
            f"(includes month splits), {flagged} flagged."
        )
        return row_ids

    # ------------------------------------------------------------------

    def _parse_travel(self, client: Client, user) -> list[int]:
        self.stdout.write("\n[TRAVEL]  Parsing sample_travel.csv ...")
        file_obj = self._read_fixture("sample_travel.csv")
        results  = parse_travel_file(file_obj, client.pk, user.pk)
        row_ids  = [r["activity_row_id"] for r in results]
        flagged  = ActivityRow.objects.filter(pk__in=row_ids, is_flagged=True).count()
        self.stdout.write(
            f"[TRAVEL]  -> {len(row_ids)} ActivityRows created, {flagged} flagged."
        )
        return row_ids

    # ------------------------------------------------------------------

    def _print_summary(self, client, sap_ids, uti_ids, trv_ids):
        all_ids  = sap_ids + uti_ids + trv_ids
        total    = len(all_ids)
        flagged  = ActivityRow.objects.filter(pk__in=all_ids, is_flagged=True).count()
        scope_cts = {
            s: ActivityRow.objects.filter(pk__in=all_ids, scope=s).count()
            for s in (1, 2, 3)
        }

        co2_values = (
            ActivityRow.objects
            .filter(pk__in=all_ids, co2e_kg__isnull=False)
            .values_list("co2e_kg", flat=True)
        )
        co2_sum = sum(float(v) for v in co2_values)

        sep = "-" * 56
        self.stdout.write(f"\n{sep}")
        self.stdout.write(self.style.SUCCESS("  [DONE] Sample data loaded successfully!"))
        self.stdout.write(sep)
        self.stdout.write(f"  Client       : {client.name} (pk={client.pk})")
        self.stdout.write(f"  Superuser    : analyst / demo1234")
        self.stdout.write(f"  ActivityRows : {total} total")
        self.stdout.write(f"    SAP        : {len(sap_ids)}")
        self.stdout.write(f"    Utility    : {len(uti_ids)} (after month splits)")
        self.stdout.write(f"    Travel     : {len(trv_ids)}")
        self.stdout.write(f"  Flagged      : {flagged}")
        self.stdout.write(
            f"  Scope 1/2/3  : "
            f"{scope_cts[1]} / {scope_cts[2]} / {scope_cts[3]}"
        )
        self.stdout.write(f"  Total CO2e   : {co2_sum:,.2f} kgCO2e")
        self.stdout.write(sep)
        self.stdout.write("  Start the servers:")
        self.stdout.write("    cd backend  && python manage.py runserver")
        self.stdout.write("    cd frontend && npm run dev")
        self.stdout.write(sep)
