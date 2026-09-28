import pytest

from olvm_mcp.config import ConfigError, Settings

ENV = {"OLVM_URL": "https://engine.test/ovirt-engine", "OLVM_USERNAME": "u", "OLVM_PASSWORD": "p"}


@pytest.mark.parametrize("url", [
    "https://engine.test/ovirt-engine",
    "https://engine.test/ovirt-engine/",
    "https://engine.test/ovirt-engine/api",
    "https://engine.test",
])
def test_url_is_normalized(url):
    assert Settings.from_env({**ENV, "OLVM_URL": url}).url == "https://engine.test/ovirt-engine"


def test_http_url_rejected():
    with pytest.raises(ConfigError, match="https"):
        Settings.from_env({**ENV, "OLVM_URL": "http://engine.test/ovirt-engine"})


def test_missing_values_are_named():
    with pytest.raises(ConfigError, match="OLVM_URL, OLVM_USERNAME"):
        Settings.from_env({"OLVM_PASSWORD": "p"})


def test_password_from_file(tmp_path):
    pw = tmp_path / "pw"
    pw.write_text("from-file\n", encoding="utf-8")
    env = {k: v for k, v in ENV.items() if k != "OLVM_PASSWORD"} | {"OLVM_PASSWORD_FILE": str(pw)}
    assert Settings.from_env(env).password == "from-file"


def test_missing_ca_file_rejected(tmp_path):
    with pytest.raises(ConfigError, match="OLVM_CA_FILE"):
        Settings.from_env({**ENV, "OLVM_CA_FILE": str(tmp_path / "nope.pem")})


def test_verify_modes(tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("x", encoding="utf-8")
    assert Settings.from_env(ENV).verify is True
    assert Settings.from_env({**ENV, "OLVM_CA_FILE": str(ca)}).verify == str(ca)
    assert Settings.from_env({**ENV, "OLVM_INSECURE": "true"}).verify is False


def test_password_not_in_repr():
    assert "p'" not in repr(Settings.from_env(ENV))
