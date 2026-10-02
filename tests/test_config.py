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


def test_read_only_is_the_default_mode():
    assert Settings.from_env(ENV).mode == "read_only"


def test_unknown_mode_rejected():
    with pytest.raises(ConfigError, match="OLVM_MODE"):
        Settings.from_env({**ENV, "OLVM_MODE": "admin"})


def test_operator_mode_needs_a_cluster_allow_list():
    with pytest.raises(ConfigError, match="OLVM_ALLOWED_CLUSTERS"):
        Settings.from_env({**ENV, "OLVM_MODE": "operator"})


def test_cluster_allow_list(tmp_path):
    s = Settings.from_env({**ENV, "OLVM_MODE": "Operator", "OLVM_ALLOWED_CLUSTERS": " Default, lab ",
                           "OLVM_AUDIT_LOG": str(tmp_path / "a.jsonl")})
    assert s.mode == "operator" and s.allowed_clusters == ("Default", "lab")
    assert s.cluster_allowed("default") and s.cluster_allowed("LAB")
    assert not s.cluster_allowed("prod") and not s.cluster_allowed(None)
    assert s.audit_log == tmp_path / "a.jsonl"


def test_star_allows_every_cluster():
    s = Settings.from_env({**ENV, "OLVM_MODE": "operator", "OLVM_ALLOWED_CLUSTERS": "*"})
    assert s.cluster_allowed("anything")


def test_destructive_is_off_by_default():
    assert Settings.from_env({**ENV, "OLVM_MODE": "operator", "OLVM_ALLOWED_CLUSTERS": "*"}).allow_destructive is False


def test_destructive_can_be_enabled_in_operator_mode():
    s = Settings.from_env({**ENV, "OLVM_MODE": "operator", "OLVM_ALLOWED_CLUSTERS": "*",
                           "OLVM_ALLOW_DESTRUCTIVE": "true"})
    assert s.allow_destructive is True


def test_destructive_needs_operator_mode():
    with pytest.raises(ConfigError, match="needs OLVM_MODE=operator"):
        Settings.from_env({**ENV, "OLVM_ALLOW_DESTRUCTIVE": "true"})
