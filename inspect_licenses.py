import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
engine = create_engine(os.getenv("DATABASE_URL"), future=True)

with engine.begin() as conn:
    rows = conn.execute(text("""
        SELECT license_key, plan, status, expires_at, pairs_csv, checkout_session_id
        FROM licenses
        ORDER BY rowid DESC
        LIMIT 25
    """)).fetchall()

for r in rows:
    print(r)
