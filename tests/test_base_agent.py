"""BaseAgent (src/base_agent.py) 单元测试 — kill-switch 风控与主循环。

README "Safety design" 声称: "Hard kill-switch: drawdown limits, stale-data
detection, consecutive-failure halts"。这里逐条验证。
"""

import base_agent as base_agent_module
from base_agent import AgentConfig, AgentState, BaseAgent


class DummyAgent(BaseAgent):
    """最小可运行子类: 返回预设 metrics/actions。"""

    CATEGORY = "rebalancing"

    def __init__(self, config=None, metrics=None, actions=None, raise_on=None):
        super().__init__(config)
        self._metrics = metrics or {}
        self._actions = actions or []
        self._raise_on = set(raise_on or [])

    def fetch_market_data(self):
        return {"timestamp": 0}  # timestamp=0 -> data_age_sec 很大 (见 stale 测试)

    def run_cycle(self):
        if self.state.cycle in self._raise_on:
            raise RuntimeError("injected failure")
        return {"metrics": self._metrics, "actions": self._actions, "notes": "ok"}


class FreshDataAgent(DummyAgent):
    """数据时间戳 = 现在, 不会触发 stale-data 风控。"""

    def fetch_market_data(self):
        import time

        return {"timestamp": time.time()}


# ---------------------------------------------------------------------------
# check_risk 三条硬风控
# ---------------------------------------------------------------------------

class TestCheckRisk:
    def test_drawdown_beyond_limit_trips_kill_switch(self):
        agent = DummyAgent(AgentConfig(kill_switch_loss_pct=5.0))
        ok, reason = agent.check_risk({"drawdown_pct": -6.0})
        assert not ok
        assert "kill-switch" in reason

    def test_drawdown_within_limit_passes(self):
        agent = DummyAgent(AgentConfig(kill_switch_loss_pct=5.0))
        ok, reason = agent.check_risk({"drawdown_pct": -4.9})
        assert ok
        assert reason == "ok"

    def test_stale_data_blocks_decision(self, monkeypatch):
        monkeypatch.setattr(base_agent_module.time, "time", lambda: 1_000_000.0)
        agent = DummyAgent(AgentConfig(max_data_age_sec=180))
        agent.state.data_age_sec = 999.0
        ok, reason = agent.check_risk({})
        assert not ok
        assert "stale data" in reason

    def test_too_many_consecutive_errors_blocks(self):
        agent = DummyAgent(AgentConfig(max_consecutive_errors=5))
        agent.state.error_count = 5
        ok, reason = agent.check_risk({})
        assert not ok
        assert "too many errors" in reason


# ---------------------------------------------------------------------------
# 主循环: kill-switch 触发 -> halted 并停止
# ---------------------------------------------------------------------------

class TestRunLoop:
    def test_run_halts_when_kill_switch_trips(self):
        agent = FreshDataAgent(
            AgentConfig(kill_switch_loss_pct=5.0, cycle_interval_sec=0),
            metrics={"drawdown_pct": -50.0},
        )
        history = agent.run(cycles=3)
        assert agent.state.status == "halted"
        assert agent.state.kill_switch_active is True
        assert "HALTED" in agent.state.notes
        # break 发生在快照落 history 之前 -> halted 轮不产生历史快照
        assert len(history) == 0

    def test_run_stops_after_max_consecutive_errors(self):
        agent = FreshDataAgent(
            AgentConfig(max_consecutive_errors=2, cycle_interval_sec=0),
            raise_on={1, 2, 3},
        )
        agent.run(cycles=5)
        assert agent.state.error_count >= 2
        assert agent.state.status == "error"

    def test_error_counter_resets_on_success(self):
        agent = FreshDataAgent(
            AgentConfig(max_consecutive_errors=3, cycle_interval_sec=0),
            raise_on={1},
        )
        agent.run(cycles=2)
        assert agent.state.error_count == 0  # 第 2 轮成功后清零
        assert agent.state.status == "running"

    def test_actions_surface_in_state(self):
        action = {"type": "REBALANCE", "token_id": 1}
        agent = FreshDataAgent(AgentConfig(cycle_interval_sec=0), actions=[action])
        agent.run(cycles=1)
        assert agent.state.actions == [action]


# ---------------------------------------------------------------------------
# AgentState / ERC-8004 注册
# ---------------------------------------------------------------------------

class TestStateAndRegistration:
    def test_state_to_dict_roundtrip(self):
        state = AgentState(cycle=3, status="running")
        d = state.to_dict()
        assert d["cycle"] == 3
        assert d["status"] == "running"
        assert isinstance(d["metrics"], dict)
        assert isinstance(d["actions"], list)

    def test_registration_file_shape(self):
        agent = DummyAgent(
            AgentConfig(agent_name="rangeguard.agent", service_endpoint="https://example/a2a")
        )
        reg = agent.to_registration_file()
        assert reg["type"].startswith("https://eips.ethereum.org/EIPS/eip-8004")
        assert reg["name"] == "rangeguard.agent"
        assert reg["category"] == "rebalancing"
        assert reg["services"][0]["endpoint"] == "https://example/a2a"
        assert reg["x402Support"] is True

    def test_agent_name_defaults_to_class_name(self):
        agent = DummyAgent()
        assert agent.config.agent_name == "DummyAgent"
