# 交接:ROUTE 资产解析去重改动的测试验证

> **交付对象**:测试验证 Agent(零上下文)。本文自包含,不要求阅读此前对话。
> **仓库**:`/Users/shenyidong/Documents/xiaodao`,Problem Locator,Python 3.12。
> **本轮范围**:验证 commit `428d35ee8f3a1924c7b50050e3eb9d558a15479c` 对 `src/problem_locator/runtime/context_policy.py` 的去重重构没有破坏行为,并同步更新一处已知需要改的测试断言。**这不是功能改动,只是消除重复计算,不改变任何可观察行为。**

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

**效果**:每个技能、每次 ROUTE 任务派发,重复加载从 5 次降到 3 次。没有改 `catalog.py`、没有改 `AssetCatalogPort` 接口、没有改"要不要过滤技能目录"这类行为语义。

## 2. 已知需要同步修改的测试

`tests/deterministic/unit/runtime/test_diagnosis_runtime.py:1287`:

```python
assert catalog.check_calls == [(job.agent_profile_ref,)]
```

这一行测的是"资产解析遇到第一个失败的 ref 就立刻停止,不会继续往下解析别的 ref"这个行为——测试用例只往 `FakeAssetCatalog` 里注册了 `agent_profile_ref` 一个资产,并通过 `catalog.inject_failure("resolve", typed_failure)` 对 `resolve()` 注入了一个失败,断言最终 `receipt.job_outcome.error` 的 code/details 符合预期,同时确认只有一次 `check()` 调用就停手了。

因为 `check()` 调用被去掉了,这一行字面上会失败(`check_calls` 恒为 `[]`),但它想验证的行为(fail-fast、错误码/details 正确传播)本身没变,不受这次改动影响。改成:

```python
assert catalog.resolve_calls == [job.agent_profile_ref]
```

`resolve_calls: list[VersionedRef]`(注意不是 tuple 套 tuple)是 `FakeAssetCatalog` 已有字段(`fakes.py:1926`),每次调用 `resolve()` 都会 `append`(`fakes.py:1965`),语义上是对等替换,不需要改这个测试的其他部分(1280-1286 行的断言应该原样保留、原样通过)。

## 3. 建议跑哪些测试

- `tests/deterministic/unit/runtime/test_diagnosis_runtime.py`(先改完上面那一行断言)
- `tests/deterministic/integration/test_s07_settings_catalog_runtime_seam.py`
- `tests/deterministic/integration/test_bootstrap_composition.py`
- `tests/deterministic/contracts/test_execution_replay_scenarios.py` —— 这个文件里也有一处 `check_calls` 断言(`:157`),但它测的是 `application/external_commands.py:795` 那条**批量** `check(fixed_asset_refs(...))` 调用,跟本次改动的 `context_policy.py` 单 ref 调用点是两回事,预期不受影响。**如果它也挂了,说明我的判断有遗漏,要停下来重新排查,不要直接改断言糊过去。**

如果条件允许,建议把 `tests/deterministic/` 整体跑一遍兜底——资产解析是很多路径共享的底层机制,定位到具体调用点不代表影响面一定只有列出的这几个文件。

## 4. 停止条件

- 除了上面明确列出的这一行,如果还有别的测试因为这次改动挂了:先判断是不是"check() 与 resolve() 等价"这个证明有遗漏(比如某个测试专门构造了两者行为不一致的场景),而不是直接放宽断言让测试通过。
- 这次改动的前提是"不改变任何可观察行为,只消除重复计算"。如果验证过程中发现某处测试其实依赖了"技能被重复加载"这个副作用(比如断言某个 mock/spy 的调用次数是 5 次而不是语义结果),按同样的方式对等改写调用次数,不要改测试想验证的语义本身。

## 5. 不在本轮范围内(已分析,先不做)

技能新鲜度校验本身(`catalog.py` 的 `_skill_is_current`)每次都做完整 SHA-256 内容哈希,即使技能包内容根本没变。可以用 mtime/size 做一层廉价前置指纹(只 `stat()`,不读文件内容),指纹没变就跳过完整哈希,只有疑似变化时才退回现在这套逻辑——这样能把"没有热更新发生"这个绝大多数情况下的校验成本降到接近零,且不削弱热更新检测的正确性。

这个改动会涉及给 `VersionedAssetCatalog`(跨请求复用的单例)加可变缓存状态,如果 dispatcher 是多线程的还要考虑加锁,风险和工作量都明显更大。已跟用户确认过设计方向,本轮不做,只记录在这里,留作后续可选项。
