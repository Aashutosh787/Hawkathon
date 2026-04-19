"""
database.py — SQLite setup for Tether auth + chat history.
No external service required; DB file lives next to this module.
"""
import os
import aiosqlite

DB_PATH = os.path.join(os.path.dirname(__file__), "tether.db")


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            TEXT PRIMARY KEY,
                email         TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                name          TEXT,
                role          TEXT DEFAULT 'user',
                created_at    TEXT,
                smtp_user     TEXT,
                smtp_pass_enc TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    TEXT NOT NULL,
                bot_type   TEXT NOT NULL,
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat "
            "ON chat_messages(user_id, bot_type, created_at)"
        )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_reports (
                user_id     TEXT PRIMARY KEY,
                report_json TEXT NOT NULL,
                major       TEXT,
                year        TEXT,
                school      TEXT,
                created_at  TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS outreach_drafts (
                id                  TEXT PRIMARY KEY,
                user_id             TEXT NOT NULL,
                employer_id         TEXT,
                employer_name       TEXT NOT NULL,
                employer_email      TEXT,
                email_source        TEXT,
                role_to_target      TEXT,
                subject             TEXT,
                body                TEXT,
                status              TEXT DEFAULT 'no_draft',
                created_at          TEXT NOT NULL,
                sent_at             TEXT,
                error               TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_outreach_user_employer "
            "ON outreach_drafts(user_id, employer_id)"
        )

        await db.execute("""
            CREATE TABLE IF NOT EXISTS saved_job_plans (
                id         TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL,
                job_title  TEXT NOT NULL,
                company    TEXT NOT NULL,
                location   TEXT,
                salary     TEXT,
                plan_json  TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS saved_contacts (
                id              TEXT PRIMARY KEY,
                user_id         TEXT NOT NULL,
                company         TEXT NOT NULL,
                company_domain  TEXT,
                first_name      TEXT,
                last_name       TEXT,
                email           TEXT NOT NULL,
                position        TEXT,
                confidence      INTEGER DEFAULT 0,
                linkedin_url    TEXT,
                job_title       TEXT,
                job_url         TEXT,
                draft_subject   TEXT,
                draft_body      TEXT,
                created_at      TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # ── Column migrations (safe to run on existing DBs) ────────────────
        # ALTER TABLE ADD COLUMN fails if the column already exists in SQLite,
        # so we catch the error and continue.
        for col_def in ["smtp_user TEXT", "smtp_pass_enc TEXT"]:
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col_def}")
            except Exception:
                pass  # Column already exists — ignore

        await db.commit()
