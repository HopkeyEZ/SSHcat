"""Session — encapsulates all per-tab SSH state (connection, threads, channel)."""

import threading
from typing import Optional

import paramiko
from PySide6 import QtCore

from .ssh_manager import SshManager, SshExecHelper
from .threads import SshReaderThread, SshWriterThread, PanelRefreshThread
from .terminal_widget import TerminalWidget


class Session(QtCore.QObject):
    """封装单个 SSH 标签页的所有状态：连接、通道、线程、终端控件。"""

    connected = QtCore.Signal()
    disconnected = QtCore.Signal()
    connect_failed = QtCore.Signal(str)
    log_message = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ssh = SshManager()
        self.exec_helper = SshExecHelper(self.ssh)
        self.terminal = TerminalWidget()

        self._channel: Optional[paramiko.Channel] = None
        self._reader: Optional[SshReaderThread] = None
        self._panel_thread: Optional[PanelRefreshThread] = None
        self._conn_info: Optional[dict] = None  # 用于重连

    @property
    def is_connected(self) -> bool:
        return self.ssh.connected

    @property
    def conn_info(self) -> Optional[dict]:
        return self._conn_info

    def connect(self, host, port, username, password=None, key_path=None):
        """在后台线程中发起 SSH 连接。"""
        self._conn_info = {"host": host, "port": int(port), "username": username,
                           "password": password, "key_path": key_path}

        def do_connect():
            try:
                self.ssh.connect(host, port, username, password, key_path)
                cols, rows = self.terminal._calc_grid()
                self._channel = self.ssh.open_shell(cols, rows)
                self.terminal.set_channel(self._channel)

                self._reader = SshReaderThread(self._channel)
                self._reader.data_ready.connect(self.terminal.feed_data)
                self._reader.disconnected.connect(self._on_disconnected)
                self._reader.start()

                self._panel_thread = PanelRefreshThread(self.exec_helper)
                self._panel_thread.start()

                self.connected.emit()
                self.log_message.emit(f"已连接到 {host}:{port}")
            except Exception as e:
                self.connect_failed.emit(str(e))

        threading.Thread(target=do_connect, daemon=True).start()

    @QtCore.Slot()
    def _on_disconnected(self):
        self.log_message.emit("连接已断开")
        self.disconnected.emit()
        # 尝试自动重连
        if self._conn_info:
            self._try_reconnect()

    def _try_reconnect(self):
        info = self._conn_info
        self._conn_info = None  # 只重连一次

        def do_reconnect():
            import time
            time.sleep(2)
            try:
                self.cleanup_threads()
                self.ssh.connect(info["host"], info["port"], info["username"],
                                 info.get("password"), info.get("key_path"))
                cols, rows = self.terminal._calc_grid()
                self._channel = self.ssh.open_shell(cols, rows)
                self.terminal.set_channel(self._channel)

                self._reader = SshReaderThread(self._channel)
                self._reader.data_ready.connect(self.terminal.feed_data)
                self._reader.disconnected.connect(self._on_disconnected)
                self._reader.start()

                self._panel_thread = PanelRefreshThread(self.exec_helper)
                self._panel_thread.start()

                self._conn_info = info  # 恢复以支持再次重连
                self.connected.emit()
                self.log_message.emit("自动重连成功")
            except Exception as e:
                self.connect_failed.emit(f"自动重连失败: {e}")

        self.log_message.emit("正在尝试自动重连...")
        threading.Thread(target=do_reconnect, daemon=True).start()

    def disconnect(self):
        """手动断开，不重连。"""
        self._conn_info = None
        self.cleanup_threads()
        self.ssh.close()

    def cleanup_threads(self):
        if self._reader:
            self._reader.stop()
            self._reader.wait(2000)
            self._reader = None

        if self._panel_thread:
            self._panel_thread.stop()
            self._panel_thread.wait(2000)
            self._panel_thread = None

        self.terminal.set_channel(None)

        if self._channel:
            try:
                self._channel.close()
            except Exception:
                pass
            self._channel = None

    @property
    def panel_thread(self) -> Optional[PanelRefreshThread]:
        return self._panel_thread

    def get_sftp(self) -> Optional[paramiko.SFTPClient]:
        """从当前连接打开 SFTP 通道。"""
        if not self.ssh.connected or not self.ssh._ssh:
            return None
        try:
            return self.ssh._ssh.open_sftp()
        except Exception:
            return None
