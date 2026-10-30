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
                comp_value_salary   NUMRANGE,
                comp_value_hourly   NUMRANGE,
                pay                 TEXT,
                app_date            DATE,
                job_id              TEXT,
                link                TEXT,
                energy_related      BOOLEAN DEFAULT FALSE,
                status              TEXT DEFAULT 'applied',
                job_description     TEXT,
                jd_source           TEXT,
                posted_date         DATE,
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
            ("department", "TEXT"),
            ("us_citizen_required", "BOOLEAN"),
            ("compensation_type", "BOOLEAN"),
            ("comp_value_salary", "NUMRANGE"),
            ("comp_value_hourly", "NUMRANGE"),
        ]:
            cur.execute(
                f"ALTER TABLE applications ADD COLUMN IF NOT EXISTS {col} {typ}"
            )
        # Drop unused date columns from older schemas.
        for col in ("due_date", "application_deadline"):
            cur.execute(f"ALTER TABLE applications DROP COLUMN IF EXISTS {col}")

        # Compensation is stored as a range so a posted range ("$82,625 -
        # $103,653") is kept intact. Older DBs have NUMERIC columns: promote
        # them to NUMRANGE, turning each stored number into a point range.
        for col in ("comp_value_salary", "comp_value_hourly"):
            cur.execute(
                """
                SELECT data_type FROM information_schema.columns
                WHERE table_name = 'applications' AND column_name = %s
                """,
                (col,),
            )
            row = cur.fetchone()
            if row and row[0] != "numrange":
                cur.execute(
                    f"""
                    ALTER TABLE applications
                    ALTER COLUMN {col} TYPE NUMRANGE
                    USING CASE WHEN {col} IS NULL THEN NULL
                               ELSE numrange({col}, {col}, '[]') END
                    """
                )

        # Backfill the ranges from the raw `pay` text. The old parser averaged
        # the endpoints, so rows saved as "$82,625 - $103,653" hold 93139;
        # re-parsing restores the real range (a no-op once they agree).
        cur.execute(
            """
            SELECT id, pay, compensation_type, comp_value_salary, comp_value_hourly
            FROM applications WHERE pay IS NOT NULL
            """
        )
        for row_id, pay_str, old_type, old_sal, old_hour in cur.fetchall():
            c_type, c_sal, c_hour = parse.process_compensation(
                pay_str, comp_type=old_type
            )
            if c_sal is None:
                continue
            if (
                parse.range_endpoints(c_sal) == parse.range_endpoints(old_sal)
                and parse.range_endpoints(c_hour) == parse.range_endpoints(old_hour)
            ):
                continue
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
        "app_date", "job_id", "link", "energy_related",
        "status", "job_description", "jd_source",
        "posted_date", "department", "us_citizen_required",
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


# Columns the detail page may edit inline. The job description text, its
# source and the jd_images table belong to the JD capture widget, so they are
# deliberately left out (an inline edit can never touch them).
EDITABLE_COLUMNS = (
    "role", "company", "department", "location", "location_type",
    "compensation_type", "comp_value_salary", "comp_value_hourly", "pay",
    "app_date", "posted_date",
    "job_id", "link", "us_citizen_required", "energy_related", "status",
)


def update_application(app_id, updates):
    """Update the given columns of one application; True if the row exists.

    Only EDITABLE_COLUMNS may be written (anything else raises ValueError).
    Compensation is passed as a group (compensation_type + both ranges + pay)
    so the derived ranges stay in sync with the raw `pay` text.
    """
    unknown = set(updates) - set(EDITABLE_COLUMNS)
    if unknown:
        raise ValueError(f"Not editable: {', '.join(sorted(unknown))}")
    if not updates:
        return get_application(app_id) is not None
    assignments = ", ".join(f"{col} = %s" for col in updates)
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE applications SET {assignments} WHERE id = %s",
            [*updates.values(), app_id],
        )
        return cur.rowcount > 0


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
