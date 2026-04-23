"""SSH tunnel / port forwarding — local and remote tunnels."""

import select
import socket
import threading
from typing import Optional

import paramiko
from PySide6 import QtCore


class TunnelEntry:
    """描述一条端口转发规则。"""
    def __init__(self, local_port: int, remote_host: str, remote_port: int,
                 direction: str = "local"):
        self.local_port = local_port
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.direction = direction  # "local" or "remote"

    def label(self) -> str:
        if self.direction == "local":
            return f"L:{self.local_port} → {self.remote_host}:{self.remote_port}"
        return f"R:{self.remote_port} → localhost:{self.local_port}"


class LocalForwarder(threading.Thread):
    """本地端口转发：监听本地端口，通过 SSH 隧道连接到远程目标。

    工作方式: 本地 localhost:local_port → SSH → remote_host:remote_port
    """
    def __init__(self, transport: paramiko.Transport, local_port: int,
                 remote_host: str, remote_port: int):
        super().__init__(daemon=True)
        self._transport = transport
        self._local_port = local_port
        self._remote_host = remote_host
        self._remote_port = remote_port
        self._server: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._handlers: list[threading.Thread] = []

    @property
    def local_port(self) -> int:
        return self._local_port

    def stop(self):
        self._stop.set()
        if self._server:
            try:
                self._server.close()
            except Exception:
                pass

    def run(self):
        try:
            self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server.bind(("127.0.0.1", self._local_port))
            self._server.listen(5)
            self._server.settimeout(1.0)

            while not self._stop.is_set():
                try:
                    client_sock, addr = self._server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                # 为每个连接创建隧道处理线程
                t = threading.Thread(target=self._handle_client,
                                     args=(client_sock,), daemon=True)
                t.start()
                self._handlers.append(t)
        except Exception:
            pass
        finally:
            if self._server:
                try:
                    self._server.close()
                except Exception:
                    pass

    def _handle_client(self, client_sock: socket.socket):
        try:
            chan = self._transport.open_channel(
                "direct-tcpip",
                (self._remote_host, self._remote_port),
                client_sock.getpeername(),
            )
        except Exception:
            client_sock.close()
            return

        if chan is None:
            client_sock.close()
            return

        try:
            while not self._stop.is_set():
                r, _, _ = select.select([client_sock, chan], [], [], 1.0)
                if client_sock in r:
                    data = client_sock.recv(65536)
                    if not data:
                        break
                    chan.sendall(data)
                if chan in r:
                    data = chan.recv(65536)
                    if not data:
                        break
                    client_sock.sendall(data)
        except Exception:
            pass
        finally:
            chan.close()
            client_sock.close()


class TunnelManager(QtCore.QObject):
    """管理多条端口转发规则。"""

    tunnel_started = QtCore.Signal(str)   # label
    tunnel_stopped = QtCore.Signal(str)
    tunnel_error = QtCore.Signal(str, str)  # label, error

    def __init__(self, parent=None):
        super().__init__(parent)
        self._forwarders: dict[str, LocalForwarder] = {}

    def start_local_forward(self, transport: paramiko.Transport, entry: TunnelEntry):
        """启动本地端口转发。"""
        label = entry.label()
        if label in self._forwarders:
            return  # 已存在
        try:
            fwd = LocalForwarder(transport, entry.local_port,
                                 entry.remote_host, entry.remote_port)
            fwd.start()
            self._forwarders[label] = fwd
            self.tunnel_started.emit(label)
        except Exception as e:
            self.tunnel_error.emit(label, str(e))

    def stop_forward(self, label: str):
        fwd = self._forwarders.pop(label, None)
        if fwd:
            fwd.stop()
            self.tunnel_stopped.emit(label)

    def stop_all(self):
        for label in list(self._forwarders.keys()):
            self.stop_forward(label)

    def active_tunnels(self) -> list[str]:
        return list(self._forwarders.keys())
