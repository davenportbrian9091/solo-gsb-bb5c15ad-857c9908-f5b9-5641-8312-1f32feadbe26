"""中立时段试算接口测试。共享夹具 client 见 conftest.py。"""
from __future__ import annotations

from datetime import datetime, timezone


def neutralized(client, runner, windows):
    return client.post(
        f"/api/runners/{runner}/splits/neutralized", json={"windows": windows}
    )


def window(start, end):
    return {"start": start, "end": end}


def segment(data, frm, to):
    return next(
        s for s in data["segments"]
        if s["from_station"] == frm and s["to_station"] == to
    )


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


# 种子选手 R001：08:00 / 08:32 / 09:12 / 09:55 / 10:45 / 11:30，
# 分段 1920 / 2400 / 2580 / 3000 / 2700 秒，总计 12600 秒

def test_neutralized_basic_deduction(client):
    r = neutralized(client, "R001", [window("2026-09-25T08:10:00", "2026-09-25T08:20:00")])
    assert r.status_code == 200
    data = r.json()
    assert data["runner_id"] == "R001"
    assert data["complete"] is True

    s12 = segment(data, 1, 2)
    assert s12["status"] == "ok"
    assert s12["raw_seconds"] == 32 * 60
    assert s12["deducted_seconds"] == 10 * 60
    assert s12["net_seconds"] == 22 * 60
    # 其余分段无扣除，净秒数等于原始秒数
    for frm, to, raw in [(2, 3, 2400), (3, 4, 2580), (4, 5, 3000), (5, 6, 2700)]:
        seg = segment(data, frm, to)
        assert seg["deducted_seconds"] == 0
        assert seg["net_seconds"] == raw

    assert data["total_seconds"] == 12600
    assert data["deducted_seconds"] == 600
    assert data["net_seconds"] == 12000
    assert data["net"] == "3:20:00"


def test_neutralized_overlapping_and_adjacent_windows_merged(client):
    # 08:05-08:15 与 08:10-08:20 重叠、与 08:20-08:25 相接：合并为 08:05-08:25，
    # 重叠部分只扣一次
    r = neutralized(client, "R001", [
        window("2026-09-25T08:10:00", "2026-09-25T08:20:00"),
        window("2026-09-25T08:05:00", "2026-09-25T08:15:00"),
        window("2026-09-25T08:20:00", "2026-09-25T08:25:00"),
    ])
    assert r.status_code == 200
    data = r.json()
    assert segment(data, 1, 2)["deducted_seconds"] == 20 * 60
    assert data["net_seconds"] == 12600 - 20 * 60
    # 响应回显合并后的 UTC 时间窗
    assert len(data["windows"]) == 1
    w = data["windows"][0]
    assert datetime.fromisoformat(w["start"]) == datetime(2026, 9, 25, 8, 5, tzinfo=timezone.utc)
    assert datetime.fromisoformat(w["end"]) == datetime(2026, 9, 25, 8, 25, tzinfo=timezone.utc)


def test_neutralized_cross_segment_window_clipped_without_double_count(client):
    # 时间窗 08:30-08:40 跨越分段边界 08:32：两段分别裁剪扣除，边界不重复扣秒
    r = neutralized(client, "R001", [window("2026-09-25T08:30:00", "2026-09-25T08:40:00")])
    data = r.json()
    assert segment(data, 1, 2)["deducted_seconds"] == 2 * 60   # 08:30-08:32
    assert segment(data, 2, 3)["deducted_seconds"] == 8 * 60   # 08:32-08:40
    assert data["deducted_seconds"] == 10 * 60
    assert data["net_seconds"] == 12600 - 10 * 60


def test_neutralized_window_outside_race_deducts_nothing(client):
    r = neutralized(client, "R001", [window("2026-09-25T07:00:00", "2026-09-25T07:30:00")])
    data = r.json()
    assert all(s["deducted_seconds"] == 0 for s in data["segments"])
    assert data["net_seconds"] == 12600


def test_neutralized_window_covering_whole_race_nets_zero(client):
    r = neutralized(client, "R001", [window("2026-09-25T07:00:00", "2026-09-25T12:00:00")])
    data = r.json()
    assert all(s["net_seconds"] == 0 for s in data["segments"])
    assert data["deducted_seconds"] == 12600
    assert data["net_seconds"] == 0
    assert data["net"] == "0:00:00"


def test_neutralized_windows_normalized_to_utc(client):
    # 带时区偏移的时间窗统一转 UTC 后再扣除
    r = neutralized(client, "R001", [
        window("2026-09-25T16:10:00+08:00", "2026-09-25T16:20:00+08:00")
    ])
    data = r.json()
    assert segment(data, 1, 2)["deducted_seconds"] == 10 * 60
    w = data["windows"][0]
    assert datetime.fromisoformat(w["start"]) == datetime(2026, 9, 25, 8, 10, tzinfo=timezone.utc)


def test_neutralized_rejects_window_end_not_after_start(client):
    # 终点早于起点
    r = neutralized(client, "R001", [window("2026-09-25T08:20:00", "2026-09-25T08:10:00")])
    assert r.status_code == 422
    assert "时间窗" in r.json()["detail"]
    # 终点等于起点同样拒绝
    r = neutralized(client, "R001", [window("2026-09-25T08:10:00", "2026-09-25T08:10:00")])
    assert r.status_code == 422


def test_neutralized_pending_segments_stay_pending(client):
    # R003 缺第 4 站：3→4、4→5 待定，有效分段仍正常扣除
    r = neutralized(client, "R003", [window("2026-09-25T08:20:00", "2026-09-25T08:30:00")])
    assert r.status_code == 200
    data = r.json()
    assert data["complete"] is False
    s12 = segment(data, 1, 2)
    assert s12["status"] == "ok"
    assert s12["raw_seconds"] == 40 * 60
    assert s12["deducted_seconds"] == 10 * 60
    assert s12["net_seconds"] == 30 * 60
    for frm, to in [(3, 4), (4, 5)]:
        seg = segment(data, frm, to)
        assert seg["status"] == "pending"
        assert "站点 4" in seg["reason"]
        assert seg["raw_seconds"] is None
        assert seg["deducted_seconds"] is None
        assert seg["net_seconds"] is None
    # 存在待定分段时不给出净总秒数
    assert data["total_seconds"] is None
    assert data["deducted_seconds"] is None
    assert data["net_seconds"] is None
    assert data["net"] is None


def test_neutralized_time_regression_segment_stays_pending(client):
    # R006 第 3 站时间早于第 2 站：2→3 待定，其余分段正常扣除
    r = neutralized(client, "R006", [window("2026-09-25T08:30:00", "2026-09-25T08:40:00")])
    data = r.json()
    assert data["complete"] is False
    s23 = segment(data, 2, 3)
    assert s23["status"] == "pending"
    assert "时间倒退" in s23["reason"]
    assert segment(data, 1, 2)["deducted_seconds"] == 10 * 60
    assert data["net_seconds"] is None


def test_neutralized_unknown_runner_404(client):
    r = neutralized(client, "NOBODY", [window("2026-09-25T08:00:00", "2026-09-25T09:00:00")])
    assert r.status_code == 404


def test_neutralized_empty_windows_no_deduction(client):
    r = neutralized(client, "R001", [])
    assert r.status_code == 200
    data = r.json()
    assert data["windows"] == []
    assert all(s["deducted_seconds"] == 0 for s in data["segments"])
    assert data["net_seconds"] == data["total_seconds"] == 12600


def test_neutralized_has_no_side_effects(client):
    # 发布 v1 后试算：实时成绩、打卡记录、已发布版本均不受影响
    pub = client.post("/api/publications")
    assert pub.status_code == 201 and pub.json()["version"] == 1
    splits_before = client.get("/api/runners/R001/splits").json()
    checkins_before = client.get("/api/checkins", params={"runner_id": "R001"}).json()

    r = neutralized(client, "R001", [window("2026-09-25T08:10:00", "2026-09-25T08:20:00")])
    assert r.status_code == 200
    assert r.json()["net_seconds"] == 12000

    assert client.get("/api/runners/R001/splits").json() == splits_before
    assert client.get("/api/checkins", params={"runner_id": "R001"}).json() == checkins_before
    latest = client.get("/api/publications/latest").json()
    assert latest["version"] == 1
    assert latest == pub.json()


def test_neutralized_reflects_latest_checkins(client):
    post(client, "N-1", "TN1", 1, "2026-09-25T08:00:00")
    post(client, "N-2", "TN1", 2, "2026-09-25T08:30:00")
    post(client, "N-3", "TN1", 3, "2026-09-25T09:00:00")
    win = [window("2026-09-25T08:10:00", "2026-09-25T08:20:00")]

    data = neutralized(client, "TN1", win).json()
    assert segment(data, 1, 2)["net_seconds"] == 30 * 60 - 10 * 60

    # 撤销第 2 站后：1→2、2→3 变为待定
    client.post("/api/checkins/N-2/revoke")
    data = neutralized(client, "TN1", win).json()
    assert segment(data, 1, 2)["status"] == "pending"
    assert segment(data, 2, 3)["status"] == "pending"

    # 补传新的第 2 站后即时恢复，扣除按新打卡时间裁剪
    post(client, "N-2b", "TN1", 2, "2026-09-25T08:40:00")
    data = neutralized(client, "TN1", win).json()
    s12 = segment(data, 1, 2)
    assert s12["raw_seconds"] == 40 * 60
    assert s12["deducted_seconds"] == 10 * 60
    assert s12["net_seconds"] == 30 * 60

    # 更正第 2 站后重新请求反映最新打卡
    post(client, "N-2c", "TN1", 2, "2026-09-25T08:35:00", corrects="N-2b")
    data = neutralized(client, "TN1", win).json()
    s12 = segment(data, 1, 2)
    assert s12["raw_seconds"] == 35 * 60
    assert s12["deducted_seconds"] == 10 * 60
    assert s12["net_seconds"] == 25 * 60
