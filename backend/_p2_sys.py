"""临时：只读查看 eNSP Pro 资源概况（会短暂挤掉本地 ainet 的会话，之后由 ainet 重新登录）。用完删除。"""
import asyncio, json
from enspro import EnsproClient
async def main():
    c = EnsproClient("https://172.16.133.138:8443", timeout=60)
    try:
        for p in ("/service/ensp/system/summary", "/service/ensp/system/detail", "/service/ensp/device/getStartedDeviceNum"):
            try:
                print(p, json.dumps(await c.call("GET", p), ensure_ascii=False)[:1500])
            except Exception as e:
                print(p, "ERR", e)
    finally:
        await c.aclose()
asyncio.run(main())
