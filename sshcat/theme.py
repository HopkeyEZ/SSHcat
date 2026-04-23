"""Dracula color theme, ANSI color mapping, and 256-color palette."""

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
    std = ["black", "red", "green", "brown", "blue", "magenta", "cyan", "white"]
    for name in std:
        palette.append(ANSI_COLORS[name])
    # 8-15: 亮色
    bright = ["brightblack", "brightred", "brightgreen", "brightyellow",
              "brightblue", "brightmagenta", "brightcyan", "brightwhite"]
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


# ====================== 键盘映射表 ======================

# Qt Key -> ANSI escape sequence 映射
# 用于 TerminalWidget.keyPressEvent 和单元测试
KEY_MAP = {
    "Up":       b"\x1b[A",
    "Down":     b"\x1b[B",
    "Right":    b"\x1b[C",
    "Left":     b"\x1b[D",
    "Home":     b"\x1b[H",
    "End":      b"\x1b[F",
    "Delete":   b"\x1b[3~",
    "PageUp":   b"\x1b[5~",
    "PageDown": b"\x1b[6~",
    "Insert":   b"\x1b[2~",
    "F1":       b"\x1bOP",
    "F2":       b"\x1bOQ",
    "F3":       b"\x1bOR",
    "F4":       b"\x1bOS",
    "F5":       b"\x1b[15~",
    "Return":   b"\r",
    "Backspace": b"\x7f",
    "Tab":      b"\t",
    "Escape":   b"\x1b",
}

# Ctrl 组合键映射
CTRL_KEY_MAP = {
    "C": b"\x03",  # ETX (interrupt)
    "D": b"\x04",  # EOT
    "Z": b"\x1a",  # SUB (suspend)
    "L": b"\x0c",  # FF (clear)
    "A": b"\x01",  # SOH (home)
    "E": b"\x05",  # ENQ (end)
    "K": b"\x0b",  # VT (kill line after cursor)
    "U": b"\x15",  # NAK (kill line before cursor)
    "W": b"\x17",  # ETB (delete word)
}
