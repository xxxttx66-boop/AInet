"""临时：读独立库里的基线，检查是否出厂配置（不打印密文）。用完删除。"""
import json, re, sqlite3, sys, difflib
con = sqlite3.connect(r"D:\AINET\backend\_p2\ainet.db")
rows = [(k, v) for k, v in con.execute("select key, value from app_settings") if "baseline" in k]
SKIP = re.compile(r"cipher|irreversible|engine-id|^\s*!.*time|Last configuration|^\s*#\s*$|snmp-agent local-engineid", re.I)
cfgs = {}
for k, v in rows:
    d = json.loads(v); text = d.get("config") or ""
    name = k.split(":")[-1]
    lines = [l for l in text.splitlines() if l.strip() and not SKIP.search(l)]
    cfgs[k] = lines
    ips = [l.strip() for l in lines if re.match(r"\s*ip address \d", l)]
    sysn = [l.strip() for l in lines if l.strip().startswith("sysname")]
    print(k, d.get("at"), "行数", len(lines), sysn, "ip:", ips, "dhcp enable" in text, "ospf" in text, "ip route-static" in text,
          "acl" in text.lower() and "acl number" in text, "vlan batch" in text, "nat" in text.split("dhcp")[0][:0] or "nat-policy" in text)
keys = sorted(cfgs)
if len(sys.argv) > 2 and sys.argv[1] != "-":
    a, b = sys.argv[1], sys.argv[2]
    ka = next(k for k in keys if k.endswith(":" + a)); kb = next(k for k in keys if k.endswith(":" + b))
    print("diff", a, b, [l for l in difflib.unified_diff(cfgs[ka], cfgs[kb], lineterm="", n=0)][:40])
if "--dump" in sys.argv:
    k = next(k for k in keys if k.endswith(":" + sys.argv[sys.argv.index("--dump") + 1]))
    print("\n".join(cfgs[k]))
