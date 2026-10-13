"""在目标 eNSP Pro 上按 src.json 复制沙箱 ainet_test / ainet_campus（幂等：已有的设备 / 连线跳过）。"""
import asyncio, json, os, sys
from enspro import EnsproClient
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = json.load(open(os.path.join(HERE, "_p2_src.json"), encoding="utf-8"))
DRY = "--dry" in sys.argv

def ifkey(sb):
    return {i["id"]: (d["name"], i["name"], i.get("serialId")) for d in sb["devices"] for i in d["deviceIfs"]}

def summary(sb):
    devs = sorted((d["name"], d["deviceType"]["name"], round(d["devicePosition"]["positionX"], 4),
                   round(d["devicePosition"]["positionY"], 4),
                   tuple((i["name"], i.get("serialId")) for i in sorted(d["deviceIfs"], key=lambda i: i.get("serialId", 0))))
                  for d in sb["devices"])
    k = ifkey(sb)
    links = sorted(tuple(sorted([k[l["leftIfId"]][:2], k[l["rightIfId"]][:2]])) for l in sb["links"])
    return devs, links

async def main():
    c = EnsproClient("https://172.16.133.138:8443")
    try:
        await c.ensure()
        types = await c.call("GET", "/service/ensp/device-type/all", params={"sandboxId": 1})
        tmap = {t["name"]: t for t in types["devices"]}
        for name in ("ainet_test", "ainet_campus"):
            src = SRC[name]
            sbs = {s["name"]: s for s in await c.sandboxes()}
            if name not in sbs:
                print(name, "创建沙箱", "(dry)" if DRY else "")
                if DRY:
                    continue
                r = await c.call("POST", "/service/ensp/sandbox/create", {
                    "name": name, "isPublic": 0, "maxVms": src["maxVms"], "keepTime": src["keepTime"],
                    "projectId": 1, "type": 0, "userId": 1})
                print("  create ->", r)
                sbs = {s["name"]: s for s in await c.sandboxes()}
            sid = sbs[name]["id"]
            tgt = await c.sandbox(sid)
            have = {d["name"] for d in tgt["devices"]}
            new = []
            for d in src["devices"]:
                if d["name"] in have:
                    continue
                st = d["deviceType"]
                tt = tmap[st["name"]]
                sa = [(a["ifType"]["name"], a["ifNumber"], a["ifSlotId"], a["ifPicId"], a["ifPortStart"]) for a in st["ifAssembles"]]
                ta = [(a["ifType"]["name"], a["ifNumber"], a["ifSlotId"], a["ifPicId"], a["ifPortStart"]) for a in tt["ifAssembles"]]
                assert sorted(sa) == sorted(ta), (d["name"], sa, ta)
                pos = lambda p: {"positionX": p["positionX"], "positionY": p["positionY"]}
                new.append({"name": d["name"], "deviceTypeId": tt["id"], "devicePosition": pos(d["devicePosition"]),
                            "namePosition": pos(d["namePosition"])})
            print(name, "id", sid, "待建设备", [(x["name"], x["deviceTypeId"]) for x in new])
            if new and not DRY:
                print("  batchCreate ->", await c.call("POST", "/service/ensp/device/batchCreate", {"sandboxId": sid, "devices": new}))
                tgt = await c.sandbox(sid)
            if DRY:
                continue
            tif = {(d["name"], i["name"]): (d["id"], i["id"]) for d in tgt["devices"] for i in d["deviceIfs"]}
            sk, tk = ifkey(src), ifkey(tgt)
            tlinks = {tuple(sorted([tk[l["leftIfId"]][:2], tk[l["rightIfId"]][:2]])) for l in tgt["links"]}
            for l in src["links"]:
                a, b = sk[l["leftIfId"]][:2], sk[l["rightIfId"]][:2]
                if tuple(sorted([a, b])) in tlinks:
                    continue
                (ld, li), (rd, ri) = tif[a], tif[b]
                print("  link", a, b, "->", await c.create_link(sid, ld, li, rd, ri))
            tgt = await c.sandbox(sid)
            # PC 属性
            for d in src["devices"]:
                sattr = {k: v for k, v in src["_attrs"].get(d["name"], {}).items() if k.startswith("ipv4.")}
                if not sattr:
                    continue
                tid = next(x["id"] for x in tgt["devices"] if x["name"] == d["name"])
                cur = await c.device_attrs(tid)
                diff = {k: v for k, v in sattr.items() if (cur.get(k) or "") != (v or "")}
                print("  attrs", d["name"], "目标当前", {k: cur.get(k) for k in sattr}, "需改", diff)
                if diff:
                    await c.set_device_attrs(tid, diff)
                    cur = await c.device_attrs(tid)
                    print("  attrs 更新后", {k: cur.get(k) for k in sattr})
            s1, s2 = summary(src), summary(tgt)
            print(name, "设备一致:", s1[0] == s2[0], "连线一致:", s1[1] == s2[1], f"({len(s2[0])} 台, {len(s2[1])} 条)")
            if s1 != s2:
                print("  src", s1); print("  tgt", s2)
            print("  目标设备 id", {d["name"]: d["id"] for d in tgt["devices"]}, "状态", {d["name"]: d["status"] for d in tgt["devices"]})
        print("沙箱", [(s["id"], s["name"]) for s in await c.sandboxes()])
    finally:
        await c.aclose(); print("logged out")
asyncio.run(main())
