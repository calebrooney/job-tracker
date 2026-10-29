"""Database layer shared by the web GUI and the CLI.

Uses a single Postgres `applications` table modeled on the tracking
spreadsheet. Connection info comes from the DATABASE_URL env var.
"""

import json
import os

from dotenv import load_dotenv
from . import ROOT_DIR, parse

# Load .env from the repository root (works no matter what cwd `applied` uses).
load_dotenv(os.path.join(ROOT_DIR, ".env"))

import psycopg2
import psycopg2.extras

# Default to a local database named "jobtracker" if none is configured.
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/jobtracker")

# Allowed dropdown values, reused by both the web form and the CLI.
LOCATION_TYPES = ["onsite", "hybrid", "remote"]
STATUSES = ["applied", "interview", "rejected", "offer"]


def get_connection():
    """Open a new Postgres connection using DATABASE_URL."""
    return psycopg2.connect(DATABASE_URL)


def init_db():
    """Create the applications table if needed; add newer columns if missing."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS applications (
                id                  SERIAL PRIMARY KEY,
                role                TEXT,
                company             TEXT,
                location            TEXT,
                location_type       TEXT,
                compensation_type   BOOLEAN,
                comp_value_salary   NUMERIC,
                comp_value_hourly   NUMERIC,
                pay                 TEXT,
                app_date            DATE,
                due_date            DATE,
                job_id              TEXT,
                link                TEXT,
                energy_related      BOOLEAN DEFAULT FALSE,
                status              TEXT DEFAULT 'applied',
                job_description     TEXT,
                jd_source           TEXT,
                posted_date         DATE,
                application_deadline DATE,
                department          TEXT,
                us_citizen_required BOOLEAN,
                created_at          TIMESTAMPTZ DEFAULT now()
            )
            """
        )
        # Per-page rendered JD images (PNG), so nothing has to be kept as a
        # local PDF. Cascade delete cleans up when the application is removed.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS jd_images (
                id             SERIAL PRIMARY KEY,
                application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
                page           INTEGER NOT NULL,
                mime_type      TEXT NOT NULL DEFAULT 'image/png',
                data           BYTEA NOT NULL,
                width          INTEGER,
                height         INTEGER,
                created_at     TIMESTAMPTZ DEFAULT now(),
                UNIQUE (application_id, page)
            )
            """
        )
        # Existing DBs created before these columns need a light migration.
        for col, typ in [
            ("posted_date", "DATE"),
            ("application_deadline", "DATE"),
            ("department", "TEXT"),
            ("us_citizen_required", "BOOLEAN"),
            ("compensation_type", "BOOLEAN"),
            ("comp_value_salary", "NUMERIC"),
            ("comp_value_hourly", "NUMERIC"),
        ]:
            cur.execute(
                f"ALTER TABLE applications ADD COLUMN IF NOT EXISTS {col} {typ}"
            )

        # Backfill compensation fields from existing legacy pay column if needed
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'applications' AND column_name = 'pay'
            """
        )
        if cur.fetchone():
            cur.execute(
                """
                SELECT id, pay FROM applications
                WHERE pay IS NOT NULL AND comp_value_salary IS NULL
                """
            )
            for row_id, pay_str in cur.fetchall():
                c_type, c_sal, c_hour = parse.process_compensation(pay_str)
                cur.execute(
                    """
                    UPDATE applications
                    SET compensation_type = %s, comp_value_salary = %s, comp_value_hourly = %s
                    WHERE id = %s
                    """,
                    (c_type, c_sal, c_hour, row_id),
                )


def add_application(data):
    """Insert one application row.

    `data` is a dict whose keys match the column names. Missing keys are
    stored as NULL. Returns the new row id.
    """
    fields = [
        "role", "company", "location", "location_type",
        "compensation_type", "comp_value_salary", "comp_value_hourly", "pay",
        "app_date", "due_date", "job_id", "link", "energy_related",
        "status", "job_description", "jd_source",
        "posted_date", "application_deadline", "department",
        "us_citizen_required",
    ]
    values = [data.get(f) for f in fields]
    placeholders = ", ".join(["%s"] * len(fields))
    columns = ", ".join(fields)
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO applications ({columns}) VALUES ({placeholders}) RETURNING id",
            values,
        )
        return cur.fetchone()[0]


def delete_application(app_id):
    """Delete an application by id."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM applications WHERE id = %s", (app_id,))


def list_applications():
    """Return all applications as dict-like rows, newest first."""
    with get_connection() as conn, conn.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor
    ) as cur:
        cur.execute("SELECT * FROM applications ORDER BY app_date DESC NULLS LAST, id DESC")
        return cur.fetchall()


def get_application(app_id):
    """Return a single application by id, or None if not found."""
    with get_connection() as conn, conn.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor
    ) as cur:
        cur.execute("SELECT * FROM applications WHERE id = %s", (app_id,))
        return cur.fetchone()


def log_extraction(backend, model, latency_ms, predicted, confirmed):
    """Record VLM-predicted vs user-confirmed fields (live accuracy trend)."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS extraction_logs (
                id           SERIAL PRIMARY KEY,
                backend      TEXT,
                model        TEXT,
                latency_ms   INTEGER,
                predicted    JSONB,
                confirmed    JSONB,
                created_at   TIMESTAMPTZ DEFAULT now()
            )
            """
        )
        cur.execute(
            "INSERT INTO extraction_logs (backend, model, latency_ms, predicted, confirmed)"
            " VALUES (%s, %s, %s, %s, %s)",
            (
                backend, model, latency_ms,
                psycopg2.extras.Json(predicted, dumps=lambda o: json.dumps(o, default=str)),
                psycopg2.extras.Json(confirmed, dumps=lambda o: json.dumps(o, default=str)),
            ),
        )


def update_jd(app_id, job_description, jd_source):
    """Store captured job description text and its source for an application."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE applications SET job_description = %s, jd_source = %s WHERE id = %s",
            (job_description, jd_source, app_id),
        )


def store_jd_images(app_id, pages):
    """Replace an application's stored JD page images.

    `pages` is a list of (page_number, image_bytes, mime_type, width, height)
    tuples. Re-capturing a JD replaces its old images in the same transaction.
    """
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM jd_images WHERE application_id = %s", (app_id,))
        cur.executemany(
            """
            INSERT INTO jd_images (application_id, page, mime_type, data, width, height)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            [(app_id, page, mime, data, width, height)
             for page, data, mime, width, height in pages],
        )


def list_jd_images(app_id):
    """Metadata for an application's stored JD pages, ordered by page."""
    with get_connection() as conn, conn.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor
    ) as cur:
        cur.execute(
            """
            SELECT page, mime_type, width, height, octet_length(data) AS bytes
            FROM jd_images
            WHERE application_id = %s
            ORDER BY page
            """,
            (app_id,),
        )
        return cur.fetchall()


def get_jd_image(app_id, page):
    """Return (mime_type, image_bytes) for one stored page, or None."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT mime_type, data FROM jd_images WHERE application_id = %s AND page = %s",
            (app_id, page),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return row[0], bytes(row[1])
