import pytest

from olvm_mcp.safety import ConfirmationError, ConfirmationTokens


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def tokens(clock):
    return ConfirmationTokens(ttl_seconds=300, clock=clock)


def test_token_redeems_once(tokens):
    t = tokens.issue("stop_vm", "vm1", {}, "status=up")
    assert t.startswith("confirm-")
    tokens.redeem(t, "stop_vm", "vm1", {}, "status=up")
    with pytest.raises(ConfirmationError, match="unknown or was already used"):
        tokens.redeem(t, "stop_vm", "vm1", {}, "status=up")


def test_tokens_are_unique(tokens):
    assert len({tokens.issue("stop_vm", "vm1", {}, "s") for _ in range(50)}) == 50


def test_unknown_token_is_rejected(tokens):
    with pytest.raises(ConfirmationError, match="unknown"):
        tokens.redeem("confirm-made-up", "stop_vm", "vm1", {}, "status=up")


def test_expired_token_is_rejected(tokens, clock):
    t = tokens.issue("stop_vm", "vm1", {}, "status=up")
    clock.now += 301
    with pytest.raises(ConfirmationError, match="expired"):
        tokens.redeem(t, "stop_vm", "vm1", {}, "status=up")


@pytest.mark.parametrize("action, obj, params", [
    ("remove_vm", "vm1", {}),                     # different action
    ("stop_vm", "vm2", {}),                       # different object
    ("stop_vm", "vm1", {"remove_disks": False}),  # different arguments
])
def test_token_is_bound_to_its_request(tokens, action, obj, params):
    t = tokens.issue("stop_vm", "vm1", {}, "status=up")
    with pytest.raises(ConfirmationError, match="different request"):
        tokens.redeem(t, action, obj, params, "status=up")


def test_changed_target_is_rejected(tokens):
    t = tokens.issue("stop_vm", "vm1", {}, "status=up")
    with pytest.raises(ConfirmationError, match=r"changed since the preview \(status=up -> status=paused\)"):
        tokens.redeem(t, "stop_vm", "vm1", {}, "status=paused")


def test_failed_attempt_spends_the_token(tokens):
    t = tokens.issue("stop_vm", "vm1", {}, "status=up")
    with pytest.raises(ConfirmationError):
        tokens.redeem(t, "stop_vm", "vm2", {}, "status=up")
    with pytest.raises(ConfirmationError, match="already used"):
        tokens.redeem(t, "stop_vm", "vm1", {}, "status=up")


def test_expired_tokens_are_purged(tokens, clock):
    tokens.issue("stop_vm", "vm1", {}, "s")
    clock.now += 301
    tokens.issue("stop_vm", "vm1", {}, "s")
    assert len(tokens._pending) == 1
