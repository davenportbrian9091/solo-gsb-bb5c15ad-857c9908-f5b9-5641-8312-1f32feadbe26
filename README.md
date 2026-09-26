# 越野赛分段计时 API

基于 FastAPI + SQLite 的越野赛分段计时服务：

- 接收带**唯一事件号**、选手号、站点序号、发生时间的打卡；
- 支持**批量登记**（1~100 条，同一事务，批内可更正前面刚写入的记录，任一失败整体回滚）；
- 支持按事件号**更正**（新事件取代原事件）与**撤销**；
- 支持**更正预演**：试算更正前后结果，整体回滚，不产生任何副作用；
- 同一站点多条有效打卡按 (UTC 时间, 事件号) 升序取**第一条**（时间相同取事件号较小者）；
- 按站点顺序重算相邻站分段用时，**乱序补传**可填补缺段，更正 / 撤销即时更新结果；
- 相邻站缺打卡或**时间倒退**时，该分段标记为 `pending`（待定）并说明原因；
- 提供**分段入选解释**：按站点列出该选手全部打卡（事件号、时间、状态、是否入选），
  仅 `active` 参与，同站按 (UTC 时间, 事件号) 升序选一条，未入选记录说明原因
  （已更正 / 已撤销 / 较晚 / 同时间平局），并同返回按入选事件计算的现有分段，
  解释与分段基于**同一次数据库读取**；
- 提供**中立时段试算**：提交若干起止时间窗（统一转 UTC，重叠或相接的先合并），
  按各有效分段的起终打卡时间裁剪扣除暂停计时，逐段返回原始 / 扣除 / 净秒数，
  全部有效时给出净总秒数；纯试算**不写库**，不影响实时成绩与已发布版本；
- 提供**设备时钟校准试算**：提交 0~100 条站点校准窗（区间左闭右开，同站重叠拒绝、
  相接允许），按原始打卡时间匹配窗口叠加有符号偏移后，重新选取每站最早记录并
  复用相邻站分段规则试算成绩；返回入选事件的原始与校准时间；纯试算**不写库**；
- 支持**成绩发布**：在同一 SQLite 事务内读取当前打卡、按现有分段规则为所有有记录选手
  （含未完赛、保留 `pending` 分段者）生成**单调递增、不可变的版本快照**并整版原子保存；
- 已发布版本按版本号 / 最新版查询；后续打卡、更正或撤销只影响实时成绩，**不改已发布版本**；
- 支持**接力队成绩核算**：提交已发布版本号与按赛段顺序排列的选手号（每段仅由对应选手承担，
  人数 = 版本站点数 - 1 且不得重复），从**同一版本快照**读取各人的对应分段与两端入选打卡
  （**不读实时成绩**），逐棒返回用时与交接等待；选手不在版本内、负责分段 `pending` 或交接站
  下一棒出发早于上一棒到达时队伍待定（空总用时、逐棒原因），否则给出队伍总用时
  （首棒起点至末棒终点 = 各棒用时 + 全部等待）；
- 支持**关门线试算**：提交已发布版本号与第 2 站至终点各站相对第 1 站出发的**截止秒数**
  （数量 = 版本站点数 - 1、逐站严格递增），从**同一版本快照**读取入选打卡与分段
  （**不读实时成绩**），按选手号返回最终状态、首次关门站及逐站判定与累计秒数；
  恰在截止时刻算通过，首次超时后各站一律已关门、不因后来赶上而恢复；第 1 站缺打卡或
  尚未关门时遇到待定分段则该站及后续站点无法判定；纯试算**不写库**；
- 提供选手分段结果查询。

## 启动

```bash
docker compose up --build
```

一条命令即可：容器启动时自动建表，数据库为空时写入示例数据（选手 R001–R006，覆盖完整成绩、乱序补传、缺站、更正、撤销、时间倒退等场景）。

- 本地端口：**8000**
- API 地址：http://localhost:8000
- 交互式文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/api/health

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `STATION_COUNT` | `6` | 赛道站点总数，分段为 1→2 … (N-1)→N |
| `DB_PATH` | 容器内 `/data/timing.db`，本地 `timing.db` | SQLite 数据库文件路径 |

Compose 通过命名卷 `timing-data` 持久化数据；删除卷（`docker compose down -v`）后重启会重新初始化并播种示例数据。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/checkins` | 登记打卡；携带 `corrects` 字段即为更正指定事件 |
| `POST` | `/api/checkins/batch` | 批量登记 1~100 条打卡：同一事务处理，任一失败整体回滚 |
| `POST` | `/api/checkins/preview-correction` | 预演更正：返回更正前后结果对照，整体回滚、无副作用 |
| `POST` | `/api/checkins/{event_no}/revoke` | 按事件号撤销打卡 |
| `GET` | `/api/checkins?runner_id=` | 打卡列表（含已更正 / 已撤销记录） |
| `GET` | `/api/checkins/{event_no}` | 按事件号查询单条打卡 |
| `GET` | `/api/runners/{runner_id}/splits` | 选手实时分段结果 |
| `GET` | `/api/runners/{runner_id}/splits/explain` | 分段入选解释：按站点列出全部打卡及是否入选（含原因），并返回按入选事件计算的现有分段 |
| `POST` | `/api/runners/{runner_id}/splits/neutralized` | 中立时段试算：扣除暂停计时后的净成绩，逐段返回原始 / 扣除 / 净秒数（纯试算，不写库） |
| `POST` | `/api/runners/{runner_id}/splits/calibrated` | 设备时钟校准试算：按站点校准窗偏移打卡时间后重选重算，返回入选事件的原始与校准时间及完整试算成绩（纯试算，不写库） |
| `POST` | `/api/publications` | 发布成绩版本：同一事务读取当前打卡、整版快照原子保存，返回新版本完整成绩 |
| `GET` | `/api/publications/{version}` | 按版本号查询该版本完整成绩（不存在返回 404） |
| `GET` | `/api/publications/latest` | 查询最新发布版本完整成绩（尚未发布时返回 404） |
| `POST` | `/api/publications/{version}/cutoffs` | 关门线试算：基于版本快照逐选手评估各站关门状态与累计秒数（只读快照，不写库） |
| `POST` | `/api/relay-teams` | 接力队成绩核算：基于已发布版本快照，按赛段顺序的选手号逐棒核算用时与交接等待（只读快照） |

### 示例

登记打卡：

```bash
curl -X POST http://localhost:8000/api/checkins \
  -H 'Content-Type: application/json' \
  -d '{"event_no":"E1001","runner_id":"R100","station_seq":1,"occurred_at":"2026-09-25T08:00:00"}'
```

更正（`E1002` 取代 `E1001`）：

```bash
curl -X POST http://localhost:8000/api/checkins \
  -H 'Content-Type: application/json' \
  -d '{"event_no":"E1002","runner_id":"R100","station_seq":1,"occurred_at":"2026-09-25T08:05:00","corrects":"E1001"}'
```

批量登记（1~100 条，按顺序在同一事务内处理）：

```bash
curl -X POST http://localhost:8000/api/checkins/batch \
  -H 'Content-Type: application/json' \
  -d '{
    "checkins": [
      {"event_no":"E2001","runner_id":"R100","station_seq":1,"occurred_at":"2026-09-25T08:00:00"},
      {"event_no":"E2002X","runner_id":"R100","station_seq":2,"occurred_at":"2026-09-25T12:00:00"},
      {"event_no":"E2002","runner_id":"R100","station_seq":2,"occurred_at":"2026-09-25T08:32:00","corrects":"E2002X"},
      {"event_no":"E2003","runner_id":"R100","station_seq":3,"occurred_at":"2026-09-25T09:12:00"}
    ]
  }'
```

每条沿用单条登记的字段与校验；批内后续记录可用 `corrects` 更正**本批前面刚写入且仍有效**的事件
（上例中 `E2002` 更正同批的 `E2002X`）。全部成功时返回 `201`，携带各条事件号（按提交顺序）以及
受影响选手按现有分段规则计算的最终完成状态与总用时：

```json
{
  "results": [
    {"index": 0, "event_no": "E2001"},
    {"index": 1, "event_no": "E2002X"},
    {"index": 2, "event_no": "E2002"},
    {"index": 3, "event_no": "E2003"}
  ],
  "affected_runners": [
    {"runner_id": "R100", "complete": false, "total_seconds": null, "total": null}
  ]
}
```

任一条失败时整批回滚（新增记录与状态变更全部撤销），响应为对应语义的错误码（404 / 409 / 422），
并在 `detail.failures` 中给出失败下标（从 0 开始）与原因。前向引用（`corrects` 指向本批后面才出现
的事件，含更正自身）、批内重复事件号、无效更正（目标不存在或已失效）均按此返回：

```json
{
  "detail": {
    "message": "批量打卡在下标 2 处失败，整批已回滚",
    "failures": [
      {"index": 2, "event_no": "E2003", "reason": "事件号 E2003 与本批下标 0 重复"}
    ]
  }
}
```

预演更正（字段与正式更正相同，`corrects` 必填且须指向有效事件）：

```bash
curl -X POST http://localhost:8000/api/checkins/preview-correction \
  -H 'Content-Type: application/json' \
  -d '{"event_no":"E1002","runner_id":"R100","station_seq":1,"occurred_at":"2026-09-25T08:05:00","corrects":"E1001"}'
```

预演与正式更正使用相同的事件号唯一性、站点范围与状态校验；按现有"同站取最早 + 相邻站"规则
试算更正前后结果，返回受影响选手各自的完成状态、总用时及**仅发生变化的分段**；更正跨选手时
会同时覆盖原选手与新选手。整个预演在事务内试算后**整体回滚**：不占用事件号、不改变打卡状态
或成绩，相同请求随后可原样正式提交。响应节选：

```json
{
  "event_no": "E1002",
  "corrects": "E1001",
  "affected_runners": [
    {
      "runner_id": "R100",
      "before": {"complete": false, "total_seconds": null, "total": null},
      "after": {"complete": true, "total_seconds": 12600, "total": "3:30:00"},
      "changed_segments": [
        {
          "from_station": 1,
          "to_station": 2,
          "before": {"from_station": 1, "to_station": 2, "status": "pending", "reason": "站点 1 缺少有效打卡"},
          "after": {"from_station": 1, "to_station": 2, "status": "ok", "duration_seconds": 1920, "duration": "0:32:00"}
        }
      ]
    }
  ]
}
```

撤销：

```bash
curl -X POST http://localhost:8000/api/checkins/E1002/revoke
```

查询分段结果：

```bash
curl http://localhost:8000/api/runners/R001/splits
```

响应节选：

```json
{
  "runner_id": "R001",
  "complete": true,
  "total_seconds": 12600,
  "total": "3:30:00",
  "segments": [
    {"from_station": 1, "to_station": 2, "status": "ok", "duration_seconds": 1920, "duration": "0:32:00"},
    {"from_station": 3, "to_station": 4, "status": "pending", "reason": "站点 4 缺少有效打卡"}
  ]
}
```

## 分段入选解释

查询实时分段**采用了哪些打卡**（选手无任何打卡记录时返回 `404`）：

```bash
curl http://localhost:8000/api/runners/R004/splits/explain
```

按站点列出该选手**全部打卡**的事件号、时间、状态及是否入选；仅 `active` 打卡参与，
同一站点按 (UTC 时间, 事件号) 升序选一条，未入选记录给出原因
（`已更正` / `已撤销` / `较晚` / `同时间平局`）。响应中的 `splits` 为按这些入选事件
计算的现有分段结果，与解释基于**同一次数据库读取**，二者严格一致；更正、撤销、补传后
解释即时更新。响应节选（R004 第 2 站的错误打卡 `S-R004-2X` 已被 `S-R004-2` 更正）：

```json
{
  "runner_id": "R004",
  "station_count": 6,
  "stations": [
    {
      "station_seq": 2,
      "checkins": [
        {"event_no": "S-R004-2", "occurred_at": "2026-09-25T08:40:00Z", "status": "active", "selected": true, "reason": null},
        {"event_no": "S-R004-2X", "occurred_at": "2026-09-25T12:30:00Z", "status": "superseded", "selected": false, "reason": "已更正：被事件 S-R004-2 取代"}
      ]
    }
  ],
  "splits": {
    "runner_id": "R004",
    "complete": true,
    "total_seconds": 13680,
    "total": "3:48:00",
    "segments": [
      {"from_station": 1, "to_station": 2, "status": "ok", "duration_seconds": 2280, "duration": "0:38:00"}
    ]
  }
}
```

## 中立时段试算

赛事方可提交若干中立时段（暂停计时）的起止时间，评估扣除后的净成绩
（选手无任何打卡记录时返回 `404`）：

```bash
curl -X POST http://localhost:8000/api/runners/R001/splits/neutralized \
  -H 'Content-Type: application/json' \
  -d '{
    "windows": [
      {"start": "2026-09-25T08:10:00", "end": "2026-09-25T08:20:00"},
      {"start": "2026-09-25T16:15:00+08:00", "end": "2026-09-25T16:25:00+08:00"}
    ]
  }'
```

规则：

- 原始分段复用实时成绩的"仅有效打卡 + 同站最早入选 + 相邻站"规则，与实时分段
  基于**同一次数据库读取**生成；补传、更正、撤销后重新请求即反映最新打卡；
- 时间窗统一转 UTC（无时区按 UTC 处理，可混用带时区偏移的写法，如上例）；
  **终点不晚于起点的区间返回 `422`**；
- 重叠或相接的时间窗**先合并**，再按各有效分段的起终打卡时间**裁剪扣除**，
  跨段或重叠不会重复扣秒；响应中的 `windows` 回显合并后的 UTC 时间窗；
- 逐段返回 `raw_seconds`（原始秒数）、`deducted_seconds`（扣除秒数）、
  `net_seconds`（净秒数）；缺站或时间倒退的分段保持 `pending`，三项均为 `null`；
- 只有全部分段有效时才给出 `total_seconds` / `deducted_seconds` / `net_seconds`
  （净总秒数）与 `net`（H:MM:SS），否则均为 `null`；
- **纯试算**：不写入数据库，不改变实时成绩与已发布版本。

响应节选（上例两个时间窗合并为 08:10–08:25 UTC，落在分段 1→2 内）：

```json
{
  "runner_id": "R001",
  "station_count": 6,
  "complete": true,
  "windows": [
    {"start": "2026-09-25T08:10:00Z", "end": "2026-09-25T08:25:00Z"}
  ],
  "segments": [
    {"from_station": 1, "to_station": 2, "status": "ok", "raw_seconds": 1920, "deducted_seconds": 900, "net_seconds": 1020},
    {"from_station": 2, "to_station": 3, "status": "ok", "raw_seconds": 2400, "deducted_seconds": 0, "net_seconds": 2400}
  ],
  "total_seconds": 12600,
  "deducted_seconds": 900,
  "net_seconds": 11700,
  "net": "3:15:00"
}
```

## 设备时钟校准试算

计时设备时钟不准时，可提交若干**站点校准窗**试算修正后的成绩
（选手无任何打卡记录时返回 `404`）：

```bash
curl -X POST http://localhost:8000/api/runners/R001/splits/calibrated \
  -H 'Content-Type: application/json' \
  -d '{
    "windows": [
      {"station_seq": 1, "start": "2026-09-25T08:00:00", "end": "2026-09-25T09:00:00", "offset_seconds": -600},
      {"station_seq": 2, "start": "2026-09-25T16:32:00+08:00", "end": "2026-09-25T17:00:00+08:00", "offset_seconds": 60}
    ]
  }'
```

规则：

- 每条校准窗含 `station_seq`（站点号）、`start` / `end`（起止时间）与
  `offset_seconds`（**有符号整数**秒偏移）；一次请求 0~100 条；
- 时间统一转 UTC（无时区按 UTC 处理，可混用带时区偏移的写法，如上例），
  区间为**左闭右开** `[start, end)`；**终点不晚于起点、站点越界、同站窗口重叠
  均返回 `422`**，相接（前窗终点 == 后窗起点）允许；
- 从**同一次读取**的全部 `active` 打卡出发，按**原始打卡时间**匹配本站校准窗：
  命中则 校准时间 = 原始时间 + 偏移，未命中保持原值；再按 (校准时间, 事件号)
  升序**重新选取每站最早记录**（平局取事件号较小者，与实时分段规则一致），
  随后复用相邻站分段规则重算——缺站或校准后时间倒退的分段仍为 `pending`；
- 响应回显归一化后的校准窗（UTC，按站点、起点升序）、各站入选事件的
  **原始时间 `occurred_at` 与校准时间 `calibrated_at`**，以及完整试算成绩
  （分段、总用时）；**空窗口请求的结果与实时分段一致**；
- **纯试算**：不写入数据库，不改变实时成绩与已发布版本。

响应节选（站 1 校准为 07:50，站 2 校准为 08:33）：

```json
{
  "runner_id": "R001",
  "station_count": 6,
  "complete": true,
  "windows": [
    {"station_seq": 1, "start": "2026-09-25T08:00:00Z", "end": "2026-09-25T09:00:00Z", "offset_seconds": -600},
    {"station_seq": 2, "start": "2026-09-25T08:32:00Z", "end": "2026-09-25T09:00:00Z", "offset_seconds": 60}
  ],
  "stations": [
    {"station_seq": 1, "event_no": "S-R001-1", "occurred_at": "2026-09-25T08:00:00Z", "calibrated_at": "2026-09-25T07:50:00Z"},
    {"station_seq": 2, "event_no": "S-R001-2", "occurred_at": "2026-09-25T08:32:00Z", "calibrated_at": "2026-09-25T08:33:00Z"}
  ],
  "segments": [
    {"from_station": 1, "to_station": 2, "status": "ok", "duration_seconds": 2580, "duration": "0:43:00"}
  ],
  "total_seconds": 13200,
  "total": "3:40:00"
}
```

## 成绩发布与版本查询

发布（无需请求体）：在**同一 SQLite 事务**内先取得写锁再读取全部当前打卡，按现有分段规则
（同站取最早、相邻站重算、缺站 / 时间倒退为 `pending`）为**所有有打卡记录的选手**生成完整成绩，
版本号从 1 起单调递增，版本头与全体选手快照原子提交：

```bash
curl -X POST http://localhost:8000/api/publications
```

```json
{
  "version": 1,
  "published_at": "2026-09-25T03:00:00+00:00",
  "station_count": 6,
  "results": [
    {
      "runner_id": "R001",
      "station_count": 6,
      "complete": true,
      "total_seconds": 12600,
      "total": "3:30:00",
      "stations": [
        {"station_seq": 1, "event_no": "S-R001-1", "occurred_at": "2026-09-25T08:00:00+00:00"}
      ],
      "segments": [
        {"from_station": 1, "to_station": 2, "status": "ok", "duration_seconds": 1920, "duration": "0:32:00"}
      ]
    },
    {
      "runner_id": "R003",
      "complete": false,
      "total_seconds": null,
      "total": null,
      "segments": [
        {"from_station": 3, "to_station": 4, "status": "pending", "reason": "站点 4 缺少有效打卡"}
      ]
    }
  ]
}
```

- 未完赛选手同样出现在 `results` 中并保留各自的 `pending` 分段（如上例 R003）；
- 发布事务先取写锁，**并发的打卡 / 更正 / 撤销会被阻塞至本次发布完成**，因此同一版本的全部
  选手成绩来自同一个提交时点，不会混入不同时间点的数据（连接设置了 5 秒 `busy_timeout`）。

按版本号查询完整成绩：

```bash
curl http://localhost:8000/api/publications/1
```

查询最新版本（尚未发布过任何版本时返回 `404`；指定版本不存在同样返回 `404`）：

```bash
curl http://localhost:8000/api/publications/latest
```

**已发布版本不可变**：发布之后新的打卡、更正或撤销只影响实时成绩
（`GET /api/runners/{id}/splits`），不会改动任何已发布版本；再次调用
`POST /api/publications` 才会生成下一个版本号（2、3、…）：

```bash
# 发布 v1 后撤销 R001 的第 6 站并补传新打卡
curl -X POST http://localhost:8000/api/checkins/S-R001-6/revoke
curl -X POST http://localhost:8000/api/checkins \
  -H 'Content-Type: application/json' \
  -d '{"event_no":"E3001","runner_id":"R001","station_seq":6,"occurred_at":"2026-09-25T11:35:00"}'

curl -X POST http://localhost:8000/api/publications   # -> version 2，反映新实时成绩
curl http://localhost:8000/api/publications/1         # 仍是发布时的 v1 快照
curl http://localhost:8000/api/publications/latest    # -> version 2
```

## 接力队成绩核算

接力队由若干选手按赛段顺序组成：提交**已发布成绩版本号**与按赛段顺序排列的**选手号数组**，
第 `i` 名选手只承担第 `i` 个赛段（站点 `i+1 → i+2`）。核算**只读指定版本的快照**
（`publication_entries`），完全不读取 `checkins` 实时表，因此发布后的打卡 / 更正 / 撤销
不影响结果。

- 人数必须恰好等于**该版本站点数 - 1**（分段数），选手号不得重复，否则返回 `422`；
  版本不存在返回 `404`（版本校验先于人数 / 重复校验）；
- 每棒从该选手在版本快照中的**对应分段**与**两端入选打卡**（站点、事件号、时间）取值；
- 交接站（站点 2 … N-1）的**等待秒数** = 下一棒出发时间 − 上一棒到达时间，同时到达 / 出发
  时等待为 `0`（合法）；
- 出现以下任一情形时对应棒次待定，整队 `complete=false`、`total_seconds/total` 为 `null`，
  并在 `legs[].reason` / `handovers[].reason` 给出逐棒原因：
  - 选手不在版本内（该版本快照中无此选手的任何记录）；
  - 该选手负责的分段在版本中为 `pending`（缺站或时间倒退，原因沿用快照）；
  - 交接站下一棒出发**早于**上一棒到达（时间倒流，下一棒标记待定，该交接等待无法核算）；
- 全部棒次可核算时，队伍总用时为**首棒起点至末棒终点**，并严格等于
  **各棒用时之和 + 全部交接等待之和**。

请求示例（先为五名接力选手登记各自负责赛段两端的打卡并发布）：

```bash
curl -X POST http://localhost:8000/api/checkins -H 'Content-Type: application/json' \
  -d '{"event_no":"T1-S1","runner_id":"T1","station_seq":1,"occurred_at":"2026-09-25T08:00:00"}'
curl -X POST http://localhost:8000/api/checkins -H 'Content-Type: application/json' \
  -d '{"event_no":"T1-S2","runner_id":"T1","station_seq":2,"occurred_at":"2026-09-25T08:30:00"}'
curl -X POST http://localhost:8000/api/checkins -H 'Content-Type: application/json' \
  -d '{"event_no":"T2-S2","runner_id":"T2","station_seq":2,"occurred_at":"2026-09-25T08:35:00"}'
curl -X POST http://localhost:8000/api/checkins -H 'Content-Type: application/json' \
  -d '{"event_no":"T2-S3","runner_id":"T2","station_seq":3,"occurred_at":"2026-09-25T09:20:00"}'
# … T3 承担 3→4（09:25→10:10）、T4 承担 4→5（10:10→11:00）、T5 承担 5→6（11:05→12:10）
curl -X POST http://localhost:8000/api/publications
```

```bash
curl -X POST http://localhost:8000/api/relay-teams \
  -H 'Content-Type: application/json' \
  -d '{"version": 1, "runner_ids": ["T1", "T2", "T3", "T4", "T5"]}'
```

响应（棒次用时 30/45/45/50/65 分钟，交接等待 5/5/0/5 分钟，总用时 4:10:00）：

```json
{
  "version": 1,
  "station_count": 6,
  "runner_ids": ["T1", "T2", "T3", "T4", "T5"],
  "complete": true,
  "total_seconds": 15000,
  "total": "4:10:00",
  "legs": [
    {"leg": 1, "runner_id": "T1", "from_station": 1, "to_station": 2,
     "start_event_no": "T1-S1", "start_at": "2026-09-25T08:00:00Z",
     "finish_event_no": "T1-S2", "finish_at": "2026-09-25T08:30:00Z",
     "duration_seconds": 1800, "duration": "0:30:00", "reason": null},
    {"leg": 2, "runner_id": "T2", "from_station": 2, "to_station": 3,
     "start_event_no": "T2-S2", "start_at": "2026-09-25T08:35:00Z",
     "finish_event_no": "T2-S3", "finish_at": "2026-09-25T09:20:00Z",
     "duration_seconds": 2700, "duration": "0:45:00", "reason": null}
  ],
  "handovers": [
    {"station_seq": 2, "incoming_runner_id": "T1", "outgoing_runner_id": "T2",
     "arrived_at": "2026-09-25T08:30:00Z", "departed_at": "2026-09-25T08:35:00Z",
     "wait_seconds": 300, "wait": "0:05:00", "reason": null},
    {"station_seq": 3, "incoming_runner_id": "T2", "outgoing_runner_id": "T3",
     "arrived_at": "2026-09-25T09:20:00Z", "departed_at": "2026-09-25T09:25:00Z",
     "wait_seconds": 300, "wait": "0:05:00", "reason": null}
  ]
}
```

待定时（下例中 `R003` 在种子版本 v1 内缺第 4 站、其负责的 3→4 分段为 `pending`）
`complete=false`、总用时为 `null`，相应棒次的 `duration_seconds/duration` 为 `null`
并给出原因；仍可核算的其他棒次与交接站照常返回：

```bash
curl -X POST http://localhost:8000/api/relay-teams \
  -H 'Content-Type: application/json' \
  -d '{"version": 1, "runner_ids": ["R001", "R002", "R003", "R004", "R006"]}'
```

```json
{
  "complete": false,
  "total_seconds": null,
  "total": null,
  "legs": [
    {"leg": 3, "runner_id": "R003", "from_station": 3, "to_station": 4,
     "start_event_no": "S-R003-3", "start_at": "2026-09-25T09:35:00Z",
     "finish_event_no": null, "finish_at": null,
     "duration_seconds": null, "duration": null,
     "reason": "选手 R003 负责的分段 3→4 在版本中为待定：站点 4 缺少有效打卡"}
  ]
}
```

## 关门线试算

赛事方在设定 / 调整各站关门时间时，可提交**已发布成绩版本号**与**第 2 站至终点各站
相对第 1 站出发的截止秒数**，试算每个选手会在哪一站关门。试算**只读指定版本的快照**
（`publication_entries`），完全不读取 `checkins` 实时表、也不写入任何数据，因此发布后的
打卡 / 更正 / 撤销不影响同版本的试算结果（与接力核算一样，结果只随版本变化）。

请求字段为 `cutoff_seconds`（也接受简称 `cutoffs`）：数组按站点顺序给出第 2 站、第 3 站
…… 第 N 站的截止秒数，数量须恰好等于**该版本站点数 - 1**，且**逐站严格递增**（相邻相等
也拒绝），否则返回 `422`；指定版本不存在返回 `404`（版本校验先于截止秒数校验）。

```bash
curl -X POST http://localhost:8000/api/publications/1/cutoffs \
  -H 'Content-Type: application/json' \
  -d '{"cutoff_seconds": [2000, 4400, 7000, 10000, 13000]}'
```

规则：

- 逐选手按站点顺序比较**累计到站秒数**（本站入选打卡时间 − 第 1 站入选打卡时间），
  累计秒数 **<= 截止秒数算通过，恰在截止时刻也算通过**；累计秒数 > 截止秒数为**首次超时**，
  该站记为关门站（`over`）并记录为选手的 `first_closed_station`；
- 首次关门后，**后续站点一律标记已关门**（`closed`），即使后续打卡重新落在关门线内
  （后来赶上）也**不恢复**，且关门后各站不再给出累计秒数；
- **第 1 站缺打卡**（无出发基准）时，该选手第 1 站及后续站点全部标记无法判定（`unknown`），
  不使用其后续打卡推测；
- **尚未关门**时遇到**待定分段**（缺站或时间倒退，原因沿用快照），该分段到站及后续站点
  全部标记 `unknown`，不用后续打卡反推累计秒数；一旦已经关门，之后的缺站 / 时间倒退只标记
  `closed`，不再转为无法判定；
- 按选手号返回版本内**全体有打卡记录选手**的最终状态（`finished` 各站均在关门线内 /
  `closed` 有关门站 / `unknown` 无法判定）、首次关门站及第 1 站至终点的逐站判定
  （状态、该站截止秒数、累计秒数与 H:MM:SS、原因）；
- **纯试算**：不写入数据库，不改变实时成绩与已发布版本，重复请求结果稳定。

响应节选（种子版本 v1，截止秒数 `[2000, 4400, 7000, 10000, 13000]`：
R001 全程在关门线内完赛；R002 第 2 站 2400 秒超过截止 2000 秒，首次关门后各站均已关门；
R003 在关门线内通过前 3 站后，因缺第 4 站（分段待定）第 4 站起无法判定）：

```json
{
  "version": 1,
  "station_count": 6,
  "cutoff_seconds": [2000, 4400, 7000, 10000, 13000],
  "runners": [
    {
      "runner_id": "R001",
      "status": "finished",
      "first_closed_station": null,
      "stations": [
        {"station_seq": 1, "cutoff_seconds": 0, "status": "pass",
         "cumulative_seconds": 0, "cumulative": "0:00:00", "reason": null},
        {"station_seq": 2, "cutoff_seconds": 2000, "status": "pass",
         "cumulative_seconds": 1920, "cumulative": "0:32:00", "reason": null},
        {"station_seq": 3, "cutoff_seconds": 4400, "status": "pass",
         "cumulative_seconds": 4320, "cumulative": "1:12:00", "reason": null}
      ]
    },
    {
      "runner_id": "R002",
      "status": "closed",
      "first_closed_station": 2,
      "stations": [
        {"station_seq": 1, "cutoff_seconds": 0, "status": "pass",
         "cumulative_seconds": 0, "cumulative": "0:00:00", "reason": null},
        {"station_seq": 2, "cutoff_seconds": 2000, "status": "over",
         "cumulative_seconds": 2400, "cumulative": "0:40:00",
         "reason": "累计到站 0:40:00（2400 秒）超过关门截止 0:33:20（2000 秒），首次超时"},
        {"station_seq": 3, "cutoff_seconds": 4400, "status": "closed",
         "cumulative_seconds": null, "cumulative": null,
         "reason": "已在第 2 站关门（首次超过关门截止时间），后续站点一律标记已关门"}
      ]
    },
    {
      "runner_id": "R003",
      "status": "unknown",
      "first_closed_station": null,
      "stations": [
        {"station_seq": 4, "cutoff_seconds": 7000, "status": "unknown",
         "cumulative_seconds": null, "cumulative": null,
         "reason": "尚未关门时分段 3→4 待定（站点 4 缺少有效打卡），本站及后续站点无法判定"}
      ]
    }
  ]
}
```

## 本地开发

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload        # 默认监听 8000 端口
pytest                               # 运行测试
```

## 实现说明

- 打卡记录永久保留，更正 / 撤销只改变状态（`active` / `superseded` / `revoked`），便于审计；
- 分段结果在查询时基于全部有效打卡实时重算，因此补传、更正、撤销立即反映到结果中；
- 分段入选解释与分段结果共用同一套"同站按 (UTC 时间, 事件号) 升序选一条有效打卡"的规则，
  并在**同一次数据库读取**上推导：先一次查询取出该选手全部打卡（含已更正 / 已撤销），
  再据此给出每条记录的入选情况与原因、计算现有分段，二者严格一致；
- 时间统一归一化为 UTC 存储，无时区的时间按 UTC 处理；
- 更正与写入在同一事务中完成，事件号唯一性由数据库主键保证（重复返回 409）；
- 批量登记在同一数据库事务内按顺序处理全部记录，任一失败即整体回滚（含已做的状态变更），
  成功时基于全部新数据按现有分段规则重算受影响选手的最终完成状态与总用时；
- 更正预演复用正式更正的校验与写入逻辑，在 `SAVEPOINT` 内试算后 `ROLLBACK TO` 整体回滚，对数据库零副作用；
- 中立时段试算与实时分段共用"同站最早入选 + 相邻站重算"规则并在同一次读取上推导；
  时间窗统一转 UTC 后先合并重叠 / 相接区间，再与各有效分段的 [起, 终] 区间求交集扣除，
  合并后的窗口互不重叠、每段独立裁剪，因此跨段或重叠不会重复扣秒；试算只读不写，
  不影响实时成绩与已发布版本；
- 设备时钟校准试算先校验校准窗（左闭右开、终点晚于起点、站点范围、同站不重叠，
  相接允许），再在同一次读取的全部 `active` 打卡上按原始时间匹配窗口叠加有符号偏移，
  按 (校准时间, 事件号) 重选每站最早记录后直接复用相邻站分段逻辑（缺站 / 倒退仍
  `pending`），因此校准不会修正无效打卡状态、也不补缺站；试算全程只读不写，
  不影响实时成绩与已发布版本；
- 成绩发布在 `BEGIN IMMEDIATE` 事务内"先取写锁、再读全部打卡"，版本头（`publications`，
  `INTEGER PRIMARY KEY AUTOINCREMENT` 保证版本号单调递增、不重用）与全体选手快照
  （`publication_entries`，逐选手 `SplitsOut` JSON）在同一事务内原子提交；持锁期间并发打卡 /
  更正 / 撤销一律阻塞（配合 `busy_timeout`），同一版本不会混入不同时间点的数据；
- 已发布版本是独立于 `checkins` 实时表的快照，后续打卡 / 更正 / 撤销不会改写历史版本；
- 接力队核算先按版本号加载整版快照（不存在 `404`），再校验人数 = 快照 `station_count - 1`
  与选手号不重复（`422`）；各棒的分段状态、用时与两端入选打卡全部取自该选手在快照中的
  `SplitsOut`（棒次与分段序号一一对应），全程不读 `checkins` 实时表，因此结果只随版本变化；
  交接等待 = 下一棒起点入选时间 − 上一棒终点入选时间，出发早于到达时双方标记待定、不给用时
  与等待；总用时由"各棒用时 + 全部等待"求和（与首棒起点至末棒终点的时间跨度恒等，代码内置
  断言保证），任一一棒待定时总用时为 `null`。
- 关门线试算先按版本号加载整版快照（不存在 `404`），再校验截止秒数数量 = 快照
  `station_count - 1` 且逐站严格递增（`422`）；各选手的入选打卡与分段状态全部取自该选手在
  快照中的 `SplitsOut`，全程不读 `checkins` 实时表，因此同版本结果不受实时打卡变化影响。
  判定为按站点顺序的三态扫描：尚未关门时分段 `pending`（缺站 / 时间倒退）则该站起 `unknown`
  且不用后续打卡推测；累计到站秒数 > 本站截止秒数的首站记 `over`，此后各站一律 `closed`、
  不给出累计秒数，后来赶上也不恢复；`<=`（含恰在截止时刻）为 `pass`。第 1 站缺打卡时无出发
  基准，该选手全程 `unknown`。试算只读不写，不产生新版本。
