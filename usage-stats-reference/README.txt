会话长期统计参考方案（2026-10-13）

用途
供公司内网核对和适配，没有连接生产数据库，也未接入当前项目的业务代码。
生产版本和部分字段尚未确认；这不是已验证可上线的补丁，没有 Test Flow verdict。

已确认口径
生产 agent_messages 只保存用户消息，并包含 conversation_id、run_id；直接 COUNT(*) 即用户消息数。
一次成功入库的用户输入计一条，正文和日志附件合算；每次追加输入再加一条。
助手回复、附件数、日志行数不计。幂等重试不重复计，被拒绝且未入库的请求不计。
已入库但最后未采用的输入仍计数，不按消息状态过滤。

方案
一张永久表保存每个终态 run 的消息数，主键为 (conversation_id, run_id)。
查询先按 conversation_id 汇总，再 count(*)；同时提供一行一会话的普通视图。
兼容单轮和多轮会话，不需要先断定生产是否支持多 run。
会话数表示至少有一轮结束的会话数；正在执行的新一轮消息等该轮结束才计入。
COMPLETED、FAILED、INTERRUPTED、CANCELLED 都计入，终态列表仍需与生产核对。

日期和用户
示例时区暂用 Asia/Shanghai，首次建表前必须确认或修改，不能直接当作已确定配置。
created_at 是会话最初创建时间，run_created_at 是本轮创建时间。
created_day 由数据库生成；查询直接与 datetime.date 参数比较，不写日期参数 cast。
只有建表表达式使用 CAST(... AS DATE)。日期范围包含开始日，不包含结束日。
这等价于按固定时区零点划分的 created_at 范围，只用于按天的边界。
所有源时间字符串必须带时区偏移或 Z，不能靠连接默认时区猜测。
这是按会话创建日归属的累计输入量，不是消息当天发生量，也不是 DAU。
晚结束或多轮会话会增加过去日期的数字。
owner_key 必须稳定，并在统计范围内唯一；跨租户不唯一时需定义联合标识。
NULL/空 owner_key 不计入独立用户数，接口另返回未知用户会话数，不能冒充完整用户数。
每日独立用户数不能相加成整段独立用户数。
同一会话的 owner_key 和 created_at 必须一致；上线前及定期执行 preflight.sql 的一致性检查。
范围查询只能发现范围内的冲突，不能代替全表一致性检查。

生产字段前提（参考用户描述和本地代码，尚待生产核对）
agent_conversations: conversation_id、owner_key、body。
agent_conversation_runs: conversation_id、run_id、ordinal（首轮为 1）、status、body。
agent_messages: conversation_id、run_id；一行代表一次成功入库的用户输入（用户已确认）。
写入驱动为 psycopg 3，repository 将 ? 占位符转为 %s（用户已确认）。
run body: created_at、completed_at；非首轮保存 conversation_created_at。
body 可以是 JSONB 或合法 JSON TEXT。源表 schema 示例为 public。
终态 run 不再新增消息，统计前消息仍完整。若只清理消息但保留 run，零条无法与真实零条区分。
缺失或非法时间会让本批失败，不用 updated_at 或当前时间替代；先修正映射或确认历史处理办法。

接入
1. 先执行 preflight.sql 的字段检查，确认统计时区和上述前提。
2. 执行 schema.sql；不要把统计表纳入业务清理，也不要添加级联删除外键。
3. writer.py 使用 repository.database_transaction() 和 ? 占位符，复用现有 psycopg 3 封装。Dashboard 使用 asyncpg。
4. _close 让外层收集实际结束的 conversation_id/run_id；真正的最外层事务成功提交后，调用 record_closed_run。
   所有关闭路径都要覆盖；一个事务关闭多个 run 时，每个 run 都需要记录。
   不要在 finally 中调用，不要用 current_run_id 替代实际结束的 run_id。
   不要在主事务里增加统计查询后仅捕获 SQL 异常，SQL 错误可能使主事务失效。
5. 不再另建 stats_pool；调用 record_closed_run(self.repository, conversation_id=..., run_id=...)。
   本地 database_transaction() 在已有事务内复用连接并创建 savepoint，不会独立提交。
   只有最外层业务事务成功提交后再调用，统计事务才与业务事务分开；生产须保持这一调用边界。
   SET LOCAL 仅限制进入统计事务后的 SQL，连接池租借和事务入口检查仍遵循 repository 的超时配置。
   普通统计异常会被捕获，但总等待时间不保证在 1 秒内；如果业务要求完全不等待，需接现有后台队列。
6. writer.py 用一条 INSERT ... SELECT 读取已提交的 run、会话和消息，避免多次 SELECT 看到不同快照。
   未找到源 run 且没有既有统计时返回失败；若已经保存，重复调用成功返回。
7. backfill.sql 每次补最多 1000 个缺失项。在独立事务中设置 lock_timeout、statement_timeout 后执行。
   串行分批执行并核对结果；有在线写入竞争时，一批插入 0 条不一定表示没有剩余项，应重新检查。
   回填遍历所有仍保留的终态 run，不限于 current_run_id。
8. Dashboard 调用 dashboard.py；无记录的日期不返回行，确认覆盖范围后可由展示层补零。

失败和保留
完整终态快照使用 ON CONFLICT DO NOTHING，重复关闭或回填不会重复加消息，也不会覆盖旧统计。
已经保存的错误统计需要单独修订，重复回填不会自动改正。
独立写入允许业务先成功，因此进程退出或断库仍可能漏一次即时写入，必须定期补录并监测失败和积压。
若要避免清理造成永久漏数，清理源数据前必须确认统计已保存；否则暂缓该项清理并告警。
这会延后清理，不影响正常会话处理。生产是否接受这一规则仍待确认。
若不能延后清理或保存可靠待办，只能提供尽力统计，不能承诺零丢失。
已经清理的 run/消息无法恢复，应明确历史覆盖起点。

建议的生产验收场景（尚未执行）
正文+附件为 1；追加两次变成 3；重复同一请求不增加。
同一会话两轮分别 3、2 条，结果为会话 1、消息 5、同一用户 1。
两个会话同一用户，结果为会话 2、用户 1。
重复关闭、回填、在线写入竞争不重复计数；主事务回滚不产生统计。
统计超时不影响业务成功，后续补录恢复；源数据清理后，既有统计不减少。
跨午夜、月末使用正确业务日期；结束日当天不计入范围；无数据时汇总为 0。
正式接入本仓库时，须使用 tools/test-flow/run.ps1 或 run.sh 取得对应 verdict。

公开技术依据
https://www.postgresql.org/docs/current/ddl-generated-columns.html
https://www.postgresql.org/docs/current/transaction-iso.html
https://www.postgresql.org/docs/current/sql-insert.html
https://magicstack.github.io/asyncpg/current/usage.html
https://www.psycopg.org/psycopg3/docs/basic/transactions.html
