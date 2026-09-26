"""请求 / 响应模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class CheckinCreate(BaseModel):
    event_no: str = Field(..., min_length=1, max_length=64, description="唯一事件号")
    runner_id: str = Field(..., min_length=1, max_length=64, description="选手号")
    station_seq: int = Field(..., ge=1, description="站点序号（从 1 开始）")
    occurred_at: datetime = Field(..., description="打卡发生时间，ISO 8601")
    corrects: Optional[str] = Field(
        default=None, description="要更正的原事件号；填写后原事件被本事件取代"
    )


class CorrectionPreview(CheckinCreate):
    """更正预演请求：字段与打卡一致，但 corrects 必填且须指向有效事件。"""

    corrects: str = Field(
        ..., min_length=1, max_length=64, description="要更正的原事件号（必填，须为有效事件）"
    )


class CheckinOut(BaseModel):
    event_no: str
    runner_id: str
    station_seq: int
    occurred_at: datetime
    status: str = Field(..., description="active=有效，superseded=已被更正，revoked=已撤销")
    corrects: Optional[str]
    created_at: datetime


class StationTime(BaseModel):
    station_seq: int
    event_no: str
    occurred_at: datetime


class Segment(BaseModel):
    from_station: int
    to_station: int
    status: Literal["ok", "pending"]
    duration_seconds: Optional[int] = Field(default=None, description="分段用时（秒）")
    duration: Optional[str] = Field(default=None, description="分段用时，H:MM:SS")
    reason: Optional[str] = Field(default=None, description="待定原因")


class SplitsOut(BaseModel):
    runner_id: str
    station_count: int
    complete: bool = Field(..., description="所有分段均有效时为 true")
    total_seconds: Optional[int] = Field(default=None, description="总用时（秒），仅 complete 时给出")
    total: Optional[str] = Field(default=None, description="总用时，H:MM:SS")
    stations: list[StationTime] = Field(..., description="各站点采用的有效打卡（同站取最早）")
    segments: list[Segment]


class CheckinSelection(BaseModel):
    """单条打卡在实时分段计算中的入选情况。"""

    event_no: str
    occurred_at: datetime
    status: str = Field(..., description="active=有效，superseded=已被更正，revoked=已撤销")
    selected: bool = Field(..., description="是否被实时分段采用")
    reason: Optional[str] = Field(
        default=None,
        description="未入选原因：已更正 / 已撤销 / 较晚 / 同时间平局；入选时为 null",
    )


class StationSelection(BaseModel):
    """同一站点的全部打卡及各自的入选情况。"""

    station_seq: int
    checkins: list[CheckinSelection] = Field(
        ..., description="该站全部打卡，按 (UTC 时间, 事件号) 升序"
    )


class SplitsExplainOut(BaseModel):
    """实时分段的入选解释，以及按入选事件计算的现有分段结果。

    stations 与 splits 基于同一次数据库读取推导，二者严格一致。
    """

    runner_id: str
    station_count: int
    stations: list[StationSelection] = Field(
        ..., description="按站点列出全部打卡的事件号、时间、状态及是否入选"
    )
    splits: SplitsOut = Field(..., description="按入选事件计算的现有分段结果")


class RunnerSummary(BaseModel):
    """某一时点上选手的完成状态与总用时。"""

    complete: bool = Field(..., description="所有分段均有效时为 true")
    total_seconds: Optional[int] = Field(default=None, description="总用时（秒），仅 complete 时给出")
    total: Optional[str] = Field(default=None, description="总用时，H:MM:SS")


class BatchCheckinCreate(BaseModel):
    """批量打卡请求：1~100 条，按顺序在同一事务内处理。"""

    checkins: list[CheckinCreate] = Field(
        ...,
        min_length=1,
        max_length=100,
        description="按顺序处理的打卡列表（1~100 条）；后续记录可用 corrects "
        "更正本批前面刚写入且仍有效的事件",
    )


class BatchItemOut(BaseModel):
    """批量打卡中单条记录的受理结果。"""

    index: int = Field(..., description="在请求数组中的下标（从 0 开始）")
    event_no: str = Field(..., description="该条打卡的事件号")


class BatchRunnerResult(RunnerSummary):
    """批量提交后某受影响选手的最终完成状态与总用时。"""

    runner_id: str


class BatchCheckinOut(BaseModel):
    """批量打卡响应：仅在整批全部成功时返回。"""

    results: list[BatchItemOut] = Field(..., description="各条打卡的事件号（按提交顺序）")
    affected_runners: list[BatchRunnerResult] = Field(
        ...,
        description="受影响选手（含跨选手更正的原选手）按现有分段规则计算的最终完成状态与总用时",
    )


class SegmentChange(BaseModel):
    """同一分段在更正前后的对照。"""

    from_station: int
    to_station: int
    before: Segment
    after: Segment


class RunnerPreview(BaseModel):
    """单个受影响选手的更正预演结果。"""

    runner_id: str
    before: RunnerSummary = Field(..., description="更正前的完成状态与总用时")
    after: RunnerSummary = Field(..., description="更正后的完成状态与总用时")
    changed_segments: list[SegmentChange] = Field(..., description="仅列出发生变化的分段")


class PreviewCorrectionOut(BaseModel):
    """更正预演响应：整体回滚，不产生任何副作用。"""

    event_no: str
    runner_id: str
    station_seq: int
    occurred_at: datetime
    corrects: str
    affected_runners: list[RunnerPreview] = Field(
        ..., description="受影响选手；更正跨选手时包含原选手与新选手"
    )


class NeutralWindow(BaseModel):
    """中立时段（暂停计时）的起止时间。"""

    start: datetime = Field(..., description="时间窗起点，ISO 8601；无时区按 UTC 处理")
    end: datetime = Field(..., description="时间窗终点，ISO 8601；须晚于起点")


class NeutralizedCreate(BaseModel):
    """中立时段试算请求：若干时间窗，重叠或相接的会先合并再逐段扣除。"""

    windows: list[NeutralWindow] = Field(
        ...,
        max_length=100,
        description="中立时段列表（0~100 个）；重叠或相接的时间窗会先合并",
    )


class NeutralizedSegment(BaseModel):
    """单个分段的中立时段试算结果。"""

    from_station: int
    to_station: int
    status: Literal["ok", "pending"]
    raw_seconds: Optional[int] = Field(default=None, description="原始分段秒数")
    deducted_seconds: Optional[int] = Field(default=None, description="扣除秒数")
    net_seconds: Optional[int] = Field(
        default=None, description="净秒数 = 原始秒数 - 扣除秒数"
    )
    reason: Optional[str] = Field(default=None, description="待定原因")


class NeutralizedOut(BaseModel):
    """中立时段试算结果：纯试算不写库，基于请求时最新打卡计算。"""

    runner_id: str
    station_count: int
    complete: bool = Field(..., description="所有分段均有效时为 true")
    windows: list[NeutralWindow] = Field(
        ..., description="合并后的中立时段（UTC，按起点升序，互不重叠也不相接）"
    )
    segments: list[NeutralizedSegment]
    total_seconds: Optional[int] = Field(
        default=None, description="原始总秒数，仅 complete 时给出"
    )
    deducted_seconds: Optional[int] = Field(
        default=None, description="总扣除秒数，仅 complete 时给出"
    )
    net_seconds: Optional[int] = Field(
        default=None, description="净总秒数，仅 complete 时给出"
    )
    net: Optional[str] = Field(default=None, description="净总用时，H:MM:SS")


class CalibrationWindow(BaseModel):
    """设备时钟校准窗：某站点一段时间范围内统一叠加的时钟偏移。"""

    station_seq: int = Field(..., ge=1, description="站点序号（从 1 开始）")
    start: datetime = Field(..., description="校准窗起点，ISO 8601；无时区按 UTC 处理")
    end: datetime = Field(..., description="校准窗终点，ISO 8601；须晚于起点")
    offset_seconds: int = Field(
        ..., description="有符号整数时钟偏移（秒）：校准时间 = 原始打卡时间 + 偏移"
    )


class CalibratedCreate(BaseModel):
    """设备时钟校准试算请求：0~100 条校准窗，区间左闭右开，同站窗口不得重叠（相接允许）。"""

    windows: list[CalibrationWindow] = Field(
        ...,
        max_length=100,
        description="校准窗列表（0~100 条）；按原始打卡时间匹配，"
        "同站窗口重叠返回 422，相接允许",
    )


class CalibratedStation(BaseModel):
    """某站点入选打卡的原始时间与校准时间。"""

    station_seq: int
    event_no: str
    occurred_at: datetime = Field(..., description="入选事件的原始打卡时间（UTC）")
    calibrated_at: datetime = Field(
        ..., description="校准时间 = 原始打卡时间 + 命中校准窗的偏移；未命中时等于原始时间"
    )


class CalibratedOut(BaseModel):
    """设备时钟校准试算结果：纯试算不写库，基于请求时最新打卡计算。"""

    runner_id: str
    station_count: int
    complete: bool = Field(..., description="校准后所有分段均有效时为 true")
    windows: list[CalibrationWindow] = Field(
        ..., description="归一化后的校准窗（UTC，按站点、起点升序）"
    )
    stations: list[CalibratedStation] = Field(
        ..., description="各站点入选事件的原始与校准时间（按站点升序）"
    )
    segments: list[Segment] = Field(
        ..., description="按校准时间重算的相邻站分段；缺站或倒退仍为 pending"
    )
    total_seconds: Optional[int] = Field(
        default=None, description="校准后总用时（秒），仅 complete 时给出"
    )
    total: Optional[str] = Field(default=None, description="校准后总用时，H:MM:SS")


class RelayTeamCreate(BaseModel):
    """接力队成绩核算请求：指定已发布版本与按赛段顺序排列的选手号。

    人数须等于该版本站点数减一、选手号不得重复；这些约束在服务层
    （版本快照读出后）校验，非法时返回 422。
    """

    version: int = Field(..., description="已发布成绩版本号")
    runner_ids: list[str] = Field(
        ...,
        description="按赛段顺序排列的选手号；第 i 人只承担第 i 段"
        "（站点 i+1 → 站点 i+2），人数须等于版本站点数 - 1 且不得重复",
    )


class RelayLegOut(BaseModel):
    """接力队中单棒的核算结果。"""

    leg: int = Field(..., description="棒次，从 1 开始")
    runner_id: Optional[str] = Field(
        default=None, description="承担本棒的选手号；选手不在版本内时为 null"
    )
    from_station: int
    to_station: int
    start_event_no: Optional[str] = Field(
        default=None, description="本棒起点入选打卡的事件号（取自版本快照）"
    )
    start_at: Optional[datetime] = Field(
        default=None, description="本棒起点入选打卡时间"
    )
    finish_event_no: Optional[str] = Field(
        default=None, description="本棒终点入选打卡的事件号"
    )
    finish_at: Optional[datetime] = Field(
        default=None, description="本棒终点入选打卡时间"
    )
    duration_seconds: Optional[int] = Field(
        default=None, description="本棒用时（秒）；本棒待定时为 null"
    )
    duration: Optional[str] = Field(
        default=None, description="本棒用时，H:MM:SS；本棒待定时为 null"
    )
    reason: Optional[str] = Field(
        default=None, description="本棒待定原因（选手不在版本 / 分段 pending / 交接出发过早）"
    )


class RelayHandoverOut(BaseModel):
    """交接站（两棒之间）的核算结果。"""

    station_seq: int = Field(..., description="交接站序号")
    incoming_runner_id: Optional[str] = Field(
        default=None, description="到达本交接站的上一棒选手号"
    )
    outgoing_runner_id: Optional[str] = Field(
        default=None, description="从本交接站出发的下一棒选手号"
    )
    arrived_at: Optional[datetime] = Field(
        default=None, description="上一棒到达本交接站的时间"
    )
    departed_at: Optional[datetime] = Field(
        default=None, description="下一棒从本交接站出发的时间"
    )
    wait_seconds: Optional[int] = Field(
        default=None,
        description="交接等待秒数 = 下一棒出发 - 上一棒到达；无法核算或出发过早时为 null",
    )
    wait: Optional[str] = Field(
        default=None, description="交接等待，H:MM:SS；无法核算或出发过早时为 null"
    )
    reason: Optional[str] = Field(
        default=None, description="无法核算或出发过早的原因；正常交接时为 null"
    )


class RelayTeamOut(BaseModel):
    """接力队成绩核算结果：全部数据来自同一个已发布版本快照。"""

    version: int = Field(..., description="核算所依据的已发布版本号")
    station_count: int = Field(..., description="该版本的赛道站点总数")
    runner_ids: list[str] = Field(
        ..., description="按赛段顺序提交的选手号"
    )
    complete: bool = Field(
        ...,
        description="所有棒次均可核算（选手在版本内、对应分段非 pending、"
        "交接站下一棒出发不早于上一棒到达）时为 true",
    )
    total_seconds: Optional[int] = Field(
        default=None,
        description="队伍总用时（秒）= 首棒起点至末棒终点 = 各棒用时 + 全部等待；"
        "队伍待定时为 null",
    )
    total: Optional[str] = Field(
        default=None, description="队伍总用时，H:MM:SS；队伍待定时为 null"
    )
    legs: list[RelayLegOut] = Field(..., description="逐棒结果，按棒次升序")
    handovers: list[RelayHandoverOut] = Field(
        ..., description="逐交接站结果，按站点升序（站点 2 … N-1）"
    )


class PublicationOut(BaseModel):
    """一个已发布版本的完整成绩快照。"""

    version: int = Field(..., description="发布版本号，从 1 单调递增")
    published_at: datetime = Field(..., description="发布时间")
    station_count: int = Field(..., description="发布时的赛道站点总数")
    results: list[SplitsOut] = Field(
        ...,
        description="该版本全体有打卡记录选手的完整分段结果（按选手号排序）；"
        "未完赛选手同样保留 pending 分段",
    )
