"""接力队成绩核算测试。夹具 client 见 conftest.py。"""
from __future__ import annotations

import pytest


def post(client, event_no, runner, station, occurred_at, corrects=None):
    body = {
        "event_no": event_no,
        "runner_id": runner,
        "station_seq": station,
        "occurred_at": occurred_at,
    }
    if corrects is not None:
        body["corrects"] = corrects
    return client.post("/api/checkins", json=body)


def add_leg_runner(client, runner_id, leg_no, start_t, finish_t):
    """登记一名只在 {leg_no, leg_no+1} 两站有打卡的选手（恰好支撑第 leg_no 棒）。"""
    post(client, f"{runner_id}-S{leg_no}", runner_id, leg_no, start_t)
    post(
        client,
        f"{runner_id}-S{leg_no + 1}",
        runner_id,
        leg_no + 1,
        finish_t,
    )


# 五名选手的完整接力编排：棒次用时 30/45/45/50/65 分钟，
# 交接等待 5/5/0/5 分钟；首棒 08:00 出发，末棒 12:10 到达。
HAPPY_TEAM = ["T1", "T2", "T3", "T4", "T5"]
HAPPY_TIMES = {
    "T1": (1, "2026-09-25T08:00:00", "2026-09-25T08:30:00"),
    "T2": (2, "2026-09-25T08:35:00", "2026-09-25T09:20:00"),
    "T3": (3, "2026-09-25T09:25:00", "2026-09-25T10:10:00"),
    "T4": (4, "2026-09-25T10:10:00", "2026-09-25T11:00:00"),
    "T5": (5, "2026-09-25T11:05:00", "2026-09-25T12:10:00"),
}


def seed_happy_team(client):
    for runner_id, (leg_no, start_t, finish_t) in HAPPY_TIMES.items():
        add_leg_runner(client, runner_id, leg_no, start_t, finish_t)


def relay(client, version, runner_ids):
    return client.post(
        "/api/relay-teams", json={"version": version, "runner_ids": runner_ids}
    )


# ---------- 请求校验 ----------


def test_version_missing_returns_404(client):
    r = relay(client, 999, ["R001"] * 5)
    assert r.status_code == 404
    assert "不存在" in r.json()["detail"]


def test_version_0_returns_404_not_422(client):
    # 版本不存在优先于人数 / 重复校验
    assert relay(client, 0, []).status_code == 404


@pytest.mark.parametrize("runner_ids", [[], ["R001"] * 4, ["R001"] * 6])
def test_wrong_count_returns_422(client, runner_ids):
    version = client.post("/api/publications").json()["version"]
    r = relay(client, version, runner_ids)
    assert r.status_code == 422


def test_duplicate_runner_returns_422(client):
    version = client.post("/api/publications").json()["version"]
    r = relay(client, version, ["R001", "R002", "R002", "R004", "R006"])
    assert r.status_code == 422
    assert "重复" in r.json()["detail"]


def test_count_uses_station_count_from_snapshot(client, monkeypatch):
    """人数以版本快照的 station_count 为准，而非当前 STATION_COUNT 配置。"""
    version = client.post("/api/publications").json()["version"]
    monkeypatch.setenv("STATION_COUNT", "4")  # 发布后配置变化不影响核算
    assert relay(client, version, ["R001"] * 3).status_code == 422
    # 快照仍是 6 站：5 名不同选手即人数合法（R003/R006 有 pending 分段，不影响人数校验）
    assert relay(client, version, ["R001", "R002", "R003", "R004", "R006"]).status_code == 200


# ---------- 正常核算 ----------


def test_happy_path_totals_equal_legs_plus_waits(client):
    seed_happy_team(client)
    version = client.post("/api/publications").json()["version"]

    r = relay(client, version, HAPPY_TEAM)
    assert r.status_code == 200
    data = r.json()
    assert data["version"] == version
    assert data["station_count"] == 6
    assert data["runner_ids"] == HAPPY_TEAM
    assert data["complete"] is True

    legs = data["legs"]
    assert [(l["leg"], l["runner_id"], l["from_station"], l["to_station"]) for l in legs] == [
        (1, "T1", 1, 2),
        (2, "T2", 2, 3),
        (3, "T3", 3, 4),
        (4, "T4", 4, 5),
        (5, "T5", 5, 6),
    ]
    assert [l["duration_seconds"] for l in legs] == [1800, 2700, 2700, 3000, 3900]
    assert [l["duration"] for l in legs] == [
        "0:30:00",
        "0:45:00",
        "0:45:00",
        "0:50:00",
        "1:05:00",
    ]
    assert all(l["reason"] is None for l in legs)
    # 逐棒两端入选打卡事件号 / 时间取自版本快照
    assert legs[0]["start_event_no"] == "T1-S1"
    assert legs[0]["start_at"].startswith("2026-09-25T08:00:00")
    assert legs[0]["finish_event_no"] == "T1-S2"
    assert legs[-1]["finish_event_no"] == "T5-S6"
    assert legs[-1]["finish_at"].startswith("2026-09-25T12:10:00")

    handovers = data["handovers"]
    assert [h["station_seq"] for h in handovers] == [2, 3, 4, 5]
    assert [h["wait_seconds"] for h in handovers] == [300, 300, 0, 300]
    assert [h["wait"] for h in handovers] == [
        "0:05:00",
        "0:05:00",
        "0:00:00",
        "0:05:00",
    ]
    assert all(h["reason"] is None for h in handovers)
    assert [h["incoming_runner_id"] for h in handovers] == HAPPY_TEAM[:-1]
    assert [h["outgoing_runner_id"] for h in handovers] == HAPPY_TEAM[1:]

    # 队伍总用时 = 各棒用时 + 全部等待 = 首棒起点至末棒终点 = 4:10:00
    assert data["total_seconds"] == 1800 + 2700 + 2700 + 3000 + 3900 + 900
    assert data["total_seconds"] == 4 * 3600 + 10 * 60
    assert data["total"] == "4:10:00"


def test_zero_wait_handover_allowed(client):
    # 全部交接站同时到达 / 出发（等待 0）：合法，不视为出发过早
    for runner_id, leg_no in zip(["W1", "W2", "W3", "W4", "W5"], range(1, 6)):
        start = {1: "08:00", 2: "08:30", 3: "09:20", 4: "10:10", 5: "11:00"}[leg_no]
        finish = {1: "08:30", 2: "09:20", 3: "10:10", 4: "11:00", 5: "12:00"}[leg_no]
        add_leg_runner(
            client,
            runner_id,
            leg_no,
            f"2026-09-25T{start}:00",
            f"2026-09-25T{finish}:00",
        )
    version = client.post("/api/publications").json()["version"]
    data = relay(client, version, ["W1", "W2", "W3", "W4", "W5"]).json()
    assert data["complete"] is True
    assert [h["wait_seconds"] for h in data["handovers"]] == [0, 0, 0, 0]
    assert data["total_seconds"] == 4 * 3600
    assert data["total"] == "4:00:00"


# ---------- 待定场景 ----------


def test_runner_not_in_version_marks_team_pending(client):
    version = client.post("/api/publications").json()["version"]
    # R009 在该版本中没有任何打卡记录；其余为种子完整选手
    r = relay(client, version, ["R001", "R002", "R009", "R004", "R006"])
    assert r.status_code == 200
    data = r.json()
    assert data["complete"] is False
    assert data["total_seconds"] is None
    assert data["total"] is None

    third = data["legs"][2]
    assert third["runner_id"] == "R009"
    assert third["duration_seconds"] is None
    assert third["duration"] is None
    assert third["start_event_no"] is None
    assert third["finish_event_no"] is None
    assert third["start_at"] is None
    assert "不在版本" in third["reason"]

    # 与该棒相邻的交接站因缺少一端打卡无法核算
    h3, h4 = data["handovers"][1], data["handovers"][2]
    assert h3["station_seq"] == 3 and h4["station_seq"] == 4
    assert h3["wait_seconds"] is None and h4["wait_seconds"] is None
    assert "缺少" in h3["reason"] and "缺少" in h4["reason"]
    # 不相关的棒次仍给出快照中的用时
    assert data["legs"][0]["duration_seconds"] == 32 * 60
    assert data["legs"][0]["reason"] is None


def test_pending_segment_in_snapshot_marks_team_pending(client):
    version = client.post("/api/publications").json()["version"]
    # 种子 R003 缺第 4 站：分段 3→4、4→5 pending；令其承担第 3 棒（3→4）
    r = relay(client, version, ["R001", "R002", "R003", "R004", "R006"])
    assert r.status_code == 200
    data = r.json()
    assert data["complete"] is False
    assert data["total_seconds"] is None

    third = data["legs"][2]
    assert third["runner_id"] == "R003"
    assert third["duration_seconds"] is None
    assert third["duration"] is None
    assert "待定" in third["reason"]
    assert "站点 4 缺少有效打卡" in third["reason"]
    # R003 在站 3 有打卡、站 4 没有：仍回显可用的一端
    assert third["start_event_no"] == "S-R003-3"
    assert third["start_at"].startswith("2026-09-25T09:35:00")
    assert third["finish_event_no"] is None
    # 站 3 交接：R002 09:15 到、R003 09:35 发，分段 pending 不影响交接本身
    assert data["handovers"][1]["wait_seconds"] == 20 * 60


def test_handover_departure_before_arrival_marks_team_pending(client):
    # 交接站 2：下一棒 09:00 从站 2 出发，早于上一棒 09:10 到站 2；其余交接正常
    add_leg_runner(client, "U1", 1, "2026-09-25T08:00:00", "2026-09-25T09:10:00")
    add_leg_runner(client, "U2", 2, "2026-09-25T09:00:00", "2026-09-25T09:40:00")
    add_leg_runner(client, "U3", 3, "2026-09-25T09:45:00", "2026-09-25T10:20:00")
    add_leg_runner(client, "U4", 4, "2026-09-25T10:25:00", "2026-09-25T11:00:00")
    add_leg_runner(client, "U5", 5, "2026-09-25T11:05:00", "2026-09-25T11:50:00")
    version = client.post("/api/publications").json()["version"]

    r = relay(client, version, ["U1", "U2", "U3", "U4", "U5"])
    assert r.status_code == 200
    data = r.json()
    assert data["complete"] is False
    assert data["total_seconds"] is None

    h2 = data["handovers"][0]  # 站 2：U1 09:10 到，U2 09:00 发
    assert h2["station_seq"] == 2
    assert h2["wait_seconds"] is None
    assert h2["wait"] is None
    assert h2["incoming_runner_id"] == "U1"
    assert h2["outgoing_runner_id"] == "U2"
    assert "早于" in h2["reason"]

    # 时间倒流的是下一棒（第 2 棒）：棒次待定、不给用时，即使其快照分段本身有效
    second = data["legs"][1]
    assert second["runner_id"] == "U2"
    assert second["duration_seconds"] is None
    assert second["duration"] is None
    assert "早于" in second["reason"]
    # 上一棒本身分段有效，仍给出用时；其他交接站不受影响
    assert data["legs"][0]["duration_seconds"] == 70 * 60
    assert data["handovers"][1]["wait_seconds"] == 300  # 站 3 正常等待 5 分钟
    assert data["handovers"][2]["wait_seconds"] == 300  # 站 4 正常等待 5 分钟
    assert data["handovers"][3]["wait_seconds"] == 300  # 站 5 正常等待 5 分钟
    assert all("早于" not in (h["reason"] or "") for h in data["handovers"][1:])


# ---------- 快照一致性 ----------


def test_relay_reads_snapshot_not_live_results(client):
    add_leg_runner(client, "V1", 1, "2026-09-25T08:00:00", "2026-09-25T08:30:00")
    version = client.post("/api/publications").json()["version"]

    # 发布后补传 V1 的其余站点，并新增发布后才出现的选手 V9（只影响实时成绩）
    for i, t in enumerate(
        [
            "2026-09-25T09:10:00",
            "2026-09-25T09:50:00",
            "2026-09-25T10:40:00",
            "2026-09-25T11:20:00",
        ],
        start=3,
    ):
        post(client, f"V1-{i}", "V1", i, t)
    add_leg_runner(client, "V9", 3, "2026-09-25T09:20:00", "2026-09-25T10:00:00")

    r = relay(client, version, ["V1", "R002", "V9", "R004", "R006"])
    assert r.status_code == 200
    data = r.json()
    # V9 不在该版本快照内：即使实时表中已存在，仍视为不在版本
    assert "不在版本" in data["legs"][2]["reason"]
    # V1 第 1 棒两端打卡仍是发布时的快照值，不受补传影响
    first = data["legs"][0]
    assert first["duration_seconds"] == 30 * 60
    assert first["start_event_no"] == "V1-S1"
    assert first["finish_event_no"] == "V1-S2"


def test_repeated_requests_are_stable(client):
    seed_happy_team(client)
    version = client.post("/api/publications").json()["version"]
    payload = {"version": version, "runner_ids": HAPPY_TEAM}
    first = client.post("/api/relay-teams", json=payload).json()
    second = client.post("/api/relay-teams", json=payload).json()
    assert first == second
