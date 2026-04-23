"""Tests for sshcat.threads — progressive sleep, panel command parsing."""

import pytest

from sshcat.threads import SshReaderThread


# ====================== 渐进式 Sleep ======================

class TestProgressiveSleep:
    """SshReaderThread.calc_sleep 应按阈值返回渐进 sleep 时长。"""

    def test_fast_sleep_at_zero(self):
        assert SshReaderThread.calc_sleep(0) == SshReaderThread.SLEEP_FAST

    def test_fast_sleep_below_threshold(self):
        for i in range(SshReaderThread.IDLE_FAST_THRESHOLD):
            assert SshReaderThread.calc_sleep(i) == SshReaderThread.SLEEP_FAST

    def test_medium_sleep_at_fast_threshold(self):
        assert SshReaderThread.calc_sleep(SshReaderThread.IDLE_FAST_THRESHOLD) == SshReaderThread.SLEEP_MEDIUM

    def test_medium_sleep_below_medium_threshold(self):
        for i in range(SshReaderThread.IDLE_FAST_THRESHOLD, SshReaderThread.IDLE_MEDIUM_THRESHOLD):
            assert SshReaderThread.calc_sleep(i) == SshReaderThread.SLEEP_MEDIUM

    def test_slow_sleep_at_medium_threshold(self):
        assert SshReaderThread.calc_sleep(SshReaderThread.IDLE_MEDIUM_THRESHOLD) == SshReaderThread.SLEEP_SLOW

    def test_slow_sleep_large_count(self):
        assert SshReaderThread.calc_sleep(1000) == SshReaderThread.SLEEP_SLOW

    def test_sleep_values_increasing(self):
        """sleep 时长应 fast < medium < slow"""
        assert SshReaderThread.SLEEP_FAST < SshReaderThread.SLEEP_MEDIUM
        assert SshReaderThread.SLEEP_MEDIUM < SshReaderThread.SLEEP_SLOW

    def test_sleep_values_reasonable(self):
        """所有 sleep 值应在 0 到 100ms 之间"""
        for val in (SshReaderThread.SLEEP_FAST, SshReaderThread.SLEEP_MEDIUM, SshReaderThread.SLEEP_SLOW):
            assert 0 < val < 0.1, f"Sleep value {val} out of range"


# ====================== 阈值配置 ======================

class TestThresholdConfig:
    """阈值应为正整数且 fast < medium。"""

    def test_thresholds_positive(self):
        assert SshReaderThread.IDLE_FAST_THRESHOLD > 0
        assert SshReaderThread.IDLE_MEDIUM_THRESHOLD > 0

    def test_fast_less_than_medium(self):
        assert SshReaderThread.IDLE_FAST_THRESHOLD < SshReaderThread.IDLE_MEDIUM_THRESHOLD
