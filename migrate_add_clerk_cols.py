#licensing/migrate_add_clerk_cols.py
import sqlite3

DB_PATH = "algo_pro.db"

cols = [
    ("owner_clerk_user_id", "VARCHAR(64)"),
    ("owner_email", "VARCHAR(255)"),
    ("billing_email", "VARCHAR(255)"),
]

def main():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    existing = set(r[1] for r in cur.execute("PRAGMA table_info(licenses)").fetchall())

    for name, typ in cols:
        if name in existing:
            print(f"SKIP: {name} already exists")
            continue
        cur.execute(f"ALTER TABLE licenses ADD COLUMN {name} {typ}")
        print(f"OK: added {name}")

    con.commit()
    con.close()

if __name__ == "__main__":
    main()
