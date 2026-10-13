"""测试夹具：每个测试用临时 DB，不污染 backend/data。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import ensp_runtime  # noqa: E402
import main  # noqa: E402
import topology  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_runtime(monkeypatch):
    """不受开发机环境变量与模块级短缓存影响。"""
    monkeypatch.delenv("AINET_ENSP_RUNNING_IDS", raising=False)
    monkeypatch.delenv("AINET_ENSP_DEVICES_DIRS", raising=False)
    monkeypatch.delenv("AINET_ENSP_SWITCH_NAMES", raising=False)
    monkeypatch.delenv("AINET_ENSP_CLIENT", raising=False)
    monkeypatch.setenv("AINET_ENSP_TOPO_HIS", "")  # 空值：不读任何 topo.his
    # 不碰本机真实的 eNSP（在 Windows 开发/部署机上跑测试时尤其重要）：
    # 不调 VBoxManage、不查进程、不扫真实 devices 目录
    monkeypatch.setenv("AINET_ENSP_DEVICES_DIRS", "")
    monkeypatch.setattr(ensp_runtime, "_vboxmanage_ids", lambda: set())
    monkeypatch.setattr(ensp_runtime, "_processes", lambda: (set(), None))
    ensp_runtime.clear_cache()
    topology.clear_scan_cache()
    yield
    ensp_runtime.clear_cache()
    topology.clear_scan_cache()


@pytest.fixture(autouse=True)
def _local_only(monkeypatch):
    """测试绝不连真实设备：设备主机固定为本机（各测试另把 connector 换成假设备）。
    练习种子固定为 0（参数化场景取默认值，与参数化之前一致）；考试计时器不在后台跑，测试里手动 tick。"""
    monkeypatch.setattr(config, "ENSP_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "PRACTICE_SEED", 0)
    monkeypatch.setattr(config, "EXAM_TICK", 0)


@pytest.fixture(autouse=True)
def _no_real_lab(monkeypatch):
    """测试绝不连真实的 eNSP Pro：实验后端固定为 eNSP 经典版（测试 eNSP Pro 的用例自己换成假服务），
    eNSP Pro 地址指向本机不可用端口，设备 console 密码不从开发机环境变量读取。"""
    for k in ("AINET_LAB_PROVIDER", "AINET_ENSPRO_URL", "AINET_ENSPRO_SANDBOX", "AINET_ENSPRO_AUTO_RELOGIN",
              "ENSPRO_DEVICE_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(config, "LAB_PROVIDER", "ensp")
    monkeypatch.setattr(config, "ENSPRO_URL", "https://127.0.0.1:9")
    monkeypatch.setattr(config, "ENSPRO_SANDBOX", None)
    monkeypatch.setattr(config, "ENSPRO_AUTO_RELOGIN", False)
    monkeypatch.setattr(main, "LAB", None)
    # 接入地址的运行时覆盖（库里的记录，spec §5.2「接入设置」）不跨测试残留
    import lab as lab_lib
    monkeypatch.setattr(lab_lib, "_endpoints", {})
    monkeypatch.setattr(lab_lib, "_watch", {})          # 看门狗参数的运行时覆盖（lab:watch:<环境>）同理
    # 用户添加的环境实例（里程碑 6.19）注册在 REGISTRY 里：每个测试结束后恢复成原样，不跨测试残留
    monkeypatch.setattr(lab_lib, "REGISTRY", dict(lab_lib.REGISTRY))
    # 启动可靠性（spec §10.5）的时间类开关：缺省测试里关掉（一次批量启动、不等停机间隔、不做 console 就绪探测、
    # 不做空闲停机、不强制环境变量），保持原有用例的行为；test_labpower.py 按需逐项打开
    monkeypatch.setattr(config, "ENSPRO_START_STAGGER", 0.0)
    monkeypatch.setattr(config, "ENSPRO_RESTART_GAP", 0.0)
    monkeypatch.setattr(config, "ENSPRO_PROBE", False)
    monkeypatch.setattr(config, "ENSPRO_EARLY_CHECK", 0.0)
    monkeypatch.setattr(config, "ENSPRO_IDLE_STOP_MIN", 0.0)
    monkeypatch.setattr(config, "LAB_FORCE", False)
    # 里程碑 6.17：同时运行设备上限、自动登录缺省关闭（不受开发机 / 部署机环境变量影响）
    monkeypatch.delenv("AINET_ENSPRO_MAX_DEVICES", raising=False)
    monkeypatch.delenv("AINET_AUTO_LOGIN", raising=False)
    monkeypatch.setattr(config, "ENSPRO_MAX_DEVICES", 0)
    monkeypatch.setattr(config, "AUTO_LOGIN", "")


@pytest.fixture(autouse=True)
def _no_real_ai(monkeypatch, tmp_path):
    """默认测试绝不访问 DeepSeek：不读开发机上的 Key（环境变量 / secrets 文件都屏蔽），
    客户端工厂换成「一调用就报无 Key」的桩。需要 AI 的测试自己 monkeypatch ai.make_client。"""
    import ai

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("AINET_SECRETS_FILE", str(tmp_path / "no-such-secrets.env"))

    def _stub():
        raise ai.AIError("no_key", "测试环境未 mock AI 客户端")
    monkeypatch.setattr(ai, "make_client", _stub)
    monkeypatch.setattr(ai, "_client_cache", None)
    # 页面上的 AI 接口设置（里程碑 6.18）不跨测试残留；测试连接不走真实网络（各测试自己装 MockTransport）
    monkeypatch.setattr(ai, "_settings", {"base_url": None, "model": None, "key_page": False})
    import httpx

    def _no_net(req):
        raise httpx.ConnectError("测试禁止联网", request=req)
    monkeypatch.setattr(ai, "_TEST_TRANSPORT", httpx.MockTransport(_no_net))
    monkeypatch.delenv("AINET_AI_BASE_URL", raising=False)
    monkeypatch.delenv("AINET_AI_MODEL", raising=False)


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", str(path))
    return path


@pytest.fixture
def client(db_path):
    from fastapi.testclient import TestClient

    main.SESSIONS.clear()
    with TestClient(main.app) as c:  # with 触发 lifespan -> init_db()
        yield c
    main.SESSIONS.clear()
