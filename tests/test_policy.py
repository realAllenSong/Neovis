"""Policy checks: denylist and filesystem sandbox."""

from pathlib import Path

from neovis.core.policy import PolicyConfig
from neovis.core.risk import Risk


def test_denylist_blocks_rm_rf_root():
    policy = PolicyConfig()
    assert policy.is_shell_denied("sudo rm -rf / --no-preserve-root")
    assert policy.is_shell_denied("dd if=/dev/zero of=/dev/sda")
    assert policy.is_shell_denied("shutdown now")


def test_denylist_allows_normal_commands():
    policy = PolicyConfig()
    assert policy.is_shell_denied("git status") is None
    assert policy.is_shell_denied("ls -la ~/project") is None
    assert policy.is_shell_denied("python backtest.py --fast") is None


def test_sandbox_defaults_to_home():
    policy = PolicyConfig()
    assert policy.is_path_allowed(Path.home() / "notes.txt")
    assert not policy.is_path_allowed("/etc/passwd")
    assert not policy.is_path_allowed("/tmp/escape.txt")


def test_sandbox_respects_explicit_roots(tmp_path):
    policy = PolicyConfig(sandbox_roots=[str(tmp_path)])
    assert policy.is_path_allowed(tmp_path / "a" / "b.txt")
    assert not policy.is_path_allowed(Path.home() / "outside.txt")


def test_risk_override():
    policy = PolicyConfig(risk_overrides={"list_files": "dangerous"})
    assert policy.effective_risk("list_files", Risk.SAFE) == Risk.DANGEROUS
    assert policy.effective_risk("read_file", Risk.SAFE) == Risk.SAFE
