"""Main application window — multi-tab terminal, SFTP, editor, tunnels, session groups."""

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
from .session import Session
from .sftp_manager import SftpThread
from .editor_widget import RemoteEditorWidget
from .tunnel import TunnelManager, TunnelEntry
from .crypto import encrypt_password, decrypt_password


# ====================== 连接历史 + 会话分组 ======================

_HISTORY_PATH = Path.home() / ".sshcat_history.json"
_MAX_HISTORY = 50


def load_history() -> list:
    if not _HISTORY_PATH.exists():
        return []
    try:
        data = json.loads(_HISTORY_PATH.read_text("utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_history(entries: list):
    try:
        _HISTORY_PATH.write_text(json.dumps(entries[:_MAX_HISTORY], ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass


def add_history_entry(host, port, username, password="", group="默认"):
    entries = load_history()
    entry = {"host": host, "port": int(port), "username": username,
             "password_enc": encrypt_password(password), "group": group}
    entries = [e for e in entries if not (e.get("host") == host and e.get("port") == int(port) and e.get("username") == username)]
    entries.insert(0, entry)
    save_history(entries)


# ====================== 菜单样式 ======================

_MENU_STYLE = f"""
    QMenu {{
        background-color: {DRACULA['bg_lighter']};
        color: {DRACULA['fg']};
        border: 1px solid {DRACULA['selection']};
        border-radius: 4px; padding: 4px;
    }}
    QMenu::item {{ padding: 6px 20px; border-radius: 3px; }}
    QMenu::item:selected {{ background-color: {DRACULA['selection']}; }}
"""


# ====================== 主窗口 ======================

class MainWindow(QtWidgets.QMainWindow):
    log_signal = QtCore.Signal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("SSHcat")
        self.resize(1200, 700)
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "icon.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QtGui.QIcon(icon_path))

        self._sessions: dict[int, Session] = {}
        self._tunnel_mgr = TunnelManager(self)

        self._build_ui()
        self._setup_styles()
        self._setup_dark_titlebar()
        self._load_session_tree()
        self.log_signal.connect(self._append_log)

    def _current_session(self) -> Optional[Session]:
        idx = self.tab_widget.currentIndex()
        return self._sessions.get(idx)

    def log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.log_signal.emit(f"[{ts}] {msg}")

    def _append_log(self, msg: str):
        self.log_edit.append(msg)
        self.log_edit.verticalScrollBar().setValue(self.log_edit.verticalScrollBar().maximum())

    # ==================== UI ====================

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        sp_main = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        root.addWidget(sp_main)

        top = QtWidgets.QWidget()
        top_lay = QtWidgets.QHBoxLayout(top)
        top_lay.setContentsMargins(0, 0, 0, 0)
        self.splitter_lr = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        top_lay.addWidget(self.splitter_lr)
        sp_main.addWidget(top)

        # 左面板
        left_scroll = QtWidgets.QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        lc = QtWidgets.QWidget()
        left_scroll.setWidget(lc)
        self.splitter_lr.addWidget(left_scroll)
        ll = QtWidgets.QVBoxLayout(lc)
        ll.setContentsMargins(10, 10, 10, 10)

        self._build_conn(ll)
        self._build_tree(ll)
        self._build_dir(ll)
        self._build_sftp(ll)
        self._build_sys(ll)
        self._build_tunnel(ll)
        ll.addStretch(1)

        # 右面板：多标签终端
        right = QtWidgets.QWidget()
        rl = QtWidgets.QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        d = DRACULA
        self.tab_widget = QtWidgets.QTabWidget()
        self.tab_widget.setTabsClosable(True)
        self.tab_widget.setMovable(True)
        self.tab_widget.tabCloseRequested.connect(self._on_tab_close)
        self.tab_widget.currentChanged.connect(self._on_tab_changed)
        self.tab_widget.setStyleSheet(f"""
            QTabWidget::pane {{ border: none; background-color: {d['bg']}; }}
            QTabBar::tab {{
                background-color: {d['bg_darker']}; color: {d['comment']};
                padding: 6px 16px; border: 1px solid {d['selection']};
                border-bottom: none; border-top-left-radius: 6px; border-top-right-radius: 6px;
                margin-right: 2px;
            }}
            QTabBar::tab:selected {{
                background-color: {d['bg']}; color: {d['fg']};
                border-bottom: 2px solid {d['purple']};
            }}
            QTabBar::tab:hover {{ background-color: {d['bg_lighter']}; }}
        """)
        rl.addWidget(self.tab_widget)
        self.splitter_lr.addWidget(right)
        self.splitter_lr.setStretchFactor(0, 1)
        self.splitter_lr.setStretchFactor(1, 3)
        self.splitter_lr.setSizes([300, 900])

        self.log_edit = QtWidgets.QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumHeight(120)
        self.log_edit.setFont(QtGui.QFont("Consolas", 9))
        sp_main.addWidget(self.log_edit)
        sp_main.setStretchFactor(0, 5)
        sp_main.setStretchFactor(1, 1)

    def _build_conn(self, pl):
        g = QtWidgets.QGroupBox("连接信息")
        gl = QtWidgets.QGridLayout(g)
        r = 0
        gl.addWidget(QtWidgets.QLabel("服务器 IP"), r, 0)
        self.ip_edit = QtWidgets.QLineEdit(); self.ip_edit.setPlaceholderText("例: 192.168.1.100")
        gl.addWidget(self.ip_edit, r, 1)
        r += 1; gl.addWidget(QtWidgets.QLabel("端口"), r, 0)
        self.port_edit = QtWidgets.QLineEdit("22"); gl.addWidget(self.port_edit, r, 1)
        r += 1; gl.addWidget(QtWidgets.QLabel("用户名"), r, 0)
        self.user_edit = QtWidgets.QLineEdit("root"); gl.addWidget(self.user_edit, r, 1)
        r += 1; gl.addWidget(QtWidgets.QLabel("密码"), r, 0)
        self.pass_edit = QtWidgets.QLineEdit(); self.pass_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        gl.addWidget(self.pass_edit, r, 1)
        r += 1; gl.addWidget(QtWidgets.QLabel("密钥文件"), r, 0)
        kr = QtWidgets.QHBoxLayout()
        self.key_edit = QtWidgets.QLineEdit(); self.key_edit.setPlaceholderText("可选")
        bk = QtWidgets.QPushButton("浏览"); bk.setFixedWidth(50); bk.clicked.connect(self._browse_key)
        kr.addWidget(self.key_edit); kr.addWidget(bk)
        gl.addLayout(kr, r, 1)
        r += 1; br = QtWidgets.QHBoxLayout()
        self.btn_connect = QtWidgets.QPushButton("连接 (新标签)")
        self.btn_disconnect = QtWidgets.QPushButton("断开当前"); self.btn_disconnect.setEnabled(False)
        self.btn_connect.clicked.connect(self.on_connect)
        self.btn_disconnect.clicked.connect(self.on_disconnect)
        br.addWidget(self.btn_connect); br.addWidget(self.btn_disconnect)
        gl.addLayout(br, r, 0, 1, 2)
        r += 1; sr = QtWidgets.QHBoxLayout()
        self.status_dot = QtWidgets.QLabel(); self.status_dot.setFixedSize(12, 12)
        self.status_dot.setStyleSheet("border-radius:6px;background:#ff5555;")
        self.status_label = QtWidgets.QLabel("未连接")
        sr.addWidget(self.status_dot); sr.addWidget(self.status_label); sr.addStretch(1)
        gl.addLayout(sr, r, 0, 1, 2)
        pl.addWidget(g)

    def _build_tree(self, pl):
        g = QtWidgets.QGroupBox("会话管理")
        gl = QtWidgets.QVBoxLayout(g)
        self.session_tree = QtWidgets.QTreeWidget()
        self.session_tree.setHeaderLabels(["连接"])
        self.session_tree.setMaximumHeight(160)
        self.session_tree.setFont(QtGui.QFont("Consolas", 9))
        self.session_tree.setStyleSheet(f"""
            QTreeWidget {{ background-color: {DRACULA['bg_darker']}; border: 1px solid {DRACULA['selection']}; border-radius: 4px; }}
            QTreeWidget::item {{ padding: 2px 4px; }}
            QTreeWidget::item:hover {{ background-color: {DRACULA['selection']}; }}
            QTreeWidget::item:selected {{ background-color: {DRACULA['purple']}; color: {DRACULA['fg']}; }}
        """)
        self.session_tree.itemDoubleClicked.connect(self._on_tree_dblclick)
        gl.addWidget(self.session_tree)
        br = QtWidgets.QHBoxLayout()
        ba = QtWidgets.QPushButton("新建分组"); ba.clicked.connect(self._add_group)
        bd = QtWidgets.QPushButton("删除"); bd.clicked.connect(self._del_entry)
        br.addWidget(ba); br.addWidget(bd)
        gl.addLayout(br)
        pl.addWidget(g)

    def _build_dir(self, pl):
        g = QtWidgets.QGroupBox("当前目录")
        gl = QtWidgets.QVBoxLayout(g)
        dh = QtWidgets.QHBoxLayout()
        self.btn_back = QtWidgets.QPushButton()
        self.btn_back.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_ArrowBack))
        self.btn_back.setFixedSize(28, 28); self.btn_back.clicked.connect(self._on_back)
        dh.addWidget(self.btn_back)
        self.dir_path_label = QtWidgets.QLabel("—")
        self.dir_path_label.setStyleSheet(f"color:{DRACULA['yellow']};font-weight:bold;")
        self.dir_path_label.setWordWrap(True)
        dh.addWidget(self.dir_path_label, 1)
        gl.addLayout(dh)
        self.file_list = QtWidgets.QListWidget()
        self.file_list.setMaximumHeight(180)
        self.file_list.setFont(QtGui.QFont("Consolas", 9))
        self.file_list.setStyleSheet(f"""
            QListWidget {{ background-color: {DRACULA['bg_darker']}; border: 1px solid {DRACULA['selection']}; border-radius: 4px; }}
            QListWidget::item {{ padding: 2px 4px; }}
            QListWidget::item:hover {{ background-color: {DRACULA['selection']}; }}
        """)
        self.file_list.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.file_list.customContextMenuRequested.connect(self._on_file_ctx)
        self.file_list.itemDoubleClicked.connect(self._on_file_dblclick)
        gl.addWidget(self.file_list)
        pl.addWidget(g)

    def _build_sftp(self, pl):
        g = QtWidgets.QGroupBox("SFTP 文件传输")
        gl = QtWidgets.QVBoxLayout(g)
        br = QtWidgets.QHBoxLayout()
        self.btn_upload = QtWidgets.QPushButton("上传文件"); self.btn_upload.clicked.connect(self._sftp_upload)
        self.btn_download = QtWidgets.QPushButton("下载文件"); self.btn_download.clicked.connect(self._sftp_download)
        br.addWidget(self.btn_upload); br.addWidget(self.btn_download)
        gl.addLayout(br)
        self.sftp_progress = QtWidgets.QProgressBar(); self.sftp_progress.setRange(0, 100)
        self.sftp_progress.setValue(0); self.sftp_progress.setVisible(False)
        gl.addWidget(self.sftp_progress)
        self.sftp_status = QtWidgets.QLabel("")
        self.sftp_status.setStyleSheet(f"color:{DRACULA['comment']};font-size:9pt;")
        gl.addWidget(self.sftp_status)
        pl.addWidget(g)

    def _build_sys(self, pl):
        g = QtWidgets.QGroupBox("系统状态")
        gl = QtWidgets.QVBoxLayout(g); gl.setSpacing(8)
        bs = f"""QProgressBar {{ background-color: {DRACULA['bg_darker']}; border: 1px solid {DRACULA['selection']};
            border-radius: 4px; height: 18px; text-align: center; color: {DRACULA['fg']}; font-size: 9pt; }}
            QProgressBar::chunk {{ border-radius: 3px; }}"""
        gl.addWidget(QtWidgets.QLabel("CPU 占用"))
        self.cpu_bar = QtWidgets.QProgressBar(); self.cpu_bar.setRange(0,100); self.cpu_bar.setFormat("%v%")
        self.cpu_bar.setStyleSheet(bs + f" QProgressBar::chunk {{ background-color: {DRACULA['cyan']}; border-radius: 3px; }}")
        gl.addWidget(self.cpu_bar)
        gl.addWidget(QtWidgets.QLabel("内存占用"))
        self.mem_bar = QtWidgets.QProgressBar(); self.mem_bar.setRange(0,100); self.mem_bar.setFormat("%v%")
        self.mem_bar.setStyleSheet(bs + f" QProgressBar::chunk {{ background-color: {DRACULA['green']}; border-radius: 3px; }}")
        gl.addWidget(self.mem_bar)
        gl.addWidget(QtWidgets.QLabel("磁盘占用"))
        self.disk_bar = QtWidgets.QProgressBar(); self.disk_bar.setRange(0,100); self.disk_bar.setFormat("%v%")
        self.disk_bar.setStyleSheet(bs + f" QProgressBar::chunk {{ background-color: {DRACULA['orange']}; border-radius: 3px; }}")
        gl.addWidget(self.disk_bar)
        self.uptime_label = QtWidgets.QLabel("运行时间: —")
        self.uptime_label.setStyleSheet(f"color:{DRACULA['comment']};font-size:9pt;"); self.uptime_label.setWordWrap(True)
        gl.addWidget(self.uptime_label)
        pl.addWidget(g)

    def _build_tunnel(self, pl):
        g = QtWidgets.QGroupBox("端口转发")
        gl = QtWidgets.QVBoxLayout(g)
        ar = QtWidgets.QHBoxLayout()
        self.tun_lport = QtWidgets.QLineEdit(); self.tun_lport.setPlaceholderText("本地端口"); self.tun_lport.setFixedWidth(70)
        ar.addWidget(self.tun_lport); ar.addWidget(QtWidgets.QLabel("→"))
        self.tun_rhost = QtWidgets.QLineEdit("127.0.0.1"); self.tun_rhost.setPlaceholderText("远程地址")
        ar.addWidget(self.tun_rhost); ar.addWidget(QtWidgets.QLabel(":"))
        self.tun_rport = QtWidgets.QLineEdit(); self.tun_rport.setPlaceholderText("远程端口"); self.tun_rport.setFixedWidth(70)
        ar.addWidget(self.tun_rport)
        gl.addLayout(ar)
        br = QtWidgets.QHBoxLayout()
        bs_ = QtWidgets.QPushButton("启动转发"); bs_.clicked.connect(self._start_tunnel)
        be = QtWidgets.QPushButton("停止全部"); be.clicked.connect(self._stop_tunnels)
        br.addWidget(bs_); br.addWidget(be)
        gl.addLayout(br)
        self.tunnel_list = QtWidgets.QListWidget(); self.tunnel_list.setMaximumHeight(80)
        self.tunnel_list.setFont(QtGui.QFont("Consolas", 9))
        self.tunnel_list.setStyleSheet(f"QListWidget {{ background-color: {DRACULA['bg_darker']}; border: 1px solid {DRACULA['selection']}; border-radius: 4px; }}")
        gl.addWidget(self.tunnel_list)
        self._tunnel_mgr.tunnel_started.connect(lambda l: (self.tunnel_list.addItem(l), self.log(f"隧道已启动: {l}")))
        self._tunnel_mgr.tunnel_stopped.connect(lambda l: self.log(f"隧道已停止: {l}"))
        self._tunnel_mgr.tunnel_error.connect(lambda l, e: self.log(f"隧道错误 {l}: {e}"))
        pl.addWidget(g)

    # ==================== 样式 ====================

    def _setup_styles(self):
        d = DRACULA
        self.setStyleSheet(f"""
        * {{ font-family: "Segoe UI","Microsoft YaHei","Helvetica Neue",sans-serif; font-size: 10pt; }}
        QMainWindow, QWidget {{ background-color: {d['bg']}; color: {d['fg']}; }}
        QGroupBox {{ border: 1px solid {d['selection']}; border-radius: 6px; margin-top: 8px; padding: 10px; font-weight: bold; color: {d['fg']}; }}
        QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; padding: 0 8px; color: {d['purple']}; background-color: transparent; }}
        QPushButton {{ background-color: {d['comment']}; color: {d['fg']}; border-radius: 4px; padding: 5px 12px; border: 1px solid {d['comment']}; }}
        QPushButton:hover {{ background-color: #7082b6; }}
        QPushButton:pressed {{ background-color: {d['selection']}; }}
        QPushButton:disabled {{ background-color: {d['selection']}; color: #888; border-color: {d['selection']}; }}
        QLineEdit, QTextEdit, QPlainTextEdit {{ background-color: {d['bg_darker']}; border: 1px solid {d['selection']}; border-radius: 4px; padding: 3px 5px; selection-background-color: {d['selection']}; selection-color: {d['fg']}; }}
        QLineEdit:focus, QTextEdit:focus {{ border-color: {d['purple']}; }}
        QLabel {{ background-color: transparent; }}
        QScrollArea {{ border: none; }}
        QSplitter::handle {{ background-color: {d['selection']}; }}
        QSplitter::handle:horizontal {{ width: 3px; }}
        QSplitter::handle:vertical {{ height: 3px; }}
        QScrollBar:vertical {{ background: {d['bg_darker']}; width: 10px; border-radius: 5px; }}
        QScrollBar::handle:vertical {{ background: {d['comment']}; border-radius: 5px; min-height: 20px; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
        QScrollBar:horizontal {{ background: {d['bg_darker']}; height: 10px; border-radius: 5px; }}
        QScrollBar::handle:horizontal {{ background: {d['comment']}; border-radius: 5px; min-width: 20px; }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0px; }}
        """)

    def _setup_dark_titlebar(self):
        if sys.platform != "win32":
            return
        try:
            hwnd = int(self.winId())
            dwm = ctypes.windll.dwmapi
            v = ctypes.c_int(1)
            dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(v), ctypes.sizeof(v))
            dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(ctypes.c_int(0x00362a28)), 4)
            dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(ctypes.c_int(0x00f2f8f8)), 4)
        except Exception:
            pass

    # ==================== 会话分组树 ====================

    def _load_session_tree(self):
        self.session_tree.clear()
        groups: dict[str, QtWidgets.QTreeWidgetItem] = {}
        for e in load_history():
            gn = e.get("group", "默认")
            if gn not in groups:
                gi = QtWidgets.QTreeWidgetItem([gn])
                gi.setForeground(0, QtGui.QColor(DRACULA["purple"]))
                f = gi.font(0); f.setBold(True); gi.setFont(0, f)
                self.session_tree.addTopLevelItem(gi)
                groups[gn] = gi
            label = f"{e.get('username','')}@{e.get('host','')}:{e.get('port',22)}"
            ch = QtWidgets.QTreeWidgetItem([label])
            ch.setData(0, QtCore.Qt.UserRole, e)
            ch.setForeground(0, QtGui.QColor(DRACULA["fg"]))
            groups[gn].addChild(ch)
        self.session_tree.expandAll()

    def _on_tree_dblclick(self, item, col):
        data = item.data(0, QtCore.Qt.UserRole)
        if not data:
            return
        self.ip_edit.setText(data.get("host", ""))
        self.port_edit.setText(str(data.get("port", 22)))
        self.user_edit.setText(data.get("username", ""))
        self.pass_edit.setText(decrypt_password(data.get("password_enc", "")))
        self.on_connect()

    def _add_group(self):
        name, ok = QtWidgets.QInputDialog.getText(self, "新建分组", "分组名称:")
        if ok and name.strip():
            gi = QtWidgets.QTreeWidgetItem([name.strip()])
            gi.setForeground(0, QtGui.QColor(DRACULA["purple"]))
            f = gi.font(0); f.setBold(True); gi.setFont(0, f)
            self.session_tree.addTopLevelItem(gi)

    def _del_entry(self):
        item = self.session_tree.currentItem()
        if not item:
            return
        data = item.data(0, QtCore.Qt.UserRole)
        if data:
            entries = load_history()
            entries = [e for e in entries if not (e.get("host") == data.get("host") and e.get("port") == data.get("port") and e.get("username") == data.get("username"))]
            save_history(entries)
        p = item.parent()
        if p:
            p.removeChild(item)
        else:
            idx = self.session_tree.indexOfTopLevelItem(item)
            if idx >= 0:
                self.session_tree.takeTopLevelItem(idx)

    def _browse_key(self):
        p, _ = QtWidgets.QFileDialog.getOpenFileName(self, "选择 SSH 密钥文件", str(Path.home() / ".ssh"), "All Files (*)")
        if p:
            self.key_edit.setText(p)

    # ==================== 多标签连接 ====================

    def on_connect(self):
        host = self.ip_edit.text().strip()
        port_s = self.port_edit.text().strip()
        user = self.user_edit.text().strip()
        pw = self.pass_edit.text()
        kp = self.key_edit.text().strip() or None
        if not host:
            QtWidgets.QMessageBox.warning(self, "提示", "请输入服务器 IP"); return
        if not user:
            QtWidgets.QMessageBox.warning(self, "提示", "请输入用户名"); return
        try:
            port = int(port_s)
        except ValueError:
            port = 22

        self.btn_connect.setEnabled(False)
        self.log(f"正在连接 {host}:{port} ...")

        sess = Session(self)
        tab_idx = self.tab_widget.addTab(sess.terminal, f"{user}@{host}")
        self._sessions[tab_idx] = sess
        self.tab_widget.setCurrentIndex(tab_idx)

        sess.connected.connect(lambda s=sess, h=host, p=port, u=user, pw_=pw: self._sess_ok(s, h, p, u, pw_))
        sess.disconnected.connect(lambda s=sess: self._sess_disc(s))
        sess.connect_failed.connect(lambda m: self._sess_fail(m))
        sess.log_message.connect(self.log)
        sess.connect(host, port, user, pw, kp)

    def _sess_ok(self, sess, host, port, user, pw):
        self.btn_connect.setEnabled(True)
        self.btn_disconnect.setEnabled(True)
        self.status_dot.setStyleSheet("border-radius:6px;background:#50fa7b;")
        self.status_label.setText("已连接")
        sess.terminal.setFocus()
        if sess.panel_thread:
            sess.panel_thread.dir_ready.connect(self._update_dir)
            sess.panel_thread.sys_ready.connect(self._update_sys)
        add_history_entry(host, port, user, pw)
        self._load_session_tree()

    def _sess_fail(self, msg):
        self.log(f"连接失败: {msg}")
        self.btn_connect.setEnabled(True)

    def _sess_disc(self, sess):
        for idx, s in list(self._sessions.items()):
            if s is sess:
                self.tab_widget.setTabText(idx, self.tab_widget.tabText(idx) + " (断开)")
                break
        if self._current_session() is sess:
            self.status_dot.setStyleSheet("border-radius:6px;background:#ff5555;")
            self.status_label.setText("已断开")
            self._reset_panels()

    def on_disconnect(self):
        sess = self._current_session()
        if sess:
            sess.disconnect()
            self.status_dot.setStyleSheet("border-radius:6px;background:#ff5555;")
            self.status_label.setText("未连接")
            self.btn_disconnect.setEnabled(False)
            self._reset_panels()

    def _on_tab_close(self, index):
        sess = self._sessions.pop(index, None)
        if sess:
            sess.disconnect()
        self.tab_widget.removeTab(index)
        new = {}
        for i in range(self.tab_widget.count()):
            for _, s in self._sessions.items():
                if self.tab_widget.widget(i) is s.terminal:
                    new[i] = s; break
        self._sessions = new
        if not self._sessions:
            self.status_dot.setStyleSheet("border-radius:6px;background:#ff5555;")
            self.status_label.setText("未连接")
            self.btn_disconnect.setEnabled(False)
            self._reset_panels()

    def _on_tab_changed(self, index):
        sess = self._sessions.get(index)
        if sess and sess.is_connected:
            self.status_dot.setStyleSheet("border-radius:6px;background:#50fa7b;")
            self.status_label.setText("已连接")
            self.btn_disconnect.setEnabled(True)
        else:
            self.status_dot.setStyleSheet("border-radius:6px;background:#ff5555;")
            self.status_label.setText("未连接" if not sess else "已断开")
            self.btn_disconnect.setEnabled(False)

    def _reset_panels(self):
        self.dir_path_label.setText("—"); self.file_list.clear()
        self.cpu_bar.setValue(0); self.mem_bar.setValue(0); self.disk_bar.setValue(0)
        self.uptime_label.setText("运行时间: —")

    # ==================== 面板更新 ====================

    @QtCore.Slot(str, list)
    def _update_dir(self, cwd, fns):
        nn = []
        if cwd and cwd != "/":
            nn.append("../")
        for n in fns:
            n = n.strip()
            if n:
                nn.append(n)
        if getattr(self, '_last_cwd', None) == cwd and getattr(self, '_last_fns', None) == nn:
            return
        self._last_cwd = cwd; self._last_fns = list(nn)
        self.dir_path_label.setText(cwd or "—")
        self.file_list.clear()
        for n in nn:
            it = QtWidgets.QListWidgetItem(n)
            if n == "../":
                it.setForeground(QtGui.QColor(DRACULA["orange"]))
                it.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_ArrowUp))
            elif n.endswith("/"):
                it.setForeground(QtGui.QColor(DRACULA["cyan"]))
                it.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_DirIcon))
            elif n.endswith("@"):
                it.setForeground(QtGui.QColor(DRACULA["pink"]))
            elif n.endswith("*"):
                it.setForeground(QtGui.QColor(DRACULA["green"]))
            else:
                it.setForeground(QtGui.QColor(DRACULA["fg"]))
                it.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_FileIcon))
            self.file_list.addItem(it)

    @QtCore.Slot(float, float, float, str)
    def _update_sys(self, cpu, mem, disk, up):
        self.cpu_bar.setValue(int(cpu)); self.mem_bar.setValue(int(mem)); self.disk_bar.setValue(int(disk))
        self.uptime_label.setText(f"运行时间: {up}" if up else "运行时间: —")

    # ==================== 文件操作 ====================

    def _nav(self, path):
        sess = self._current_session()
        if not sess or not sess.is_connected or not sess.terminal._writer:
            return
        sess.terminal._send_data(f"cd {self._sq(path)}\r".encode("utf-8"))
        if sess.panel_thread:
            sess.panel_thread.set_cwd(path)
        self._last_cwd = None; self._last_fns = None
        self.log(f"进入目录: {path}")

    @QtCore.Slot(QtWidgets.QListWidgetItem)
    def _on_file_dblclick(self, item):
        sess = self._current_session()
        if not sess or not sess.is_connected:
            return
        name = item.text().strip()
        cwd = self.dir_path_label.text().strip()
        if cwd == "—" or not cwd or not name:
            return
        if name.endswith("/"):
            dn = name.rstrip("/")
            if dn == "..":
                ap = "/".join(cwd.rstrip("/").split("/")[:-1]) or "/"
            else:
                ap = cwd.rstrip("/") + "/" + dn
            self._nav(ap)
        else:
            dn = name.rstrip("*@")
            rp = cwd.rstrip("/") + "/" + dn
            self._open_editor(sess, rp)

    def _open_editor(self, sess, rp):
        sftp = sess.get_sftp()
        if not sftp:
            self.log("无法打开 SFTP 通道"); return
        ed = RemoteEditorWidget(sftp, rp)
        ed.log_message.connect(self.log)
        idx = self.tab_widget.addTab(ed, f"✏ {os.path.basename(rp)}")
        self.tab_widget.setCurrentIndex(idx)
        self.log(f"编辑: {rp}")

    @QtCore.Slot()
    def _on_back(self):
        cwd = self.dir_path_label.text().strip()
        if cwd == "—" or not cwd or cwd == "/":
            return
        self._nav("/".join(cwd.rstrip("/").split("/")[:-1]) or "/")

    @QtCore.Slot(QtCore.QPoint)
    def _on_file_ctx(self, pos):
        item = self.file_list.itemAt(pos)
        sess = self._current_session()
        if not item or not sess or not sess.is_connected:
            return
        name = item.text().strip()
        if not name:
            return
        menu = QtWidgets.QMenu(self); menu.setStyleSheet(_MENU_STYLE)
        is_dir = name.endswith("/")
        dn = name.rstrip("/*@")
        cwd = self.dir_path_label.text().strip()
        if is_dir:
            menu.addAction(f"进入 {dn}").triggered.connect(lambda: self._on_file_dblclick(item))
            menu.addSeparator()
        else:
            rp = (cwd.rstrip("/") + "/" + dn) if cwd != "—" else ""
            menu.addAction(f"编辑 {dn}").triggered.connect(lambda: self._open_editor(sess, rp))
            menu.addAction(f"下载 {dn}").triggered.connect(lambda: self._sftp_dl_file(sess, rp))
            menu.addSeparator()
        if dn != "..":
            menu.addAction(f"删除 {dn}").triggered.connect(lambda: self._confirm_del(name))
        menu.exec(self.file_list.mapToGlobal(pos))

    def _confirm_del(self, name):
        sess = self._current_session()
        if not sess:
            return
        is_dir = name.endswith("/")
        dn = name.rstrip("/*@")
        if dn == "..":
            return
        cwd = self.dir_path_label.text().strip()
        if cwd == "—":
            return
        ap = cwd.rstrip("/") + "/" + dn
        r = QtWidgets.QMessageBox.question(self, "确认删除", f"确定要删除 \"{ap}\" 吗？\n此操作不可恢复！",
                                           QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No)
        if r != QtWidgets.QMessageBox.Yes:
            return
        cmd = f"rm -rf {self._sq(ap)}" if is_dir else f"rm -f {self._sq(ap)}"
        self.log(f"正在删除: {cmd}")
        threading.Thread(target=lambda: self.log(sess.exec_helper.exec_cmd(cmd + " 2>&1").strip() or f"已删除: {dn}"), daemon=True).start()

    # ==================== SFTP ====================

    def _sftp_upload(self):
        sess = self._current_session()
        if not sess or not sess.is_connected:
            self.log("请先连接服务器"); return
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "选择要上传的文件")
        if not files:
            return
        cwd = self.dir_path_label.text().strip()
        if cwd == "—":
            cwd = "~"
        sftp = sess.get_sftp()
        if not sftp:
            self.log("无法打开 SFTP 通道"); return
        t = SftpThread(sftp, self)
        for f in files:
            t.add_upload(f, cwd.rstrip("/") + "/" + os.path.basename(f))
        t.progress.connect(self._sftp_prog)
        t.finished.connect(self._sftp_fin)
        t.all_done.connect(self._sftp_done)
        self.sftp_progress.setVisible(True); self.sftp_progress.setValue(0)
        t.start()
        self.log(f"开始上传 {len(files)} 个文件")

    def _sftp_download(self):
        sess = self._current_session()
        if not sess or not sess.is_connected:
            self.log("请先连接服务器"); return
        item = self.file_list.currentItem()
        if not item:
            self.log("请先选择文件"); return
        name = item.text().strip().rstrip("*@")
        if not name or name.endswith("/"):
            self.log("请选择文件而非目录"); return
        cwd = self.dir_path_label.text().strip()
        rp = (cwd.rstrip("/") + "/" + name) if cwd != "—" else name
        self._sftp_dl_file(sess, rp)

    def _sftp_dl_file(self, sess, rp):
        save_path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "保存文件", os.path.basename(rp))
        if not save_path:
            return
        sftp = sess.get_sftp()
        if not sftp:
            self.log("无法打开 SFTP 通道"); return
        t = SftpThread(sftp, self)
        t.add_download(rp, save_path)
        t.progress.connect(self._sftp_prog)
        t.finished.connect(self._sftp_fin)
        t.all_done.connect(self._sftp_done)
        self.sftp_progress.setVisible(True); self.sftp_progress.setValue(0)
        t.start()
        self.log(f"开始下载: {rp}")

    @QtCore.Slot(str, int, int)
    def _sftp_prog(self, fn, done, total):
        if total > 0:
            self.sftp_progress.setValue(int(done * 100 / total))
        self.sftp_status.setText(f"{fn}: {done // 1024}KB / {total // 1024}KB")

    @QtCore.Slot(str, bool, str)
    def _sftp_fin(self, fn, ok, msg):
        self.log(f"{'✓' if ok else '✗'} {fn}: {msg}")

    @QtCore.Slot()
    def _sftp_done(self):
        self.sftp_progress.setVisible(False)
        self.sftp_status.setText("传输完成")

    # ==================== 隧道 ====================

    def _start_tunnel(self):
        sess = self._current_session()
        if not sess or not sess.is_connected:
            self.log("请先连接服务器"); return
        try:
            lp = int(self.tun_lport.text())
            rh = self.tun_rhost.text().strip()
            rp = int(self.tun_rport.text())
        except (ValueError, AttributeError):
            self.log("请输入有效的端口号"); return
        transport = sess.ssh._ssh.get_transport() if sess.ssh._ssh else None
        if not transport:
            self.log("无可用的 SSH 传输通道"); return
        entry = TunnelEntry(lp, rh, rp)
        self._tunnel_mgr.start_local_forward(transport, entry)

    def _stop_tunnels(self):
        self._tunnel_mgr.stop_all()
        self.tunnel_list.clear()
        self.log("所有隧道已停止")

    # ==================== 工具 ====================

    @staticmethod
    def _sq(s):
        return "'" + s.replace("'", "'\\''") + "'"

    def closeEvent(self, event):
        self._tunnel_mgr.stop_all()
        for s in self._sessions.values():
            s.disconnect()
        self._sessions.clear()
        super().closeEvent(event)
