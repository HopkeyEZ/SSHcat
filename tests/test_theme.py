"""Tests for sshcat.theme — color resolution, 256-palette, key mappings."""

import pytest

from sshcat.theme import (
    DRACULA, ANSI_COLORS, resolve_color, _build_256_palette,
    KEY_MAP, CTRL_KEY_MAP,
)


# ====================== resolve_color ======================

class TestResolveColor:
    """resolve_color 应根据输入类型正确解析颜色值。"""

    def test_default_fg(self):
        assert resolve_color("default", is_fg=True) == DRACULA["fg"]

    def test_default_bg(self):
        assert resolve_color("default", is_fg=False) == DRACULA["bg"]

    def test_none_fg(self):
        assert resolve_color(None, is_fg=True) == DRACULA["fg"]

    def test_none_bg(self):
        assert resolve_color(None, is_fg=False) == DRACULA["bg"]

    # 命名颜色
    def test_named_red(self):
        assert resolve_color("red") == ANSI_COLORS["red"]

    def test_named_brightgreen(self):
        assert resolve_color("brightgreen") == ANSI_COLORS["brightgreen"]

    def test_named_all_16_colors(self):
        for name, expected in ANSI_COLORS.items():
            assert resolve_color(name) == expected, f"Mismatch for {name}"

    # 6位 hex RGB 字符串
    def test_hex_rgb_string(self):
        assert resolve_color("ff8800") == "#ff8800"

    def test_hex_rgb_all_zeros(self):
        assert resolve_color("000000") == "#000000"

    def test_hex_rgb_all_f(self):
        assert resolve_color("ffffff") == "#ffffff"

    def test_invalid_hex_string_fallback(self):
        assert resolve_color("zzzzzz", is_fg=True) == DRACULA["fg"]

    def test_short_string_fallback(self):
        assert resolve_color("abc", is_fg=False) == DRACULA["bg"]

    # 256色索引
    def test_index_0_black(self):
        result = resolve_color(0)
        assert result == ANSI_COLORS["black"]

    def test_index_1_red(self):
        assert resolve_color(1) == ANSI_COLORS["red"]

    def test_index_15_brightwhite(self):
        assert resolve_color(15) == ANSI_COLORS["brightwhite"]

    def test_index_16_cube_start(self):
        # 16 = rgb(0,0,0) in 6x6x6 cube → "#000000"
        result = resolve_color(16)
        assert result == "#000000"

    def test_index_231_cube_end(self):
        # 231 = rgb(5,5,5) → "#ffffff" (approx)
        result = resolve_color(231)
        assert result == "#ffffff"

    def test_index_232_grayscale_start(self):
        result = resolve_color(232)
        assert result == "#080808"

    def test_index_255_grayscale_end(self):
        result = resolve_color(255)
        # 232 + 23 = 255, v = 8 + 10*23 = 238
        assert result == "#eeeeee"

    def test_index_out_of_range_fallback(self):
        assert resolve_color(256, is_fg=True) == DRACULA["fg"]
        assert resolve_color(-1, is_fg=False) == DRACULA["bg"]

    # 不支持的类型
    def test_unsupported_type_fallback(self):
        assert resolve_color(3.14, is_fg=True) == DRACULA["fg"]


# ====================== 256-palette ======================

class TestPalette256:
    """_build_256_palette 应返回 256 个有效 hex 色值。"""

    def test_palette_length(self):
        palette = _build_256_palette()
        assert len(palette) == 256

    def test_palette_all_hex(self):
        palette = _build_256_palette()
        for i, color in enumerate(palette):
            assert color.startswith("#"), f"Index {i}: {color} is not hex"
            assert len(color) == 7, f"Index {i}: {color} wrong length"

    def test_palette_first_16_match_ansi(self):
        palette = _build_256_palette()
        std_names = ["black", "red", "green", "brown", "blue", "magenta", "cyan", "white"]
        bright_names = ["brightblack", "brightred", "brightgreen", "brightyellow",
                        "brightblue", "brightmagenta", "brightcyan", "brightwhite"]
        for i, name in enumerate(std_names + bright_names):
            assert palette[i] == ANSI_COLORS[name], f"Index {i} ({name}) mismatch"

    def test_palette_is_cached(self):
        p1 = _build_256_palette()
        p2 = _build_256_palette()
        assert p1 is p2  # 同一个对象，缓存生效


# ====================== KEY_MAP ======================

class TestKeyMap:
    """KEY_MAP 和 CTRL_KEY_MAP 应包含正确的 ANSI 转义序列。"""

    def test_arrow_keys(self):
        assert KEY_MAP["Up"] == b"\x1b[A"
        assert KEY_MAP["Down"] == b"\x1b[B"
        assert KEY_MAP["Right"] == b"\x1b[C"
        assert KEY_MAP["Left"] == b"\x1b[D"

    def test_navigation_keys(self):
        assert KEY_MAP["Home"] == b"\x1b[H"
        assert KEY_MAP["End"] == b"\x1b[F"
        assert KEY_MAP["PageUp"] == b"\x1b[5~"
        assert KEY_MAP["PageDown"] == b"\x1b[6~"
        assert KEY_MAP["Delete"] == b"\x1b[3~"
        assert KEY_MAP["Insert"] == b"\x1b[2~"

    def test_function_keys(self):
        assert KEY_MAP["F1"] == b"\x1bOP"
        assert KEY_MAP["F2"] == b"\x1bOQ"
        assert KEY_MAP["F3"] == b"\x1bOR"
        assert KEY_MAP["F4"] == b"\x1bOS"
        assert KEY_MAP["F5"] == b"\x1b[15~"

    def test_basic_keys(self):
        assert KEY_MAP["Return"] == b"\r"
        assert KEY_MAP["Backspace"] == b"\x7f"
        assert KEY_MAP["Tab"] == b"\t"
        assert KEY_MAP["Escape"] == b"\x1b"

    def test_ctrl_c_interrupt(self):
        assert CTRL_KEY_MAP["C"] == b"\x03"

    def test_ctrl_d_eof(self):
        assert CTRL_KEY_MAP["D"] == b"\x04"

    def test_ctrl_z_suspend(self):
        assert CTRL_KEY_MAP["Z"] == b"\x1a"

    def test_ctrl_l_clear(self):
        assert CTRL_KEY_MAP["L"] == b"\x0c"

    def test_ctrl_readline_shortcuts(self):
        assert CTRL_KEY_MAP["A"] == b"\x01"  # Home
        assert CTRL_KEY_MAP["E"] == b"\x05"  # End
        assert CTRL_KEY_MAP["K"] == b"\x0b"  # Kill after cursor
        assert CTRL_KEY_MAP["U"] == b"\x15"  # Kill before cursor
        assert CTRL_KEY_MAP["W"] == b"\x17"  # Delete word

    def test_all_key_map_values_are_bytes(self):
        for k, v in KEY_MAP.items():
            assert isinstance(v, bytes), f"KEY_MAP[{k}] is not bytes"
        for k, v in CTRL_KEY_MAP.items():
            assert isinstance(v, bytes), f"CTRL_KEY_MAP[{k}] is not bytes"
