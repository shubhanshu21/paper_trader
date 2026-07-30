"""
scripts/migrate_to_mysql.py — Production-grade chunked data migration from SQLite to MySQL.

Migrates:
  1. Runtime DB (data/runtime/trading.db) -> positions & backtest_runs
  2. Historical DB (dataset/fno_bhavcopy.db) -> fno_bhavcopy & candles (35GB)

Optimizations applied:
  - Disables uniqueness/foreign key checks during import to maximize speed
  - Stream rows from SQLite using generator to keep memory overhead near-zero (<50MB)
  - Batch inserts in chunks of 50,000 rows using raw PyMySQL executemany (10x faster than ORM)
  - Commits in transactions per chunk to ensure progress is saved and rollback is possible
  - Shows clear real-time speed/progress indicators
"""
import os
import sys
import time
import sqlite3
import pymysql
from dotenv import load_dotenv

# Load env variables from project root
load_dotenv()

# MySQL config from env
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME", "automate")

# SQLite database paths
RUNTIME_DB = "data/runtime/trading.db"
BHAVCOPY_DB = "dataset/fno_bhavcopy.db"


def get_mysql_conn():
    if not DB_USER or not DB_PASSWORD:
        print("ERROR: DB_USER and DB_PASSWORD must be configured in .env")
        sys.exit(1)
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor
    )


def migrate_table(sqlite_path, table_name, mysql_conn, select_query=None, insert_query=None, chunk_size=50000):
    print(f"\n>>> Migrating table '{table_name}' from {sqlite_path}...")
    if not os.path.exists(sqlite_path):
        print(f"Skipping '{table_name}': SQLite file {sqlite_path} does not exist.")
        return

    lite_conn = sqlite3.connect(sqlite_path)
    lite_conn.row_factory = sqlite3.Row
    lite_cur = lite_conn.cursor()

    # Get total rows count if possible (quick check, skip if slow but here we try with a timeout/limit)
    file_size = os.path.getsize(sqlite_path)
    total_rows = None
    if file_size > 100 * 1024 * 1024:
        print(f"File size {file_size / (1024*1024):.1f}MB is large. Skipping estimation for speed...")
    else:
        print("Estimating row count...")
        try:
            lite_cur.execute(f"SELECT COUNT(*) FROM {table_name}")
            total_rows = lite_cur.fetchone()[0]
            print(f"Total rows to migrate: {total_rows:,}")
        except Exception as e:
            print(f"Could not count rows: {e}. Moving forward with streaming...")

    # Get column names to build dynamically if queries aren't provided
    lite_cur.execute(f"PRAGMA table_info({table_name})")
    cols = [r["name"] for r in lite_cur.fetchall()]

    if not select_query:
        select_query = f"SELECT * FROM {table_name}"
    if not insert_query:
        col_list = ", ".join(cols)
        val_placeholders = ", ".join([f"%({c})s" for c in cols])
        insert_query = f"INSERT INTO {table_name} ({col_list}) VALUES ({val_placeholders})"

    # Stream rows from SQLite in chunks
    lite_cur.execute(select_query)
    
    my_cur = mysql_conn.cursor()
    # Disable foreign key & unique key checks for speed
    my_cur.execute("SET foreign_key_checks = 0;")
    my_cur.execute("SET unique_checks = 0;")
    
    # Truncate existing data in MySQL to avoid duplicate key issues
    print(f"Truncating existing table '{table_name}' in MySQL...")
    my_cur.execute(f"TRUNCATE TABLE {table_name}")
    mysql_conn.commit()

    chunk = []
    migrated_count = 0
    start_time = time.time()
    last_print = start_time

    while True:
        rows = lite_cur.fetchmany(chunk_size)
        if not rows:
            break
        
        # Convert sqlite3.Row to dict
        chunk = [dict(r) for r in rows]
        
        # Batch insert into MySQL
        my_cur.executemany(insert_query, chunk)
        mysql_conn.commit()

        migrated_count += len(chunk)
        now = time.time()
        
        # Progress reporting
        if now - last_print > 3.0 or migrated_count == total_rows:
            elapsed = now - start_time
            rate = migrated_count / elapsed if elapsed > 0 else 0
            pct_str = f" ({migrated_count/total_rows*100:.1f}%)" if total_rows else ""
            print(f"  Migrated {migrated_count:,} rows{pct_str}... Speed: {rate:.1f} rows/sec")
            last_print = now

    # Enable constraints back
    my_cur.execute("SET foreign_key_checks = 1;")
    my_cur.execute("SET unique_checks = 1;")
    mysql_conn.commit()

    elapsed = time.time() - start_time
    print(f"Finished '{table_name}': Migrated {migrated_count:,} rows in {elapsed:.2f} seconds.")
    lite_conn.close()


def main():
    print("====================================================")
    print("   SQLite -> MySQL Database Migration Tool")
    print("====================================================")
    
    # Check MySQL connectivity
    try:
        mysql_conn = get_mysql_conn()
        print("Connected to MySQL successfully!")
    except Exception as e:
        print(f"ERROR: Cannot connect to MySQL: {e}")
        sys.exit(1)

    # 1. Migrate Runtime Tables
    migrate_table(RUNTIME_DB, "positions", mysql_conn)
    migrate_table(RUNTIME_DB, "backtest_runs", mysql_conn)

    # 2. Migrate Historical bhavcopy Tables
    migrate_table(BHAVCOPY_DB, "fno_bhavcopy", mysql_conn, chunk_size=100000)
    migrate_table(BHAVCOPY_DB, "candles", mysql_conn, chunk_size=100000)

    # 3. Add historical DB index creation if needed
    # (Since Alembic already created indexes, MySQL will build them as rows insert.
    # If it is slow, we could drop indexes and rebuild them here, but MySQL InnoDB handles index updates 
    # relatively well. We disabled unique_checks to speed it up.)
    
    mysql_conn.close()
    print("\nAll migrations completed successfully!")


if __name__ == "__main__":
    main()
