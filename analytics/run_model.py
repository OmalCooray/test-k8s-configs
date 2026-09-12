import os
import re
import duckdb

client_id = os.environ["POLARIS_CLIENT_ID"]
client_secret = os.environ["POLARIS_CLIENT_SECRET"]

con = duckdb.connect(":memory:")
con.execute("INSTALL iceberg")
con.execute("LOAD iceberg")
con.execute("INSTALL httpfs")
con.execute("LOAD httpfs")
con.execute(f"""
    CREATE SECRET polaris_secret (
        TYPE iceberg,
        CLIENT_ID '{client_id}',
        CLIENT_SECRET '{client_secret}',
        ENDPOINT 'http://polaris:8181/api/catalog'
    )
""")
con.execute("""
    ATTACH 'lakehouse' AS lakehouse (
        TYPE iceberg,
        ENDPOINT 'http://polaris:8181/api/catalog',
        ACCESS_DELEGATION_MODE 'vended_credentials'
    )
""")

with open("/scripts/nyc_taxi_model.sql") as f:
    sql_text = f.read()

# Strip comments, split into individual statements on top-level semicolons.
sql_text = re.sub(r"--.*", "", sql_text)
statements = [s.strip() for s in sql_text.split(";") if s.strip()]

for i, stmt in enumerate(statements, 1):
    label = stmt.split("\n", 1)[0][:80]
    print(f"[{i}/{len(statements)}] {label}...", flush=True)
    try:
        con.execute(stmt)
    except duckdb.Error as exc:
        if stmt.strip().upper().startswith("DROP TABLE") and "does not exist" in str(exc).lower():
            print(f"  (table did not exist yet, skipping drop: {exc})", flush=True)
            continue
        raise

print("\n=== verification ===", flush=True)
for table in ["dim_vendor", "dim_payment_type", "dim_rate_code", "fct_trips", "mart_daily_summary"]:
    count = con.execute(f"SELECT count(*) FROM lakehouse.nyc_taxi.{table}").fetchone()[0]
    print(f"{table}: {count} rows", flush=True)

print("\n=== sample: mart_daily_summary (first 10 rows) ===", flush=True)
result = con.execute("SELECT * FROM lakehouse.nyc_taxi.mart_daily_summary LIMIT 10")
cols = [d[0] for d in result.description]
print(" | ".join(cols), flush=True)
for row in result.fetchall():
    print(" | ".join(str(v) for v in row), flush=True)

print("\nDone.", flush=True)
