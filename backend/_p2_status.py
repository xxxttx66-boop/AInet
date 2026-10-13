"""临时：当前沙箱设备状态 + 看门狗（用完删除）。"""
import json, httpx
c = httpx.Client(base_url="http://127.0.0.1:8796", timeout=120)
c.post("/api/login", json={"login": "admin", "password": "admin"}).raise_for_status()
d = c.get("/api/topology/current").json()
print(d.get("message"))
print({x["name"]: (x["status"], x.get("state_code")) for x in d["topology"]["devices"]})
print("progress", d.get("progress"), "start_errors", d.get("start_errors"), "console_errors", d.get("console_errors"))
w = c.get("/api/lab").json()["watch"]["devices"]
print("watch", {k: (v["phase"], v["restarts"], round(v["elapsed_s"])) for k, v in w.items()})
