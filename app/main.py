"""FastAPI 入口：越野赛分段计时 API。"""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from typing import Iterator, Optional

from fastapi import Depends, FastAPI

from . import db, seed, service
from .models import (
    BatchCheckinCreate,
    BatchCheckinOut,
    CalibratedCreate,
    CalibratedOut,
    CheckinCreate,
    CheckinOut,
    CorrectionPreview,
    CutoffsCreate,
    CutoffsOut,
    NeutralizedCreate,
    NeutralizedOut,
    PreviewCorrectionOut,
    PublicationOut,
    RelayTeamCreate,
    RelayTeamOut,
    SplitsExplainOut,
    SplitsOut,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 容器启动时初始化数据库；库为空时写入示例数据
    db.init_db()
    conn = db.get_conn()
    try:
        seed.seed_if_empty(conn)
    finally:
        conn.close()
    yield


app = FastAPI(
    title="越野赛分段计时 API",
    version="1.6.0",
    description=(
        "接收带唯一事件号的打卡，支持按事件号更正 / 撤销、批量登记（同一事务，"
        "任一失败整体回滚），并可预演更正（整体回滚）；"
        "按站点顺序重算相邻站分段用时，缺站或时间倒退的分段标记为待定；"
        "提供分段入选解释：按站点列出全部打卡及是否入选，"
        "与分段结果基于同一次数据库读取；"
        "支持中立时段试算：扣除暂停计时后的净成绩，纯试算不写库；"
        "支持设备时钟校准试算：按站点校准窗偏移打卡时间后重选重算，纯试算不写库；"
        "支持发布不可变成绩版本（同一事务整版原子快照）并按版本 / 最新版查询；"
        "支持基于已发布版本快照的接力队成绩核算：按赛段顺序提交选手号，"
        "逐棒取用时与交接等待，只读版本快照、不读实时成绩；"
        "支持基于已发布版本快照的关门线试算：提交第 2 站至终点各站相对"
        "第 1 站出发的截止秒数，逐选手按站点顺序比较累计到站秒数，"
        "只读版本快照、纯试算不写库。"
    ),
    lifespan=lifespan,
)


def get_db() -> Iterator[sqlite3.Connection]:
    conn = db.get_conn()
    try:
        yield conn
    finally:
        conn.close()


@app.get("/", summary="服务信息")
def root() -> dict:
    return {
        "service": "trail-race split timing API",
        "docs": "/docs",
        "station_count": service.station_count(),
    }


@app.get("/api/health", summary="健康检查")
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/api/checkins",
    response_model=CheckinOut,
    status_code=201,
    summary="登记打卡（携带 corrects 即为更正）",
)
def post_checkin(
    payload: CheckinCreate, conn: sqlite3.Connection = Depends(get_db)
) -> CheckinOut:
    return service.create_checkin(conn, payload)


@app.post(
    "/api/checkins/batch",
    response_model=BatchCheckinOut,
    status_code=201,
    summary="批量登记打卡（1~100 条，同一事务，任一失败整体回滚）",
)
def post_checkins_batch(
    payload: BatchCheckinCreate, conn: sqlite3.Connection = Depends(get_db)
) -> BatchCheckinOut:
    return service.create_checkins_batch(conn, payload)


@app.post(
    "/api/checkins/preview-correction",
    response_model=PreviewCorrectionOut,
    summary="预演更正（试算更正前后结果，整体回滚，无副作用）",
)
def preview_correction(
    payload: CorrectionPreview, conn: sqlite3.Connection = Depends(get_db)
) -> PreviewCorrectionOut:
    return service.preview_correction(conn, payload)


@app.post(
    "/api/checkins/{event_no}/revoke",
    response_model=CheckinOut,
    summary="按事件号撤销打卡",
)
def revoke_checkin(
    event_no: str, conn: sqlite3.Connection = Depends(get_db)
) -> CheckinOut:
    return service.revoke_checkin(conn, event_no)


@app.get("/api/checkins", response_model=list[CheckinOut], summary="打卡列表")
def list_checkins(
    runner_id: Optional[str] = None, conn: sqlite3.Connection = Depends(get_db)
) -> list[CheckinOut]:
    return service.list_checkins(conn, runner_id)


@app.get(
    "/api/checkins/{event_no}",
    response_model=CheckinOut,
    summary="按事件号查询打卡",
)
def get_checkin(
    event_no: str, conn: sqlite3.Connection = Depends(get_db)
) -> CheckinOut:
    return service.get_checkin(conn, event_no)


@app.get(
    "/api/runners/{runner_id}/splits",
    response_model=SplitsOut,
    summary="查询选手分段结果",
)
def get_splits(
    runner_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> SplitsOut:
    return service.get_splits(conn, runner_id)


@app.get(
    "/api/runners/{runner_id}/splits/explain",
    response_model=SplitsExplainOut,
    summary="解释实时分段采用哪些打卡（含按入选事件计算的现有分段）",
)
def get_splits_explain(
    runner_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> SplitsExplainOut:
    return service.explain_splits(conn, runner_id)


@app.post(
    "/api/runners/{runner_id}/splits/neutralized",
    response_model=NeutralizedOut,
    summary="中立时段试算：扣除暂停计时后的净成绩（纯试算，不写库）",
)
def post_splits_neutralized(
    runner_id: str, payload: NeutralizedCreate, conn: sqlite3.Connection = Depends(get_db)
) -> NeutralizedOut:
    return service.compute_neutralized_splits(conn, runner_id, payload)


@app.post(
    "/api/runners/{runner_id}/splits/calibrated",
    response_model=CalibratedOut,
    summary="设备时钟校准试算：按站点校准窗偏移打卡时间后重选重算（纯试算，不写库）",
)
def post_splits_calibrated(
    runner_id: str, payload: CalibratedCreate, conn: sqlite3.Connection = Depends(get_db)
) -> CalibratedOut:
    return service.compute_calibrated_splits(conn, runner_id, payload)


@app.post(
    "/api/publications",
    response_model=PublicationOut,
    status_code=201,
    summary="发布成绩版本（同一事务读取当前打卡、整版原子保存）",
)
def post_publication(
    conn: sqlite3.Connection = Depends(get_db),
) -> PublicationOut:
    return service.publish_results(conn)


@app.get(
    "/api/publications/latest",
    response_model=PublicationOut,
    summary="查询最新发布版本完整成绩",
)
def get_publication_latest(
    conn: sqlite3.Connection = Depends(get_db),
) -> PublicationOut:
    return service.get_latest_publication(conn)


@app.get(
    "/api/publications/{version}",
    response_model=PublicationOut,
    summary="按版本号查询完整成绩",
)
def get_publication(
    version: int, conn: sqlite3.Connection = Depends(get_db)
) -> PublicationOut:
    return service.get_publication(conn, version)


@app.post(
    "/api/publications/{version}/cutoffs",
    response_model=CutoffsOut,
    summary="关门线试算：按版本快照逐选手评估各站关门状态（纯试算，不写库）",
)
def post_publication_cutoffs(
    version: int,
    payload: CutoffsCreate,
    conn: sqlite3.Connection = Depends(get_db),
) -> CutoffsOut:
    return service.compute_cutoffs(conn, version, payload)


@app.post(
    "/api/relay-teams",
    response_model=RelayTeamOut,
    summary="接力队成绩核算（基于已发布版本快照，只读不读实时成绩）",
)
def post_relay_team(
    payload: RelayTeamCreate, conn: sqlite3.Connection = Depends(get_db)
) -> RelayTeamOut:
    return service.compute_relay_team(conn, payload)
