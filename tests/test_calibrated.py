"""设备时钟校准试算接口测试。共享夹具 client 见 conftest.py。"""
from __future__ import annotations

from datetime import datetime, timezone


def calibrated(client, runner, windows):
    return client.post(
        f"/api/runners/{runner}/splits/calibrated", json={"windows": windows}
    )


def win(station, start, end, offset):
    return {
        "station_seq": station,
        "start": start,
        "end": end,
        "offset_seconds": offset,
    }


def segment(data, frm, to):
    return next(
        s for s in data["segments"]
        if s["from_station"] == frm and s["to_station"] == to
    )


def station_entry(data, seq):
    return next(s for s in data["stations"] if s["station_seq"] == seq)


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


# 种子选手 R001：08:00 / 08:32 / 09:12 / 09:55 / 10:45 / 11:30 UTC，
# 分段 1920 / 2400 / 2580 / 3000 / 2700 秒，总计 12600 秒。

def test_calibrated_negative_offset_shifts_first_station(client):
    # 站点 1 设备快了 10 分钟：[08:00,09:00) 内打卡 -600 秒
    r = calibrated(client, "R001", [
        win(1, "2026-09-25T08:00:00", "2026-09-25T09:00:00", -600),
    ])
    assert r.status_code == 200
    data = r.json()
    assert data["complete"] is True

    s1 = station_entry(data, 1)
    assert s1["event_no"] == "S-R001-1"
    assert datetime.fromisoformat(s1["occurred_at"]) == datetime(
        2026, 9, 25, 8, 0, tzinfo=timezone.utc
    )
    assert datetime.fromisoformat(s1["calibrated_at"]) == datetime(
        2026, 9, 25, 7, 50, tzinfo=timezone.utc
    )
    # 未命中窗口的站点保持原始时间
    s2 = station_entry(data, 2)
    assert s2["calibrated_at"] == s2["occurred_at"]

    # 1→2：08:32 - 07:50 = 42 分钟；其余分段不变；总用时增加 10 分钟
    assert segment(data, 1, 2)["duration_seconds"] == 42 * 60
    assert segment(data, 2, 3)["duration_seconds"] == 2400
    assert data["total_seconds"] == 12600 + 600
    assert data["total"] == "3:40:00"


def test_calibrated_window_is_half_open(client):
    # 终点开：站 2 打卡恰为 08:32，窗口 [08:00,08:32) 不命中
    r = calibrated(client, "R001", [
        win(2, "2026-09-25T08:00:00", "2026-09-25T08:32:00", 60),
    ])
    data = r.json()
    s2 = station_entry(data, 2)
    assert s2["calibrated_at"] == s2["occurred_at"]
    assert segment(data, 1, 2)["duration_seconds"] == 1920

    # 起点闭：窗口 [08:32,09:00) 命中 08:32
    r = calibrated(client, "R001", [
        win(2, "2026-09-25T08:32:00", "2026-09-25T09:00:00", 60),
    ])
    data = r.json()
    assert datetime.fromisoformat(station_entry(data, 2)["calibrated_at"]) == datetime(
        2026, 9, 25, 8, 33, tzinfo=timezone.utc
    )
    assert segment(data, 1, 2)["duration_seconds"] == 33 * 60
    assert segment(data, 2, 3)["duration_seconds"] == 39 * 60
    # 仅中间站点平移，总用时不变
    assert data["total_seconds"] == 12600


def test_calibrated_adjacent_windows_each_matched(client):
    # 同站两窗相接：[08:00,08:32) -60 与 [08:32,09:00) +120，允许
    r = calibrated(client, "R001", [
        win(2, "2026-09-25T08:32:00", "2026-09-25T09:00:00", 120),
        win(1, "2026-09-25T08:00:00", "2026-09-25T08:32:00", -60),
    ])
    assert r.status_code == 200
    data = r.json()
    # 回显归一化（UTC）且按站点、起点升序
    assert [w["station_seq"] for w in data["windows"]] == [1, 2]
    assert datetime.fromisoformat(station_entry(data, 1)["calibrated_at"]) == datetime(
        2026, 9, 25, 7, 59, tzinfo=timezone.utc
    )
    assert datetime.fromisoformat(station_entry(data, 2)["calibrated_at"]) == datetime(
        2026, 9, 25, 8, 34, tzinfo=timezone.utc
    )
    # 08:34 - 07:59 = 35 分钟
    assert segment(data, 1, 2)["duration_seconds"] == 35 * 60


def test_calibrated_reselects_earliest_after_shift(client):
    # 同站两条 active：A=08:40、B=08:50，实时取 A；
    # 窗口使 A +15 分钟 -> 08:55，B 不命中保持 08:50 -> 改选 B
    post(client, "CA-1", "TC1", 1, "2026-09-25T08:00:00")
    post(client, "CA-2A", "TC1", 2, "2026-09-25T08:40:00")
    post(client, "CA-2B", "TC1", 2, "2026-09-25T08:50:00")
    post(client, "CA-3", "TC1", 3, "2026-09-25T09:00:00")

    r = calibrated(client, "TC1", [
        win(2, "2026-09-25T08:30:00", "2026-09-25T08:45:00", 900),
    ])
    data = r.json()
    s2 = station_entry(data, 2)
    assert s2["event_no"] == "CA-2B"
    assert datetime.fromisoformat(s2["occurred_at"]) == datetime(
        2026, 9, 25, 8, 50, tzinfo=timezone.utc
    )
    assert datetime.fromisoformat(s2["calibrated_at"]) == datetime(
        2026, 9, 25, 8, 50, tzinfo=timezone.utc
    )
    assert segment(data, 1, 2)["duration_seconds"] == 50 * 60
    assert segment(data, 2, 3)["duration_seconds"] == 10 * 60

    # 校准后 A、B 同为 08:50：平局按事件号取较小者 A
    r = calibrated(client, "TC1", [
        win(2, "2026-09-25T08:30:00", "2026-09-25T08:45:00", 600),
    ])
    data = r.json()
    s2 = station_entry(data, 2)
    assert s2["event_no"] == "CA-2A"
    assert datetime.fromisoformat(s2["occurred_at"]) == datetime(
        2026, 9, 25, 8, 40, tzinfo=timezone.utc
    )
    assert datetime.fromisoformat(s2["calibrated_at"]) == datetime(
        2026, 9, 25, 8, 50, tzinfo=timezone.utc
    )


def test_calibrated_fixes_time_regression(client):
    # R006 第 3 站 09:10 早于第 2 站 09:30，实时 2→3 倒退待定；
    # 站 3 +30 分钟 -> 09:40，倒退消除，全部完整
    r = calibrated(client, "R006", [
        win(3, "2026-09-25T09:00:00", "2026-09-25T10:00:00", 1800),
    ])
    data = r.json()
    assert data["complete"] is True
    s23 = segment(data, 2, 3)
    assert s23["status"] == "ok"
    assert s23["duration_seconds"] == 10 * 60
    assert station_entry(data, 3)["event_no"] == "S-R006-3"
    assert datetime.fromisoformat(station_entry(data, 3)["calibrated_at"]) == datetime(
        2026, 9, 25, 9, 40, tzinfo=timezone.utc
    )
    # 08:20 -> 12:00 = 220 分钟
    assert data["total_seconds"] == 220 * 60


def test_calibrated_over_correction_creates_regression(client):
    # 站 2 +2 小时 -> 10:32 晚于站 3 的 09:12：2→3 倒退待定，总用时置空
    r = calibrated(client, "R001", [
        win(2, "2026-09-25T08:30:00", "2026-09-25T09:00:00", 7200),
    ])
    data = r.json()
    assert data["complete"] is False
    assert data["total_seconds"] is None
    assert data["total"] is None
    s23 = segment(data, 2, 3)
    assert s23["status"] == "pending"
    assert "时间倒退" in s23["reason"]
    # 倒退之外的分段照常计算
    assert segment(data, 1, 2)["status"] == "ok"
    assert segment(data, 3, 4)["status"] == "ok"


def test_calibrated_missing_station_stays_pending(client):
    # R003 缺第 4 站：校准不补缺站，3→4、4→5 仍待定
    r = calibrated(client, "R003", [
        win(3, "2026-09-25T09:00:00", "2026-09-25T10:00:00", 300),
    ])
    data = r.json()
    assert data["complete"] is False
    assert data["total_seconds"] is None
    for frm, to in [(3, 4), (4, 5)]:
        seg = segment(data, frm, to)
        assert seg["status"] == "pending"
        assert "站点 4" in seg["reason"]
        assert seg["duration_seconds"] is None


def test_calibrated_superseded_checkin_ignored(client):
    # R004 站 2：12:30 的错误打卡已被 08:40 的更正事件取代；
    # 命中已取代事件时间的窗口不得让它重新参与
    r = calibrated(client, "R004", [
        win(2, "2026-09-25T12:00:00", "2026-09-25T13:00:00", 6000),
        win(2, "2026-09-25T08:30:00", "2026-09-25T09:00:00", 0),
    ])
    data = r.json()
    s2 = station_entry(data, 2)
    assert s2["event_no"] == "S-R004-2"
    assert s2["calibrated_at"] == s2["occurred_at"]
    assert {s["event_no"] for s in data["stations"]} >= {"S-R004-2"}
    assert all(s["event_no"] != "S-R004-2X" for s in data["stations"])
    # 成绩与实时一致（README 中 R004 总用时 13680 秒）
    assert data["total_seconds"] == 13680


def test_calibrated_windows_normalized_to_utc(client):
    # +08:00 的窗口先转 UTC 再匹配：16:32+08 = 08:32 UTC
    r = calibrated(client, "R001", [
        win(2, "2026-09-25T16:32:00+08:00", "2026-09-25T17:00:00+08:00", 60),
    ])
    data = r.json()
    w = data["windows"][0]
    assert datetime.fromisoformat(w["start"]) == datetime(
        2026, 9, 25, 8, 32, tzinfo=timezone.utc
    )
    assert datetime.fromisoformat(w["end"]) == datetime(
        2026, 9, 25, 9, 0, tzinfo=timezone.utc
    )
    assert w["offset_seconds"] == 60
    assert segment(data, 1, 2)["duration_seconds"] == 33 * 60


def test_calibrated_rejects_station_out_of_range(client):
    r = calibrated(client, "R001", [
        win(7, "2026-09-25T08:00:00", "2026-09-25T09:00:00", 60),
    ])
    assert r.status_code == 422
    assert "站点" in r.json()["detail"]


def test_calibrated_rejects_end_not_after_start(client):
    r = calibrated(client, "R001", [
        win(1, "2026-09-25T09:00:00", "2026-09-25T08:00:00", 60),
    ])
    assert r.status_code == 422
    assert "校准窗" in r.json()["detail"]
    r = calibrated(client, "R001", [
        win(1, "2026-09-25T08:00:00", "2026-09-25T08:00:00", 60),
    ])
    assert r.status_code == 422


def test_calibrated_rejects_overlap_same_station(client):
    # 同站重叠（含完全相同）拒绝；不同站重叠允许
    r = calibrated(client, "R001", [
        win(2, "2026-09-25T08:30:00", "2026-09-25T08:45:00", 60),
        win(2, "2026-09-25T08:40:00", "2026-09-25T09:00:00", 120),
    ])
    assert r.status_code == 422
    assert "站点 2" in r.json()["detail"]

    r = calibrated(client, "R001", [
        win(2, "2026-09-25T08:30:00", "2026-09-25T08:45:00", 60),
        win(2, "2026-09-25T08:30:00", "2026-09-25T08:45:00", 120),
    ])
    assert r.status_code == 422

    r = calibrated(client, "R001", [
        win(1, "2026-09-25T08:30:00", "2026-09-25T08:45:00", 60),
        win(2, "2026-09-25T08:30:00", "2026-09-25T08:45:00", 120),
    ])
    assert r.status_code == 200


def test_calibrated_rejects_too_many_windows_and_bad_offset(client):
    r = calibrated(
        client, "R001",
        [win(1, "2026-09-25T08:00:00", "2026-09-25T09:00:00", 0)] * 101,
    )
    assert r.status_code == 422

    r = calibrated(client, "R001", [
        win(1, "2026-09-25T08:00:00", "2026-09-25T09:00:00", 1.5),
    ])
    assert r.status_code == 422


def test_calibrated_unknown_runner_404(client):
    r = calibrated(client, "NOBODY", [])
    assert r.status_code == 404


def test_calibrated_empty_windows_matches_live_splits(client):
    r = calibrated(client, "R001", [])
    assert r.status_code == 200
    data = r.json()
    live = client.get("/api/runners/R001/splits").json()

    assert data["windows"] == []
    assert data["complete"] == live["complete"]
    assert data["total_seconds"] == live["total_seconds"] == 12600
    assert data["total"] == live["total"]
    assert data["segments"] == live["segments"]
    # 入选事件一致，且校准时间全部等于原始时间
    assert [(s["station_seq"], s["event_no"]) for s in data["stations"]] == [
        (s["station_seq"], s["event_no"]) for s in live["stations"]
    ]
    for s in data["stations"]:
        assert s["calibrated_at"] == s["occurred_at"]


def test_calibrated_has_no_side_effects(client):
    # 发布 v1 后试算：实时成绩、打卡记录、已发布版本均不受影响
    pub = client.post("/api/publications")
    assert pub.status_code == 201 and pub.json()["version"] == 1
    splits_before = client.get("/api/runners/R006/splits").json()
    checkins_before = client.get("/api/checkins", params={"runner_id": "R006"}).json()

    r = calibrated(client, "R006", [
        win(3, "2026-09-25T09:00:00", "2026-09-25T10:00:00", 1800),
    ])
    assert r.status_code == 200
    assert r.json()["complete"] is True

    assert client.get("/api/runners/R006/splits").json() == splits_before
    assert client.get("/api/checkins", params={"runner_id": "R006"}).json() == checkins_before
    latest = client.get("/api/publications/latest").json()
    assert latest["version"] == 1
    assert latest == pub.json()


def test_calibrated_reflects_latest_checkins(client):
    post(client, "CL-1", "TC2", 1, "2026-09-25T08:00:00")
    post(client, "CL-2", "TC2", 2, "2026-09-25T08:40:00")
    post(client, "CL-3", "TC2", 3, "2026-09-25T09:00:00")
    windows = [win(2, "2026-09-25T08:30:00", "2026-09-25T09:00:00", 300)]

    data = calibrated(client, "TC2", windows).json()
    assert segment(data, 1, 2)["duration_seconds"] == 45 * 60

    # 撤销第 2 站后：1→2、2→3 变为待定
    client.post("/api/checkins/CL-2/revoke")
    data = calibrated(client, "TC2", windows).json()
    assert segment(data, 1, 2)["status"] == "pending"
    assert segment(data, 2, 3)["status"] == "pending"
    assert data["stations"] == [] or all(s["station_seq"] != 2 for s in data["stations"])

    # 补传第 2 站 08:35：+5 分钟 -> 08:40，分段即时反映
    post(client, "CL-2b", "TC2", 2, "2026-09-25T08:35:00")
    data = calibrated(client, "TC2", windows).json()
    assert station_entry(data, 2)["event_no"] == "CL-2b"
    assert segment(data, 1, 2)["duration_seconds"] == 40 * 60
    assert segment(data, 2, 3)["duration_seconds"] == 20 * 60
