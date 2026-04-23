"""Tests for sshcat.ssh_manager — HostKeyPolicy, SshManager lifecycle."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from sshcat.ssh_manager import HostKeyPolicy, SshManager, _known_hosts_path, _ensure_ssh_dir


# ====================== HostKeyPolicy ======================

class TestHostKeyPolicy:
    """HostKeyPolicy 应在首次连接时接受并保存密钥。"""

    def test_known_hosts_path(self):
        path = _known_hosts_path()
        assert path.name == "known_hosts"
        assert ".ssh" in str(path)

    def test_ensure_ssh_dir_creates_directory(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "fakehome"
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        ssh_dir = _ensure_ssh_dir()
        assert ssh_dir.exists()
        assert ssh_dir.name == ".ssh"

    def test_policy_accepts_new_host(self, tmp_path, monkeypatch):
        """新主机应被接受（不抛异常）"""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        policy = HostKeyPolicy()
        mock_client = MagicMock()
        mock_key = MagicMock()
        mock_key.get_name.return_value = "ssh-rsa"
        mock_key.asbytes.return_value = b"fakekeydata"
        # 不应抛异常
        policy.missing_host_key(mock_client, "newhost.example.com", mock_key)


# ====================== SshManager ======================

class TestSshManager:
    """SshManager 生命周期测试。"""

    def test_initial_state_not_connected(self):
        mgr = SshManager()
        assert not mgr.connected

    def test_close_when_not_connected(self):
        mgr = SshManager()
        mgr.close()  # 不应抛异常
        assert not mgr.connected

    def test_close_idempotent(self):
        mgr = SshManager()
        mgr.close()
        mgr.close()
        assert not mgr.connected

    def test_open_shell_without_connect_raises(self):
        mgr = SshManager()
        with pytest.raises(RuntimeError, match="SSH 未连接"):
            mgr.open_shell()
