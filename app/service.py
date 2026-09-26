"""核心业务逻辑：打卡写入 / 更正 / 撤销 / 分段重算 / 更正预演 / 成绩发布。"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from . import db
from .models import (
    BatchCheckinCreate,
    BatchCheckinOut,
    BatchItemOut,
    BatchRunnerResult,
    CalibratedCreate,
    CalibratedOut,
    CalibratedStation,
    CalibrationWindow,
    CheckinCreate,
    CheckinOut,
    CheckinSelection,
    CorrectionPreview,
    CutoffRunnerOut,
    CutoffStationOut,
    CutoffsCreate,
    CutoffsOut,
    NeutralWindow,
    NeutralizedCreate,
    NeutralizedOut,
    NeutralizedSegment,
    PreviewCorrectionOut,
    PublicationOut,
    RelayHandoverOut,
    RelayLegOut,
    RelayTeamCreate,
    RelayTeamOut,
    RunnerPreview,
    RunnerSummary,
    Segment,
    SegmentChange,
    SplitsExplainOut,
    SplitsOut,
    StationSelection,
    StationTime,
)

ACTIVE = "active"
SUPERSEDED = "superseded"
REVOKED = "revoked"


def station_count() -> int:
    """赛道站点总数（分段数 = 站点数 - 1）。"""
    return int(os.environ.get("STATION_COUNT", "6"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_utc(dt: datetime) -> datetime:
    """统一为 UTC；无时区信息的时间按 UTC 处理。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fmt_duration(seconds: int) -> str:
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{sign}{h}:{m:02d}:{s:02d}"


def _row_to_out(row: sqlite3.Row) -> CheckinOut:
    return CheckinOut(
        event_no=row["event_no"],
        runner_id=row["runner_id"],
        station_seq=row["station_seq"],
        occurred_at=datetime.fromisoformat(row["occurred_at"]),
        status=row["status"],
        corrects=row["corrects"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _get_row(conn: sqlite3.Connection, event_no: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM checkins WHERE event_no = ?", (event_no,)
    ).fetchone()


def _validate_payload(
    conn: sqlite3.Connection, payload: CheckinCreate
) -> sqlite3.Row | None:
    """登记与预演共用的校验：站点范围、事件号唯一性、corrects 目标存在且有效。

    返回被更正的原事件行；payload 未携带 corrects 时返回 None。
    """
    count = station_count()
    if payload.station_seq > count:
        raise HTTPException(
            status_code=422,
            detail=f"站点序号 {payload.station_seq} 超出范围（1..{count}）",
        )
    if _get_row(conn, payload.event_no) is not None:
        raise HTTPException(status_code=409, detail=f"事件号 {payload.event_no} 已存在")

    origin = None
    if payload.corrects is not None:
        origin = _get_row(conn, payload.corrects)
        if origin is None:
            raise HTTPException(
                status_code=404, detail=f"被更正的事件 {payload.corrects} 不存在"
            )
        if origin["status"] != ACTIVE:
            raise HTTPException(
                status_code=409,
                detail=f"事件 {payload.corrects} 状态为 {origin['status']}，不能更正；"
                "请更正当前有效的替代事件",
            )
    return origin


def _insert_correction(
    conn: sqlite3.Connection,
    payload: CheckinCreate,
    occurred: datetime,
    created: datetime,
) -> None:
    """在当前事务内执行更正写入：原事件置为 superseded 并插入新事件。"""
    if payload.corrects is not None:
        conn.execute(
            "UPDATE checkins SET status = ? WHERE event_no = ?",
            (SUPERSEDED, payload.corrects),
        )
    conn.execute(
        "INSERT INTO checkins"
        " (event_no, runner_id, station_seq, occurred_at, status, corrects, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            payload.event_no,
            payload.runner_id,
            payload.station_seq,
            occurred.isoformat(),
            ACTIVE,
            payload.corrects,
            created.isoformat(),
        ),
    )


def create_checkin(conn: sqlite3.Connection, payload: CheckinCreate) -> CheckinOut:
    """登记打卡；携带 corrects 时视为更正，原事件被取代。"""
    _validate_payload(conn, payload)
    occurred = _to_utc(payload.occurred_at)
    created = _now()
    with conn:  # 事务：更正与写入同时成功或失败
        _insert_correction(conn, payload, occurred, created)
    return CheckinOut(
        event_no=payload.event_no,
        runner_id=payload.runner_id,
        station_seq=payload.station_seq,
        occurred_at=occurred,
        status=ACTIVE,
        corrects=payload.corrects,
        created_at=created,
    )


def _batch_failure(status_code: int, index: int, event_no: str, reason: str) -> HTTPException:
    """构造批量失败响应：携带失败下标与原因，状态码沿用单条登记的语义。"""
    return HTTPException(
        status_code=status_code,
        detail={
            "message": f"批量打卡在下标 {index} 处失败，整批已回滚",
            "failures": [{"index": index, "event_no": event_no, "reason": reason}],
        },
    )


def create_checkins_batch(
    conn: sqlite3.Connection, payload: BatchCheckinCreate
) -> BatchCheckinOut:
    """批量登记打卡：1~100 条按顺序在同一事务内处理。

    - 每条沿用单条登记的字段与校验（站点范围、事件号唯一、corrects 目标有效）；
    - 后续记录可用 corrects 更正本批前面刚写入且仍有效的事件；
    - 前向引用（含更正自身）、批内重复事件号、无效更正在对应下标处失败；
    - 任一条失败时回滚整批的新增记录与状态变更，并返回失败下标与原因。
    """
    items = payload.checkins
    # 批内事件号首次出现的下标，用于识别批内重复与前向引用
    first_pos: dict[str, int] = {}
    for i, item in enumerate(items):
        first_pos.setdefault(item.event_no, i)

    results: list[BatchItemOut] = []
    runner_ids: list[str] = []

    with conn:  # 单事务：任一失败时回滚整批写入与状态变更
        for i, item in enumerate(items):
            dup = first_pos[item.event_no]
            if dup < i:
                raise _batch_failure(
                    409, i, item.event_no,
                    f"事件号 {item.event_no} 与本批下标 {dup} 重复",
                )
            if item.corrects is not None:
                target = first_pos.get(item.corrects)
                if target is not None and target >= i:
                    reason = (
                        f"事件不能更正自身（corrects={item.corrects}）"
                        if target == i
                        else f"前向引用：被更正的事件 {item.corrects} "
                        f"在本批下标 {target} 才登记"
                    )
                    raise _batch_failure(422, i, item.event_no, reason)
            try:
                origin = _validate_payload(conn, item)
            except HTTPException as exc:
                raise _batch_failure(
                    exc.status_code, i, item.event_no, str(exc.detail)
                ) from exc
            _insert_correction(conn, item, _to_utc(item.occurred_at), _now())
            results.append(BatchItemOut(index=i, event_no=item.event_no))
            if item.runner_id not in runner_ids:
                runner_ids.append(item.runner_id)
            if origin is not None and origin["runner_id"] not in runner_ids:
                runner_ids.append(origin["runner_id"])

        # 全部写入后按现有分段规则重算受影响选手的最终结果
        affected = [
            BatchRunnerResult(
                runner_id=rid, **_summary(_compute_splits(conn, rid)).model_dump()
            )
            for rid in runner_ids
        ]
    return BatchCheckinOut(results=results, affected_runners=affected)


def revoke_checkin(conn: sqlite3.Connection, event_no: str) -> CheckinOut:
    """按事件号撤销一条有效打卡。"""
    row = _get_row(conn, event_no)
    if row is None:
        raise HTTPException(status_code=404, detail=f"事件 {event_no} 不存在")
    if row["status"] != ACTIVE:
        raise HTTPException(
            status_code=409,
            detail=f"事件 {event_no} 状态为 {row['status']}，无需撤销",
        )
    with conn:
        conn.execute(
            "UPDATE checkins SET status = ? WHERE event_no = ?", (REVOKED, event_no)
        )
    return CheckinOut(
        event_no=row["event_no"],
        runner_id=row["runner_id"],
        station_seq=row["station_seq"],
        occurred_at=datetime.fromisoformat(row["occurred_at"]),
        status=REVOKED,
        corrects=row["corrects"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def get_checkin(conn: sqlite3.Connection, event_no: str) -> CheckinOut:
    row = _get_row(conn, event_no)
    if row is None:
        raise HTTPException(status_code=404, detail=f"事件 {event_no} 不存在")
    return _row_to_out(row)


def list_checkins(
    conn: sqlite3.Connection, runner_id: str | None = None
) -> list[CheckinOut]:
    if runner_id is None:
        rows = conn.execute(
            "SELECT * FROM checkins ORDER BY created_at, event_no"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM checkins WHERE runner_id = ? ORDER BY created_at, event_no",
            (runner_id,),
        ).fetchall()
    return [_row_to_out(r) for r in rows]


def get_splits(conn: sqlite3.Connection, runner_id: str) -> SplitsOut:
    """按站点顺序重算某选手的相邻站分段用时（详见 _compute_splits）。"""
    any_row = conn.execute(
        "SELECT 1 FROM checkins WHERE runner_id = ? LIMIT 1", (runner_id,)
    ).fetchone()
    if any_row is None:
        raise HTTPException(status_code=404, detail=f"选手 {runner_id} 无任何打卡记录")
    return _compute_splits(conn, runner_id)


def _compute_splits(conn: sqlite3.Connection, runner_id: str) -> SplitsOut:
    """按站点顺序重算某选手的相邻站分段用时。

    - 仅统计 status='active' 的打卡；同站多条有效打卡按 (UTC 时间, 事件号)
      升序取第一条（与 splits/explain 的入选规则一致）；
    - 相邻站缺打卡或时间倒退的分段标记为 pending 并说明原因；
    - 每次查询都基于最新数据重算，因此补传、更正、撤销即时生效。
    """
    rows = conn.execute(
        "SELECT * FROM checkins WHERE runner_id = ? AND status = ?",
        (runner_id, ACTIVE),
    ).fetchall()
    return _splits_from_best(_select_best(rows), runner_id)


def _select_best(rows: list[sqlite3.Row]) -> dict[int, tuple[datetime, str]]:
    """同站多条有效打卡按 (UTC 时间, 事件号) 升序选一条。

    返回 站点序号 -> (入选时间, 入选事件号)；时间相同（平局）时取事件号
    较小者，保证结果确定，且与 splits/explain 给出的解释一致。
    """
    best: dict[int, tuple[datetime, str]] = {}
    for r in rows:
        key = (datetime.fromisoformat(r["occurred_at"]), r["event_no"])
        cur = best.get(r["station_seq"])
        if cur is None or key < cur:
            best[r["station_seq"]] = key
    return best


def _splits_from_best(
    best: dict[int, tuple[datetime, str]], runner_id: str
) -> SplitsOut:
    """由"站点 -> 入选 (时间, 事件号)"按相邻站规则计算分段结果。"""
    count = station_count()
    stations = [
        StationTime(station_seq=seq, event_no=ev, occurred_at=t)
        for seq, (t, ev) in sorted(best.items())
    ]

    segments: list[Segment] = []
    for k in range(1, count):
        a = best.get(k)
        b = best.get(k + 1)
        if a is None and b is None:
            seg = Segment(
                from_station=k,
                to_station=k + 1,
                status="pending",
                reason=f"站点 {k} 与站点 {k + 1} 均缺少有效打卡",
            )
        elif a is None:
            seg = Segment(
                from_station=k,
                to_station=k + 1,
                status="pending",
                reason=f"站点 {k} 缺少有效打卡",
            )
        elif b is None:
            seg = Segment(
                from_station=k,
                to_station=k + 1,
                status="pending",
                reason=f"站点 {k + 1} 缺少有效打卡",
            )
        elif b[0] < a[0]:
            seg = Segment(
                from_station=k,
                to_station=k + 1,
                status="pending",
                reason=(
                    f"站点 {k + 1} 时间 {b[0].isoformat()} 早于"
                    f"站点 {k} 时间 {a[0].isoformat()}（时间倒退）"
                ),
            )
        else:
            seconds = int((b[0] - a[0]).total_seconds())
            seg = Segment(
                from_station=k,
                to_station=k + 1,
                status="ok",
                duration_seconds=seconds,
                duration=_fmt_duration(seconds),
            )
        segments.append(seg)

    complete = all(s.status == "ok" for s in segments)
    total_seconds = (
        sum(s.duration_seconds for s in segments) if complete else None
    )
    return SplitsOut(
        runner_id=runner_id,
        station_count=count,
        complete=complete,
        total_seconds=total_seconds,
        total=_fmt_duration(total_seconds) if total_seconds is not None else None,
        stations=stations,
        segments=segments,
    )


def _summary(splits: SplitsOut) -> RunnerSummary:
    return RunnerSummary(
        complete=splits.complete,
        total_seconds=splits.total_seconds,
        total=splits.total,
    )


def explain_splits(conn: sqlite3.Connection, runner_id: str) -> SplitsExplainOut:
    """解释实时分段采用哪些打卡，并返回按入选事件计算的现有分段结果。

    单次查询取出该选手全部打卡（含已更正 / 已撤销），入选解释与分段结果
    在这同一批数据上推导，二者严格一致；每次查询都基于最新数据，因此
    更正、撤销、补传即时反映到解释中。
    """
    rows = conn.execute(
        "SELECT * FROM checkins WHERE runner_id = ?"
        " ORDER BY station_seq, occurred_at, event_no",
        (runner_id,),
    ).fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail=f"选手 {runner_id} 无任何打卡记录")

    # 仅 active 参与入选；同站按 (UTC 时间, 事件号) 升序选一条
    best = _select_best([r for r in rows if r["status"] == ACTIVE])
    # 被更正事件号 -> 取代它的事件号（仅限同选手记录内可关联时）
    corrected_by = {r["corrects"]: r["event_no"] for r in rows if r["corrects"]}

    stations: list[StationSelection] = []
    for row in rows:  # 已按站点、时间、事件号排序
        if not stations or stations[-1].station_seq != row["station_seq"]:
            stations.append(
                StationSelection(station_seq=row["station_seq"], checkins=[])
            )
        stations[-1].checkins.append(_selection_for(row, best, corrected_by))

    return SplitsExplainOut(
        runner_id=runner_id,
        station_count=station_count(),
        stations=stations,
        splits=_splits_from_best(best, runner_id),
    )


def _selection_for(
    row: sqlite3.Row,
    best: dict[int, tuple[datetime, str]],
    corrected_by: dict[str, str],
) -> CheckinSelection:
    """生成单条打卡的入选情况；未入选时说明原因。"""
    chosen = best.get(row["station_seq"])
    selected = (
        row["status"] == ACTIVE
        and chosen is not None
        and row["event_no"] == chosen[1]
    )
    reason = None
    if not selected:
        if row["status"] == SUPERSEDED:
            by = corrected_by.get(row["event_no"])
            reason = f"已更正：被事件 {by} 取代" if by else "已更正"
        elif row["status"] == REVOKED:
            reason = "已撤销"
        elif chosen is not None and datetime.fromisoformat(row["occurred_at"]) == chosen[0]:
            reason = f"同时间平局：事件号大于入选事件 {chosen[1]}"
        else:
            reason = f"较晚：同站入选事件 {chosen[1]} 时间更早"
    return CheckinSelection(
        event_no=row["event_no"],
        occurred_at=datetime.fromisoformat(row["occurred_at"]),
        status=row["status"],
        selected=selected,
        reason=reason,
    )


def preview_correction(
    conn: sqlite3.Connection, payload: CorrectionPreview
) -> PreviewCorrectionOut:
    """预演更正：与正式更正相同的校验和写入逻辑，但整体回滚。

    在 SAVEPOINT 内模拟"原事件被取代 + 新事件生效"，按现有同站最早与
    相邻站规则重算受影响选手（跨选手时含原选手与新选手）的更正前后结果，
    随后回滚：不占用事件号、不改变打卡状态或成绩，相同请求随后可正式提交。
    """
    origin = _validate_payload(conn, payload)  # corrects 必填，origin 必非 None

    runner_ids = [origin["runner_id"]]
    if payload.runner_id not in runner_ids:
        runner_ids.append(payload.runner_id)

    before = {rid: _compute_splits(conn, rid) for rid in runner_ids}

    occurred = _to_utc(payload.occurred_at)
    conn.execute("SAVEPOINT preview_correction")
    try:
        _insert_correction(conn, payload, occurred, _now())
        after = {rid: _compute_splits(conn, rid) for rid in runner_ids}
    finally:
        conn.execute("ROLLBACK TO SAVEPOINT preview_correction")
        conn.execute("RELEASE SAVEPOINT preview_correction")

    affected = []
    for rid in runner_ids:
        b, a = before[rid], after[rid]
        changes = [
            SegmentChange(
                from_station=seg_b.from_station,
                to_station=seg_b.to_station,
                before=seg_b,
                after=seg_a,
            )
            for seg_b, seg_a in zip(b.segments, a.segments)
            if (seg_b.status, seg_b.duration_seconds, seg_b.reason)
            != (seg_a.status, seg_a.duration_seconds, seg_a.reason)
        ]
        affected.append(
            RunnerPreview(
                runner_id=rid,
                before=_summary(b),
                after=_summary(a),
                changed_segments=changes,
            )
        )
    return PreviewCorrectionOut(
        event_no=payload.event_no,
        runner_id=payload.runner_id,
        station_seq=payload.station_seq,
        occurred_at=occurred,
        corrects=payload.corrects,
        affected_runners=affected,
    )


# ---------- 中立时段试算 ----------


def _merge_windows(
    windows: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """合并重叠或相接的时间窗；返回按起点升序、互不重叠也不相接的窗口。"""
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            last_start, last_end = merged[-1]
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _overlap_seconds(
    start: datetime, end: datetime, windows: list[tuple[datetime, datetime]]
) -> int:
    """[start, end] 与若干互不重叠时间窗的交集总秒数。"""
    total = timedelta(0)
    for ws, we in windows:
        lo, hi = max(start, ws), min(end, we)
        if hi > lo:
            total += hi - lo
    return int(total.total_seconds())


def compute_neutralized_splits(
    conn: sqlite3.Connection, runner_id: str, payload: NeutralizedCreate
) -> NeutralizedOut:
    """中立时段试算：评估暂停计时后的净成绩。

    - 时间窗统一转 UTC（无时区按 UTC 处理），终点不晚于起点的区间拒绝（422）；
    - 先合并重叠或相接的时间窗，再按各有效分段的起终打卡时间裁剪扣除：
      合并后的窗口互不重叠，且每段只在自己 [起, 终] 区间内交集，
      因此跨段或重叠不会重复扣秒；
    - 原始分段复用"仅有效打卡 + 同站最早入选 + 相邻站"规则，与实时分段
      来自同一次数据库读取；补传、更正、撤销后重新请求即反映最新打卡；
    - 缺站或时间倒退的分段保持待定；只有全部分段有效时给出净总秒数；
    - 纯试算：不写入数据库，不影响实时成绩与已发布版本。
    """
    windows: list[tuple[datetime, datetime]] = []
    for i, w in enumerate(payload.windows):
        start, end = _to_utc(w.start), _to_utc(w.end)
        if end <= start:
            raise HTTPException(
                status_code=422,
                detail=f"第 {i} 个时间窗终点 {end.isoformat()} "
                f"不晚于起点 {start.isoformat()}",
            )
        windows.append((start, end))
    merged = _merge_windows(windows)

    any_row = conn.execute(
        "SELECT 1 FROM checkins WHERE runner_id = ? LIMIT 1", (runner_id,)
    ).fetchone()
    if any_row is None:
        raise HTTPException(status_code=404, detail=f"选手 {runner_id} 无任何打卡记录")

    # 与实时分段同一次读取：仅有效打卡、同站最早入选、相邻站重算
    rows = conn.execute(
        "SELECT * FROM checkins WHERE runner_id = ? AND status = ?",
        (runner_id, ACTIVE),
    ).fetchall()
    best = _select_best(rows)
    splits = _splits_from_best(best, runner_id)

    segments: list[NeutralizedSegment] = []
    for seg in splits.segments:
        if seg.status != "ok":
            segments.append(
                NeutralizedSegment(
                    from_station=seg.from_station,
                    to_station=seg.to_station,
                    status="pending",
                    reason=seg.reason,
                )
            )
            continue
        start_t, end_t = best[seg.from_station][0], best[seg.to_station][0]
        deducted = _overlap_seconds(start_t, end_t, merged)
        segments.append(
            NeutralizedSegment(
                from_station=seg.from_station,
                to_station=seg.to_station,
                status="ok",
                raw_seconds=seg.duration_seconds,
                deducted_seconds=deducted,
                net_seconds=seg.duration_seconds - deducted,
            )
        )

    complete = splits.complete
    total = splits.total_seconds if complete else None
    deducted_total = (
        sum(s.deducted_seconds for s in segments) if complete else None
    )
    net_total = total - deducted_total if complete else None
    return NeutralizedOut(
        runner_id=runner_id,
        station_count=splits.station_count,
        complete=complete,
        windows=[NeutralWindow(start=s, end=e) for s, e in merged],
        segments=segments,
        total_seconds=total,
        deducted_seconds=deducted_total,
        net_seconds=net_total,
        net=_fmt_duration(net_total) if net_total is not None else None,
    )


# ---------- 设备时钟校准试算 ----------


def _validate_calibration_windows(
    windows: list[CalibrationWindow],
) -> dict[int, list[tuple[datetime, datetime, int]]]:
    """校验并归一化校准窗，返回 站点序号 -> [(start, end, offset)]（按起点升序）。

    时间统一转 UTC（无时区按 UTC 处理），区间左闭右开；终点不晚于起点、
    站点越界、同站窗口重叠均返回 422，相接（前窗终点 == 后窗起点）允许。
    """
    count = station_count()
    per_station: dict[int, list[tuple[datetime, datetime, int]]] = {}
    for i, w in enumerate(windows):
        if w.station_seq > count:
            raise HTTPException(
                status_code=422,
                detail=f"第 {i} 个校准窗站点序号 {w.station_seq} 超出范围（1..{count}）",
            )
        start, end = _to_utc(w.start), _to_utc(w.end)
        if end <= start:
            raise HTTPException(
                status_code=422,
                detail=f"第 {i} 个校准窗终点 {end.isoformat()} "
                f"不晚于起点 {start.isoformat()}",
            )
        per_station.setdefault(w.station_seq, []).append(
            (start, end, w.offset_seconds)
        )

    for seq, wins in per_station.items():
        wins.sort()
        for (prev_start, prev_end, _), (start, end, _) in zip(wins, wins[1:]):
            if start < prev_end:
                raise HTTPException(
                    status_code=422,
                    detail=f"站点 {seq} 的校准窗重叠："
                    f"[{prev_start.isoformat()}, {prev_end.isoformat()}) 与 "
                    f"[{start.isoformat()}, {end.isoformat()})",
                )
    return per_station


def _apply_calibration(
    t: datetime, windows: list[tuple[datetime, datetime, int]]
) -> datetime:
    """按原始打卡时间匹配左闭右开校准窗；命中则叠加偏移，未命中保持原值。"""
    for start, end, offset in windows:
        if start <= t < end:
            return t + timedelta(seconds=offset)
    return t


def compute_calibrated_splits(
    conn: sqlite3.Connection, runner_id: str, payload: CalibratedCreate
) -> CalibratedOut:
    """设备时钟校准试算：评估按校准窗修正打卡时间后的成绩。

    - 校准窗校验与归一化见 _validate_calibration_windows；
    - 从同一次读取的全部 active 打卡出发，按原始打卡时间匹配本站校准窗，
      校准时间 = 原始时间 + 偏移（未命中保持原值），再按 (校准时间, 事件号)
      升序重新选取每站最早记录（与实时分段的平局规则一致）；
    - 分段复用相邻站规则：缺站或校准后时间倒退的分段仍为 pending；
    - 响应回显入选事件的原始与校准时间；空窗口时结果与实时分段一致；
    - 纯试算：不写入数据库，不影响实时成绩与已发布版本。
    """
    per_station = _validate_calibration_windows(payload.windows)

    any_row = conn.execute(
        "SELECT 1 FROM checkins WHERE runner_id = ? LIMIT 1", (runner_id,)
    ).fetchone()
    if any_row is None:
        raise HTTPException(status_code=404, detail=f"选手 {runner_id} 无任何打卡记录")

    # 与实时分段同一次读取：仅统计 active 打卡
    rows = conn.execute(
        "SELECT * FROM checkins WHERE runner_id = ? AND status = ?",
        (runner_id, ACTIVE),
    ).fetchall()

    original: dict[str, datetime] = {}
    calibrated: dict[int, tuple[datetime, str]] = {}
    for r in rows:
        t = datetime.fromisoformat(r["occurred_at"])
        original[r["event_no"]] = t
        key = (_apply_calibration(t, per_station.get(r["station_seq"], [])), r["event_no"])
        cur = calibrated.get(r["station_seq"])
        if cur is None or key < cur:
            calibrated[r["station_seq"]] = key

    splits = _splits_from_best(calibrated, runner_id)
    stations = [
        CalibratedStation(
            station_seq=seq,
            event_no=ev,
            occurred_at=original[ev],
            calibrated_at=t,
        )
        for seq, (t, ev) in sorted(calibrated.items())
    ]
    windows = [
        CalibrationWindow(
            station_seq=seq, start=start, end=end, offset_seconds=offset
        )
        for seq, wins in sorted(per_station.items())
        for start, end, offset in wins
    ]
    return CalibratedOut(
        runner_id=runner_id,
        station_count=splits.station_count,
        complete=splits.complete,
        windows=windows,
        stations=stations,
        segments=splits.segments,
        total_seconds=splits.total_seconds,
        total=splits.total,
    )


# ---------- 成绩发布 ----------


def publish_results(conn: sqlite3.Connection) -> PublicationOut:
    """发布当前实时成绩的整版快照，生成单调递增的新版本。

    在 BEGIN IMMEDIATE 事务内"先取写锁、再读全部打卡"：持锁期间任何
    并发的打卡 / 更正 / 撤销都被阻塞（见 db.immediate_transaction），
    因此读到的全部选手分段结果来自同一个提交时点，同一版本不会混入
    不同时间点的数据。版本头与全体选手快照在同一事务内原子保存；
    后续打卡、更正或撤销只作用于 checkins 实时表，不影响已发布版本。
    """
    published_at = _now()
    with db.immediate_transaction(conn):
        rows = conn.execute(
            "SELECT DISTINCT runner_id FROM checkins ORDER BY runner_id"
        ).fetchall()
        results = [_compute_splits(conn, row["runner_id"]) for row in rows]

        count = station_count()
        cursor = conn.execute(
            "INSERT INTO publications (published_at, station_count, runner_count)"
            " VALUES (?, ?, ?)",
            (published_at.isoformat(), count, len(results)),
        )
        version = cursor.lastrowid
        conn.executemany(
            "INSERT INTO publication_entries (version, runner_id, position, data)"
            " VALUES (?, ?, ?, ?)",
            [
                (version, splits.runner_id, i, splits.model_dump_json())
                for i, splits in enumerate(results)
            ],
        )

    return PublicationOut(
        version=version,
        published_at=published_at,
        station_count=count,
        results=results,
    )


def _load_publication(conn: sqlite3.Connection, version: int) -> PublicationOut | None:
    header = conn.execute(
        "SELECT * FROM publications WHERE version = ?", (version,)
    ).fetchone()
    if header is None:
        return None
    rows = conn.execute(
        "SELECT data FROM publication_entries WHERE version = ? ORDER BY position",
        (version,),
    ).fetchall()
    return PublicationOut(
        version=header["version"],
        published_at=datetime.fromisoformat(header["published_at"]),
        station_count=header["station_count"],
        results=[SplitsOut.model_validate(json.loads(r["data"])) for r in rows],
    )


def get_publication(conn: sqlite3.Connection, version: int) -> PublicationOut:
    """按版本号返回该版本的完整成绩；不存在时 404。"""
    pub = _load_publication(conn, version)
    if pub is None:
        raise HTTPException(status_code=404, detail=f"成绩版本 {version} 不存在")
    return pub


def get_latest_publication(conn: sqlite3.Connection) -> PublicationOut:
    """返回最新版本完整成绩；尚未发布过任何版本时 404。"""
    header = conn.execute(
        "SELECT MAX(version) AS version FROM publications"
    ).fetchone()
    if header is None or header["version"] is None:
        raise HTTPException(status_code=404, detail="尚未发布任何成绩版本")
    return get_publication(conn, header["version"])


# ---------- 接力队成绩核算 ----------


def compute_relay_team(conn: sqlite3.Connection, payload: RelayTeamCreate) -> RelayTeamOut:
    """基于已发布版本快照核算接力队成绩（只读，不触碰实时成绩）。

    - 提交已发布版本号与按赛段顺序排列的选手号：第 i 人只承担第 i 段
      （站点 i+1 → 站点 i+2），人数须等于该版本站点数 - 1 且不得重复，
      否则 422；版本不存在 404；
    - 各选手的分段与两端入选打卡全部取自同一次加载的版本快照
      （publication_entries），不读取 checkins 实时表，因此发布后的
      打卡 / 更正 / 撤销不影响核算；
    - 任一棒满足以下情形之一时，该棒待定：选手不在版本内（该版本无
      任何打卡记录）；其负责的分段在快照中为 pending（缺站或时间倒退，
      沿用快照中的原因）；交接站下一棒出发早于上一棒到达（时间倒流，
      对应交接站同样记为无法核算）；
    - 所有棒次均可核算时，给出每棒用时、各交接站等待秒数（出发 - 到达，
      可为 0）与队伍总用时（首棒起点至末棒终点），且总用时严格等于
      各棒用时加全部等待；队伍待定时总用时为 null。
    """
    pub = _load_publication(conn, payload.version)
    if pub is None:
        raise HTTPException(
            status_code=404, detail=f"成绩版本 {payload.version} 不存在"
        )

    runner_ids = payload.runner_ids
    expected = pub.station_count - 1
    if len(runner_ids) != expected:
        raise HTTPException(
            status_code=422,
            detail=f"接力队人数 {len(runner_ids)} 不合法："
            f"版本 {payload.version} 站点数为 {pub.station_count}，"
            f"须恰好提交 {expected} 名选手（站点数 - 1）",
        )
    seen: set[str] = set()
    duplicates: list[str] = []
    for rid in runner_ids:
        if rid in seen and rid not in duplicates:
            duplicates.append(rid)
        seen.add(rid)
    if duplicates:
        raise HTTPException(
            status_code=422,
            detail=f"接力队选手号不得重复，重复选手：{', '.join(duplicates)}",
        )

    by_runner = {r.runner_id: r for r in pub.results}

    # 先逐棒收集快照中的分段与两端入选打卡，并记录选手缺失 / 分段 pending
    legs: list[dict] = []
    for i, runner_id in enumerate(runner_ids):
        leg_no = i + 1
        from_station, to_station = leg_no, leg_no + 1
        entry: dict = {
            "leg": leg_no,
            "runner_id": runner_id,
            "from_station": from_station,
            "to_station": to_station,
            "start_event_no": None,
            "start_at": None,
            "finish_event_no": None,
            "finish_at": None,
            "duration_seconds": None,
            "reasons": [],
        }
        splits = by_runner.get(runner_id)
        if splits is None:
            entry["reasons"].append(f"选手 {runner_id} 不在版本 {payload.version} 内")
        else:
            seg = splits.segments[i]  # 快照逐选手覆盖全部分段，序号与棒次一一对应
            start = next(
                (s for s in splits.stations if s.station_seq == from_station), None
            )
            finish = next(
                (s for s in splits.stations if s.station_seq == to_station), None
            )
            if start is not None:
                entry["start_event_no"] = start.event_no
                entry["start_at"] = start.occurred_at
            if finish is not None:
                entry["finish_event_no"] = finish.event_no
                entry["finish_at"] = finish.occurred_at
            if seg.status == "pending":
                entry["duration_seconds"] = None
                entry["reasons"].append(
                    f"选手 {runner_id} 负责的分段 {from_station}→{to_station} "
                    f"在版本中为待定：{seg.reason}"
                )
            else:
                entry["duration_seconds"] = seg.duration_seconds
        legs.append(entry)

    # 逐交接站核算等待；下一棒出发早于上一棒到达时双方都标记待定
    handovers: list[dict] = []
    for leg_no in range(1, len(legs)):
        incoming, outgoing = legs[leg_no - 1], legs[leg_no]
        station_seq = leg_no + 1
        arrived_at = incoming["finish_at"]
        departed_at = outgoing["start_at"]
        handover: dict = {
            "station_seq": station_seq,
            "incoming_runner_id": incoming["runner_id"],
            "outgoing_runner_id": outgoing["runner_id"],
            "arrived_at": arrived_at,
            "departed_at": departed_at,
            "wait_seconds": None,
            "reason": None,
        }
        if arrived_at is None or departed_at is None:
            missing = []
            if arrived_at is None:
                missing.append("上一棒到达")
            if departed_at is None:
                missing.append("下一棒出发")
            handover["reason"] = (
                f"交接站 {station_seq} 缺少{'、'.join(missing)}打卡，无法核算等待"
            )
        elif departed_at < arrived_at:
            reason = (
                f"交接站 {station_seq} 下一棒（选手 {outgoing['runner_id']}）"
                f"出发时间 {departed_at.isoformat()} 早于上一棒"
                f"（选手 {incoming['runner_id']}）到达时间 {arrived_at.isoformat()}"
            )
            handover["reason"] = reason
            # 时间倒流：下一棒整体不能成立
            outgoing["reasons"].append(reason)
            outgoing["duration_seconds"] = None
        else:
            handover["wait_seconds"] = int(
                (departed_at - arrived_at).total_seconds()
            )
        handovers.append(handover)

    leg_outs: list[RelayLegOut] = []
    for entry in legs:
        duration_seconds = entry["duration_seconds"]
        leg_outs.append(
            RelayLegOut(
                leg=entry["leg"],
                runner_id=entry["runner_id"],
                from_station=entry["from_station"],
                to_station=entry["to_station"],
                start_event_no=entry["start_event_no"],
                start_at=entry["start_at"],
                finish_event_no=entry["finish_event_no"],
                finish_at=entry["finish_at"],
                duration_seconds=duration_seconds,
                duration=_fmt_duration(duration_seconds)
                if duration_seconds is not None
                else None,
                reason="；".join(entry["reasons"]) if entry["reasons"] else None,
            )
        )

    handover_outs = [
        RelayHandoverOut(
            station_seq=h["station_seq"],
            incoming_runner_id=h["incoming_runner_id"],
            outgoing_runner_id=h["outgoing_runner_id"],
            arrived_at=h["arrived_at"],
            departed_at=h["departed_at"],
            wait_seconds=h["wait_seconds"],
            wait=_fmt_duration(h["wait_seconds"])
            if h["wait_seconds"] is not None
            else None,
            reason=h["reason"],
        )
        for h in handovers
    ]

    complete = all(h.reason is None for h in handover_outs) and all(
        l.reason is None for l in leg_outs
    )
    total_seconds: int | None = None
    if complete:
        total_seconds = sum(
            l.duration_seconds for l in leg_outs
        ) + sum(h.wait_seconds for h in handover_outs)
        # 首棒起点至末棒终点必须与"各棒用时 + 全部等待"恒等
        if legs:
            span = int(
                (legs[-1]["finish_at"] - legs[0]["start_at"]).total_seconds()
            )
            assert span == total_seconds, (span, total_seconds)
        else:
            total_seconds = 0  # 版本仅有 1 个站点（无赛段）的退化情形
    return RelayTeamOut(
        version=payload.version,
        station_count=pub.station_count,
        runner_ids=runner_ids,
        complete=complete,
        total_seconds=total_seconds,
        total=_fmt_duration(total_seconds) if total_seconds is not None else None,
        legs=leg_outs,
        handovers=handover_outs,
    )


# ---------- 关门线试算 ----------


def compute_cutoffs(
    conn: sqlite3.Connection, version: int, payload: CutoffsCreate
) -> CutoffsOut:
    """基于已发布版本快照的关门线试算（只读，不触碰实时成绩，不写库）。

    - 版本不存在返回 404（版本校验先于关门线规则校验）；截止秒数数量须恰好
      等于版本站点数 - 1、且逐站严格递增，否则 422；
    - 各选手的入选打卡与分段全部取自同一次加载的版本快照
      （publication_entries），不读取 checkins 实时表，因此发布后的
      打卡 / 更正 / 撤销不影响试算结果；
    - 按站点顺序逐站比较"累计到站秒数 = 本站入选打卡时间 - 第 1 站
      入选打卡时间"与本站截止秒数：累计 <= 截止为通过（恰在截止时间
      也算通过），首次累计 > 截止记录关门站，其后各站一律标记已关门，
      即使后续打卡重新赶上也不恢复；
    - 第 1 站缺少入选打卡时，各站一律无法判定；尚未关门时遇到待定分段
      （缺站或时间倒退），该到站站及其后各站一律无法判定，不用后续打卡
      推测；已关门之后再遇缺站 / 待定，仍保持已关门。
    """
    pub = _load_publication(conn, version)
    if pub is None:
        raise HTTPException(status_code=404, detail=f"成绩版本 {version} 不存在")

    cutoffs = payload.cutoff_seconds
    expected = pub.station_count - 1
    if len(cutoffs) != expected:
        raise HTTPException(
            status_code=422,
            detail=f"截止秒数数量 {len(cutoffs)} 不合法：版本 {version} "
            f"站点数为 {pub.station_count}，须恰好提交 {expected} 个"
            "（第 2 站至终点，每站一个）",
        )
    for i in range(1, len(cutoffs)):
        if cutoffs[i] <= cutoffs[i - 1]:
            raise HTTPException(
                status_code=422,
                detail=f"截止秒数须逐站严格递增：第 {i + 2} 站为 {cutoffs[i]}，"
                f"不大于第 {i + 1} 站的 {cutoffs[i - 1]}",
            )

    runners = [_cutoffs_for_runner(splits, cutoffs) for splits in pub.results]
    return CutoffsOut(
        version=version,
        station_count=pub.station_count,
        cutoff_seconds=cutoffs,
        runners=runners,
    )


def _cutoffs_for_runner(
    splits: SplitsOut, cutoffs: list[int]
) -> CutoffRunnerOut:
    """按站点顺序对单个选手做关门线判定（数据全部来自版本快照）。

    状态机三种状态：
    - racing：尚未关门且累计到站秒数可由连续的有效分段推出；
    - closed：已在某站首次超时，其后一律已关门；
    - unknown：第 1 站缺打卡，或尚未关门时遇到待定分段，该站及其后
      各站无法判定，不用后续打卡推测。
    """
    by_station = {s.station_seq: s for s in splits.stations}
    seg_by_to = {seg.to_station: seg for seg in splits.segments}

    first = by_station.get(1)
    if first is None:
        # 第 1 站缺打卡：没有出发基准，各站一律无法判定，不用后续打卡推测
        stations = [
            CutoffStationOut(
                station_seq=seq,
                cutoff_seconds=cutoffs[seq - 2],
                verdict="unknown",
                event_no=None,
                cumulative_seconds=None,
                reason="第 1 站缺少有效打卡，缺少累计到站基准",
            )
            for seq in range(2, splits.station_count + 1)
        ]
        return CutoffRunnerOut(
            runner_id=splits.runner_id,
            status="unknown",
            cutoff_station=None,
            stations=stations,
        )

    state = "racing"  # racing | closed | unknown
    cumulative = 0
    cutoff_station: int | None = None
    stations: list[CutoffStationOut] = []

    for seq in range(2, splits.station_count + 1):
        cutoff = cutoffs[seq - 2]
        seg = seg_by_to[seq]
        station = by_station.get(seq)

        if state == "closed":
            # 关门之后一律保持已关门；缺站 / 待定同样不改变结论
            stations.append(
                CutoffStationOut(
                    station_seq=seq,
                    cutoff_seconds=cutoff,
                    verdict="closed",
                    event_no=station.event_no if station is not None else None,
                    cumulative_seconds=None,
                    reason=f"已在站点 {cutoff_station} 关门，后续站点一律视为已关门",
                )
            )
            continue

        if state == "unknown" or seg.status != "ok":
            # 尚未关门时遇到待定分段（缺站或时间倒退）：本站及其后各站
            # 无法判定，不用后续打卡推测
            state = "unknown"
            reason = (
                f"分段 {seg.from_station}→{seg.to_station} 待定：{seg.reason}"
                if seg.status == "pending"
                else "此前分段待定，无法累计到站秒数"
            )
            stations.append(
                CutoffStationOut(
                    station_seq=seq,
                    cutoff_seconds=cutoff,
                    verdict="unknown",
                    event_no=station.event_no if station is not None else None,
                    cumulative_seconds=None,
                    reason=reason,
                )
            )
            continue

        # racing 且前一分段有效：累计秒数沿连续有效分段累加，
        # 等价于 本站入选时间 - 第 1 站入选时间（此时本站入选打卡必然存在）
        cumulative += seg.duration_seconds
        assert station is not None
        if cumulative <= cutoff:
            stations.append(
                CutoffStationOut(
                    station_seq=seq,
                    cutoff_seconds=cutoff,
                    verdict="pass",
                    event_no=station.event_no,
                    cumulative_seconds=cumulative,
                    reason=None,
                )
            )
        else:
            state = "closed"
            cutoff_station = seq
            over = cumulative - cutoff
            stations.append(
                CutoffStationOut(
                    station_seq=seq,
                    cutoff_seconds=cutoff,
                    verdict="overtime",
                    event_no=station.event_no,
                    cumulative_seconds=cumulative,
                    reason=f"累计 {cumulative} 秒超过截止 {cutoff} 秒，超时 {over} 秒",
                )
            )

    return CutoffRunnerOut(
        runner_id=splits.runner_id,
        status=state,
        cutoff_station=cutoff_station,
        stations=stations,
    )
