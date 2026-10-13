"""临时包装（新主机验证用，用完删除）：Windows 上用 Edge headless、独立用户目录；补 enspro-t1-t2-link 的学员修复步骤。"""
import asyncio, os, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tests", "e2e"))
import enspro_campus_e2e as m
m.CHROME = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PROFILE = os.path.join(HERE, "_p2", "edge-profile")
class _Sub:
    DEVNULL = subprocess.DEVNULL
    @staticmethod
    def Popen(args, **kw):
        return subprocess.Popen([args[0], "--headless=new", f"--user-data-dir={PROFILE}", "--no-first-run",
                                 "--disable-gpu"] + list(args[1:]), **kw)
m.subprocess = _Sub
_orig = m.fixes
def fixes(sid, pv):
    if sid == "enspro-t1-t2-link":
        link = pv["link"]
        return [("T1", [f"ping -c 2 10.0.{link}.2", "display ip interface brief"]),
                ("T2", ["display ip interface brief", "system-view", "interface GE0/0/1", f"ip address 10.0.{link}.2 24",
                        "return"]),
                ("T1", [f"ping -c 2 10.0.{link}.2"])]
    return _orig(sid, pv)
m.fixes = fixes
class _Errs(list):     # Edge 会请求 /favicon.ico（ainet 没有，404），headless-shell 不请求：只忽略这一条
    def append(self, x):
        if str(x).rstrip().endswith("/favicon.ico"):
            print("  （忽略 Edge 的 favicon.ico 404）", flush=True)
            return
        super().append(x)
_init = m.CDP.__init__
def _cdp_init(self, ws):
    _init(self, ws)
    self.errors = _Errs()
m.CDP.__init__ = _cdp_init
sys.exit(asyncio.run(m.run()))
