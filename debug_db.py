import sqlite3

con = sqlite3.connect("algo_pro.db")
cur = con.cursor()

rows = cur.execute(
    "SELECT license_key, owner_clerk_user_id FROM licenses"
).fetchall()

for r in rows:
    print(r)

con.close()
 