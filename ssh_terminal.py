"""
SSHcat - PySide6 GUI 版本
基于原 C++ 控制台 SSH 客户端改写，使用 Dracula 暗色主题
"""

import os, sys, time, threading, re, socket, queue, ctypes
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets
import paramiko
import pyte


# ====================== Dracula 配色 ======================

DRACULA = {
    "bg":         "#282a36",
    "bg_darker":  "#21222c",
    "bg_lighter": "#343746",
    "fg":         "#f8f8f2",
    "comment":    "#6272a4",
    "purple":     "#bd93f9",
    "pink":       "#ff79c6",
    "red":        "#ff5555",
    "green":      "#50fa7b",
    "yellow":     "#f1fa8c",
    "orange":     "#ffb86c",
    "cyan":       "#8be9fd",
    "selection":  "#44475a",
}

# ANSI 16色 -> Dracula RGB 映射
ANSI_COLORS = {
    "black":         "#282a36",
    "red":           "#ff5555",
    "green":         "#50fa7b",
    "brown":         "#f1fa8c",
    "blue":          "#bd93f9",
    "magenta":       "#ff79c6",
    "cyan":          "#8be9fd",
    "white":         "#f8f8f2",
    "brightblack":   "#6272a4",
    "brightred":     "#ff6e6e",
    "brightgreen":   "#69ff94",
    "brightyellow":  "#ffffa5",
    "brightblue":    "#d6acff",
    "brightmagenta": "#ff92df",
    "brightcyan":    "#a4ffff",
    "brightwhite":   "#ffffff",
}

# 256色调色板 (前16色用 Dracula, 后面用标准)
_color_palette_256 = None

def _build_256_palette():
    global _color_palette_256
    if _color_palette_256 is not None:
        return _color_palette_256

    palette = []
    # 0-7: 标准色
    std = ["black","red","green","brown","blue","magenta","cyan","white"]
    for name in std:
        palette.append(ANSI_COLORS[name])
    # 8-15: 亮色
    bright = ["brightblack","brightred","brightgreen","brightyellow",
              "brightblue","brightmagenta","brightcyan","brightwhite"]
    for name in bright:
        palette.append(ANSI_COLORS[name])
    # 16-231: 6x6x6 颜色立方体
    for r in range(6):
        for g in range(6):
            for b in range(6):
                rv = 55 + 40 * r if r else 0
                gv = 55 + 40 * g if g else 0
                bv = 55 + 40 * b if b else 0
                palette.append(f"#{rv:02x}{gv:02x}{bv:02x}")
    # 232-255: 灰度
    for i in range(24):
        v = 8 + 10 * i
        palette.append(f"#{v:02x}{v:02x}{v:02x}")

    _color_palette_256 = palette
    return palette


def resolve_color(color_val, is_fg=True) -> str:
    """将 pyte 颜色值解析为 hex 颜色字符串"""
    if color_val == "default" or color_val is None:
        return DRACULA["fg"] if is_fg else DRACULA["bg"]

    # 命名颜色
    if isinstance(color_val, str):
        if color_val in ANSI_COLORS:
            return ANSI_COLORS[color_val]
        # 可能是 "00ff00" 格式的 RGB
        if len(color_val) == 6:
            try:
                int(color_val, 16)
                return "#" + color_val
            except ValueError:
                pass
        return DRACULA["fg"] if is_fg else DRACULA["bg"]

    # 数字索引 (256色)
    if isinstance(color_val, int):
        palette = _build_256_palette()
        if 0 <= color_val < 256:
            return palette[color_val]

    return DRACULA["fg"] if is_fg else DRACULA["bg"]


# ====================== SSH 管理 ======================

def _create_ssh_client(host, port, username, password):
    """创建经过优化的 SSH 连接"""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        hostname=host, port=int(port), username=username,
        password=password, timeout=10, allow_agent=False,
        look_for_keys=False, compress=True,
    )
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


class SshManager:
    """主终端通道专用连接 — 不加锁，只在读取/写入线程中使用"""
    def __init__(self):
        self._ssh: Optional[paramiko.SSHClient] = None

    @property
    def connected(self):
        return (self._ssh is not None
                and self._ssh.get_transport() is not None
                and self._ssh.get_transport().is_active())

    def connect(self, host, port, username, password):
        self.close()
        self._ssh = _create_ssh_client(host, port, username, password)

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


# ====================== SSH 读取线程 ======================

class SshReaderThread(QtCore.QThread):
    """高性��� SSH 数据读���线程 — 极低延迟轮询"""
    data_ready = QtCore.Signal(bytes)
    disconnected = QtCore.Signal()

    def __init__(self, channel: paramiko.Channel):
        super().__init__()
        self._channel = channel
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        chan = self._channel
        idle_count = 0
        while not self._stop.is_set():
            try:
                if chan.closed or chan.exit_status_ready():
                    break
                # recv_ready() 无阻塞检查是否有数据
                if chan.recv_ready():
                    data = chan.recv(65536)
                    if data:
                        self.data_ready.emit(data)
                        idle_count = 0
                        continue
                    else:
                        break  # 连接关闭
                # 无数据时渐进式 sleep: 1ms → 2ms → 5ms (最大)
                idle_count += 1
                if idle_count < 5:
                    time.sleep(0.001)   # 1ms — 打字时几乎无感
                elif idle_count < 20:
                    time.sleep(0.002)   # 2ms
                else:
                    time.sleep(0.005)   # 5ms — 空闲时省 CPU
            except socket.timeout:
                continue
            except Exception:
                if self._stop.is_set():
                    break
                break
        self.disconnected.emit()


# ====================== 面板刷新线程 ======================

class SshWriterThread(QtCore.QThread):
    """异步输入发送线程 — 按键不阻塞 UI"""
    def __init__(self, channel: paramiko.Channel):
        super().__init__()
        self._channel = channel
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()

    def send(self, data: bytes):
        self._queue.put(data)

    def stop(self):
        self._stop.set()
        self._queue.put(None)  # 唤醒

    def run(self):
        chan = self._channel
        while not self._stop.is_set():
            try:
                data = self._queue.get(timeout=0.2)
                if data is None:
                    break
                chan.sendall(data)
            except queue.Empty:
                continue
            except Exception:
                if self._stop.is_set():
                    break


class PanelRefreshThread(QtCore.QThread):
    """面板刷新线程 — 使用独立 SSH 连接，合并命令减少往返"""
    dir_ready = QtCore.Signal(str, list)
    sys_ready = QtCore.Signal(float, float, float, str)

    # 系统信息部分（不含目录部分）
    _SYS_CMD_PART = (
        'echo "===CPU===" && '
        '{ head -1 /proc/stat 2>/dev/null && sleep 0.3 && head -1 /proc/stat 2>/dev/null; } && '
        'echo "===NPROC===" && nproc 2>/dev/null && '
        'echo "===MEM===" && free 2>/dev/null | grep Mem && '
        'echo "===DISK===" && df / 2>/dev/null | tail -1 && '
        'echo "===UPTIME===" && { uptime -p 2>/dev/null || uptime 2>/dev/null; } && '
        'echo "===END==="'
    )

    def __init__(self, ssh: SshExecHelper, interval=3.0):
        super().__init__()
        self._ssh = ssh
        self._interval = interval
        self._stop = threading.Event()
        self._tracked_cwd: Optional[str] = None  # 跟踪当前目录
        self._cwd_lock = threading.Lock()

    def set_cwd(self, path: str):
        """从外部设置跟踪的当前目录（双击目录时调用）"""
        with self._cwd_lock:
            self._tracked_cwd = path

    def get_cwd(self) -> Optional[str]:
        with self._cwd_lock:
            return self._tracked_cwd

    def stop(self):
        self._stop.set()

    def _build_cmd(self) -> str:
        """动态构建命令，包含 cd 到跟踪的目录"""
        cwd = self.get_cwd()
        if cwd:
            # 先 cd 到跟踪的目录，再执行 pwd/ls
            safe_cwd = cwd.replace("'", "'\\''")
            cd_prefix = f"cd '{safe_cwd}' 2>/dev/null; "
        else:
            cd_prefix = ""
        return (
            cd_prefix +
            'echo "===PWD===" && pwd 2>/dev/null && '
            'echo "===LS===" && ls -1F --color=never 2>/dev/null && '
            + self._SYS_CMD_PART
        )

    def _parse_section(self, sections: dict, key: str) -> str:
        return sections.get(key, "").strip()

    def run(self):
        while not self._stop.is_set():
            if not self._ssh.connected:
                self._stop.wait(1)
                continue
            try:
                raw = self._ssh.exec_cmd(self._build_cmd())
                # 按分隔符切割
                sections = {}
                current_key = None
                for line in raw.split("\n"):
                    stripped = line.strip()
                    if stripped.startswith("===") and stripped.endswith("==="):
                        current_key = stripped.strip("=")
                        sections[current_key] = ""
                    elif current_key:
                        sections[current_key] = sections[current_key] + line + "\n"

                # 目录
                cwd = self._parse_section(sections, "PWD")
                ls_raw = self._parse_section(sections, "LS")
                file_names = [f for f in ls_raw.split("\n") if f.strip()]
                # 用实际 pwd 结果回写 tracked_cwd，保持同步
                if cwd:
                    with self._cwd_lock:
                        self._tracked_cwd = cwd
                self.dir_ready.emit(cwd, file_names)

                # CPU — 通过两次 /proc/stat 采样计算真实使用率
                cpu_pct = 0.0
                cpu_lines = self._parse_section(sections, "CPU").strip().split("\n")
                if len(cpu_lines) >= 2:
                    try:
                        v1 = [int(x) for x in cpu_lines[0].split()[1:]]
                        v2 = [int(x) for x in cpu_lines[1].split()[1:]]
                        d = [b - a for a, b in zip(v1, v2)]
                        total = sum(d)
                        idle = d[3] if len(d) > 3 else 0
                        if total > 0:
                            cpu_pct = ((total - idle) / total) * 100.0
                    except (ValueError, IndexError):
                        pass
                # fallback
                if cpu_pct == 0.0:
                    nproc_raw = self._parse_section(sections, "NPROC")
                    loadavg = self._parse_section(sections, "CPU").split("\n")[0] if "CPU" in sections else ""
                    # 无法用此 fallback，跳过

                # 内存
                mem_pct = 0.0
                mem_raw = self._parse_section(sections, "MEM")
                if mem_raw:
                    parts = mem_raw.split()
                    try:
                        total = float(parts[1])
                        used = float(parts[2])
                        if total > 0:
                            mem_pct = (used / total) * 100.0
                    except (ValueError, IndexError):
                        pass

                # 磁盘
                disk_pct = 0.0
                disk_raw = self._parse_section(sections, "DISK")
                if disk_raw:
                    m = re.search(r'(\d+)%', disk_raw)
                    if m:
                        disk_pct = float(m.group(1))

                # Uptime
                uptime = self._parse_section(sections, "UPTIME")

                self.sys_ready.emit(cpu_pct, mem_pct, disk_pct, uptime)
            except Exception:
                pass

            self._stop.wait(self._interval)


# ====================== 终端控件 ======================

class TerminalWidget(QtWidgets.QWidget):
    """高性能终端控件 — 脏区渲染 + 颜色缓存 + 异步输入 + 回滚缓冲"""

    _SCROLLBACK_MAX = 5000  # 最大回滚行数

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setAttribute(QtCore.Qt.WA_InputMethodEnabled, True)
        self.setAttribute(QtCore.Qt.WA_OpaquePaintEvent, True)

        # 字体 + 预构建变体
        self._font = QtGui.QFont()
        for name in ["Consolas", "Cascadia Mono", "Courier New", "monospace"]:
            self._font.setFamily(name)
            fm = QtGui.QFontMetrics(self._font)
            if fm.horizontalAdvance("M") == fm.horizontalAdvance("i"):
                break
        self._font.setPointSize(11)
        self._font.setStyleHint(QtGui.QFont.Monospace)
        self.setFont(self._font)

        # 预构建 8 种字体变体 (bold/italic/underline 组合)
        self._font_cache = {}
        for bold in (False, True):
            for italic in (False, True):
                for underline in (False, True):
                    key = (bold, italic, underline)
                    f = QtGui.QFont(self._font)
                    f.setBold(bold)
                    f.setItalic(italic)
                    f.setUnderline(underline)
                    self._font_cache[key] = f

        fm = QtGui.QFontMetrics(self._font)
        self._char_w = fm.horizontalAdvance("M")
        self._char_h = fm.height()
        self._ascent = fm.ascent()

        # 颜色缓存 — 避免反复创建 QColor
        self._qcolor_cache = {}

        # pyte 虚拟终端 — 使用 HistoryScreen 支持回滚
        self._cols = 80
        self._rows = 24
        self._screen = pyte.HistoryScreen(self._cols, self._rows,
                                           history=self._SCROLLBACK_MAX)
        self._stream = pyte.ByteStream(self._screen)

        # 回滚状态: _scrollback 存储滚出屏幕的历史行快照
        self._scrollback = []        # list of list[pyte.Char], 每项是一行的字符列表
        self._scroll_offset = 0      # 0 = 在最底部(实时), >0 = 向上翻了多少行
        self._prev_history_len = 0   # 上次检查时 history.top 的长度

        # 垂直滚动条
        self._scrollbar = QtWidgets.QScrollBar(QtCore.Qt.Vertical, self)
        self._scrollbar.setRange(0, 0)
        self._scrollbar.setValue(0)
        self._scrollbar.valueChanged.connect(self._on_scrollbar)
        self._scrollbar_width = 12

        # 离屏缓冲
        self._backbuf: Optional[QtGui.QImage] = None
        self._full_repaint = True

        # SSH 通道 + 异步写入线程
        self._channel: Optional[paramiko.Channel] = None
        self._writer: Optional[SshWriterThread] = None

        # 光标
        self._cursor_visible = True
        self._prev_cursor = (-1, -1)
        self._blink_timer = QtCore.QTimer(self)
        self._blink_timer.timeout.connect(self._toggle_cursor)
        self._blink_timer.start(530)

        # 刷新定时器
        self._paint_timer = QtCore.QTimer(self)
        self._paint_timer.timeout.connect(self._tick)
        self._paint_timer.start(16)  # ~60fps

        # 数据缓冲
        self._pending_data = bytearray()
        self._data_lock = threading.Lock()
        self._has_new_data = False

        # 鼠标选区 (列, 行) — 基于终端网格坐标
        self._sel_start = None   # (col, row) 或 None
        self._sel_end = None
        self._selecting = False  # 是否正在拖选

        self.setMinimumSize(200, 100)

    def _get_qcolor(self, hex_color: str) -> QtGui.QColor:
        c = self._qcolor_cache.get(hex_color)
        if c is None:
            c = QtGui.QColor(hex_color)
            self._qcolor_cache[hex_color] = c
        return c

    def set_channel(self, channel: paramiko.Channel):
        if self._writer:
            self._writer.stop()
            self._writer.wait(1000)
            self._writer = None
        self._channel = channel
        if channel:
            self._writer = SshWriterThread(channel)
            self._writer.start()

    def feed_data(self, data: bytes):
        with self._data_lock:
            self._pending_data.extend(data)
            self._has_new_data = True

    # 每次 tick 最多处理的字节数 — 防止大量数据阻塞 UI 线程
    _MAX_CHUNK = 8192

    def _process_pending(self) -> bool:
        with self._data_lock:
            if not self._pending_data:
                return False
            # 只取一部分，剩余留到下次 tick 处理
            chunk = bytes(self._pending_data[:self._MAX_CHUNK])
            del self._pending_data[:self._MAX_CHUNK]
            if self._pending_data:
                self._has_new_data = True  # 还有数据，下次继续
            else:
                self._has_new_data = False
        self._stream.feed(chunk)
        return True

    def _calc_grid(self):
        w = self.width() - self._scrollbar_width  # 减去滚动条宽度
        h = self.height()
        cols = max(10, w // self._char_w)
        rows = max(3, h // self._char_h)
        return cols, rows

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 重新定位滚动条
        self._scrollbar.setGeometry(
            self.width() - self._scrollbar_width, 0,
            self._scrollbar_width, self.height()
        )
        cols, rows = self._calc_grid()
        if cols != self._cols or rows != self._rows:
            self._cols = cols
            self._rows = rows
            self._screen.resize(rows, cols)
            self._prev_history_len = len(self._screen.history.top)
            self._backbuf = None  # 强制重建
            self._full_repaint = True
            if self._channel:
                try:
                    self._channel.resize_pty(cols, rows)
                except Exception:
                    pass

    def _toggle_cursor(self):
        self._cursor_visible = not self._cursor_visible

    def _capture_history(self):
        """从 pyte HistoryScreen 中捕获新滚出的历史行"""
        history_top = self._screen.history.top
        cur_len = len(history_top)
        if cur_len > self._prev_history_len:
            # 有新的行滚出屏幕顶部
            new_count = cur_len - self._prev_history_len
            # history.top 是 deque，新行在末尾
            for i in range(self._prev_history_len, cur_len):
                line_dict = history_top[i]
                # 转为列表快照，补齐列数
                row = []
                for col in range(self._cols):
                    row.append(line_dict[col] if col in line_dict else self._screen.default_char)
                self._scrollback.append(row)
            # 限制回滚缓冲大小
            if len(self._scrollback) > self._SCROLLBACK_MAX:
                excess = len(self._scrollback) - self._SCROLLBACK_MAX
                del self._scrollback[:excess]
        self._prev_history_len = cur_len

    def _update_scrollbar(self):
        """根据回滚缓冲更新滚动条范围和位置"""
        total = len(self._scrollback)
        self._scrollbar.blockSignals(True)
        self._scrollbar.setRange(0, total)
        # 如果在底部，保持跟随
        if self._scroll_offset == 0:
            self._scrollbar.setValue(total)
        else:
            self._scrollbar.setValue(total - self._scroll_offset)
        self._scrollbar.setPageStep(self._rows)
        self._scrollbar.blockSignals(False)

    def _on_scrollbar(self, value):
        """滚动条拖动回调"""
        total = len(self._scrollback)
        new_offset = total - value
        if new_offset < 0:
            new_offset = 0
        if new_offset != self._scroll_offset:
            self._scroll_offset = new_offset
            self._full_repaint = True
            self.update()

    def _tick(self):
        had_data = self._process_pending()

        if had_data:
            self._capture_history()
            # 有新数据时自动滚到底部
            if self._scroll_offset > 0:
                self._scroll_offset = 0
                self._full_repaint = True
            self._update_scrollbar()

        screen = self._screen
        dirty = screen.dirty
        cursor_moved = (screen.cursor.x, screen.cursor.y) != self._prev_cursor

        if had_data or dirty or cursor_moved or self._full_repaint:
            self.update()

        # 如果还有未处理的数据，立即安排下一次处理（0ms 延迟），
        # 让 Qt 事件循环有机会先处理用户输入和绘制事件，避免饿死 UI
        if self._has_new_data:
            QtCore.QTimer.singleShot(0, self._tick)

    def _get_display_line(self, display_y):
        """获取显示行的字符数据。返回 (line_data, is_screen_line, screen_y)
        line_data: dict 或 list (屏幕行是 dict, 回滚行是 list)
        is_screen_line: 是否是当前屏幕上的行
        screen_y: 如果是屏幕行，对应的 screen buffer 行号
        """
        scrollback_len = len(self._scrollback)
        # display_y 对应的全局行号 (从回滚缓冲顶部开始)
        # 可视区域 = scrollback[scrollback_len - scroll_offset - rows : scrollback_len - scroll_offset] + screen[...]
        if self._scroll_offset == 0:
            # 没有回滚，直接显示当前屏幕
            return self._screen.buffer[display_y], True, display_y

        # 有回滚: 可视窗口的起始位置在回滚缓冲中
        # 可视区覆盖 rows 行，其中前面的行来自 scrollback，后面的来自 screen
        # scrollback 提供的行数 = min(scroll_offset, rows)
        sb_lines_visible = min(self._scroll_offset, self._rows)
        screen_lines_visible = self._rows - sb_lines_visible

        if display_y < sb_lines_visible:
            # 来自回滚缓冲
            sb_index = scrollback_len - self._scroll_offset + display_y
            if 0 <= sb_index < scrollback_len:
                return self._scrollback[sb_index], False, -1
            else:
                return None, False, -1
        else:
            # 来自当前屏幕
            screen_y = display_y - sb_lines_visible
            if screen_y < self._screen.lines:
                return self._screen.buffer[screen_y], True, screen_y
            return None, False, -1

    def paintEvent(self, event):
        screen = self._screen
        cw = self._char_w
        ch = self._char_h
        term_width = self.width() - self._scrollbar_width

        # 初始化或重建离屏缓冲（适配高 DPI）
        dpr = self.devicePixelRatioF()
        phys_size = QtCore.QSize(int(self.width() * dpr), int(self.height() * dpr))
        if self._backbuf is None or self._backbuf.size() != phys_size:
            self._backbuf = QtGui.QImage(phys_size, QtGui.QImage.Format_RGB32)
            self._backbuf.setDevicePixelRatio(dpr)
            self._backbuf.fill(self._get_qcolor(DRACULA["bg"]))
            self._full_repaint = True

        bg_default = DRACULA["bg"]

        # 回滚模式/有选区时全量重绘，实时模式下用脏行优化
        if self._scroll_offset > 0 or self._full_repaint or self._sel_start is not None:
            dirty_lines = set(range(self._rows))
        else:
            dirty_lines = set(screen.dirty)
            dirty_lines.add(screen.cursor.y)
            if self._prev_cursor[1] >= 0:
                dirty_lines.add(self._prev_cursor[1])

        self._prev_cursor = (screen.cursor.x, screen.cursor.y)

        painter = QtGui.QPainter(self._backbuf)
        painter.setFont(self._font)
        bg_qc_default = self._get_qcolor(bg_default)

        has_focus = self.hasFocus()
        cursor_vis = self._cursor_visible
        at_bottom = (self._scroll_offset == 0)

        for y in dirty_lines:
            if y >= self._rows:
                continue
            py = y * ch
            painter.fillRect(0, py, term_width, ch, bg_qc_default)

            line_data, is_screen, screen_y = self._get_display_line(y)
            if line_data is None:
                continue

            for x in range(screen.columns):
                if is_screen:
                    char = line_data[x]
                else:
                    char = line_data[x] if x < len(line_data) else screen.default_char
                px = x * cw

                bg_hex = resolve_color(char.bg, is_fg=False)
                fg_hex = resolve_color(char.fg, is_fg=True)

                if char.reverse:
                    bg_hex, fg_hex = fg_hex, bg_hex

                # 选区高亮
                if self._is_selected(x, y):
                    bg_hex = DRACULA["selection"]
                    fg_hex = DRACULA["fg"]

                # 只在实时模式(底部)且是屏幕行时显示光标
                is_cursor = (at_bottom and is_screen
                             and x == screen.cursor.x and screen_y == screen.cursor.y
                             and cursor_vis and has_focus)
                if is_cursor:
                    bg_hex, fg_hex = fg_hex, bg_hex

                if bg_hex != bg_default or is_cursor or self._is_selected(x, y):
                    painter.fillRect(px, py, cw, ch, self._get_qcolor(bg_hex))

                if char.data and char.data != " ":
                    font_key = (bool(char.bold), bool(char.italics), bool(char.underscore))
                    if font_key != (False, False, False):
                        painter.setFont(self._font_cache[font_key])

                    painter.setPen(self._get_qcolor(fg_hex))
                    painter.drawText(px, py + self._ascent, char.data)

                    if font_key != (False, False, False):
                        painter.setFont(self._font)

        # 回滚模式指示条
        if not at_bottom:
            indicator_text = f" ↑ 回滚 {self._scroll_offset} 行 — 滚轮↓ 或 End 回到底部 "
            painter.setPen(self._get_qcolor(DRACULA["bg"]))
            painter.fillRect(0, 0, term_width, ch, self._get_qcolor(DRACULA["yellow"]))
            painter.drawText(4, self._ascent, indicator_text)

        painter.end()
        screen.dirty.clear()
        self._full_repaint = False

        # 从离屏缓冲 blit 到屏幕
        screen_painter = QtGui.QPainter(self)
        screen_painter.drawImage(0, 0, self._backbuf)
        screen_painter.end()

    # -------------------- 鼠标选区 --------------------

    def _pixel_to_cell(self, pos):
        """像素坐标 -> (col, row) 终端网格坐标"""
        col = max(0, min(pos.x() // self._char_w, self._cols - 1))
        row = max(0, min(pos.y() // self._char_h, self._rows - 1))
        return (col, row)

    def _sel_ordered(self):
        """返回排序后的选区 (start, end)，start <= end"""
        if self._sel_start is None or self._sel_end is None:
            return None, None
        s, e = self._sel_start, self._sel_end
        if (s[1], s[0]) > (e[1], e[0]):
            s, e = e, s
        return s, e

    def _is_selected(self, col, row):
        """判断 (col, row) 是否在选区内"""
        s, e = self._sel_ordered()
        if s is None:
            return False
        if row < s[1] or row > e[1]:
            return False
        if row == s[1] and row == e[1]:
            return s[0] <= col <= e[0]
        if row == s[1]:
            return col >= s[0]
        if row == e[1]:
            return col <= e[0]
        return True

    def _get_selected_text(self) -> str:
        """提取选区内的文本"""
        s, e = self._sel_ordered()
        if s is None:
            return ""
        lines = []
        for row in range(s[1], e[1] + 1):
            line_chars = []
            col_start = s[0] if row == s[1] else 0
            col_end = e[0] if row == e[1] else self._cols - 1
            line_data, is_screen, _ = self._get_display_line(row)
            if line_data is None:
                lines.append("")
                continue
            for col in range(col_start, col_end + 1):
                if is_screen:
                    char = line_data[col]
                else:
                    char = line_data[col] if col < len(line_data) else self._screen.default_char
                line_chars.append(char.data if char.data else " ")
            lines.append("".join(line_chars).rstrip())
        return "\n".join(lines)

    def _clear_selection(self):
        if self._sel_start is not None:
            self._sel_start = None
            self._sel_end = None
            self._full_repaint = True
            self.update()

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self._selecting = True
            self._sel_start = self._pixel_to_cell(event.pos())
            self._sel_end = self._sel_start
            self._full_repaint = True
            self.update()
        elif event.button() == QtCore.Qt.RightButton:
            self._show_context_menu(event.globalPos())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._selecting:
            self._sel_end = self._pixel_to_cell(event.pos())
            self._full_repaint = True
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self._selecting = False
        super().mouseReleaseEvent(event)

    def _show_context_menu(self, global_pos):
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
            QMenu::item:disabled {{
                color: {DRACULA['comment']};
            }}
        """)

        selected_text = self._get_selected_text()
        action_copy = menu.addAction("复制")
        action_copy.setEnabled(bool(selected_text))

        action_paste = menu.addAction("粘贴")
        clipboard = QtWidgets.QApplication.clipboard()
        action_paste.setEnabled(bool(clipboard.text()))

        action_select_all = menu.addAction("全选当前屏幕")

        chosen = menu.exec(global_pos)
        if chosen == action_copy and selected_text:
            clipboard = QtWidgets.QApplication.clipboard()
            clipboard.setText(selected_text)
            self._clear_selection()
        elif chosen == action_paste:
            clip_text = clipboard.text()
            if clip_text and self._writer:
                self._send_data(clip_text.encode("utf-8"))
        elif chosen == action_select_all:
            self._sel_start = (0, 0)
            self._sel_end = (self._cols - 1, self._rows - 1)
            self._full_repaint = True
            self.update()

    def wheelEvent(self, event):
        """鼠标滚轮控制回滚"""
        delta = event.angleDelta().y()
        if delta == 0:
            return
        # 向上滚 = delta > 0, 向下滚 = delta < 0
        lines = max(1, abs(delta) // 40)  # 约3行/格
        max_offset = len(self._scrollback)
        if delta > 0:
            # 向上滚动（查看历史）
            self._scroll_offset = min(self._scroll_offset + lines, max_offset)
        else:
            # 向下滚动（回到实时）
            self._scroll_offset = max(self._scroll_offset - lines, 0)
        self._update_scrollbar()
        self._full_repaint = True
        self.update()
        event.accept()

    def _send_data(self, data: bytes):
        if self._writer:
            self._writer.send(data)

    def keyPressEvent(self, event):
        if not self._channel:
            return

        key = event.key()
        modifiers = event.modifiers()
        text = event.text()

        data = b""

        if modifiers & QtCore.Qt.ControlModifier:
            if key == QtCore.Qt.Key_C:
                # 有选区时复制文本，否则发送 Ctrl+C 中断
                selected = self._get_selected_text()
                if selected:
                    QtWidgets.QApplication.clipboard().setText(selected)
                    self._clear_selection()
                    return
                data = b"\x03"
            elif key == QtCore.Qt.Key_D:
                data = b"\x04"
            elif key == QtCore.Qt.Key_Z:
                data = b"\x1a"
            elif key == QtCore.Qt.Key_L:
                data = b"\x0c"
            elif key == QtCore.Qt.Key_A:
                data = b"\x01"
            elif key == QtCore.Qt.Key_E:
                data = b"\x05"
            elif key == QtCore.Qt.Key_K:
                data = b"\x0b"
            elif key == QtCore.Qt.Key_U:
                data = b"\x15"
            elif key == QtCore.Qt.Key_W:
                data = b"\x17"
        elif key == QtCore.Qt.Key_Return or key == QtCore.Qt.Key_Enter:
            data = b"\r"
        elif key == QtCore.Qt.Key_Backspace:
            data = b"\x7f"
        elif key == QtCore.Qt.Key_Tab:
            data = b"\t"
        elif key == QtCore.Qt.Key_Escape:
            data = b"\x1b"
        elif key == QtCore.Qt.Key_Up:
            data = b"\x1b[A"
        elif key == QtCore.Qt.Key_Down:
            data = b"\x1b[B"
        elif key == QtCore.Qt.Key_Right:
            data = b"\x1b[C"
        elif key == QtCore.Qt.Key_Left:
            data = b"\x1b[D"
        elif key == QtCore.Qt.Key_Home:
            data = b"\x1b[H"
        elif key == QtCore.Qt.Key_End:
            data = b"\x1b[F"
        elif key == QtCore.Qt.Key_Delete:
            data = b"\x1b[3~"
        elif key == QtCore.Qt.Key_PageUp:
            data = b"\x1b[5~"
        elif key == QtCore.Qt.Key_PageDown:
            data = b"\x1b[6~"
        elif key == QtCore.Qt.Key_Insert:
            data = b"\x1b[2~"
        elif key == QtCore.Qt.Key_F1:
            data = b"\x1bOP"
        elif key == QtCore.Qt.Key_F2:
            data = b"\x1bOQ"
        elif key == QtCore.Qt.Key_F3:
            data = b"\x1bOR"
        elif key == QtCore.Qt.Key_F4:
            data = b"\x1bOS"
        elif key == QtCore.Qt.Key_F5:
            data = b"\x1b[15~"
        elif text:
            data = text.encode("utf-8")

        if data:
            self._send_data(data)

    def inputMethodEvent(self, event):
        commit = event.commitString()
        if commit and self._channel:
            self._send_data(commit.encode("utf-8"))
        event.accept()

    def sizeHint(self):
        return QtCore.QSize(self._cols * self._char_w, self._rows * self._char_h)


# ====================== 主窗口 ======================

class MainWindow(QtWidgets.QMainWindow):
    log_signal = QtCore.Signal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("SSHcat")
        self.resize(1200, 700)
        # 设置窗口图标
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QtGui.QIcon(icon_path))

        self._ssh = SshManager()
        self._exec = SshExecHelper(self._ssh)  # 复用同一连接的独立通道
        self._reader: Optional[SshReaderThread] = None
        self._panel_thread: Optional[PanelRefreshThread] = None
        self._channel: Optional[paramiko.Channel] = None

        self._build_ui()
        self._setup_styles()
        self._setup_dark_titlebar()

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
            # DWMWA_USE_IMMERSIVE_DARK_MODE = 20
            value = ctypes.c_int(1)
            dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
            # DWMWA_CAPTION_COLOR = 35 (Windows 11)
            # 背景色 #282a36 -> BGR int: 0x362a28
            bg_color = ctypes.c_int(0x00362a28)
            dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(bg_color), ctypes.sizeof(bg_color))
            # DWMWA_TEXT_COLOR = 36 (标题文字颜色)
            text_color = ctypes.c_int(0x00f2f8f8)  # #f8f8f2 -> BGR
            dwmapi.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(text_color), ctypes.sizeof(text_color))
        except Exception:
            pass

    # -------------------- 连接 / 断开 --------------------

    def on_connect(self):
        host = self.ip_edit.text().strip()
        port_str = self.port_edit.text().strip()
        username = self.user_edit.text().strip()
        password = self.pass_edit.text()

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

        # 在线程中连接，避免阻塞 UI
        def do_connect():
            try:
                # 1. 终端连接（主通道）
                self._ssh.connect(host, port, username, password)
                cols, rows = self.terminal._calc_grid()
                self._channel = self._ssh.open_shell(cols, rows)
                self.terminal.set_channel(self._channel)

                # 启动读取���程
                self._reader = SshReaderThread(self._channel)
                self._reader.data_ready.connect(self.terminal.feed_data)
                self._reader.disconnected.connect(self._on_disconnected)
                self._reader.start()

                # 启动面板刷新线程（复用同一连接的独立 exec 通道）
                self._panel_thread = PanelRefreshThread(self._exec)
                self._panel_thread.dir_ready.connect(self._update_dir)
                self._panel_thread.sys_ready.connect(self._update_sys)
                self._panel_thread.start()

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

    @QtCore.Slot()
    def _on_connect_failed(self):
        self.btn_connect.setEnabled(True)

    @QtCore.Slot()
    def _on_disconnected(self):
        self.log("连接已断开")
        self._cleanup()

    def on_disconnect(self):
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
        # 构建新列表（含 "../"）
        new_names = []
        if cwd and cwd != "/":
            new_names.append("../")
        for name in file_names:
            name = name.strip()
            if name:
                new_names.append(name)

        # 内容没变化时不刷新，避免滚动条跳动
        if (hasattr(self, '_last_dir_cwd') and self._last_dir_cwd == cwd
                and hasattr(self, '_last_dir_names') and self._last_dir_names == new_names):
            return
        self._last_dir_cwd = cwd
        self._last_dir_names = list(new_names)

        # 记住滚动位置
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

        # 恢复滚动位置
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

        # 向终端 shell 发送 cd 命令
        cmd = f"cd {self._shell_quote(abs_path)}\r"
        self.terminal._send_data(cmd.encode("utf-8"))

        # 更新面板跟踪目录
        if self._panel_thread:
            self._panel_thread.set_cwd(abs_path)

        # 清除缓存
        self._last_dir_cwd = None
        self._last_dir_names = None

        self.log(f"进入目录: {abs_path}")

        # 立即在后台获取新目录的文件列表并刷新面板
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
        """应用导航后的目录刷新"""
        if hasattr(self, '_nav_result'):
            cwd, file_names = self._nav_result
            self._last_dir_cwd = None
            self._last_dir_names = None
            self._update_dir(cwd, file_names)
            del self._nav_result

    @QtCore.Slot(QtWidgets.QListWidgetItem)
    def _on_file_double_clicked(self, item: QtWidgets.QListWidgetItem):
        """双击目录项 -> 进入该目录"""
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
        """返回上级目录"""
        if not self._channel:
            return
        panel_cwd = self.dir_path_label.text().strip()
        if panel_cwd == "—" or not panel_cwd or panel_cwd == "/":
            return
        abs_path = "/".join(panel_cwd.rstrip("/").split("/")[:-1]) or "/"
        self._navigate_to_dir(abs_path)

    @QtCore.Slot(QtCore.QPoint)
    def _on_file_context_menu(self, pos: QtCore.QPoint):
        """右键菜单 -> 删除文件/文件夹"""
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

        # 如果是目录，添加"进入目录"选项
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
        """确认并删除文件/文件夹（使用绝对路径）"""
        is_dir = name.endswith("/")
        display_name = name.rstrip("/*@")
        type_str = "文件夹" if is_dir else "文件"

        # 不允许删除 "../"
        if display_name == "..":
            return

        # 构建绝对路径
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

        # 用 exec 通道执行删除，不干扰终端
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
        """删除文件后刷新面板和终端"""
        # 在终端中发一个 ls 让用户看到变化
        if self.terminal._writer:
            self.terminal._send_data(b"ls\r")
        # 手动触发面板刷新（在后台线程中执行）
        if self._exec.connected and self._panel_thread:
            refresh_cmd = self._panel_thread._build_cmd()
            def do_refresh():
                raw = self._exec.exec_cmd(refresh_cmd)
                # 复用面板线程的解析逻辑 — 简单地重跑一次
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
                # 回到主线程更新 UI
                self.dir_ready_from_delete = (cwd, file_names)
                QtCore.QMetaObject.invokeMethod(self, "_apply_dir_refresh",
                                                 QtCore.Qt.QueuedConnection)
            threading.Thread(target=do_refresh, daemon=True).start()

    @QtCore.Slot()
    def _apply_dir_refresh(self):
        """应用删除后的目录刷新"""
        if hasattr(self, 'dir_ready_from_delete'):
            cwd, file_names = self.dir_ready_from_delete
            # 清除缓存，强制刷新列表
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
        self._cleanup()
        super().closeEvent(event)


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ====================== 入口 ======================

if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
