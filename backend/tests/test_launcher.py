from pathlib import Path

import pytest

import launcher


@pytest.fixture(autouse=True)
def private_config(monkeypatch, tmp_path):
    monkeypatch.setenv("AINET_LOCAL_CONFIG", str(tmp_path / "missing.env"))
    for name in ("AINET_BIND_HOST", "AINET_BIND_PORT", "AINET_AUTO_LOGIN", "AINET_ALLOW_LAN_AUTO_LOGIN"):
        monkeypatch.delenv(name, raising=False)


def test_default_listens_only_on_localhost():
    args = launcher.options([])
    assert (args.host, args.port) == ("127.0.0.1", 8000)


def test_local_config_preserves_process_overrides(monkeypatch, tmp_path):
    path = tmp_path / "local.env"
    path.write_text('AINET_BIND_HOST=127.0.0.1\nAINET_BIND_PORT="9000"\n# private\n', encoding="utf-8")
    monkeypatch.setenv("AINET_LOCAL_CONFIG", str(path))
    monkeypatch.setenv("AINET_BIND_PORT", "8001")
    assert launcher.options([]).port == 8001


def test_lan_auto_login_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("AINET_AUTO_LOGIN", "admin")
    with pytest.raises(SystemExit):
        launcher.options(["--host", "0.0.0.0"])
    monkeypatch.setenv("AINET_ALLOW_LAN_AUTO_LOGIN", "1")
    assert launcher.options(["--host", "0.0.0.0"]).host == "0.0.0.0"


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_single_user_auto_login_on_loopback_is_allowed(monkeypatch, host):
    monkeypatch.setenv("AINET_AUTO_LOGIN", "admin")
    assert launcher.options(["--host", host]).host == host


def test_invalid_port_and_private_config_errors_do_not_print_values(monkeypatch, tmp_path, capsys):
    with pytest.raises(SystemExit):
        launcher.options(["--port", "0"])
    path = tmp_path / "local.env"
    path.write_text("UNKNOWN=private-value", encoding="utf-8")
    monkeypatch.setenv("AINET_LOCAL_CONFIG", str(path))
    with pytest.raises(SystemExit):
        launcher.options([])
    assert "private-value" not in capsys.readouterr().err
