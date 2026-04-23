"""Remote file editor — download, edit, and upload files via SFTP."""

import os
import tempfile
import threading

import paramiko
from PySide6 import QtCore, QtGui, QtWidgets

from .theme import DRACULA


class RemoteEditorWidget(QtWidgets.QWidget):
    """远程文件编辑器 — 通过 SFTP 打开、编辑、保存远程文件。"""

    file_saved = QtCore.Signal(str)   # remote_path
    log_message = QtCore.Signal(str)

    def __init__(self, sftp: paramiko.SFTPClient, remote_path: str, parent=None):
        super().__init__(parent)
        self._sftp = sftp
        self._remote_path = remote_path
        self._modified = False

        self._build_ui()
        self._load_file()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 工具栏
        toolbar = QtWidgets.QHBoxLayout()
        toolbar.setContentsMargins(8, 4, 8, 4)

        self._path_label = QtWidgets.QLabel(self._remote_path)
        self._path_label.setStyleSheet(f"color:{DRACULA['yellow']};font-weight:bold;font-size:10pt;")
        toolbar.addWidget(self._path_label, 1)

        self._status_label = QtWidgets.QLabel("")
        self._status_label.setStyleSheet(f"color:{DRACULA['comment']};font-size:9pt;")
        toolbar.addWidget(self._status_label)

        self._btn_save = QtWidgets.QPushButton("保存到服务器")
        self._btn_save.setEnabled(False)
        self._btn_save.clicked.connect(self._save_file)
        toolbar.addWidget(self._btn_save)

        self._btn_reload = QtWidgets.QPushButton("重新加载")
        self._btn_reload.clicked.connect(self._load_file)
        toolbar.addWidget(self._btn_reload)

        layout.addLayout(toolbar)

        # 编辑器
        self._editor = QtWidgets.QPlainTextEdit()
        font = QtGui.QFont("Consolas", 11)
        font.setStyleHint(QtGui.QFont.Monospace)
        self._editor.setFont(font)
        self._editor.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: {DRACULA['bg_darker']};
                color: {DRACULA['fg']};
                border: none;
                selection-background-color: {DRACULA['selection']};
            }}
        """)
        self._editor.setTabStopDistance(40)
        self._editor.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self._editor.textChanged.connect(self._on_text_changed)
        layout.addWidget(self._editor)

    def _on_text_changed(self):
        if not self._modified:
            self._modified = True
            self._btn_save.setEnabled(True)
            self._status_label.setText("● 已修改")
            self._status_label.setStyleSheet(f"color:{DRACULA['orange']};font-size:9pt;")

    def _load_file(self):
        self._status_label.setText("加载中...")
        self._editor.setEnabled(False)

        def do_load():
            try:
                with self._sftp.open(self._remote_path, "r") as f:
                    content = f.read().decode("utf-8", "replace")
                QtCore.QMetaObject.invokeMethod(
                    self, "_set_content",
                    QtCore.Qt.QueuedConnection,
                    QtCore.Q_ARG(str, content)
                )
            except Exception as e:
                QtCore.QMetaObject.invokeMethod(
                    self, "_set_error",
                    QtCore.Qt.QueuedConnection,
                    QtCore.Q_ARG(str, str(e))
                )

        threading.Thread(target=do_load, daemon=True).start()

    @QtCore.Slot(str)
    def _set_content(self, content: str):
        self._editor.setPlainText(content)
        self._editor.setEnabled(True)
        self._modified = False
        self._btn_save.setEnabled(False)
        self._status_label.setText("已加载")
        self._status_label.setStyleSheet(f"color:{DRACULA['green']};font-size:9pt;")

    @QtCore.Slot(str)
    def _set_error(self, error: str):
        self._editor.setEnabled(True)
        self._status_label.setText(f"加载失败: {error}")
        self._status_label.setStyleSheet(f"color:{DRACULA['red']};font-size:9pt;")

    def _save_file(self):
        content = self._editor.toPlainText()
        self._btn_save.setEnabled(False)
        self._status_label.setText("保存中...")

        def do_save():
            try:
                with self._sftp.open(self._remote_path, "w") as f:
                    f.write(content.encode("utf-8"))
                QtCore.QMetaObject.invokeMethod(
                    self, "_on_saved",
                    QtCore.Qt.QueuedConnection
                )
            except Exception as e:
                QtCore.QMetaObject.invokeMethod(
                    self, "_on_save_error",
                    QtCore.Qt.QueuedConnection,
                    QtCore.Q_ARG(str, str(e))
                )

        threading.Thread(target=do_save, daemon=True).start()

    @QtCore.Slot()
    def _on_saved(self):
        self._modified = False
        self._btn_save.setEnabled(False)
        self._status_label.setText("已保存 ✓")
        self._status_label.setStyleSheet(f"color:{DRACULA['green']};font-size:9pt;")
        self.file_saved.emit(self._remote_path)
        self.log_message.emit(f"文件已保存: {self._remote_path}")

    @QtCore.Slot(str)
    def _on_save_error(self, error: str):
        self._btn_save.setEnabled(True)
        self._status_label.setText(f"保存失败: {error}")
        self._status_label.setStyleSheet(f"color:{DRACULA['red']};font-size:9pt;")

    @property
    def is_modified(self) -> bool:
        return self._modified

    @property
    def remote_path(self) -> str:
        return self._remote_path
