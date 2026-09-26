"""关门线试算测试。夹具 client 见 conftest.py。"""
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


def cutoffs(client, version, cutoff_seconds):
    return client.post(
        f"/api/publications/{version}/cutoffs",
        json={"cutoff_seconds": cutoff_seconds},
    )


def by_runner(data):
    return {r["runner_id"]: r for r in data["runners"]}


def station_map(runner):
    return {s["station_seq"]: s for s in runner["stations"]}


# ---------- 请求校验 ----------


def test_version_missing_returns_404(client):
    r = cutoffs(client, 999, [2000, 4500, 7000, 10000, 13000])
    assert r.status_code == 404
    assert "不存在" in r.json()["detail"]


def test_version_0_returns_404_before_rule_validation(client):
    # 版本不存在优先于关门线规则校验：即使数量也不合法，仍返回 404
    assert cutoffs(client, 0, []).status_code == 404


def test_wrong_count_returns_422(client):
    version = client.post("/api/publications").json()["version"]
    for bad in [[], [2000] * 4, [2000] * 6]:
        r = cutoffs(client, version, bad)
        assert r.status_code == 422
        assert "数量" in r.json()["detail"]


def test_not_strictly_increasing_returns_422(client):
    version = client.post("/api/publications").json()["version"]
    for bad in [
        [2000, 4500, 4500, 7000, 9000],  # 相等
        [2000, 4500, 7000, 6900, 9000],  # 倒退
    ]:
        r = cutoffs(client, version, bad)
        assert r.status_code == 422
        assert "严格递增" in r.json()["detail"]


def test_missing_field_returns_422(client):
    version = client.post("/api/publications").json()["version"]
    r = client.post(f"/api/publications/{version}/cutoffs", json={})
    assert r.status_code == 422


# ---------- 正常试算（种子数据，6 站） ----------


GENEROUS = [2000, 4500, 7000, 10000, 13000]
# R001 累计到站秒数：1920 / 4320 / 6900 / 9900 / 12600
# R002 累计到站秒数：2400 / 4200 / 6900 / 9900 / 12900
# R004 累计到站秒数：2280 / 4680 / 7380 / 10680 / 13680


def test_all_pass_happy_path(client):
    version = client.post("/api/publications").json()["version"]
    r = cutoffs(client, version, GENEROUS)
    assert r.status_code == 200
    data = r.json()
    assert data["version"] == version
    assert data["station_count"] == 6
    assert data["cutoff_seconds"] == GENEROUS
    assert [x["runner_id"] for x in data["runners"]] == [
        f"R00{k}" for k in range(1, 7)
    ]

    r001 = by_runner(data)["R001"]
    assert r001["status"] == "racing"
    assert r001["cutoff_station"] is None
    st = station_map(r001)
    assert [s for s in st] == [2, 3, 4, 5, 6]
    assert st[2]["verdict"] == "pass"
    assert st[2]["cumulative_seconds"] == 1920
    assert st[2]["event_no"] == "S-R001-2"
    assert st[2]["cutoff_seconds"] == 2000
    assert st[2]["reason"] is None
    assert [st[s]["cumulative_seconds"] for s in range(2, 7)] == [
        1920,
        4320,
        6900,
        9900,
        12600,
    ]
    assert all(st[s]["verdict"] == "pass" for s in range(2, 7))


def test_exactly_on_cutoff_passes(client):
    version = client.post("/api/publications").json()["version"]
    # R001 第 2 站累计恰好 1920 秒：恰在截止时间算通过
    r = cutoffs(client, version, [1920, 4320, 6900, 9900, 12600])
    assert r.status_code == 200
    r001 = by_runner(r.json())["R001"]
    assert r001["status"] == "racing"
    assert r001["cutoff_station"] is None
    assert all(s["verdict"] == "pass" for s in r001["stations"])
    # R002 第 2 站累计 2400 > 1920：在第 2 站首次超时
    r002 = by_runner(r.json())["R002"]
    assert r002["status"] == "closed"
    assert r002["cutoff_station"] == 2
    assert station_map(r002)[2]["verdict"] == "overtime"


def test_first_overtime_closes_runner_and_rest_are_closed(client):
    version = client.post("/api/publications").json()["version"]
    # R001 第 2 站累计 1920 > 1900：首次超时关门；
    # 后续截止非常宽松，即使重新赶上也不恢复
    r = cutoffs(client, version, [1900, 99999, 99999, 99999, 99999])
    r001 = by_runner(r.json())["R001"]
    assert r001["status"] == "closed"
    assert r001["cutoff_station"] == 2
    st = station_map(r001)
    assert st[2]["verdict"] == "overtime"
    assert st[2]["cumulative_seconds"] == 1920
    assert "超时 20 秒" in st[2]["reason"]
    for seq in range(3, 7):
        assert st[seq]["verdict"] == "closed"
        assert st[seq]["cumulative_seconds"] is None
        assert "站点 2 关门" in st[seq]["reason"]
    # 已关门站点仍有打卡时回显事件号，但不改变判定
    assert st[3]["event_no"] == "S-R001-3"


def test_overtime_mid_race_later_catchup_does_not_recover(client):
    version = client.post("/api/publications").json()["version"]
    # R001：站 2 通过（1920 <= 2000），站 3 首次超时（4320 > 4000）；
    # 站 4 截止很宽、累计 6900 本可通过，但必须保持已关门
    r = cutoffs(client, version, [2000, 4000, 99999, 99999, 99999])
    st = station_map(by_runner(r.json())["R001"])
    assert st[2]["verdict"] == "pass"
    assert st[3]["verdict"] == "overtime"
    assert st[3]["cumulative_seconds"] == 4320
    for seq in range(4, 7):
        assert st[seq]["verdict"] == "closed"
        assert st[seq]["cumulative_seconds"] is None


# ---------- 待定 / 缺站 / 时间倒退 ----------


def test_pending_segment_before_close_marks_rest_unknown(client):
    version = client.post("/api/publications").json()["version"]
    # R003 缺第 4 站：站 2/3 累计 2400/5100 可判，站 4 分段 3→4 待定，
    # 站 4 及其后一律无法判定，不用站 5/6 的打卡推测
    r = cutoffs(client, version, [2400, 5100, 7000, 10000, 13000])
    r003 = by_runner(r.json())["R003"]
    assert r003["status"] == "unknown"
    assert r003["cutoff_station"] is None
    st = station_map(r003)
    assert st[2]["verdict"] == "pass"
    assert st[2]["cumulative_seconds"] == 2400
    assert st[3]["verdict"] == "pass"
    assert st[3]["cumulative_seconds"] == 5100
    assert st[4]["verdict"] == "unknown"
    assert st[4]["cumulative_seconds"] is None
    assert st[4]["event_no"] is None
    assert "待定" in st[4]["reason"]
    assert "站点 4 缺少有效打卡" in st[4]["reason"]
    # 站 5/6 虽有打卡（S-R003-5/6），也不用来推测
    for seq in (5, 6):
        assert st[seq]["verdict"] == "unknown"
        assert st[seq]["cumulative_seconds"] is None
    assert st[5]["event_no"] == "S-R003-5"


def test_pending_does_not_mask_earlier_overtime(client):
    version = client.post("/api/publications").json()["version"]
    # R003 站 2 累计 2400 > 2300：先关门；站 4 的缺站不再产生 unknown，
    # 其后各站保持已关门
    r = cutoffs(client, version, [2300, 5100, 7000, 10000, 13000])
    st = station_map(by_runner(r.json())["R003"])
    assert st[2]["verdict"] == "overtime"
    assert st[3]["verdict"] == "closed"
    for seq in range(4, 7):
        assert st[seq]["verdict"] == "closed"
        assert "unknown" not in st[seq]["verdict"]


def test_revoked_station2_all_stations_unknown(client):
    version = client.post("/api/publications").json()["version"]
    # 种子 R005 第 2 站被撤销：分段 1→2 待定，各站一律无法判定
    r = cutoffs(client, version, GENEROUS)
    r005 = by_runner(r.json())["R005"]
    assert r005["status"] == "unknown"
    assert r005["cutoff_station"] is None
    st = station_map(r005)
    assert [st[s]["verdict"] for s in range(2, 7)] == ["unknown"] * 5
    assert all(st[s]["cumulative_seconds"] is None for s in range(2, 7))
    assert st[2]["event_no"] is None
    assert "站点 2 缺少有效打卡" in st[2]["reason"]
    # 站 3 有打卡也不用来推测
    assert st[3]["event_no"] == "S-R005-3"


def test_missing_station1_all_unknown(client):
    # 新选手只在 2、3 站有打卡（第 1 站缺打卡）
    post(client, "M-2", "M001", 2, "2026-09-25T08:40:00")
    post(client, "M-3", "M001", 3, "2026-09-25T09:20:00")
    version = client.post("/api/publications").json()["version"]

    r = cutoffs(client, version, GENEROUS)
    m001 = by_runner(r.json())["M001"]
    assert m001["status"] == "unknown"
    assert m001["cutoff_station"] is None
    st = station_map(m001)
    assert [st[s]["verdict"] for s in range(2, 7)] == ["unknown"] * 5
    assert all(st[s]["cumulative_seconds"] is None for s in range(2, 7))
    assert "第 1 站" in st[2]["reason"]
    # 不用 2/3 站打卡推测
    assert st[2]["event_no"] == "M-2"


def test_time_reversal_marks_from_that_station_unknown(client):
    version = client.post("/api/publications").json()["version"]
    # 种子 R006：站 2 累计 4200，站 3 打卡早于站 2（时间倒退）→ 站 3 起无法判定
    r = cutoffs(client, version, [4200, 5100, 7000, 10000, 13000])
    r006 = by_runner(r.json())["R006"]
    assert r006["status"] == "unknown"
    st = station_map(r006)
    assert st[2]["verdict"] == "pass"
    assert st[2]["cumulative_seconds"] == 4200
    for seq in range(3, 7):
        assert st[seq]["verdict"] == "unknown"
        assert st[seq]["cumulative_seconds"] is None
    assert "时间倒退" in st[3]["reason"]


# ---------- 快照一致性 / 无副作用 ----------


def test_trial_reads_snapshot_not_live_results(client):
    version = client.post("/api/publications").json()["version"]
    payload = [1900, 99999, 99999, 99999, 99999]
    first = cutoffs(client, version, payload).json()
    # R001 在版本快照中于第 2 站关门
    assert by_runner(first)["R001"]["cutoff_station"] == 2

    # 发布后撤销 R001 第 2 站并补传更早的打卡，只影响实时成绩
    assert client.post("/api/checkins/S-R001-2/revoke").status_code == 200
    post(client, "P-R001-2", "R001", 2, "2026-09-25T08:20:00")
    # 发布后才出现的新选手
    post(client, "N-1", "N001", 1, "2026-09-25T08:00:00")

    second = cutoffs(client, version, payload).json()
    assert second == first  # 同版本结果完全不变：仍按快照关门、无 N001
    assert "N001" not in by_runner(second)


def test_trial_does_not_write_database(client):
    version = client.post("/api/publications").json()["version"]
    snapshot_before = client.get(f"/api/publications/{version}").json()
    checkin_before = client.get("/api/checkins/S-R001-2").json()
    cutoffs(client, version, GENEROUS)
    cutoffs(client, version, [1900, 99999, 99999, 99999, 99999])
    # 试算不改变版本快照与打卡状态，也不产生新版本
    assert client.get(f"/api/publications/{version}").json() == snapshot_before
    assert client.get("/api/checkins/S-R001-2").json() == checkin_before
    assert client.get("/api/publications/latest").json()["version"] == version


def test_repeated_requests_are_stable(client):
    version = client.post("/api/publications").json()["version"]
    first = cutoffs(client, version, GENEROUS).json()
    second = cutoffs(client, version, GENEROUS).json()
    assert first == second
