"""临时：调本地 ainet 接口。用法 python _p2_api.py METHOD PATH [JSON]（用完删除）。"""
import json, sys, httpx
c = httpx.Client(base_url="http://127.0.0.1:8796", timeout=600)
c.post("/api/login", json={"login": "admin", "password": "admin"}).raise_for_status()
m, p = sys.argv[1], sys.argv[2]
def _parse(a):
    if a.startswith("{") or a == "null":
        return json.loads(a)
    out = {}
    for kv in a.split(";"):      # k=v;k2=v2（PowerShell 传 JSON 引号会丢），v 能按 JSON 解析就解析
        k, v = kv.split("=", 1)
        try:
            out[k] = json.loads(v)
        except ValueError:
            out[k] = v.split("|") if "|" in v or k == "devices" else v      # a|b|c -> 列表
    return out
body = _parse(sys.argv[3]) if len(sys.argv) > 3 else None
r = c.request(m, p, json=body)
print(r.status_code, json.dumps(r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text, ensure_ascii=False)[:int(sys.argv[4]) if len(sys.argv) > 4 else 3000])
