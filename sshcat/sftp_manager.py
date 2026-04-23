"""SFTP file transfer — upload, download, progress tracking."""

import os
import stat
import threading
from pathlib import Path
from typing import Optional

import paramiko
from PySide6 import QtCore


class SftpWorker(QtCore.QObject):
    """SFTP 文件传输工作器 — 在后台线程中执行上传/下载。"""

    progress = QtCore.Signal(str, int, int)        # (filename, transferred, total)
    finished = QtCore.Signal(str, bool, str)        # (filename, success, message)
    all_done = QtCore.Signal()

    def __init__(self, sftp: paramiko.SFTPClient):
        super().__init__()
        self._sftp = sftp
        self._tasks: list = []
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def add_upload(self, local_path: str, remote_path: str):
        self._tasks.append(("upload", local_path, remote_path))

    def add_download(self, remote_path: str, local_path: str):
        self._tasks.append(("download", remote_path, local_path))

    def run(self):
        for task in self._tasks:
            if self._cancel.is_set():
                break
            op, src, dst = task
            filename = os.path.basename(src)
            try:
                if op == "upload":
                    self._do_upload(src, dst, filename)
                else:
                    self._do_download(src, dst, filename)
                self.finished.emit(filename, True, "完成")
            except Exception as e:
                self.finished.emit(filename, False, str(e))
        self.all_done.emit()

    def _do_upload(self, local_path: str, remote_path: str, filename: str):
        file_size = os.path.getsize(local_path)

        def callback(transferred, total):
            if self._cancel.is_set():
                raise InterruptedError("取消传输")
            self.progress.emit(filename, transferred, total)

        self._sftp.put(local_path, remote_path, callback=callback)

    def _do_download(self, remote_path: str, local_path: str, filename: str):
        file_stat = self._sftp.stat(remote_path)
        file_size = file_stat.st_size if file_stat.st_size else 0

        def callback(transferred, total):
            if self._cancel.is_set():
                raise InterruptedError("取消传输")
            self.progress.emit(filename, transferred, total)

        self._sftp.get(remote_path, local_path, callback=callback)


class SftpThread(QtCore.QThread):
    """在独立线程中运行 SFTP 传输任务。"""

    progress = QtCore.Signal(str, int, int)
    finished = QtCore.Signal(str, bool, str)
    all_done = QtCore.Signal()

    def __init__(self, sftp: paramiko.SFTPClient, parent=None):
        super().__init__(parent)
        self._worker = SftpWorker(sftp)
        self._worker.progress.connect(self.progress)
        self._worker.finished.connect(self.finished)
        self._worker.all_done.connect(self.all_done)

    def add_upload(self, local_path: str, remote_path: str):
        self._worker.add_upload(local_path, remote_path)

    def add_download(self, remote_path: str, local_path: str):
        self._worker.add_download(remote_path, local_path)

    def cancel(self):
        self._worker.cancel()

    def run(self):
        self._worker.run()
