"""High-performance terminal emulator widget — dirty-region rendering, scrollback, 256-color."""

import threading
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets
import paramiko
import pyte

from .theme import DRACULA, resolve_color, KEY_MAP, CTRL_KEY_MAP
from .threads import SshWriterThread


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
        """获取显示行的字符数据。返回 (line_data, is_screen_line, screen_y)"""
        scrollback_len = len(self._scrollback)
        if self._scroll_offset == 0:
            # 没有回滚，直接显示当前屏幕
            return self._screen.buffer[display_y], True, display_y

        # 有回滚: 可视窗口的起始位置在回滚缓冲中
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
            # Ctrl+C: 有选区时复制，否则发送中断
            if key == QtCore.Qt.Key_C:
                selected = self._get_selected_text()
                if selected:
                    QtWidgets.QApplication.clipboard().setText(selected)
                    self._clear_selection()
                    return
            # 查表获取 Ctrl 组合键
            key_name = QtGui.QKeySequence(key).toString()
            if key_name in CTRL_KEY_MAP:
                data = CTRL_KEY_MAP[key_name]
        else:
            # 查表获取特殊键
            _QT_KEY_NAME_MAP = {
                QtCore.Qt.Key_Return: "Return",
                QtCore.Qt.Key_Enter: "Return",
                QtCore.Qt.Key_Backspace: "Backspace",
                QtCore.Qt.Key_Tab: "Tab",
                QtCore.Qt.Key_Escape: "Escape",
                QtCore.Qt.Key_Up: "Up",
                QtCore.Qt.Key_Down: "Down",
                QtCore.Qt.Key_Right: "Right",
                QtCore.Qt.Key_Left: "Left",
                QtCore.Qt.Key_Home: "Home",
                QtCore.Qt.Key_End: "End",
                QtCore.Qt.Key_Delete: "Delete",
                QtCore.Qt.Key_PageUp: "PageUp",
                QtCore.Qt.Key_PageDown: "PageDown",
                QtCore.Qt.Key_Insert: "Insert",
                QtCore.Qt.Key_F1: "F1",
                QtCore.Qt.Key_F2: "F2",
                QtCore.Qt.Key_F3: "F3",
                QtCore.Qt.Key_F4: "F4",
                QtCore.Qt.Key_F5: "F5",
            }
            key_name = _QT_KEY_NAME_MAP.get(key)
            if key_name and key_name in KEY_MAP:
                data = KEY_MAP[key_name]
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
