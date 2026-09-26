"""关门线试算测试。夹具 client 见 conftest.py。"""
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


def cutoffs(client, version, values, key="cutoff_seconds"):
    return client.post(
        f"/api/publications/{version}/cutoffs", json={key: values}
    )


def by_runner(data):
    return {r["runner_id"]: r for r in data["runners"]}


def station_map(runner):
    return {s["station_seq"]: s for s in runner["stations"]}


# 种子选手（v1）相对各自第 1 站出发的累计到站秒数
# R001：08:00 出发 -> 1920 / 4320 / 6900 / 9900 / 12600（完整）
# R002：08:05 出发 -> 2400 / 4200 / 6900 / 10200 / 13200（完整）
# R003：08:10 出发 -> 2400 / 5100 / 缺站4（分段 3→4、4→5 pending）/ 10500 / 13500
# R004：08:02 出发 -> 2280 / 4680 / 7380 / 10680 / 13680（完整）
# R005：08:15 出发 -> 第 2 站撤销（分段 1→2、2→3 pending）/ 5100 / …
# R006：08:20 出发 -> 4200 / 第 3 站时间倒退（分段 2→3 pending）/ …


# ---------- 请求校验 ----------


def test_version_missing_returns_404(client):
    # 即使截止秒数本身不合法，版本不存在仍优先返回 404
    r = cutoffs(client, 999, [1])
    assert r.status_code == 404
    assert "不存在" in r.json()["detail"]


def test_version_0_returns_404_not_422(client):
    assert cutoffs(client, 0, []).status_code == 404


@pytest.mark.parametrize("values", [[], [1] * 4, [1] * 6])
def test_wrong_count_returns_422(client, values):
    version = client.post("/api/publications").json()["version"]
    r = cutoffs(client, version, values)
    assert r.status_code == 422
    assert "数量" in r.json()["detail"]


@pytest.mark.parametrize(
    "values",
    [
        [2000, 2000, 7000, 10000, 13000],   # 相邻相等，非严格递增
        [3000, 2000, 7000, 10000, 13000],  # 递减
        [5000, 4000, 3000, 2000, 1000],    # 全程递减
    ],
)
def test_non_increasing_cutoffs_return_422(client, values):
    version = client.post("/api/publications").json()["version"]
    r = cutoffs(client, version, values)
    assert r.status_code == 422
    assert "严格递增" in r.json()["detail"]


def test_missing_field_returns_422(client):
    version = client.post("/api/publications").json()["version"]
    r = client.post(f"/api/publications/{version}/cutoffs", json={})
    assert r.status_code == 422


def test_cutoffs_short_alias_accepted(client):
    version = client.post("/api/publications").json()["version"]
    r = cutoffs(client, version, [2000, 4400, 7000, 10000, 13000], key="cutoffs")
    assert r.status_code == 200
    # 响应统一回显为 cutoff_seconds
    assert r.json()["cutoff_seconds"] == [2000, 4400, 7000, 10000, 13000]


def test_count_uses_station_count_from_snapshot(client, monkeypatch):
    """截止秒数数量以版本快照的 station_count 为准，而非当前 STATION_COUNT。"""
    version = client.post("/api/publications").json()["version"]
    monkeypatch.setenv("STATION_COUNT", "4")  # 发布后配置变化不影响试算
    assert cutoffs(client, version, [1, 2, 3]).status_code == 422  # 数量按快照 6 校验
    assert cutoffs(client, version, [1, 2, 3, 4, 5]).status_code == 200


# ---------- 通过 / 关门 ----------


def test_happy_path_finished_and_closed(client):
    version = client.post("/api/publications").json()["version"]
    values = [2000, 4400, 7000, 10000, 13000]
    r = cutoffs(client, version, values)
    assert r.status_code == 200
    data = r.json()
    assert data["version"] == version
    assert data["station_count"] == 6
    assert data["cutoff_seconds"] == values
    # 版本全体有打卡记录选手，按选手号排序
    assert [x["runner_id"] for x in data["runners"]] == [
        f"R00{k}" for k in range(1, 7)
    ]

    runners = by_runner(data)

    # R001：累计 1920/4320/6900/9900/12600 全部在关门线内
    r1 = runners["R001"]
    assert r1["status"] == "finished"
    assert r1["first_closed_station"] is None
    s1 = station_map(r1)
    assert [s1[k]["status"] for k in range(1, 7)] == ["pass"] * 6
    assert s1[1]["cutoff_seconds"] == 0
    assert s1[1]["cumulative_seconds"] == 0
    assert [s1[k]["cumulative_seconds"] for k in range(2, 7)] == [
        1920,
        4320,
        6900,
        9900,
        12600,
    ]
    assert s1[6]["cumulative"] == "3:30:00"
    assert all(s1[k]["reason"] is None for k in range(1, 7))

    # R002：第 2 站累计 2400 > 截止 2000，首次关门；后续一律 closed
    r2 = runners["R002"]
    assert r2["status"] == "closed"
    assert r2["first_closed_station"] == 2
    s2 = station_map(r2)
    assert s2[1]["status"] == "pass"
    assert s2[2]["status"] == "over"
    assert s2[2]["cumulative_seconds"] == 2400
    assert "超过" in s2[2]["reason"]
    assert [s2[k]["status"] for k in range(3, 7)] == ["closed"] * 4
    # 关门后各站不给出累计秒数（即使实际打卡存在）
    assert all(s2[k]["cumulative_seconds"] is None for k in range(3, 7))
    assert all("已关门" in s2[k]["reason"] for k in range(3, 7))
    # 关门站回显本站截止秒数
    assert s2[2]["cutoff_seconds"] == 2000


def test_exactly_on_cutoff_passes(client):
    """恰在截止时间算通过：截止秒数与 R001 的累计秒数逐一相等。"""
    version = client.post("/api/publications").json()["version"]
    values = [1920, 4320, 6900, 9900, 12600]
    runners = by_runner(cutoffs(client, version, values).json())
    r1 = runners["R001"]
    assert r1["status"] == "finished"
    assert r1["first_closed_station"] is None
    s1 = station_map(r1)
    assert [s1[k]["status"] for k in range(1, 7)] == ["pass"] * 6
    assert all(s1[k]["reason"] is None for k in range(1, 7))

    # R002 第 2 站 2400 > 1920：边界之外一秒以上即超时
    r2 = runners["R002"]
    assert r2["status"] == "closed"
    assert r2["first_closed_station"] == 2
    assert station_map(r2)[2]["status"] == "over"


def test_closed_does_not_recover_when_catching_up_later(client):
    """关门后即使后续站重新赶在关门线内，也不恢复。"""
    # C1：08:00 出发，第 3 站拖到 09:30（5400 秒），之后越跑越快
    post(client, "C1-1", "C1", 1, "2026-09-25T08:00:00")
    post(client, "C1-2", "C1", 2, "2026-09-25T08:30:00")   # 1800
    post(client, "C1-3", "C1", 3, "2026-09-25T09:30:00")   # 5400 超时
    post(client, "C1-4", "C1", 4, "2026-09-25T09:50:00")   # 6600 < 8000 赶上
    post(client, "C1-5", "C1", 5, "2026-09-25T10:30:00")   # 9000 < 10000
    post(client, "C1-6", "C1", 6, "2026-09-25T11:00:00")   # 10800 < 12000
    version = client.post("/api/publications").json()["version"]

    runner = by_runner(
        cutoffs(client, version, [2000, 5000, 8000, 10000, 12000]).json()
    )["C1"]
    stations = station_map(runner)
    assert runner["status"] == "closed"
    assert runner["first_closed_station"] == 3
    assert [stations[k]["status"] for k in range(1, 7)] == [
        "pass",
        "pass",
        "over",
        "closed",
        "closed",
        "closed",
    ]
    # 后来赶上也不恢复、不重新计算累计秒数
    assert stations[4]["cumulative_seconds"] is None
    assert stations[6]["cumulative_seconds"] is None


def test_close_at_final_station(client):
    """在终点首次超时同样记为关门。"""
    version = client.post("/api/publications").json()["version"]
    # R001 终点累计 12600；给 12599 使其恰在终点超时
    runners = by_runner(
        cutoffs(client, version, [1920, 4320, 6900, 9900, 12599]).json()
    )
    r1 = runners["R001"]
    assert r1["status"] == "closed"
    assert r1["first_closed_station"] == 6
    stations = station_map(r1)
    assert stations[5]["status"] == "pass"
    assert stations[6]["status"] == "over"
    assert stations[6]["cumulative_seconds"] == 12600


# ---------- 待定 / 缺站 ----------


def test_missing_station_1_marks_runner_unknown(client):
    """第 1 站缺打卡：第 1 站及后续站点全部无法判定，不用后续打卡推测。"""
    post(client, "M1-2", "M1", 2, "2026-09-25T08:40:00")
    post(client, "M1-3", "M1", 3, "2026-09-25T09:20:00")
    version = client.post("/api/publications").json()["version"]

    runner = by_runner(cutoffs(client, version, [3000, 5000, 7000, 9000, 11000]).json())["M1"]
    assert runner["status"] == "unknown"
    assert runner["first_closed_station"] is None
    stations = station_map(runner)
    assert [stations[k]["status"] for k in range(1, 7)] == ["unknown"] * 6
    assert all(stations[k]["cumulative_seconds"] is None for k in range(1, 7))
    assert "第 1 站缺少有效打卡" in stations[1]["reason"]


def test_pending_segment_before_close_marks_rest_unknown(client):
    """尚未关门时遇到待定分段（缺站）：该站及后续站点无法判定。

    R003 缺第 4 站：第 5 站虽有打卡且累计 10500 在截止 11000 之内，
    也不拿来推测——第 4 站起一律 unknown。
    """
    version = client.post("/api/publications").json()["version"]
    values = [3000, 6000, 7000, 11000, 14000]
    runner = by_runner(cutoffs(client, version, values).json())["R003"]
    assert runner["status"] == "unknown"
    assert runner["first_closed_station"] is None
    stations = station_map(runner)
    assert [stations[k]["status"] for k in range(1, 7)] == [
        "pass",   # 站 1 出发基准
        "pass",   # 2400 <= 3000
        "pass",   # 5100 <= 6000
        "unknown",  # 分段 3→4 pending（缺第 4 站）
        "unknown",  # 不因第 5 站打卡 10500 <= 11000 而恢复
        "unknown",
    ]
    assert stations[4]["cumulative_seconds"] is None
    assert stations[5]["cumulative_seconds"] is None
    assert "待定" in stations[4]["reason"]
    assert "站点 4 缺少有效打卡" in stations[4]["reason"]


def test_revoked_station_2_pending_from_station_2(client):
    """R005 第 2 站被撤销：尚未关门时分段 1→2 待定，第 2 站起无法判定。"""
    version = client.post("/api/publications").json()["version"]
    values = [3000, 6000, 7000, 11000, 14000]
    runner = by_runner(cutoffs(client, version, values).json())["R005"]
    assert runner["status"] == "unknown"
    assert runner["first_closed_station"] is None
    stations = station_map(runner)
    assert stations[1]["status"] == "pass"
    assert [stations[k]["status"] for k in range(2, 7)] == ["unknown"] * 5
    assert stations[2]["cumulative_seconds"] is None
    # 第 3 站虽有打卡（09:40），不用于推测
    assert stations[3]["cumulative_seconds"] is None


def test_time_regression_before_close_marks_unknown(client):
    """R006 第 3 站时间倒退：第 2 站通过后，分段 2→3 pending，第 3 站起 unknown。"""
    version = client.post("/api/publications").json()["version"]
    values = [5000, 6000, 8000, 11000, 14000]  # R006 第 2 站 4200 <= 5000 通过
    runner = by_runner(cutoffs(client, version, values).json())["R006"]
    assert runner["status"] == "unknown"
    stations = station_map(runner)
    assert [stations[k]["status"] for k in range(1, 7)] == [
        "pass",
        "pass",
        "unknown",  # 时间倒退：分段 2→3 pending
        "unknown",
        "unknown",
        "unknown",
    ]
    assert "时间倒退" in stations[3]["reason"]


def test_pending_after_close_keeps_closed_not_unknown(client):
    """已关门后遇到待定分段（R006 第 3 站倒退）仍只标记 closed。"""
    version = client.post("/api/publications").json()["version"]
    # R006 第 2 站累计 4200 > 4000：先关门，之后时间倒退不改变 closed
    runner = by_runner(
        cutoffs(client, version, [4000, 6000, 8000, 11000, 14000]).json()
    )["R006"]
    assert runner["status"] == "closed"
    assert runner["first_closed_station"] == 2
    stations = station_map(runner)
    assert stations[2]["status"] == "over"
    assert [stations[k]["status"] for k in range(3, 7)] == ["closed"] * 4


def test_missing_station_after_close_stays_closed(client):
    """关门后再缺站（R003 缺第 4 站）也只标记 closed，不转 unknown。"""
    version = client.post("/api/publications").json()["version"]
    # R003 第 2 站累计 2400 > 2000：第 2 站即关门
    runner = by_runner(
        cutoffs(client, version, [2000, 6000, 7000, 11000, 14000]).json()
    )["R003"]
    assert runner["status"] == "closed"
    assert runner["first_closed_station"] == 2
    stations = station_map(runner)
    assert stations[2]["status"] == "over"
    assert [stations[k]["status"] for k in range(3, 7)] == ["closed"] * 4


# ---------- 快照一致性 / 无副作用 ----------


def test_trial_reads_snapshot_not_live_results(client):
    """只读版本快照：发布后的补传 / 撤销 / 新选手不影响同版本试算。"""
    version = client.post("/api/publications").json()["version"]
    snapshot_before = client.get(f"/api/publications/{version}").json()
    values = [2000, 4400, 7000, 10000, 13000]
    first = cutoffs(client, version, values).json()

    # 发布后：撤销 R001 第 3 站并补传、新增发布后才出现的选手 Z9
    assert client.post("/api/checkins/S-R001-3/revoke").status_code == 200
    post(client, "P-R001-3", "R001", 3, "2026-09-25T10:00:00")
    post(client, "Z9-1", "Z9", 1, "2026-09-25T08:00:00")
    post(client, "Z9-2", "Z9", 2, "2026-09-25T20:00:00")

    second = cutoffs(client, version, values).json()
    assert first == second  # 同版本结果不受实时打卡变化影响
    assert "Z9" not in by_runner(second)
    # 快照本身未被试算改动，且没有产生新版本
    assert client.get(f"/api/publications/{version}").json() == snapshot_before
    assert client.get("/api/publications/latest").json()["version"] == version


def test_repeated_requests_are_stable(client):
    version = client.post("/api/publications").json()["version"]
    payload = {"cutoff_seconds": [2000, 4400, 7000, 10000, 13000]}
    first = client.post(f"/api/publications/{version}/cutoffs", json=payload).json()
    second = client.post(f"/api/publications/{version}/cutoffs", json=payload).json()
    assert first == second


def test_every_station_carries_cutoff_and_full_station_list(client):
    """逐站判定覆盖第 1 站至终点，且各站回显对应截止秒数。"""
    version = client.post("/api/publications").json()["version"]
    values = [2000, 4400, 7000, 10000, 13000]
    data = cutoffs(client, version, values).json()
    for runner in data["runners"]:
        assert [s["station_seq"] for s in runner["stations"]] == [1, 2, 3, 4, 5, 6]
        by_seq = station_map(runner)
        assert by_seq[1]["cutoff_seconds"] == 0
        assert [by_seq[k]["cutoff_seconds"] for k in range(2, 7)] == values
