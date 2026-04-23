"""SSH connection management — connect, authenticate, open shell channels."""

import os
import socket
import threading
from pathlib import Path
from typing import Optional

import paramiko


# ====================== 已知主机文件 ======================

def _known_hosts_path() -> Path:
    """返回 known_hosts 文件路径（~/.ssh/known_hosts）"""
    return Path.home() / ".ssh" / "known_hosts"


def _ensure_ssh_dir():
    """确保 ~/.ssh 目录存在"""
    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    return ssh_dir


class HostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """自定义主机密钥策略：首次连接自动信任并保存，后续验证指纹。

    行为类似 SSH 的 StrictHostKeyChecking=accept-new：
    - 新主机 → 自动接受并记录到 known_hosts
    - 已知主机 → 验证指纹是否匹配，不匹配则拒绝
    """

    def missing_host_key(self, client, hostname, key):
        kh_path = _known_hosts_path()
        _ensure_ssh_dir()

        # 尝试加载已有的 known_hosts
        host_keys = paramiko.HostKeys()
        if kh_path.exists():
            try:
                host_keys.load(str(kh_path))
            except Exception:
                pass

        # 检查是否已有该主机的记录
        existing = host_keys.lookup(hostname)
        if existing is not None:
            key_type = key.get_name()
            if key_type in existing:
                if existing[key_type] != key:
                    raise paramiko.SSHException(
                        f"主机 {hostname} 的密钥指纹已变更！"
                        f"可能存在中间人攻击。"
                        f"如确认安全，请删除 {kh_path} 中对应条目后重试。"
                    )
                return  # 指纹匹配，放行

        # 新主机 → 接受并保存
        host_keys.add(hostname, key.get_name(), key)
        try:
            host_keys.save(str(kh_path))
        except Exception:
            pass


# ====================== SSH 连接创建 ======================

def _create_ssh_client(host, port, username, password=None, key_path=None):
    """创建经过优化的 SSH 连接，支持密码认证和密钥认证。"""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(HostKeyPolicy())

    # 加载已知主机
    kh_path = _known_hosts_path()
    if kh_path.exists():
        try:
            ssh.load_host_keys(str(kh_path))
        except Exception:
            pass

    connect_kwargs = dict(
        hostname=host,
        port=int(port),
        username=username,
        timeout=10,
        allow_agent=False,
        look_for_keys=False,
        compress=True,
    )

    # 密钥认证优先
    if key_path and os.path.isfile(key_path):
        try:
            pkey = paramiko.RSAKey.from_private_key_file(key_path)
        except paramiko.ssh_exception.SSHException:
            try:
                pkey = paramiko.Ed25519Key.from_private_key_file(key_path)
            except paramiko.ssh_exception.SSHException:
                pkey = paramiko.ECDSAKey.from_private_key_file(key_path)
        connect_kwargs["pkey"] = pkey
    elif password:
        connect_kwargs["password"] = password

    ssh.connect(**connect_kwargs)

    t = ssh.get_transport()
    if t:
        t.set_keepalive(15)
        # 增大 SSH 窗口大小，减少流控等待
        t.default_window_size = 2097152          # 2 MB
        t.default_max_packet_size = 65536        # 64 KB
        try:
            t.packetizer.REKEY_BYTES = pow(2, 40)
            t.packetizer.REKEY_PACKETS = pow(2, 40)
        except Exception:
            pass
        # TCP_NODELAY — 禁用 Nagle 算法，按键立刻上行
        try:
            sock = t.sock
            if hasattr(sock, 'setsockopt'):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
    return ssh


# ====================== SSH 连接管理器 ======================

class SshManager:
    """主终端通道专用连接 — 不加锁，只在读取/写入线程中使用"""
    def __init__(self):
        self._ssh: Optional[paramiko.SSHClient] = None

    @property
    def connected(self):
        return (self._ssh is not None
                and self._ssh.get_transport() is not None
                and self._ssh.get_transport().is_active())

    def connect(self, host, port, username, password=None, key_path=None):
        self.close()
        self._ssh = _create_ssh_client(host, port, username, password, key_path)

    def open_shell(self, cols=80, rows=24) -> paramiko.Channel:
        if not self._ssh:
            raise RuntimeError("SSH 未连接")
        chan = self._ssh.get_transport().open_session()
        chan.get_pty(term="xterm-256color", width=cols, height=rows)
        chan.invoke_shell()
        chan.settimeout(0.01)  # 10ms 超时，极低延迟
        return chan

    def close(self):
        try:
            if self._ssh:
                self._ssh.close()
        except Exception:
            pass
        self._ssh = None


class SshExecHelper:
    """在已有 SSH 连接上执行命令 — 与终端 shell 通道互不干扰"""
    def __init__(self, ssh_manager: SshManager):
        self._mgr = ssh_manager
        self._lock = threading.Lock()

    @property
    def connected(self):
        return self._mgr.connected

    def exec_cmd(self, cmd: str) -> str:
        with self._lock:
            ssh = self._mgr._ssh
            if not ssh:
                return ""
            try:
                _, stdout, _ = ssh.exec_command(cmd, timeout=8)
                return stdout.read().decode("utf-8", "replace")
            except Exception:
                return ""
