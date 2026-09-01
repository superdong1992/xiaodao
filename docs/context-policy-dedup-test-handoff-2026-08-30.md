# ROUTE 资产解析去重改动的复核与收口记录

> **状态**：2026-09-01 已完成代码复核、专项复现和修正；最终验证以 `FIXED_ISSUES.md`
> 对应条目的 Test Flow verdict 为准。本文保留原提交的调查背景，并修正其中关于 Fake 调用记录和
> mtime/size 缓存的错误结论。

---

## 1. 改了什么,为什么

`runtime/context_policy.py` 的 `RuntimeAssetResolver` 在解析一个 ROUTE Job 的资产时,对同一个已注册技能(Methods Skill)的 package,会重复调用 `load_specialized_skill_registration()`——这个函数会完整读取、解析并对整个 package 目录树逐文件算 SHA-256,开销不小。改动前,同一个技能在一次 ROUTE 任务里最多被这样重复加载 **5 次**:

1. `catalog.check([ref])` 内部做新鲜度校验,读一次
2. `catalog.resolve(ref)` 内部又独立做一次新鲜度校验,读一次
3. `_validate_resolved_asset()` 为了比对 `combined_sha256` 又读一次
4. `_skill_index_entry()` 自己调用 `_validate_resolved_asset()`,再读一次
5. `_skill_index_entry()` 紧接着又显式调用 `load_specialized_skill_registration(root)`,第五次

这纯粹是浪费,不是安全特性本身要求的。技能内容的"热更新检测"这件事是必要的、不能删——已跟用户确认:SKILL_DIR 下的技能包会在服务进程不重启的情况下被原地覆盖(比如直接 rsync),`ASSET_VERSION_UNAVAILABLE` 这条失败路径(`domain/coordinator.py:1731` `_asset_unavailable`)就是为了防止某个 Job 悄悄执行在和它当初锁定的 `content_hash` 不一致的技能内容上。但没必要为**同一次**资产解析,对**同一个**技能反复付 5 遍加载代价。

### 具体改动(只涉及这一个文件)

- `_validate_resolved_asset()` 的返回值从 `Path` 改成 `tuple[Path, ResolvedSpecializedSkillV1 | None]`——把已经加载好的对象一并返回,不再让调用方读完就扔。
- `_load_entry_text()` / `_skill_index_entry()` 改为直接复用这个返回值,不再自己重新调用 `load_specialized_skill_registration`。
- `RuntimeAssetResolver._resolve()` 去掉了 `catalog.check([ref])` 这一步预检查,只保留 `catalog.resolve(ref)`。

### 为什么去掉 `check()` 是安全的(等价性证明)

`check()` 和 `resolve()` 内部走的是完全相同的 `_ref_is_current(ref)` 判断(见 `runtime/catalog.py`),没有任何一条路径会让两者对"这个 ref 是否新鲜"给出不同答案。唯一需要确认的是失败时抛出的 `_invalid_asset(...)` 携带的 `details` 是否一致:

- 真实 catalog:`catalog.py` 的 `_catalog_port_error()` 恒定 `details=[]`。
- 测试用的 `FakeAssetCatalog`(`tests/deterministic/contracts/fakes.py`):`_port_error()` 默认 `details=()`,`resolve()` 的失败路径(`fakes.py:1963-1972`)调用时没有传 `details`。

两条路径在这条边上都是空,所以 `_invalid_asset()`(默认空)和 `_invalid_asset(exc.error.details)`(实际也是空)产生的异常在两个实现下都完全等价。

2026-09-01 的运行探针确认，`428d35e` 完成后每个 Skill 仍有 3 次加载：Catalog 新鲜度校验 1 次，
Resolver 2 次。后续增加 Job 内 `_ResolvedSkillSnapshot`，把 `_resolve_skill()` 已验证的
`ResolvedSpecializedSkillV1` 直接交给 `_skill_index_entry()`，不再为构造索引重新扫描目录。

**最终效果**：每个 Skill、每次 ROUTE Job 从最初 5 次降到 2 次；Catalog 和 Resolver 各完整校验
一次。没有修改 `AssetCatalogPort`，没有引入跨 Job 缓存，也没有削弱热更新漂移检测。

## 2. 已知需要同步修改的测试

`tests/deterministic/unit/runtime/test_diagnosis_runtime.py:1287`:

```python
assert catalog.check_calls == [(job.agent_profile_ref,)]
```

这一行测的是"资产解析遇到第一个失败的 ref 就立刻停止,不会继续往下解析别的 ref"这个行为——测试用例只往 `FakeAssetCatalog` 里注册了 `agent_profile_ref` 一个资产,并通过 `catalog.inject_failure("resolve", typed_failure)` 对 `resolve()` 注入了一个失败,断言最终 `receipt.job_outcome.error` 的 code/details 符合预期,同时确认只有一次 `check()` 调用就停手了。

因为 `check()` 调用被去掉了，这一行必然失败。原交接文档建议直接改成 `resolve_calls`，但专项
复现发现这个建议也不成立：`FakeAssetCatalog.resolve()` 会先执行注入失败，再追加
`resolve_calls`，因此失败调用不会进入该列表。

最终测试使用局部观测子类，在委托给 Fake 之前记录 attempted ref，再断言只尝试了
`job.agent_profile_ref`；同时保留错误码、details、State 未读取、Backend 未启动和
`check_calls == []`。这样验证的是实际 fail-fast 行为，不改变共享 Fake 的失败语义。

## 3. 专项回归

- `test_public_asset_fake_typed_resolve_failure_preserves_details_as_outcome`
- `test_route_reuses_one_validated_skill_snapshot_for_the_index`
- `test_asset_content_drift_never_substitutes_the_frozen_job_version`
- `test_asset_content_drift_with_unchanged_size_and_mtime_is_rejected`
- `tests/deterministic/integration/test_s07_settings_catalog_runtime_seam.py`
- `tests/deterministic/integration/test_bootstrap_composition.py`
- `tests/deterministic/contracts/test_execution_replay_scenarios.py` —— 这个文件里也有一处 `check_calls` 断言(`:157`),但它测的是 `application/external_commands.py:795` 那条**批量** `check(fixed_asset_refs(...))` 调用,跟本次改动的 `context_policy.py` 单 ref 调用点是两回事,预期不受影响。**如果它也挂了,说明我的判断有遗漏,要停下来重新排查,不要直接改断言糊过去。**

如果条件允许,建议把 `tests/deterministic/` 整体跑一遍兜底——资产解析是很多路径共享的底层机制,定位到具体调用点不代表影响面一定只有列出的这几个文件。

## 4. 停止条件

- 除了上面明确列出的这一行,如果还有别的测试因为这次改动挂了:先判断是不是"check() 与 resolve() 等价"这个证明有遗漏(比如某个测试专门构造了两者行为不一致的场景),而不是直接放宽断言让测试通过。
- 这次改动的前提是"不改变任何可观察行为,只消除重复计算"。如果验证过程中发现某处测试其实依赖了"技能被重复加载"这个副作用(比如断言某个 mock/spy 的调用次数是 5 次而不是语义结果),按同样的方式对等改写调用次数,不要改测试想验证的语义本身。

## 5. 不在本轮范围内(已分析,先不做)

不得用 mtime/size 未变化作为跳过 SHA-256 的充分条件。等长内容可以在恢复 mtime 后绕过这种缓存，
会削弱当前的字节级漂移检测；专项回归已经固定这条不可回归行为。

如果 2 次完整加载仍是实测瓶颈，后续方向应是内容寻址的不可变 Skill 目录和原子 Catalog 绑定切换，
让完整校验前移到注册阶段。该方案涉及部署合同和历史 Job 引用，本轮不实施。
