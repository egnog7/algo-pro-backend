from db import engine

with engine.begin() as conn:
    conn.exec_driver_sql("""
        ALTER TABLE licenses
        ADD COLUMN IF NOT EXISTS enabled_modules_csv TEXT
    """)

    conn.exec_driver_sql("""
        ALTER TABLE licenses
        ADD COLUMN IF NOT EXISTS max_modules INTEGER DEFAULT 0
    """)

print("Module columns added.")