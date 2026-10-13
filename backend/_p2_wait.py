"""临时：启动设备并等就绪，打印状态变化与耗时。python _p2_wait.py <设备名,...> [超时秒] [--no-start]（用完删除）"""
import json, sys, time, httpx
c = httpx.Client(base_url="http://127.0.0.1:8796", timeout=120)
c.post("/api/login", json={"login": "admin", "password": "admin"}).raise_for_status()
names = sys.argv[1].split(","); limit = float(sys.argv[2]) if len(sys.argv) > 2 else 1500
topo = c.get("/api/topology/current").json()
ids = {d["name"]: d["console"] for d in topo["topology"]["devices"] if d["name"] in names}
t0 = time.time()
if "--no-start" not in sys.argv:
    r = c.post("/api/lab/devices/start", json={"devices": list(ids.values())})
    print("start", r.status_code, json.dumps(r.json(), ensure_ascii=False)[:300], flush=True)
last, ready_at, nev = {}, {}, 0
while time.time() - t0 < limit:
    try:
        cur = c.get("/api/topology/current").json()
        pw = c.get("/api/lab").json()
    except Exception as e:
        print("err", e, flush=True); time.sleep(5); continue
    st = {d["name"]: (d.get("status"), (cur.get("progress") or {}).get(str(d["id"]), "")) for d in cur["topology"]["devices"] if d["name"] in names}
    for n, s in st.items():
        if last.get(n) != s:
            print(f"{time.time()-t0:6.0f}s {n} {s}", flush=True); last[n] = s
        if s[0] == "online" and n not in ready_at: ready_at[n] = time.time() - t0
    ev = ((pw.get("watch") or {}).get("events") or [])
    for e in ev[nev:]:
        print("  watchdog", json.dumps(e, ensure_ascii=False)[:200], flush=True)
    nev = len(ev)
    if cur.get("console_errors"): print("  console_errors", cur["console_errors"], flush=True)
    if len(ready_at) == len(names) and not any((cur.get("progress") or {}).values()):
        break
    time.sleep(5)
print("ready", {n: round(v) for n, v in ready_at.items()}, "total", round(time.time() - t0), flush=True)
