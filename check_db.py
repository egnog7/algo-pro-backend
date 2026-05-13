import sqlite3

db = sqlite3.connect(r"C:/Users/casse/Desktop/bots/licensing/algo_pro.db")

print("Tables:")
print(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())

print("\nLicenses:")
try:
    print(db.execute("SELECT license_key, status FROM licenses").fetchall())
except Exception as e:
    print("Error querying licenses table:", e)