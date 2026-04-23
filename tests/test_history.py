"""Tests for connection history persistence in sshcat.main_window."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch

from sshcat.main_window import load_history, save_history, add_history_entry, _MAX_HISTORY


class TestConnectionHistory:
    """连接历史的加载、保存、去重功能测试。"""

    def test_load_empty_when_no_file(self, tmp_path):
        fake_path = tmp_path / "nonexistent.json"
        with patch("sshcat.main_window._HISTORY_PATH", fake_path):
            assert load_history() == []

    def test_save_and_load_roundtrip(self, tmp_path):
        fake_path = tmp_path / "history.json"
        entries = [{"host": "192.168.1.1", "port": 22, "username": "root"}]
        with patch("sshcat.main_window._HISTORY_PATH", fake_path):
            save_history(entries)
            loaded = load_history()
        assert loaded == entries

    def test_add_entry_deduplication(self, tmp_path):
        fake_path = tmp_path / "history.json"
        with patch("sshcat.main_window._HISTORY_PATH", fake_path):
            add_history_entry("10.0.0.1", 22, "admin")
            add_history_entry("10.0.0.2", 22, "root")
            add_history_entry("10.0.0.1", 22, "admin")  # duplicate
            entries = load_history()
        # 去重后应有 2 条，最新在前
        assert len(entries) == 2
        assert entries[0]["host"] == "10.0.0.1"

    def test_max_history_limit(self, tmp_path):
        fake_path = tmp_path / "history.json"
        with patch("sshcat.main_window._HISTORY_PATH", fake_path):
            for i in range(_MAX_HISTORY + 5):
                add_history_entry(f"host-{i}", 22, "user")
            entries = load_history()
        assert len(entries) <= _MAX_HISTORY

    def test_load_corrupted_file(self, tmp_path):
        fake_path = tmp_path / "history.json"
        fake_path.write_text("not valid json!!!", encoding="utf-8")
        with patch("sshcat.main_window._HISTORY_PATH", fake_path):
            assert load_history() == []
