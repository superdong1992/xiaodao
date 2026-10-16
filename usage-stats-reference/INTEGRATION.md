# 会话长期统计生产接入指南

适用对象：在公司内网接入统计的后端、数据库和 Dashboard 开发人员。更新日期：2026-10-16。

本方案保存已结束 run 的用户消息数，再按会话汇总。业务数据清理后，统计记录继续保留。接入顺序是核对生产字段、适配脚本、确认提交边界、建表、启用写入和补录，最后开放 Dashboard。

现有 [README.txt](README.txt) 是方案概要。本指南补充生产差异核对和操作步骤；配套代码仍是参考实现，没有接入业务入口、定时任务或清理任务，也没有完成生产 PostgreSQL 专项验收。下面标为“待确认”的内容必须在内网核对，不能直接套用本地字段。

## 文件与接入范围

| 文件 | 用途 | 接入时的处理 |
| --- | --- | --- |
| [preflight.sql](preflight.sql) | 只读核对字段和部分数据条件 | 分段执行，最后一段依赖新统计视图 |
| [schema.sql](schema.sql) | 统计明细表、索引和会话汇总视图 | 先确定时区、终态和 schema，再纳入迁移 |
| [backfill.sql](backfill.sql) | 补录缺失或计数滞后的终态 run，也是在线刷新模板 | 核对字段映射，保留在线替换标记 |
| [writer.py](writer.py) | 同步 psycopg 3 写入适配 | 复制到正式 Python 包，随包部署同目录 SQL |
| [dashboard.py](dashboard.py) | asyncpg 汇总和每日趋势查询 | 接入已有连接池、身份认证和授权范围 |

目录名 `usage-stats-reference` 不是示例中的 Python 导入包名。生产可以放到自己的 `usage_stats` 包中，保持 `writer.py` 与 `backfill.sql` 同目录，并核对安装包或镜像确实包含 SQL 文件。

## 统计口径

| 指标 | 当前定义 |
| --- | --- |
| 消息数 | 同一 `conversation_id/run_id` 下，`agent_messages` 行数加 `agent_followup_tasks` 行数；正文和日志附件合算一次输入，每次报告追问再计 1，不过滤两表状态 |
| 会话数 | 所选创建日期范围内，至少有一个终态 run 已写入统计的不同 `conversation_id` 数量 |
| 用户数 | 上述会话中不同非空 `owner_key` 的数量；缺失归属另记为未知用户会话 |
| 每日趋势 | 按会话最初创建日归属，累加其已结束 run 的全部消息 |

永久表是一行一个 run，主键为 `(conversation_id, run_id)`。Dashboard 先按 `conversation_id` 合并再 `COUNT(*)`，不能直接对明细表 `COUNT(*)` 当作会话数。普通视图 `agent_conversation_usage_stats` 提供一行一会话的汇总。

同一会话两轮分别有 3 条和 2 条用户消息，最终计为会话 1、消息 5、用户 1。新一轮未结束时，该轮消息暂不计入。晚结束、后续轮次和已结束 run 的报告追问都会增加过去创建日的数字，因此此趋势不表示消息当天发送量，也不表示 DAU。整段用户数必须单独去重，不能相加每日用户数。

报告追问成功入库就计数，排队、执行中、失败和取消都不影响这一口径；助手回复、事件行和附件数量不计。幂等由业务层保证同一请求只保存一条输入，统计刷新按实际行数重算，不执行 `+1`。内网必须核对报告追问是否只存在于 `agent_followup_tasks`；若同一次输入也写入 `agent_messages`，需先调整映射，不能直接相加。

当前查询包含统计范围内全部用户和租户，未提供按用户或租户授权的过滤。若 Dashboard 面向普通用户，必须在服务端从可信登录态确定可见范围，并将同一范围同时用于汇总、趋势和缓存键；不能直接把这份全局查询暴露给用户。

## 先在内网核对生产差异

### 已知信息和待确认字段

用户已确认：`agent_messages` 只含用户消息，有 `conversation_id` 和 `run_id`；写入驱动为 psycopg 3，repository 会把 `?` 转成 `%s`；事务入口是 `with self.repository.database_transaction() as stats_db`。查询端按需求使用 asyncpg 和 `datetime.date`。

| 项目 | 参考实现的假设 | 生产需要确认或调整的内容 |
| --- | --- | --- |
| 源表位置 | 四张表都在 `public` | 实际 schema、大小写和权限；所有文件中的表引用一起调整 |
| 会话表 | `conversation_id` 唯一，有 `owner_key`、`body` | 用户提供的会话字段已知；核对唯一性、身份稳定性和 JSON 内时间含义 |
| run 表 | 有 `conversation_id`、`run_id`、`ordinal`、`status`、`body` | 这些 run 字段尚未由生产确认；特别是首轮 `ordinal = 1` 和终态保存位置 |
| 关闭对象 | `body` 能提供实际结束的 `run_id` | 用户此前只确认了会话 ID 和时间字段；缺少 run ID 时，必须从关闭操作的真实上下文取得 |
| run 与消息关系 | 两个 ID 一起定位该轮消息 | 核对空 run ID、孤立消息、重复用户请求、跨轮复用或复制消息的情况 |
| 报告追问 | `agent_followup_tasks` 有 `conversation_id`、`run_id`，一行代表一次已入库追问 | 核对业务请求幂等约束、两表是否重复保存同一次输入，以及排队、失败、取消等状态的保留方式 |
| 最初创建时间 | run 或会话 JSON 有 `conversation_created_at`；首轮可用 run 的 `created_at` | 确认追问或重开后哪个字段仍保存会话最初时间，不能把当前轮时间当作会话时间 |
| 本轮时间 | run JSON 的 `created_at`、`completed_at` | 核对字段是否存在、是否带 `Z` 或时区偏移；若是独立列则改 SQL 映射 |
| 终态 | `COMPLETED`、`FAILED`、`INTERRUPTED`、`CANCELLED` | 核对大小写和完整状态列表，同时调整表约束、在线模板、预检和补录查询 |
| 用户身份 | `owner_key` 在统计范围内稳定且唯一 | 跨租户不唯一时增加租户维度，或定义无碰撞的联合身份；历史转移归属需另定口径 |
| 事务封装 | 嵌套事务复用连接和 savepoint | 阅读生产实现和调用链，确认真正最外层提交点；仅有方法名不足以证明独立提交 |
| 终态后追加 | 终态 run 可继续新增报告追问，新增后刷新计数 | 确认追问提交和幂等重试均触发提交后刷新，原关闭入口也保留刷新 |
| 数据保留 | 写统计时源会话、run、诊断输入和追问仍完整 | 核对自动清理、软删除、手动删除和隐私删除；源清理前需外部保护，不能只检查统计行存在 |
| 运行环境 | PostgreSQL 支持当前 DDL，代码返回行支持 `[0]` | 记录 PG、psycopg、asyncpg 版本和 row factory；字典行须改取值方式 |

多 run 能力尚不确定时，保留当前复合主键设计。预检未观察到多 run，只代表当前保留样本没有出现，不能证明产品不会产生。

### 创建时间映射必须单独核对

[backfill.sql](backfill.sql) 当前依次选取：run JSON 的 `conversation_created_at`、会话 JSON 的 `conversation_created_at`、首轮 run JSON 的 `created_at`。它**没有直接使用会话 JSON 的 `created_at`**，因为本地版本在重开时会更新当前轮时间。

若生产的 `agent_conversations.body.created_at` 始终是会话最初创建时间，可将统计 `created_at` 的来源改为该字段。若生产使用独立时间列，则直接映射该列。必须用一条跨轮会话验证：每轮写出的统计 `created_at` 都相同，`run_created_at` 则保留各轮创建时间。

不要用 `updated_at`、当前时间或清理时间补缺失创建时间。无偏移的时间字符串需要先明确源时区后再转换；当前 SQL 的类型转换不会替你检查这一语义。

### 分阶段预检

1. 在内网先查看部署版本、相关表定义、索引及事务实现。只需记录字段、类型、状态枚举和脱敏结论，无需导出正文、附件或连接凭据。
2. 先运行 `preflight.sql` 的 `information_schema.columns` 查询。后续查询只有在对应字段确认存在后才执行，不能第一次就整文件运行。
3. 执行多 run、缺失完成时间和空 run ID 检查。补查源 ID 唯一性、孤立消息、追问请求幂等、两表是否重复保存输入、无时区时间、用户身份和两类输入的保留完整性；现有预检未覆盖这些条件，也不会自动修正异常。
4. 建表并有数据后，再执行最后的 `metadata_conflict` 检查。日期范围查询可能漏掉跨日期的元数据冲突，不能替代全表检查。

大表只读查询同样可能消耗资源。先估算范围并查看执行计划，在内网约定的窗口执行，必要时分段；局部抽查通过不能当作历史全量完整性的证明。

## 适配脚本和事务入口

### 在线 SQL 模板

`writer.py` 会读取同目录 `backfill.sql`，替换以下两个固定字符串：

```text
-- ONLINE_FILTER
LIMIT 1000 -- ONLINE_LIMIT
```

第一处替换成两个 `?` 参数的会话和 run 条件，第二处替换成 `LIMIT 1`。修改字段时保留这些字符串及其位置。每个标记必须恰好出现一次，否则 writer 返回 `False`；适配后仍须核对生成 SQL 只选择指定 run，参数顺序为 `conversation_id`、`run_id`。不要把 `BEGIN`、`COMMIT`、`SET` 或额外查询加进这个共用模板，事务和超时由调用方设置。

写入端经过 repository 的占位符转换，查询端使用 asyncpg 的 `$1`、`$2`。两种占位符不可混用。`fetchone()[0]` 依赖位置取值；生产封装返回字典时同步修改。

### 挂在真正的最外层提交之后

本地 [store.py](../src/problem_locator/agent/store.py) 的 `_close` 会设置完成时间和终态，但由调用方保存 body。统计必须等这些变更和消息一起提交后再查询。先从每个 `_close` 调用点向外追踪，找到最终提交事务的位置，并覆盖成功、失败、取消、重启中断等所有路径。

本地 [database.py](../src/problem_locator/storage/database.py) 的嵌套写事务使用 savepoint。因此在 `_close` 内再次调用 `database_transaction()`，不会得到独立提交的统计事务。生产也须检查是否存在隐式事务；psycopg 对已经开启事务的连接采用嵌套事务语义，见 [psycopg 事务说明](https://www.psycopg.org/psycopg3/docs/basic/transactions.html)。

下面是**插入位置示意，不是可直接复制的完整业务方法**。原有校验、保存、通知、锁和返回逻辑必须保留；变量 `body`、`status`、`business_result` 均来自实际业务操作。

```python
# 前提：此处就是最外层业务事务，外面没有另一层事务或隐式事务。
closed_run_keys = set()
with self.repository.database_transaction() as db:
    # 原有业务操作，取得 body、status。
    self._close(db, body, status)
    self._save(db, body)  # 按生产实际保存入口调整，保留原有业务语义。
    closed_run_keys.add((body["conversation_id"], body["run_id"]))
    # 原有其余业务操作，生成 business_result。

# 仅在上面的最外层事务已确认成功提交后到达这里。
for conversation_id, run_id in closed_run_keys:
    refresh_closed_run(
        self.repository, conversation_id=conversation_id, run_id=run_id,
    )
# 继续原有提交后逻辑并返回 business_result；统计 False 不改业务成功结果。
```

一个事务关闭多个 run 时收集所有实际 ID；不要读取稍后可能已切换的 `current_run_id`。集合只属于本次事务，不能放在共享实例字段；子事务回滚时也必须撤销它对应的待写项。不要在 `finally` 中统计，也不要因事务块内部提前 `return` 而漏掉提交后写入。

若生产事务上下文会吞掉回滚异常，必须用它提供的明确提交结果或可靠的提交回调判断成功，不能仅以“走到 with 后面”作为依据。当前参考包没有提供提交回调，也没有自动把 `_close` 注册到回调上。

`refresh_closed_run` 内部按要求使用 `with repository.database_transaction() as stats_db`。它将普通统计异常记录为警告并返回 `False`，成功插入、更新或保留已有统计时返回 `True`；`True` 本身不证明源数据完整。调用方不应因 `False` 重新执行已经提交的业务。旧函数名 `record_closed_run` 保留为包装，原有关闭调用可以继续使用。后台补录负责恢复漏写或滞后的计数。

### 追问入库后也刷新，幂等分支不能提前返回

本地 [ReportFollowupService.submit](../src/problem_locator/followup/service.py) 先查已有请求，再调用 [FollowupStore.submit](../src/problem_locator/followup/store.py) 入库。后者在自己的事务退出后返回新 `receipt`；前者目前会在命中 `previous` 时直接返回。接入时要把这两个分支汇合到统计刷新处。不要挂在追问成功完成的 worker 回调上，否则排队取消、失败等已入库输入可能漏计。

以下示意对应当前方法结构，`else` 中的所有原有业务检查均须保留：

```python
def submit(self, conversation_id, run_id, request_id, text, owner_key=None):
    request = FollowupRequest(request_id=request_id, text=text)
    with self.agent.operation_lease(conversation_id, owner_key=owner_key):
        previous = self.store.request(
            conversation_id, run_id, request_id, text, owner_key,
        )
        if previous is not None:
            receipt = previous  # 不提前 return；重试也可补偿上次刷新失败。
        else:
            # 保留原有 enabled、停止状态、服务可用性、过期和报告来源检查。
            # source 仍由原有 self._source(...) 检查取得。
            receipt = self.store.submit(
                conversation_id, run_id, request, source, owner_key,
                logs_supported=self.read_search_supported,
            )

        # 前提：store.submit 的最外层事务已提交，外面没有另一个业务事务。
        # operation_lease 在本地是使用保护，不会开启数据库写事务。
        refresh_closed_run(
            self.store.repository,
            conversation_id=receipt.conversation_id,
            run_id=receipt.run_id,
        )
        return receipt
```

使用返回凭据里的 `receipt.run_id`，不能改用当前会话的 `current_run_id`。新提交若失败或回滚，不应执行刷新；成功提交后刷新失败只影响统计，不改变已返回的业务凭据。命中历史请求时也刷新，但因为没有新增输入行，消息数不会增加。

报告已发布不一定表示原 run 已结束。若追问提交时 run 尚未终态且没有统计记录，刷新返回 `False`，之后由原 run 的关闭入口或定期补录计入。因此新增追问挂接不能替代原有关闭挂接，成功、失败、取消和中断等关闭路径仍要覆盖。生产事务边界与本地不一致时，应将刷新移到真实的最外层提交之后。

这是同步写入，仍会增加请求耗时。当前 `statement_timeout = 1000ms` 和 `lock_timeout = 250ms` 只约束进入统计事务后设置生效的 SQL，不能限制连接池等待及事务入口检查的总时间。若要求业务完全不等待统计，需要接入现有可靠后台任务；当前文件没有该功能。异步请求处理器也不能直接在事件循环中执行这段同步数据库操作。

## 建表与权限

1. 确定业务统计时区。`schema.sql` 暂用 `Asia/Shanghai`；这只是示例，不能从服务器时区自动推定。上线前选定后，生成列负责固定日期归属。
2. 用生产迁移账号执行适配后的 DDL。文件自带 `BEGIN/COMMIT`；若迁移框架管理事务，先按框架要求调整。DDL 没有 `IF NOT EXISTS`，重跑前检查已有对象的结构，不用删除表解决冲突。
3. 在线写入账号需要四张源表（包括 `agent_followup_tasks`）的 `SELECT`、统计表 `INSERT/SELECT`、`message_count` 列的 `UPDATE` 和相应 schema 的 `USAGE`，以及现有 repository 自身要求的权限。Dashboard 账号按实际查询对象授予 `SELECT`。账号的租户限制和 RLS 也要在预期身份下验收。
4. 确认业务清理、人工删除和级联操作不会删除统计表；统计表不建立指向源业务表的级联外键。将长期统计纳入既有备份和恢复范围。
5. 检查 `agent_messages(conversation_id, run_id)` 和 `agent_followup_tasks(conversation_id, run_id)` 的查询是否有可用索引，并评估终态候选扫描、统计日期过滤和聚合的计划。不要只因参考包有两个统计索引就假定生产性能已满足要求。

已部署旧参考版时不需要新增表结构，也不要重跑建表或重建统计表。配套替换 `writer.py` 和 `backfill.sql`，增加追问提交后的挂接及上述权限，再对仍完整保留的源数据补录。`record_closed_run` 的旧调用仍可用；新调用推荐使用 `refresh_closed_run`。

统计表保存 ID、用户标识、时间、状态和数量，不保存消息正文或附件。源会话软删除不会自动排除其统计；若隐私删除要求同步处理用户标识或统计记录，应先明确保留政策，再增加对应处理流程。

## 回填与持续补录

先启用已经适配的在线刷新，再从仍完整保留的历史数据回填；两者按同一主键去重。每批独立提交，串行执行并记录变更行数、耗时和失败类别。SQL 保留旧返回列名 `inserted_count`，但该值现在包含新增行和计数更新行，旧调用按列名或位置读取均可。示意如下，`sql` 是已适配的 `backfill.sql` 原文，调用位置不能处于另一个事务中：

```python
with repository.database_transaction() as stats_db:
    stats_db.execute("SET LOCAL lock_timeout = '250ms'")
    stats_db.execute("SET LOCAL statement_timeout = '5s'")
    inserted_count = stats_db.execute(sql).fetchone()[0]
```

5 秒只是回填示例，须按生产执行计划和资源预算设置。当前一批最多处理 1000 个缺失或计数滞后的 run；已有且计数未落后的 run 不占批次名额。1000 只限制写入数量，寻找滞后项时仍会扫描所有候选终态 run 并统计输入，需要在生产评估索引和查询开销。若需要调小，注意在线代码依赖 `LIMIT 1000 -- ONLINE_LIMIT` 的精确替换，模板和 writer 必须配套修改并验证。

回填遍历全部仍保留的终态 run，不限于 `current_run_id`，也不按 `deleted_at` 排除软删除会话。缺少源会话的 run 会被连接条件排除，任一输入表已单独清理都可能导致偏小数量。只增不减能保留既有较大计数，却无法恢复已经清理的输入；若清理后又新增输入，残留行数也可能长期低于旧计数。因此必须先确认历史覆盖边界，不能用最早一条统计的日期推定“自那天起全部完整”。

并发在线刷新可能使某一批变更 0 条。应重新检查缺失或滞后项再判断结束。下面的只读查询只检查**与当前回填筛选条件一致**的待处理 run，不证明消息完整、时间正确，也不包含已没有父会话的 run；生产字段和终态改变时一同修改：

```sql
SELECT COUNT(*) AS pending_run_count
FROM public.agent_conversation_runs r
JOIN public.agent_conversations c ON c.conversation_id = r.conversation_id
LEFT JOIN public.agent_run_usage_stats s
  ON s.conversation_id = r.conversation_id AND s.run_id = r.run_id
WHERE r.status IN ('COMPLETED', 'FAILED', 'INTERRUPTED', 'CANCELLED')
  AND (
      s.run_id IS NULL OR s.message_count < (
          (SELECT COUNT(*) FROM public.agent_messages m
           WHERE m.conversation_id = r.conversation_id AND m.run_id = r.run_id)
        + (SELECT COUNT(*) FROM public.agent_followup_tasks f
           WHERE f.conversation_id = r.conversation_id AND f.run_id = r.run_id)
      )
  );
```

缺失或非法时间可能使整批失败。不要无限重试同一坏批，也不要用当前时间填充。先定位异常 ID，修正映射或登记明确的历史缺口，再继续；若排除某些记录，必须同时记录排除范围和原因。

冲突更新仅在新计数更大时修改 `message_count`，不执行累加，也不修改 `owner_key`、创建时间、完成时间、终态和 `recorded_at` 等元数据。重复关闭、在线刷新和回填不会重复计数；并发较旧快照不能把已提交的新计数降回去。回填可修复仍有完整源数据的历史追问漏计，已有过计和错误元数据则需要单独确定依据、受影响 ID 和审计记录，不会自动修正。

首次回填完成后仍需定时补录，弥补“业务已提交、统计尚未提交时进程退出”的窗口。生产要确定执行周期、积压阈值、负责人和告警路径；当前包没有调度器。

在清理任何源会话、run、诊断输入或追问之前，要确认涉及的每个终态 run 的最新计数已保存，并在同一业务保护范围内阻止继续写入；只检查统计行存在不够。缺失或滞后时先补录，补录失败则暂缓该项清理并告警。这个保护必须由生产清理流程实现，参考包没有自动挂接。仅有定时补录不能保证清理前一定写入；若不能增加清理保护或可靠待办，须将结果标为尽力统计。已经清理的源数据无法从这些脚本恢复。

## Dashboard 接入

将 [dashboard.py](dashboard.py) 放入正式包，传入已有 asyncpg pool。示例查询 2026 年 10 月，包含 10 月 1 日，不包含 11 月 1 日：

```python
from datetime import date

result = await fetch_dashboard(pool, date(2026, 10, 1), date(2026, 11, 1))
```

两个参数必须是 `datetime.date`，不能是字符串或 `datetime.datetime`。API 如果接收日期字符串，在服务端校验并转成 date 后再调用。SQL 对 `created_day DATE` 直接比较 `$1`、`$2`，不需要 `::date` 参数转换；asyncpg 的映射见 [类型与参数说明](https://magicstack.github.io/asyncpg/current/usage.html)。

固定时区的 `created_day` 用于按天筛选和分组，等价于该时区每日零点边界下的会话创建时间范围。若需要任意时分秒区间，另用带时区的 `created_at` 边界，不能复用本接口当作精确时间查询。

返回值包含 `summary` 和 `daily`。汇总包含会话数、用户数、消息数、未知用户会话数及元数据冲突数；发现范围内冲突时函数会抛错。汇总和趋势位于同一个只读 Repeatable Read 事务，避免两次查询看到不同批次，语义见 [PostgreSQL 隔离级别说明](https://www.postgresql.org/docs/current/transaction-iso.html)。

无记录时汇总为 0，`daily` 为 `[]`；无记录的日期不自动补行。只在已确认覆盖的日期范围内补零，未知或已清理的历史应标为未覆盖。Dashboard 还需展示统计覆盖起点和最近一次成功补录时间，这两个元数据没有由当前函数返回，需要接入方维护。不要把 `recorded_at` 的最大值当作完整性证明。

## 上线与回退顺序

1. 在内网填完字段映射、时区、授权范围、历史覆盖和事务边界记录，完成脚本适配。
2. 在与生产版本和封装一致的测试环境执行下面的验收场景，检查 SQL 打包和单 run 筛选。
3. 首次接入应用建表迁移；旧版替换仅更新代码、挂接点和权限。先接好失败监测、持续补录和清理保护，再小范围启用在线刷新。
4. 核对实际结束 run 的统计结果及业务延迟。在线写入稳定后分批回填完整历史，处理异常项。
5. 核对缺失或滞后项、全局元数据一致性和两类输入的样本数量，再按真实授权范围开放 Dashboard，显示数据覆盖范围。

若上线后统计写入影响延迟，关闭接入层的在线统计开关或回退业务挂接，同时保留统计表和既有数据。源码包没有自带开关，需接入方实现。停写期间确保源数据不会先被清理，恢复后用补录修复缺口。若发现数字错误，先暂停展示受影响范围，再核对来源；不要删除统计表重建，也不要把回填当作过计或元数据修订工具。

## 生产验收清单

以下场景须在完成字段适配的环境逐项执行，不能用通用单测通过代替。对账时选择源消息尚完整的样本。

| 场景 | 预期结果 |
| --- | --- |
| 正文加多个附件，随后报告追问两次 | 初次 1 条，最终 3 条；`UNUSED` 等已入库消息仍计数，追问失败或取消也不减 |
| 同一请求重试，包括首次统计失败后重试 | 业务层只落一条用户输入，重试仍刷新但不重复计数；若业务已重复落库，统计会如实计为多条 |
| 报告可追问但原 run 尚未结束 | 追问提交后的刷新暂不创建统计；原 run 关闭或补录后计入全部输入 |
| 一会话两轮分别 3 条和 2 条 | 会话 1、用户 1、消息 5；第二轮未结束前只计首轮 |
| 同一用户两个会话、两个用户不同会话 | 前者用户 1，后者用户 2；空归属进入未知用户会话数 |
| 所有终态路径 | 成功、失败、取消、重启中断等实际状态均写入，单事务多 run 无遗漏 |
| 主事务回滚、嵌套 savepoint 回滚、提前返回 | 未提交的关闭不写统计；成功提交路径不会漏掉挂接 |
| 在线重复写、回填与在线竞争 | 每个主键只有一条，消息数不重复累加；并发旧快照不能覆盖较新计数 |
| 已有统计漏计追问，前面超过 1000 个 run 已准确 | 补录更新滞后计数，准确记录不占本批 1000 个名额；元数据保持原值 |
| 统计 SQL 超时、断连或权限失败 | 已提交业务仍成功，故障可观测；修复后补录恢复 |
| 业务提交后杀进程、统计尚未写入 | 持续补录恢复；源清理受保护 |
| 清理源数据、恢复数据库备份 | 最新统计保存并阻止追加后才清理；已有计数不降低，备份恢复保留统计 |
| 已有统计过计或元数据错误 | 自动刷新不调低、不覆盖元数据，另按有依据的修订流程处理 |
| 跨午夜、跨月、结束日边界 | 按选定时区归属；结束日当天排除；晚结束增加原创建日 |
| 缺时间、坏时间、清理过的消息 | 不误报成完整历史；异常批可定位，覆盖范围如实标注 |
| 同一会话不同轮的用户或创建时间冲突 | 全局检查识别冲突，Dashboard 不发布成可信统计 |
| 用户或租户越权查询 | 服务端拒绝或按可信身份过滤，汇总与趋势保持同一范围 |
| 安装包、时区和实际数据库版本 | SQL 文件存在、在线只选目标 run、生成日期和查询参数正常 |

正式接入本仓库时，按 [Test Flow 说明](../tools/test-flow/README.md) 保存专项结果；推送前先查看 `dev.default --plan-only`，再由规定入口执行完整确定性验证。生产字段适配和上述验收仍需使用实际环境完成，已有仓库 verdict 不替代这一步。

专项用例见 [test_usage_stats_reference.py](../tests/deterministic/postgres/test_usage_stats_reference.py)，执行记录见 [TODO.md](../TODO.md) 和 [FIXED_ISSUES.md](../FIXED_ISSUES.md) 的 `PL-FIX-082`。专项针对本地 PostgreSQL 和参考代码，不代表已接入业务、通过完整 Test Flow 或完成内网生产验收。

## 内网接入记录模板

以下内容留在内网即可。若还需协助，只提供允许共享的字段定义、伪代码和脱敏结论，不提供正文、附件、用户标识或凭据。

```text
部署代码版本：
PostgreSQL / psycopg / asyncpg 版本：
源表 schema 和字段映射：
会话最初创建时间来源：
本轮创建和结束时间来源及源时区：
实际结束 run_id 的取得位置：
终态枚举及各关闭入口：
最外层事务提交位置及回滚行为：
用户与租户的唯一标识和 Dashboard 授权范围：
统计日期时区：
可证明完整的历史覆盖范围及已知缺口：
SQL 随包部署位置和查询索引：
在线开关、连接等待及 SQL 超时设置：
补录周期、清理保护、告警阈值和负责人：
验收结果、上线时间和回退负责人：
```
