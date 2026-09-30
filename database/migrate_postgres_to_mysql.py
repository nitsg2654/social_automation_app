"""Copy social_posts rows from PostgreSQL into an empty MySQL target.

Source data is read only. The script does not drop or modify either database.
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import mysql.connector
import psycopg
from psycopg import sql
from psycopg.rows import dict_row


def load_dotenv():
    """Load simple KEY=VALUE settings from project .env without overriding shell env."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def mysql_config():
    return {
        "host": os.getenv("MYSQL_MIGRATION_HOST", "127.0.0.1"),
        "port": int(os.getenv("MYSQL_PUBLISHED_PORT", "3307")),
        "user": os.environ["MYSQL_USER"],
        "password": os.environ["MYSQL_PASSWORD"],
        "database": os.getenv("MYSQL_DATABASE", "n8n_social"),
        "connection_timeout": 15,
        "charset": "utf8mb4",
    }


def mysql_value(value):
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def main():
    load_dotenv()
    source_url = os.getenv("PG_SOURCE_URL")
    if not source_url:
        sys.exit("Set PG_SOURCE_URL to the existing PostgreSQL database before running this migration.")
    schema = os.getenv("PG_SOURCE_SCHEMA", "public")
    mysql_conn = mysql.connector.connect(**mysql_config())
    pg_conn = psycopg.connect(source_url, row_factory=dict_row)
    try:
        target_cur = mysql_conn.cursor()
        target_cur.execute("SELECT COUNT(*) FROM social_posts")
        target_count = target_cur.fetchone()[0]
        if target_count:
            sys.exit(f"Target social_posts has {target_count} rows. Migration stopped to avoid overwriting target data.")
        target_cur.execute("SELECT COLUMN_NAME FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='social_posts' ORDER BY ORDINAL_POSITION")
        target_columns = {row[0] for row in target_cur.fetchall()}

        with pg_conn.cursor(name="social_posts_migration", row_factory=dict_row) as source_cur:
            source_cur.execute(sql.SQL("SELECT * FROM {}.{} ORDER BY id").format(sql.Identifier(schema), sql.Identifier("social_posts")))
            source_columns = [item.name for item in source_cur.description]
            missing = set(source_columns) - target_columns
            if missing:
                sys.exit("MySQL target is missing source columns: " + ", ".join(sorted(missing)))
            col_list = ", ".join(f"`{name.replace('`', '``')}`" for name in source_columns)
            placeholders = ", ".join(["%s"] * len(source_columns))
            insert_sql = f"INSERT INTO social_posts ({col_list}) VALUES ({placeholders})"
            migrated = 0
            while rows := source_cur.fetchmany(500):
                values = [[mysql_value(row[col]) for col in source_columns] for row in rows]
                target_cur.executemany(insert_sql, values)
                migrated += len(values)
        mysql_conn.commit()
        print(f"Copied {migrated} social_posts rows. PostgreSQL source was left unchanged.")
    except Exception:
        mysql_conn.rollback()
        raise
    finally:
        mysql_conn.close()
        pg_conn.close()


if __name__ == "__main__":
    main()
