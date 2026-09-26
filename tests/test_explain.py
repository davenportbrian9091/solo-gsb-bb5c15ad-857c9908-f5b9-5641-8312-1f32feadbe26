"""实时分段入选解释接口测试。夹具 client 见 conftest.py。"""
from __future__ import annotations


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


def explain(client, runner):
    return client.get(f"/api/runners/{runner}/splits/explain")


def station(data, seq):
    return next(s for s in data["stations"] if s["station_seq"] == seq)


def by_event(st):
    return {c["event_no"]: c for c in st["checkins"]}


# ---------- 基本行为 ----------

def test_explain_404_for_unknown_runner(client):
    assert explain(client, "NOBODY").status_code == 404


def test_explain_lists_all_checkins_by_station(client):
    data = explain(client, "R001").json()
    assert data["runner_id"] == "R001"
    assert data["station_count"] == 6
    # 每站恰好一条打卡且全部入选，按站点升序列出
    assert [s["station_seq"] for s in data["stations"]] == [1, 2, 3, 4, 5, 6]
    for st in data["stations"]:
        (c,) = st["checkins"]
        assert c["event_no"] == f"S-R001-{st['station_seq']}"
        assert c["status"] == "active"
        assert c["selected"] is True
        assert c["reason"] is None
        assert c["occurred_at"].startswith("2026-09-25T")


def test_explain_splits_match_live_splits(client):
    # 解释中的分段与实时分段接口结果一致（同一次数据库读取）
    for runner in ("R001", "R003", "R004", "R005", "R006"):
        data = explain(client, runner).json()
        assert data["splits"] == client.get(f"/api/runners/{runner}/splits").json()


# ---------- 未入选原因 ----------

def test_explain_superseded_reason(client):
    # 种子数据：R004 第 2 站的 S-R004-2X 已被 S-R004-2 更正
    st2 = station(explain(client, "R004").json(), 2)
    events = by_event(st2)
    assert events["S-R004-2"]["selected"] is True
    old = events["S-R004-2X"]
    assert old["selected"] is False
    assert old["status"] == "superseded"
    assert "已更正" in old["reason"]
    assert "S-R004-2" in old["reason"]


def test_explain_revoked_reason(client):
    st2 = station(explain(client, "R005").json(), 2)
    (c,) = st2["checkins"]
    assert c["event_no"] == "S-R005-2"
    assert c["selected"] is False
    assert c["status"] == "revoked"
    assert "已撤销" in c["reason"]


def test_explain_later_checkin_not_selected(client):
    post(client, "E-1", "TE1", 1, "2026-09-25T08:00:00")
    post(client, "E-2a", "TE1", 2, "2026-09-25T09:00:00")
    post(client, "E-2b", "TE1", 2, "2026-09-25T08:40:00")  # 更早，入选
    st2 = station(explain(client, "TE1").json(), 2)
    events = by_event(st2)
    assert events["E-2b"]["selected"] is True
    later = events["E-2a"]
    assert later["selected"] is False
    assert later["status"] == "active"
    assert "较晚" in later["reason"]


def test_explain_tie_broken_by_event_no(client):
    post(client, "T-1", "TE2", 1, "2026-09-25T08:00:00")
    post(client, "T-2b", "TE2", 2, "2026-09-25T08:30:00")
    post(client, "T-2a", "TE2", 2, "2026-09-25T08:30:00")  # 同时间，事件号更小
    data = explain(client, "TE2").json()
    events = by_event(station(data, 2))
    assert events["T-2a"]["selected"] is True
    tie = events["T-2b"]
    assert tie["selected"] is False
    assert tie["status"] == "active"
    assert "同时间平局" in tie["reason"]
    # 分段采用同一条入选事件，且与实时分段接口一致
    live = client.get("/api/runners/TE2/splits").json()
    st2_live = next(s for s in live["stations"] if s["station_seq"] == 2)
    assert st2_live["event_no"] == "T-2a"
    assert data["splits"] == live


# ---------- 更正 / 撤销 / 补传即时更新 ----------

def test_explain_updates_immediately_after_correction_revoke_backfill(client):
    post(client, "U-1", "TE3", 1, "2026-09-25T08:00:00")
    post(client, "U-2X", "TE3", 2, "2026-09-25T12:00:00")

    # 更正后：原事件标记已更正，新事件入选
    post(client, "U-2", "TE3", 2, "2026-09-25T08:30:00", corrects="U-2X")
    events = by_event(station(explain(client, "TE3").json(), 2))
    assert events["U-2"]["selected"] is True
    assert "已更正" in events["U-2X"]["reason"]

    # 撤销入选事件后：该站无入选打卡，分段 1→2 待定
    client.post("/api/checkins/U-2/revoke")
    data = explain(client, "TE3").json()
    st2 = station(data, 2)
    assert all(not c["selected"] for c in st2["checkins"])
    assert all(c["reason"] for c in st2["checkins"])
    seg12 = next(s for s in data["splits"]["segments"] if s["from_station"] == 1)
    assert seg12["status"] == "pending"

    # 补传后即时恢复入选与分段
    post(client, "U-2b", "TE3", 2, "2026-09-25T08:35:00")
    data = explain(client, "TE3").json()
    events = by_event(station(data, 2))
    assert events["U-2b"]["selected"] is True
    seg12 = next(s for s in data["splits"]["segments"] if s["from_station"] == 1)
    assert seg12["status"] == "ok"
    assert seg12["duration_seconds"] == 35 * 60
    assert data["splits"] == client.get("/api/runners/TE3/splits").json()
