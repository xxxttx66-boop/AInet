import sqlite3
con = sqlite3.connect(r"D:\AINET\backend\_p2\ainet.db")
cols = [r[1] for r in con.execute("pragma table_info(attempts)")]
print(cols)
for r in con.execute("select * from attempts order by id"):
    d = dict(zip(cols, r)); print({k: (str(v)[:160] if v is not None else None) for k, v in d.items() if k not in ("student_id",)})
