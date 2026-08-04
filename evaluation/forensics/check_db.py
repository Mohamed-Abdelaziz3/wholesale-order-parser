import sqlite3
import os

db_path = r"c:\Users\moham\OneDrive\Documents\Project @\wholesale-order-parser\evaluation\eval_orders.db"
conn = sqlite3.connect(db_path)
c = conn.cursor()

c.execute("SELECT name FROM sqlite_master WHERE type='table';")
tables = c.fetchall()
print("Tables:", tables)

for table in tables:
    tname = table[0]
    print(f"\nSchema for {tname}:")
    c.execute(f"PRAGMA table_info({tname});")
    cols = c.fetchall()
    for col in cols:
        print(col)
        
    c.execute(f"SELECT COUNT(*) FROM {tname};")
    count = c.fetchone()[0]
    print(f"Row count: {count}")
