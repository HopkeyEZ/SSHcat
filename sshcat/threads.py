"""Background threads — SSH reader, writer, and panel refresh."""

import re
import queue
import socket
import time
import threading
from typing import Optional

from PySide6 import QtCore
import paramiko

from .ssh_manager import SshExecHelper


# ====================== SSH 读取线程 ======================

class SshReaderThread(QtCore.QThread):
    """高性能 SSH 数据读取线程 — 极低延迟轮询"""
    data_ready = QtCore.Signal(bytes)
    disconnected = QtCore.Signal()

    # 渐进式 sleep 阈值（可被测试覆盖）
    IDLE_FAST_THRESHOLD = 5     # idle_count < 5 → 1ms
    IDLE_MEDIUM_THRESHOLD = 20  # idle_count < 20 → 2ms
    SLEEP_FAST = 0.001    # 1ms — 打字时几乎无感
    SLEEP_MEDIUM = 0.002  # 2ms
    SLEEP_SLOW = 0.005    # 5ms — 空闲时省 CPU

    def __init__(self, channel: paramiko.Channel):
        super().__init__()
        self._channel = channel
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    @staticmethod
    def calc_sleep(idle_count: int) -> float:
        """根据空闲计数计算 sleep 时长（供外部测试调用）。"""
        if idle_count < SshReaderThread.IDLE_FAST_THRESHOLD:
            return SshReaderThread.SLEEP_FAST
        elif idle_count < SshReaderThread.IDLE_MEDIUM_THRESHOLD:
            return SshReaderThread.SLEEP_MEDIUM
        else:
            return SshReaderThread.SLEEP_SLOW

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
                # 无数据时渐进式 sleep
                idle_count += 1
                time.sleep(self.calc_sleep(idle_count))
            except socket.timeout:
                continue
            except Exception:
                if self._stop.is_set():
                    break
                break
        self.disconnected.emit()


# ====================== SSH 写入线程 ======================

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


# ====================== 面板刷新线程 ======================

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
