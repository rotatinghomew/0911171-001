# 实验观察分析

容器环境固定在仓库 Dockerfile 的 Python 3.11.14 镜像摘要，数值分析依赖与传递依赖在 requirements.txt 中精确锁定。执行 `docker build -t local-analytics .` 后，`docker run --rm --network none local-analytics` 运行内置例子；`docker run --rm --network none local-analytics python -m unittest discover -s tests -v` 运行测试。构建时安装依赖，运行时无需联网；非容器环境使用 Python 3.11，并先执行 `python -m pip install -r requirements.txt`。

这是供内容团队正式复盘线上实验的本地分析服务。服务把入组登记、事件台账、分析计划、报告与查看记录分开，演示数据均为合成记录。

运行 `python -m unittest discover -s tests -v` 检查项目；从仓库根目录运行 `python -m experiment.demo` 查看“首周查看 → 补数复盘”的完整流程。示例不是总体效果结论。

## 解决什么问题

早期版本按“截止时刻已收到的转化”统计，会带来三类问题：

1. **晚入组者被当成未转化**：转化率的分母混入了还没走完观察期的人。
2. **旧结论无法解释**：补数到达后重新导出，旧报告的依据被悄悄替换。
3. **按天看胜负**：反复查看、每次都用单次 5% 阈值，误报率随查看次数膨胀。

现在的流程针对这些点：

- **固定个人观察期**：每个人的窗口是 `[入组时刻, 入组时刻 + 转化期限]`。
- **区分四种状态**：未成熟、待定（记录到达宽限期内）、已成熟且转化、已成熟且未转化、记录缺失。
- **冻结证据**：每次分析绑定 `as_of`（当时已收到哪些台账版本）、入组批次编号与内容摘要、预先登记的分析计划；三者一起算 SHA-256。
- **补数只进新分析**：旧报告的证据摘要与报告摘要保持不变，可随时重新校验。
- **预先指定比较**：计划里写明主比较分群（或全人群）；其他分群只作描述性展示，并做多重比较调整，明确标注“不用于决策”。
- **序贯查看**：按实际累计信息量使用 O'Brien-Fleming 消耗函数，整个查看序列共享一个误报率预算。
- **带不确定性的组间差异**：风险差（实验臂 − 对照臂）给出 Newcombe 区间，以及覆盖全部计划查看的重复置信区间。
- **证据不足时保持未定**：每臂成熟人数或记录缺失比例不达标时，决策为 `insufficient_evidence`。
- **可追溯的查看与停测**：查看记录按哈希链追加；停测决定绑定到某次查看，之后不再新增正式查看。

## 模块

| 模块 | 职责 |
| --- | --- |
| `experiment.assignment` | 由稳定标识推导分组 |
| `experiment.cohorts` | 入组登记、不可变入组批次 |
| `experiment.events` | 追加式事件台账（发生时间 / 到达时间）、记录缺口登记 |
| `experiment.plan` | 预先登记的分析计划：转化期限、主比较分群、计划查看次数、α、诊断阈值 |
| `experiment.outcomes` | 按个人固定窗口判定结果状态与记录影响 |
| `experiment.stats` | Newcombe 区间、合并 z 检验、O'Brien-Fleming 序贯界值 |
| `experiment.report` | 证据冻结、组间比较、分群报告、决策 |
| `experiment.reconcile` | 两次查看的逐人对账与变化来源分解 |
| `experiment.monitoring` | 查看记录哈希链、停测登记 |
| `experiment.service` | 串起以上步骤的服务层 |

## 时间与记录语义

- 所有时间字段是同一时钟上的整数秒。
- 事件有两个时钟：`occurred_at` 决定是否落在个人窗口内，`received_at` 决定某次分析是否“当时已知”。
- 同一 `event_id` 可多次上报（更正、撤回），台账保留按到达时间排序的版本历史；分析只看 `received_at <= as_of` 的最新版本。
- 同一用户窗口内有多条有效记录时按人只算一次，重复条数单独报告。
- 记录缺失的三种来源分别计数：登记中的缺口 `data_gap`、窗口内唯一记录被撤回 `retracted_record`、仍在到达宽限期 `reporting_grace`（后者归为“待定”）。
- 期外记录、入组前记录、人群外记录、迟到首次到达的补数记录都单独统计，便于判断它们对结果的影响。

## 分群规则

`AnalysisPlan.primary_segment` 在查看数据前确定：

- 为空：主比较是全人群。
- 指定：主比较只分析该分群，其余分群仍会出现在 `segments` 里，但 `prespecified=False`、`supports_decision=False`，区间按分群数做 Bonferroni 调整。

事后挑选分群只能得到描述性数字，不会进入决策依据。

## 多次查看

`planned_looks` 与 `alpha` 在计划中固定。第 k 次查看的界值按前 k 次查看的实际累计信息量（已成熟人数 ÷ 计划最终成熟人数）求解，报告同时给出：

- `boundary_z`：本次的序贯界值；
- `nominal_alpha`：与界值等效的单次名义水平；
- `alpha_spent`：截至本次累计消耗的误报率，应不超过计划 `alpha`。

信息量为 0 的查看不消耗 α，也不给出可宣称的结论。描述性区间固定为 95%；可重复查看的结论用 `repeated_confidence_interval`，两者分开标注。

## 审计与复核

```python
service = ExperimentService(cohort, ledger, plan)
first = service.view(as_of=..., batch_id=..., viewed_at=...)
# ... 补数 ...
second = service.view(as_of=..., batch_id=..., viewed_at=...)
review = service.compare(first, second).as_dict()
```

- `report.verify_evidence()` 重新计算冻结证据的摘要，用于确认旧分析未被改写。
- `service.history()` 返回计划、批次、查看记录、停测决定与哈希链校验结果。
- `review` 给出：人群增减、逐人状态迁移、转化变化的来源（新成熟 / 补数记录 / 缺失恢复 / 其他）、记录层面增量，以及一段可读的变化说明。

## 演示

`python -m experiment.demo` 模拟三周入组、转化期限 14 天、计划 3 次查看（第 21/24/31 天）：首周查看时大量晚入组者尚未成熟；随后补数到达；最后一次查看后把首末两次结果对账，列出变化来源，并展示旧分析证据摘要仍可校验、查看哈希链完整。

时间与数据均为合成示例，不构成总体效果结论。
