"""Main application window — connection panel, directory browser, system status, terminal."""

import json
import os
import sys
import time
import ctypes
import threading
from pathlib import Path
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets
import paramiko

from .theme import DRACULA
from .ssh_manager import SshManager, SshExecHelper
from .threads import SshReaderThread, PanelRefreshThread
from .terminal_widget import TerminalWidget
from .crypto import encrypt_password, decrypt_password


# ====================== 连接历史 ======================

_HISTORY_PATH = Path.home() / ".sshcat_history.json"
_MAX_HISTORY = 20


def load_history() -> list:
    """加载连接历史记录"""
    if not _HISTORY_PATH.exists():
        return []
    try:
        data = json.loads(_HISTORY_PATH.read_text("utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_history(entries: list):
    """保存连接历史记录"""
    try:
        _HISTORY_PATH.write_text(json.dumps(entries[:_MAX_HISTORY], ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass


def add_history_entry(host, port, username, password=""):
    """添加一条连接记录（去重，最新在前，密码加密存储）"""
    entries = load_history()
    entry = {"host": host, "port": int(port), "username": username,
             "password_enc": encrypt_password(password)}
    # 去重
    entries = [e for e in entries if not (e.get("host") == host and e.get("port") == int(port) and e.get("username") == username)]
    entries.insert(0, entry)
    save_history(entries)


# ====================== 主窗口 ======================

class MainWindow(QtWidgets.QMainWindow):
    log_signal = QtCore.Signal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("SSHcat")
        self.resize(1200, 700)
        # 设置窗口图标
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "icon.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QtGui.QIcon(icon_path))

        self._ssh = SshManager()
        self._exec = SshExecHelper(self._ssh)  # 复用同一连接的独立通道
        self._reader: Optional[SshReaderThread] = None
        self._panel_thread: Optional[PanelRefreshThread] = None
        self._channel: Optional[paramiko.Channel] = None
        self._reconnect_info = None  # 用于断线重连

        self._build_ui()
        self._setup_styles()
        self._setup_dark_titlebar()
        self._load_history_to_combo()

        self.log_signal.connect(self._append_log)

    def log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.log_signal.emit(f"[{ts}] {msg}")

    def _append_log(self, msg: str):
        self.log_edit.append(msg)
        sb = self.log_edit.verticalScrollBar()
        sb.setValue(sb.maximum())

    # -------------------- UI 构建 --------------------

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)

        root_layout = QtWidgets.QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        splitter_main = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        root_layout.addWidget(splitter_main)

        # 上半: 左面板 + 终端
        top_widget = QtWidgets.QWidget()
        top_layout = QtWidgets.QHBoxLayout(top_widget)
        top_layout.setContentsMargins(0, 0, 0, 0)

        splitter_lr = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        top_layout.addWidget(splitter_lr)
        splitter_main.addWidget(top_widget)

        # ---- 左侧面板 ----
        left_scroll = QtWidgets.QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        left_container = QtWidgets.QWidget()
        left_scroll.setWidget(left_container)
        splitter_lr.addWidget(left_scroll)

        left_layout = QtWidgets.QVBoxLayout(left_container)
        left_layout.setContentsMargins(10, 10, 10, 10)

        # 连接信息
        conn_group = QtWidgets.QGroupBox("连接信息")
        conn_layout = QtWidgets.QGridLayout(conn_group)

        row = 0
        conn_layout.addWidget(QtWidgets.QLabel("服务器 IP"), row, 0)
        self.ip_edit = QtWidgets.QLineEdit()
        self.ip_edit.setPlaceholderText("例: 192.168.1.100")
        conn_layout.addWidget(self.ip_edit, row, 1)

        row += 1
        conn_layout.addWidget(QtWidgets.QLabel("端口"), row, 0)
        self.port_edit = QtWidgets.QLineEdit("22")
        conn_layout.addWidget(self.port_edit, row, 1)

        row += 1
        conn_layout.addWidget(QtWidgets.QLabel("用户名"), row, 0)
        self.user_edit = QtWidgets.QLineEdit("root")
        conn_layout.addWidget(self.user_edit, row, 1)

        row += 1
        conn_layout.addWidget(QtWidgets.QLabel("密码"), row, 0)
        self.pass_edit = QtWidgets.QLineEdit()
        self.pass_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        conn_layout.addWidget(self.pass_edit, row, 1)

        row += 1
        conn_layout.addWidget(QtWidgets.QLabel("密钥文件"), row, 0)
        key_row = QtWidgets.QHBoxLayout()
        self.key_edit = QtWidgets.QLineEdit()
        self.key_edit.setPlaceholderText("可选，如 ~/.ssh/id_rsa")
        btn_browse_key = QtWidgets.QPushButton("浏览")
        btn_browse_key.setFixedWidth(50)
        btn_browse_key.clicked.connect(self._browse_key_file)
        key_row.addWidget(self.key_edit)
        key_row.addWidget(btn_browse_key)
        conn_layout.addLayout(key_row, row, 1)

        row += 1
        conn_layout.addWidget(QtWidgets.QLabel("历史"), row, 0)
        self.history_combo = QtWidgets.QComboBox()
        self.history_combo.addItem("— 选择历史连接 —")
        self.history_combo.currentIndexChanged.connect(self._on_history_selected)
        conn_layout.addWidget(self.history_combo, row, 1)

        row += 1
        btn_row = QtWidgets.QHBoxLayout()
        self.btn_connect = QtWidgets.QPushButton("连接服务器")
        self.btn_disconnect = QtWidgets.QPushButton("断开连接")
        self.btn_disconnect.setEnabled(False)
        self.btn_connect.clicked.connect(self.on_connect)
        self.btn_disconnect.clicked.connect(self.on_disconnect)
        btn_row.addWidget(self.btn_connect)
        btn_row.addWidget(self.btn_disconnect)
        conn_layout.addLayout(btn_row, row, 0, 1, 2)

        # 状态指示
        row += 1
        status_row = QtWidgets.QHBoxLayout()
        self.status_dot = QtWidgets.QLabel()
        self.status_dot.setFixedSize(12, 12)
        self.status_dot.setStyleSheet("border-radius:6px;background:#ff5555;")
        self.status_label = QtWidgets.QLabel("未连接")
        status_row.addWidget(self.status_dot)
        status_row.addWidget(self.status_label)
        status_row.addStretch(1)
        conn_layout.addLayout(status_row, row, 0, 1, 2)

        left_layout.addWidget(conn_group)

        # 目录面板
        dir_group = QtWidgets.QGroupBox("当前目录")
        dir_layout = QtWidgets.QVBoxLayout(dir_group)

        # 路径行：返回按钮 + 路径标签
        dir_header = QtWidgets.QHBoxLayout()
        self.btn_back = QtWidgets.QPushButton()
        self.btn_back.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_ArrowBack))
        self.btn_back.setFixedSize(28, 28)
        self.btn_back.setToolTip("返回上级目录")
        self.btn_back.setStyleSheet(f"""
            QPushButton {{
                background-color: {DRACULA['comment']};
                color: {DRACULA['fg']};
                border-radius: 4px;
                font-weight: bold;
                font-size: 14px;
            }}
            QPushButton:hover {{
                background-color: #7082b6;
            }}
            QPushButton:pressed {{
                background-color: {DRACULA['selection']};
            }}
        """)
        self.btn_back.clicked.connect(self._on_back_clicked)
        dir_header.addWidget(self.btn_back)

        self.dir_path_label = QtWidgets.QLabel("—")
        self.dir_path_label.setStyleSheet(f"color:{DRACULA['yellow']};font-weight:bold;")
        self.dir_path_label.setWordWrap(True)
        dir_header.addWidget(self.dir_path_label, 1)
        dir_layout.addLayout(dir_header)

        self.file_list = QtWidgets.QListWidget()
        self.file_list.setMaximumHeight(200)
        self.file_list.setFont(QtGui.QFont("Consolas", 9))
        self.file_list.setStyleSheet(f"""
            QListWidget {{
                background-color: {DRACULA['bg_darker']};
                border: 1px solid {DRACULA['selection']};
                border-radius: 4px;
            }}
            QListWidget::item {{
                padding: 2px 4px;
            }}
            QListWidget::item:hover {{
                background-color: {DRACULA['selection']};
            }}
        """)
        self.file_list.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.file_list.customContextMenuRequested.connect(self._on_file_context_menu)
        self.file_list.itemDoubleClicked.connect(self._on_file_double_clicked)
        dir_layout.addWidget(self.file_list)

        left_layout.addWidget(dir_group)

        # 系统状态面板 - 进度条
        sys_group = QtWidgets.QGroupBox("系统状态")
        sys_layout = QtWidgets.QVBoxLayout(sys_group)
        sys_layout.setSpacing(8)

        bar_style = f"""
            QProgressBar {{
                background-color: {DRACULA['bg_darker']};
                border: 1px solid {DRACULA['selection']};
                border-radius: 4px;
                height: 18px;
                text-align: center;
                color: {DRACULA['fg']};
                font-size: 9pt;
            }}
            QProgressBar::chunk {{
                border-radius: 3px;
            }}
        """

        # CPU
        sys_layout.addWidget(QtWidgets.QLabel("CPU 占用"))
        self.cpu_bar = QtWidgets.QProgressBar()
        self.cpu_bar.setRange(0, 100)
        self.cpu_bar.setValue(0)
        self.cpu_bar.setFormat("%v%")
        self.cpu_bar.setStyleSheet(bar_style + f"""
            QProgressBar::chunk {{ background-color: {DRACULA['cyan']}; border-radius: 3px; }}
        """)
        sys_layout.addWidget(self.cpu_bar)

        # Memory
        sys_layout.addWidget(QtWidgets.QLabel("内存占用"))
        self.mem_bar = QtWidgets.QProgressBar()
        self.mem_bar.setRange(0, 100)
        self.mem_bar.setValue(0)
        self.mem_bar.setFormat("%v%")
        self.mem_bar.setStyleSheet(bar_style + f"""
            QProgressBar::chunk {{ background-color: {DRACULA['green']}; border-radius: 3px; }}
        """)
        sys_layout.addWidget(self.mem_bar)

        # Disk
        sys_layout.addWidget(QtWidgets.QLabel("磁盘占用"))
        self.disk_bar = QtWidgets.QProgressBar()
        self.disk_bar.setRange(0, 100)
        self.disk_bar.setValue(0)
        self.disk_bar.setFormat("%v%")
        self.disk_bar.setStyleSheet(bar_style + f"""
            QProgressBar::chunk {{ background-color: {DRACULA['orange']}; border-radius: 3px; }}
        """)
        sys_layout.addWidget(self.disk_bar)

        # Uptime
        self.uptime_label = QtWidgets.QLabel("运行时间: —")
        self.uptime_label.setStyleSheet(f"color:{DRACULA['comment']};font-size:9pt;")
        self.uptime_label.setWordWrap(True)
        sys_layout.addWidget(self.uptime_label)

        left_layout.addWidget(sys_group)
        left_layout.addStretch(1)

        # ---- 右侧终端 ----
        right_container = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right_container)
        right_layout.setContentsMargins(2, 2, 2, 2)

        self.terminal = TerminalWidget()
        right_layout.addWidget(self.terminal)

        splitter_lr.addWidget(right_container)
        splitter_lr.setStretchFactor(0, 1)   # 左面板 比例 1
        splitter_lr.setStretchFactor(1, 3)   # 终端 比例 3
        splitter_lr.setSizes([300, 900])

        # 下半: 日志
        self.log_edit = QtWidgets.QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumHeight(120)
        self.log_edit.setFont(QtGui.QFont("Consolas", 9))
        splitter_main.addWidget(self.log_edit)
        splitter_main.setStretchFactor(0, 5)
        splitter_main.setStretchFactor(1, 1)

    # -------------------- 样式 --------------------

    def _setup_styles(self):
        d = DRACULA
        self.setStyleSheet(f"""
        * {{
            font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
            font-size: 10pt;
        }}
        QMainWindow, QWidget {{
            background-color: {d['bg']};
            color: {d['fg']};
        }}
        QGroupBox {{
            border: 1px solid {d['selection']};
            border-radius: 6px;
            margin-top: 8px;
            padding: 10px;
            font-weight: bold;
            color: {d['fg']};
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            subcontrol-position: top left;
            padding: 0 8px;
            color: {d['purple']};
            background-color: transparent;
        }}
        QPushButton {{
            background-color: {d['comment']};
            color: {d['fg']};
            border-radius: 4px;
            padding: 5px 12px;
            border: 1px solid {d['comment']};
        }}
        QPushButton:hover {{
            background-color: #7082b6;
        }}
        QPushButton:pressed {{
            background-color: {d['selection']};
        }}
        QPushButton:disabled {{
            background-color: {d['selection']};
            color: #888888;
            border-color: {d['selection']};
        }}
        QLineEdit, QTextEdit, QPlainTextEdit {{
            background-color: {d['bg_darker']};
            border: 1px solid {d['selection']};
            border-radius: 4px;
            padding: 3px 5px;
            selection-background-color: {d['selection']};
            selection-color: {d['fg']};
        }}
        QLineEdit:focus, QTextEdit:focus {{
            border-color: {d['purple']};
        }}
        QComboBox {{
            background-color: {d['bg_darker']};
            border: 1px solid {d['selection']};
            border-radius: 4px;
            padding: 3px 5px;
            color: {d['fg']};
        }}
        QComboBox QAbstractItemView {{
            background-color: {d['bg_darker']};
            color: {d['fg']};
            selection-background-color: {d['selection']};
        }}
        QLabel {{
            background-color: transparent;
        }}
        QScrollArea {{
            border: none;
        }}
        QSplitter::handle {{
            background-color: {d['selection']};
        }}
        QSplitter::handle:horizontal {{
            width: 3px;
        }}
        QSplitter::handle:vertical {{
            height: 3px;
        }}
        QScrollBar:vertical {{
            background: {d['bg_darker']};
            width: 10px;
            border-radius: 5px;
        }}
        QScrollBar::handle:vertical {{
            background: {d['comment']};
            border-radius: 5px;
            min-height: 20px;
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            height: 0px;
        }}
        QScrollBar:horizontal {{
            background: {d['bg_darker']};
            height: 10px;
            border-radius: 5px;
        }}
        QScrollBar::handle:horizontal {{
            background: {d['comment']};
            border-radius: 5px;
            min-width: 20px;
        }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
            width: 0px;
        }}
        """)

    def _setup_dark_titlebar(self):
        """使用 Windows DWM API 将标题栏颜色设置为与背景一致"""
        if sys.platform != "win32":
            return
        try:
            hwnd = int(self.winId())
            dwmapi = ctypes.windll.dwmapi
            value = ctypes.c_int(1)
            dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
            bg_color = ctypes.c_int(0x00362a28)
            dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(bg_color), ctypes.sizeof(bg_color))
            text_color = ctypes.c_int(0x00f2f8f8)
            dwmapi.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(text_color), ctypes.sizeof(text_color))
        except Exception:
            pass

    # -------------------- 历史记录 --------------------

    def _load_history_to_combo(self):
        entries = load_history()
        for e in entries:
            label = f"{e.get('username', '')}@{e.get('host', '')}:{e.get('port', 22)}"
            self.history_combo.addItem(label, e)

    def _on_history_selected(self, index):
        if index <= 0:
            return
        data = self.history_combo.itemData(index)
        if data:
            self.ip_edit.setText(data.get("host", ""))
            self.port_edit.setText(str(data.get("port", 22)))
            self.user_edit.setText(data.get("username", ""))
            self.pass_edit.setText(decrypt_password(data.get("password_enc", "")))
            # 自动连接
            if self.btn_connect.isEnabled():
                self.on_connect()

    def _browse_key_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "选择 SSH 密钥文件",
            str(Path.home() / ".ssh"),
            "All Files (*)"
        )
        if path:
            self.key_edit.setText(path)

    # -------------------- 连接 / 断开 --------------------

    def on_connect(self):
        host = self.ip_edit.text().strip()
        port_str = self.port_edit.text().strip()
        username = self.user_edit.text().strip()
        password = self.pass_edit.text()
        key_path = self.key_edit.text().strip() or None

        if not host:
            QtWidgets.QMessageBox.warning(self, "提示", "请输入服务器 IP")
            return
        if not username:
            QtWidgets.QMessageBox.warning(self, "提示", "请输入用户名")
            return

        try:
            port = int(port_str)
        except ValueError:
            port = 22

        self.btn_connect.setEnabled(False)
        self.log(f"正在连接 {host}:{port} ...")

        # 保存重连信息
        self._reconnect_info = {"host": host, "port": port, "username": username,
                                 "password": password, "key_path": key_path}

        # 在线程中连接，避免阻塞 UI
        def do_connect():
            try:
                self._ssh.connect(host, port, username, password, key_path)
                cols, rows = self.terminal._calc_grid()
                self._channel = self._ssh.open_shell(cols, rows)
                self.terminal.set_channel(self._channel)

                self._reader = SshReaderThread(self._channel)
                self._reader.data_ready.connect(self.terminal.feed_data)
                self._reader.disconnected.connect(self._on_disconnected)
                self._reader.start()

                self._panel_thread = PanelRefreshThread(self._exec)
                self._panel_thread.dir_ready.connect(self._update_dir)
                self._panel_thread.sys_ready.connect(self._update_sys)
                self._panel_thread.start()

                # 保存到历史
                add_history_entry(host, port, username, password)

                QtCore.QMetaObject.invokeMethod(self, "_on_connected",
                                                 QtCore.Qt.QueuedConnection)
            except Exception as e:
                self.log(f"连接失败: {e}")
                QtCore.QMetaObject.invokeMethod(self, "_on_connect_failed",
                                                 QtCore.Qt.QueuedConnection)

        t = threading.Thread(target=do_connect, daemon=True)
        t.start()

    @QtCore.Slot()
    def _on_connected(self):
        self.log("连接成功！")
        self.btn_connect.setEnabled(False)
        self.btn_disconnect.setEnabled(True)
        self.status_dot.setStyleSheet("border-radius:6px;background:#50fa7b;")
        self.status_label.setText("已连接")
        self.terminal.setFocus()
        # 刷新历史下拉
        self.history_combo.clear()
        self.history_combo.addItem("— 选择历史连接 —")
        self._load_history_to_combo()

    @QtCore.Slot()
    def _on_connect_failed(self):
        self.btn_connect.setEnabled(True)

    @QtCore.Slot()
    def _on_disconnected(self):
        self.log("连接已断开")
        self._cleanup()
        # 尝试自动重连
        if self._reconnect_info:
            self._try_reconnect()

    def _try_reconnect(self):
        """断线后自动尝试重连一次"""
        info = self._reconnect_info
        if not info:
            return
        self._reconnect_info = None  # 只重连一次，避免无限循环
        self.log("正在尝试自动重连...")

        def do_reconnect():
            import time as _t
            _t.sleep(2)  # 等待 2 秒后重连
            try:
                self._ssh.connect(info["host"], info["port"], info["username"],
                                  info.get("password"), info.get("key_path"))
                cols, rows = self.terminal._calc_grid()
                self._channel = self._ssh.open_shell(cols, rows)
                self.terminal.set_channel(self._channel)

                self._reader = SshReaderThread(self._channel)
                self._reader.data_ready.connect(self.terminal.feed_data)
                self._reader.disconnected.connect(self._on_disconnected)
                self._reader.start()

                self._panel_thread = PanelRefreshThread(self._exec)
                self._panel_thread.dir_ready.connect(self._update_dir)
                self._panel_thread.sys_ready.connect(self._update_sys)
                self._panel_thread.start()

                # 恢复重连信息以支持再次断线重连
                self._reconnect_info = info
                QtCore.QMetaObject.invokeMethod(self, "_on_connected",
                                                 QtCore.Qt.QueuedConnection)
            except Exception as e:
                self.log(f"自动重连失败: {e}")
                QtCore.QMetaObject.invokeMethod(self, "_on_connect_failed",
                                                 QtCore.Qt.QueuedConnection)

        threading.Thread(target=do_reconnect, daemon=True).start()

    def on_disconnect(self):
        self._reconnect_info = None  # 手动断开不重连
        self.log("正在断开连接...")
        self._cleanup()

    def _cleanup(self):
        if self._reader:
            self._reader.stop()
            self._reader.wait(2000)
            self._reader = None

        if self._panel_thread:
            self._panel_thread.stop()
            self._panel_thread.wait(2000)
            self._panel_thread = None

        self.terminal.set_channel(None)  # 停止 writer 线程

        if self._channel:
            try:
                self._channel.close()
            except Exception:
                pass
            self._channel = None

        self._ssh.close()

        self.btn_connect.setEnabled(True)
        self.btn_disconnect.setEnabled(False)
        self.status_dot.setStyleSheet("border-radius:6px;background:#ff5555;")
        self.status_label.setText("未连接")
        self.dir_path_label.setText("—")
        self.file_list.clear()
        self.cpu_bar.setValue(0)
        self.mem_bar.setValue(0)
        self.disk_bar.setValue(0)
        self.uptime_label.setText("运行时间: —")

    # -------------------- 面板更新 --------------------

    @QtCore.Slot(str, list)
    def _update_dir(self, cwd: str, file_names: list):
        new_names = []
        if cwd and cwd != "/":
            new_names.append("../")
        for name in file_names:
            name = name.strip()
            if name:
                new_names.append(name)

        if (hasattr(self, '_last_dir_cwd') and self._last_dir_cwd == cwd
                and hasattr(self, '_last_dir_names') and self._last_dir_names == new_names):
            return
        self._last_dir_cwd = cwd
        self._last_dir_names = list(new_names)

        scrollbar = self.file_list.verticalScrollBar()
        scroll_pos = scrollbar.value() if scrollbar else 0

        self.dir_path_label.setText(cwd or "—")
        self.file_list.clear()

        for name in new_names:
            item = QtWidgets.QListWidgetItem(name)
            if name == "../":
                item.setForeground(QtGui.QColor(DRACULA["orange"]))
                item.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_ArrowUp))
                item.setToolTip("双击返回上级目录")
            elif name.endswith("/"):
                item.setForeground(QtGui.QColor(DRACULA["cyan"]))
                item.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_DirIcon))
                item.setToolTip("双击进入目录")
            elif name.endswith("@"):
                item.setForeground(QtGui.QColor(DRACULA["pink"]))
            elif name.endswith("*"):
                item.setForeground(QtGui.QColor(DRACULA["green"]))
            else:
                item.setForeground(QtGui.QColor(DRACULA["fg"]))
                item.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_FileIcon))
            self.file_list.addItem(item)

        if scrollbar:
            scrollbar.setValue(scroll_pos)

    @QtCore.Slot(float, float, float, str)
    def _update_sys(self, cpu_pct: float, mem_pct: float, disk_pct: float, uptime: str):
        self.cpu_bar.setValue(int(cpu_pct))
        self.mem_bar.setValue(int(mem_pct))
        self.disk_bar.setValue(int(disk_pct))
        self.uptime_label.setText(f"运行时间: {uptime}" if uptime else "运行时间: —")

    # -------------------- 文件列表交互 --------------------

    def _navigate_to_dir(self, abs_path: str):
        """导航到指定绝对路径：终端 cd + 面板立即刷新"""
        if not self._channel or not self.terminal._writer:
            return

        cmd = f"cd {self._shell_quote(abs_path)}\r"
        self.terminal._send_data(cmd.encode("utf-8"))

        if self._panel_thread:
            self._panel_thread.set_cwd(abs_path)

        self._last_dir_cwd = None
        self._last_dir_names = None

        self.log(f"进入目录: {abs_path}")

        if self._exec.connected and self._panel_thread:
            refresh_cmd = self._panel_thread._build_cmd()
            def do_refresh():
                raw = self._exec.exec_cmd(refresh_cmd)
                sections = {}
                current_key = None
                for line in raw.split("\n"):
                    stripped = line.strip()
                    if stripped.startswith("===") and stripped.endswith("==="):
                        current_key = stripped.strip("=")
                        sections[current_key] = ""
                    elif current_key:
                        sections[current_key] = sections[current_key] + line + "\n"
                cwd = sections.get("PWD", "").strip()
                ls_raw = sections.get("LS", "").strip()
                file_names = [f for f in ls_raw.split("\n") if f.strip()]
                if cwd:
                    self._panel_thread.set_cwd(cwd)
                self._nav_result = (cwd, file_names)
                QtCore.QMetaObject.invokeMethod(self, "_apply_nav_refresh",
                                                 QtCore.Qt.QueuedConnection)
            threading.Thread(target=do_refresh, daemon=True).start()

    @QtCore.Slot()
    def _apply_nav_refresh(self):
        if hasattr(self, '_nav_result'):
            cwd, file_names = self._nav_result
            self._last_dir_cwd = None
            self._last_dir_names = None
            self._update_dir(cwd, file_names)
            del self._nav_result

    @QtCore.Slot(QtWidgets.QListWidgetItem)
    def _on_file_double_clicked(self, item: QtWidgets.QListWidgetItem):
        if not self._channel:
            return
        name = item.text().strip()
        if not name or not name.endswith("/"):
            return

        panel_cwd = self.dir_path_label.text().strip()
        if panel_cwd == "—" or not panel_cwd:
            return

        dir_name = name.rstrip("/")
        if dir_name == "..":
            if panel_cwd == "/":
                return
            abs_path = "/".join(panel_cwd.rstrip("/").split("/")[:-1]) or "/"
        else:
            abs_path = panel_cwd.rstrip("/") + "/" + dir_name

        self._navigate_to_dir(abs_path)

    @QtCore.Slot()
    def _on_back_clicked(self):
        if not self._channel:
            return
        panel_cwd = self.dir_path_label.text().strip()
        if panel_cwd == "—" or not panel_cwd or panel_cwd == "/":
            return
        abs_path = "/".join(panel_cwd.rstrip("/").split("/")[:-1]) or "/"
        self._navigate_to_dir(abs_path)

    @QtCore.Slot(QtCore.QPoint)
    def _on_file_context_menu(self, pos: QtCore.QPoint):
        item = self.file_list.itemAt(pos)
        if not item:
            return
        if not self._channel:
            return

        name = item.text().strip()
        if not name:
            return

        menu = QtWidgets.QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background-color: {DRACULA['bg_lighter']};
                color: {DRACULA['fg']};
                border: 1px solid {DRACULA['selection']};
                border-radius: 4px;
                padding: 4px;
            }}
            QMenu::item {{
                padding: 6px 20px;
                border-radius: 3px;
            }}
            QMenu::item:selected {{
                background-color: {DRACULA['selection']};
            }}
        """)

        is_dir = name.endswith("/")
        display_name = name.rstrip("/*@")

        if is_dir:
            action_enter = menu.addAction(f"进入目录  {display_name}")
            action_enter.triggered.connect(lambda: self._on_file_double_clicked(item))
            menu.addSeparator()

        action_delete = menu.addAction(f"删除  {display_name}")
        action_delete.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_TrashIcon))

        chosen = menu.exec(self.file_list.mapToGlobal(pos))
        if chosen == action_delete:
            self._confirm_delete(name)

    def _confirm_delete(self, name: str):
        is_dir = name.endswith("/")
        display_name = name.rstrip("/*@")
        type_str = "文件夹" if is_dir else "文件"

        if display_name == "..":
            return

        panel_cwd = self.dir_path_label.text().strip()
        if panel_cwd == "—" or not panel_cwd:
            return
        abs_path = panel_cwd.rstrip("/") + "/" + display_name

        reply = QtWidgets.QMessageBox.question(
            self, "确认删除",
            f"确定要删除{type_str} \"{abs_path}\" 吗？\n\n此操作不可恢复！",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return

        quoted = self._shell_quote(abs_path)
        if is_dir:
            cmd = f"rm -rf {quoted}"
        else:
            cmd = f"rm -f {quoted}"

        self.log(f"正在删除: {cmd}")

        def do_delete():
            result = self._exec.exec_cmd(cmd + " 2>&1")
            if result.strip():
                self.log(f"删除结果: {result.strip()}")
            else:
                self.log(f"已删除: {display_name}")
            QtCore.QMetaObject.invokeMethod(self, "_refresh_after_delete",
                                             QtCore.Qt.QueuedConnection)

        t = threading.Thread(target=do_delete, daemon=True)
        t.start()

    @QtCore.Slot()
    def _refresh_after_delete(self):
        if self.terminal._writer:
            self.terminal._send_data(b"ls\r")
        if self._exec.connected and self._panel_thread:
            refresh_cmd = self._panel_thread._build_cmd()
            def do_refresh():
                raw = self._exec.exec_cmd(refresh_cmd)
                sections = {}
                current_key = None
                for line in raw.split("\n"):
                    stripped = line.strip()
                    if stripped.startswith("===") and stripped.endswith("==="):
                        current_key = stripped.strip("=")
                        sections[current_key] = ""
                    elif current_key:
                        sections[current_key] = sections[current_key] + line + "\n"
                cwd = sections.get("PWD", "").strip()
                ls_raw = sections.get("LS", "").strip()
                file_names = [f for f in ls_raw.split("\n") if f.strip()]
                self.dir_ready_from_delete = (cwd, file_names)
                QtCore.QMetaObject.invokeMethod(self, "_apply_dir_refresh",
                                                 QtCore.Qt.QueuedConnection)
            threading.Thread(target=do_refresh, daemon=True).start()

    @QtCore.Slot()
    def _apply_dir_refresh(self):
        if hasattr(self, 'dir_ready_from_delete'):
            cwd, file_names = self.dir_ready_from_delete
            self._last_dir_cwd = None
            self._last_dir_names = None
            self._update_dir(cwd, file_names)
            del self.dir_ready_from_delete

    @staticmethod
    def _shell_quote(s: str) -> str:
        """安全转义 shell 参数，防止命令注入"""
        return "'" + s.replace("'", "'\\''") + "'"

    # -------------------- 窗口关闭 --------------------

    def closeEvent(self, event):
        self._reconnect_info = None
        self._cleanup()
        super().closeEvent(event)
