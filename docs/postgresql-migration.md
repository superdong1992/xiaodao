# 从 SQLite 迁移到 PostgreSQL

`problem-locator-postgres-import` 将当前 V11 r2、会话存储 v2 的 SQLite 历史复制到新的 PostgreSQL 数据库和新的 `DATA_ROOT`。它保留会话、报告、事件、反馈、追问以及资源文件。原目录不作修改。

此命令只支持 Linux Server。Windows 和 macOS 客户端仍直接连接 Linux Server，不需要安装数据库、代理或 Hook。

## 迁移前

1. 使用 PostgreSQL 15 或更新版本，为本次迁移准备一个专用空数据库。建议采用所选主版本最新的维护小版本。连接账号需要在该库的 `public` schema 中建表、建索引和创建 identity 序列的权限。工具拒绝已有业务表的数据库，不会清空或覆盖它。
2. 让诊断、追问、快照、经验提炼、附件上传和清理任务全部结束，再停止旧 Server。工具会获取源目录现有的 `.instance.lock`，并在读取数据库副本后检查是否还有未结束的任务。
3. 为新 `DATA_ROOT` 选择一个尚不存在的绝对路径。源目录和目标目录不能互相包含。目标目录的父目录必须已经存在，并留足空间保存完整副本和迁移证据。
4. 将目标数据库连接地址放入 `DATABASE_URL` 环境变量。命令只接收变量名，收据和错误输出不包含连接地址或密码。不要让当前 Server 使用这个空数据库。

此工具不负责 V1–V10、V11 r1 或会话存储 v1 的升级。旧数据需要先按原版本的升级流程转换为当前 SQLite 格式。

## 先查看计划

```bash
problem-locator-postgres-import \
  --source-data-root /srv/problem-locator-sqlite \
  --target-data-root /srv/problem-locator-postgresql \
  --database-url-env DATABASE_URL \
  --plan-only
```

计划会列出文件数量、总大小、源清单的 SHA-256 和是否存在 WAL。此步骤不打开 SQLite、不连接 PostgreSQL，也不写入目标目录。数据库结构、业务记录和资源引用会在执行时核对，计划中的 `PENDING_EXECUTE` 不代表已经验证通过。

## 执行迁移

```bash
problem-locator-postgres-import \
  --source-data-root /srv/problem-locator-sqlite \
  --target-data-root /srv/problem-locator-postgresql \
  --database-url-env DATABASE_URL \
  --execute
```

执行时，工具先复制完整目录，将 SQLite 主文件、WAL 和 SHM 放入独立暂存目录。SQLite 只打开这个副本，源数据库始终不打开。随后检查数据库完整性、任务状态、Case 快照、资源校验值、附件和追问快照。

工具使用当前程序创建 PostgreSQL 表结构，再在单个事务中导入历史记录。存在插入顺序要求的表将 SQLite `rowid` 保存为 `storage_order`。JSON 正文、消息、事件和回执不作改写；SQLite 中保存为文本的二进制字段按原 UTF-8 字节写入 `BYTEA`。会话附件的 `storage_path` 只将已验证的源目录前缀替换为目标目录，资源文件本身不变。

每张表都核对记录数和完整记录的 SHA-256。数据库保留原 `installation_id`，新增 PostgreSQL 后端标记，并绑定目标目录。文件清单、每表校验结果和允许的路径映射记录在目标目录的 `postgresql-import.receipt.json` 中。收据中的校验结果只证明本次导入，不代替仓库 Test Flow 的 `verdict.json`。

## 切换与回滚

命令返回 `status: IMPORTED` 且退出码为 `0` 后，同时更新 Server 的 `DATA_ROOT` 和 `DATABASE_URL`，再启动新版本。新目录没有 `completed.sqlite3`，只包含 PostgreSQL 格式标记和资源文件。正常启动还会核对数据库与目录的安装标识及路径绑定。

原 SQLite 目录保留不变。切换前可继续用旧版本和原目录回滚；切换后产生的新数据不会自动同步回 SQLite，回滚前需要另行处理这部分数据。

## 失败时

不要将失败目录当作已迁移目录启动。工具使用 `data-format.json.tmp` 和数据库中的 `postgresql-import-pending` 标记阻止未完成的结果投入使用。出现错误时保留暂存目录、数据库和原目录，不自动删除证据。

输出中的 `staging_root` 指向暂存证据，其中 `source-database` 保存源 SQLite 副本，`payload` 保存尚未发布的资源副本，`bootstrap` 保存建表时使用的临时目录。若错误发生在目录发布之后，目标目录可能已存在，但仍被未完成标记阻止启动。

修正失败原因后，使用另一个空数据库和另一个尚不存在的目标目录重新执行。不要手工删除未完成标记来绕过检查。此工具不修改现有 PostgreSQL 数据库，也不支持在失败结果上直接重试。
