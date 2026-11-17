"""本地演示：从首周查看到补数复盘。

场景（全部为合成记录，整数秒表示时间）：
- 实验在 t=0 开始，三周内每天入组读者，转化期限为入组后 14 天；
- 计划登记 3 次查看：第 21 天、第 24 天、第 31 天（O'Brien-Fleming 序贯界值）；
- 第 21 天查看时，只有最早入组者走完窗口，晚入组者仍是未成熟——如果把他们
  当作未转化，就会系统性低估转化率；
- 第 24 天查看时更多入组者成熟，但部分转化事件因上游延迟要到之后才收到；
- 第 31 天补数到达，同时仍有最近入组者未成熟：补数只进入新分析，旧分析与
  其证据摘要保持不变；
- 对账报告逐人说明哪些变化来自成熟、哪些来自补数、哪些仍是缺失。

运行：python -m experiment.demo
"""

from __future__ import annotations

import hashlib
from .assignment import arm_for
from .cohorts import Cohort, Enrollment
from .events import Conversion, EventLedger
from .plan import AnalysisPlan
from .service import ExperimentService

DAY = 86_400
EXPERIMENT = "content-entry-v1"
ARMS = ("control", "new")
SEGMENTS = ("returning", "newcomer")


def _unit(*parts: object) -> float:
    """确定性伪随机数，保证演示可重复。"""
    payload = "|".join(str(part) for part in parts).encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") / 2**64


def build_scenario() -> tuple[Cohort, EventLedger, AnalysisPlan, list[str]]:
    cohort = Cohort(EXPERIMENT, ARMS)
    ledger = EventLedger()

    # 三周内每天入组 10 人，共 210 人；分群按稳定标识分配。
    subjects: list[str] = []
    for day in range(21):
        for index in range(10):
            subject_id = f"reader-{day:02d}-{index:02d}"
            segment = SEGMENTS[int(_unit(subject_id, "segment") * len(SEGMENTS))]
            cohort.enroll(Enrollment(subject_id, day * DAY + 3_600, segment))
            subjects.append(subject_id)

    # 潜在转化：实验组略高，但差距不大；同时预置“晚到记录”和“撤回记录”。
    for subject_id in subjects:
        arm = arm_for(subject_id, EXPERIMENT, ARMS)
        joined_at = cohort.members[subject_id].joined_at
        baseline = 0.20 if arm == "control" else 0.26
        probability = min(0.95, max(0.02, baseline + (_unit(subject_id, "noise") - 0.5) * 0.10))
        if _unit(subject_id, "convert") >= probability:
            continue
        delay = 1 * DAY + int(_unit(subject_id, "delay") * 10 * DAY)
        occurred_at = joined_at + delay
        # 一部分事件在发生后数小时内到达；另一部分因上游延迟，到第 26-30 天才补数。
        if _unit(subject_id, "backfill_flag") < 0.4:
            received_at = occurred_at + int(_unit(subject_id, "reporting") * 6 * 3_600)
        else:
            received_at = max(occurred_at + 3_600, 26 * DAY + int(_unit(subject_id, "backfill") * 4 * DAY))
        ledger.record(Conversion(f"{subject_id}-c1", subject_id, occurred_at, received_at=received_at))

    # 下面四名读者从“无转化”的样本中选取，避免与已生成的转化记录叠加。
    quiet = [subject for subject in subjects if subject not in {event.subject_id for event in ledger.through(30 * 86_400)}]
    if len(quiet) < 8:
        raise RuntimeError("演示样本不足")

    # 一名读者的唯一记录被撤回：已成熟但无法判断是否转化。
    retract_subject = quiet[0]
    joined_at = cohort.members[retract_subject].joined_at
    ledger.record(Conversion(f"{retract_subject}-retract", retract_subject, joined_at + 2 * DAY, retracted=True, received_at=joined_at + 3 * DAY))

    # 一名读者在窗口外才有记录：不计为转化。
    late_subject = quiet[1]
    joined_at = cohort.members[late_subject].joined_at
    ledger.record(Conversion(f"{late_subject}-late", late_subject, joined_at + 14 * DAY + 1, received_at=joined_at + 15 * DAY))

    # 一名读者的重复记录：按人只计一次。
    dup_subject = quiet[2]
    joined_at = cohort.members[dup_subject].joined_at
    ledger.record(Conversion(f"{dup_subject}-c1", dup_subject, joined_at + 5 * DAY, received_at=joined_at + 5 * DAY + 60))
    ledger.record(Conversion(f"{dup_subject}-c2", dup_subject, joined_at + 5 * DAY + 30, received_at=joined_at + 5 * DAY + 90))

    # 一名读者在本期登记了数据缺口，之后才解除。
    gap_subject = quiet[3]
    ledger.mark_gap(gap_subject, "埋点中断，事件未上报", at=13 * DAY)
    ledger.resolve_gap(gap_subject, at=20 * DAY)

    plan = AnalysisPlan(
        experiment_id=EXPERIMENT,
        arms=ARMS,
        control_arm="control",
        treatment_arm="new",
        conversion_window=14 * DAY,
        primary_segment=None,
        planned_looks=3,
        alpha=0.05,
        recording_grace=2 * DAY,
        planned_max_matured=210,
        min_matured_per_arm=20,
        max_missing_share=0.20,
        label="内容入口首周与补数复盘",
    )
    return cohort, ledger, plan, subjects


def _print_header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def _print_look(report) -> None:
    look = report.context
    boundary = "无法计算（尚无成熟数据）" if look.boundary_z is None else f"|z|>={look.boundary_z:.3f}"
    spent = "—" if look.alpha_spent is None else f"{look.alpha_spent:.4f}"
    print(f"分析时刻 as_of={report.as_of}（第 {look.sequence} 次计划查看，"
          f"信息量 {look.information_fraction:.0%}，序贯界值 {boundary}，"
          f"名义 α={look.nominal_alpha:.4f}，累计消耗 α={spent}/{report.plan.alpha:.2f}）")
    print(f"入组批次 {report.batch_id}（摘要 {report.batch_digest[:12]}…，批次 {report.batch_size} 人，本次分析 {report.analyzed_members} 人）")
    print(f"证据摘要 {report.evidence_digest[:16]}…")
    print()
    print(f"{'组':<8}{'可分析':>7}{'未成熟':>7}{'待定':>6}{'已成熟':>7}{'转化':>6}{'未转化':>7}{'缺失':>6}{'转化率':>9}")
    for arm in report.plan.comparison_arms:
        counts = report.counts[arm]
        rate = "—" if counts["rate"] is None else f"{counts['rate']:.1%}"
        print(f"{arm:<8}{counts['eligible']:>7}{counts.get('immature', 0):>7}{counts.get('pending', 0):>6}"
              f"{counts.get('matured', 0):>7}{counts.get('converted', 0):>6}{counts.get('not_converted', 0):>7}"
              f"{counts.get('missing', 0):>6}{rate:>9}")
    comparison = report.comparison
    difference = comparison["risk_difference"]
    interval = comparison["risk_difference_interval"]
    repeated = comparison["repeated_confidence_interval"]
    if difference is None:
        print("组间差异：无法估计（成熟样本不足）")
    else:
        print(f"组间差异（新-对照）：{difference:+.1%}；{comparison['nominal_level']:.0%} 描述性区间 "
              f"[{interval[0]:+.1%}, {interval[1]:+.1%}]")
        if repeated is None:
            print("重复查看置信区间：不可估计（尚无成熟数据或未计算界值）")
        else:
            claim = comparison["claim_level"] or 0.0
            print(f"可重复查看的置信区间（覆盖全部计划查看，约 {claim:.1%} 水平）："
                  f"[{repeated[0]:+.1%}, {repeated[1]:+.1%}]")
        if comparison["z"] is None:
            print("z 与名义 p：不可估计")
        else:
            print(f"z={comparison['z']:+.3f}，名义 p={comparison['nominal_p_value']:.4f}，越过界值={comparison['crosses_boundary']}")
    print()
    records = report.diagnostics["records"]
    print("记录影响："
          f"期限外 {records['out_of_window_records']} 条，"
          f"入组前 {records['before_join_records']} 条，"
          f"重复 {records['duplicate_records']} 条，"
          f"撤回 {records['retracted_records']} 条（窗口内 {records['retracted_records_in_window']} 条），"
          f"人群外 {records['records_outside_population']} 条，"
          f"记录版本 {records['revisions_received']} 条，"
          f"迟到首次到达 {records['late_first_receipt_records']} 条")
    if report.diagnostics["missing_reasons"]:
        print("缺失原因：" + "，".join(f"{key}={value}" for key, value in report.diagnostics["missing_reasons"].items()))
    print()
    print(f"结论：{report.decision}")
    print(f"依据：{report.decision_basis}")
    print()
    print("分群（仅计划指定的分群支持决策）：")
    for segment, comparison in report.segments.items():
        diff = comparison["risk_difference"]
        text = "无法估计" if diff is None else f"{diff:+.1%}"
        print(f"  {segment:<10} 角色={comparison['role']:<11} 差异={text:<8} 预指定={comparison['prespecified']}")


def _print_reconciliation(review) -> None:
    payload = review.as_dict()
    earlier, later = payload["earlier"], payload["later"]
    print(f"旧分析 as_of={earlier['as_of']}，决策={earlier['decision']}，证据摘要 {earlier['evidence_digest'][:16]}…")
    print(f"新分析 as_of={later['as_of']}，决策={later['decision']}，证据摘要 {later['evidence_digest'][:16]}…")
    print(f"入组批次：{payload['population_change']['earlier_batch']} -> {payload['population_change']['later_batch']}"
          f"（相同={payload['population_change']['same_batch']}，共享 {payload['population_change']['shared_subjects']} 人）")
    print()
    print("变化来源：")
    for note in payload["explanation"]:
        print(f"  - {note}")
    print()
    print("新增转化的来源：")
    for source, count in payload["conversion_sources"].items():
        print(f"  {source:<20}{count:>4}")
    print()
    print("状态迁移（同一批人的前后状态）：")
    for transition, count in sorted(payload["outcome_transitions"].items()):
        print(f"  {transition:<32}{count:>4}")
    print()
    print("记录层面增量：")
    for key, value in payload["record_delta"].items():
        if value:
            print(f"  {key}: {value:+d}")


def main() -> None:
    cohort, ledger, plan, subjects = build_scenario()
    service = ExperimentService(cohort, ledger, plan)

    first_batch = cohort.register_batch("batch-all-enrolled", received_at=21 * DAY, subjects=subjects)

    _print_header("第 1 次查看：第 21 天")
    first = service.view(as_of=21 * DAY, batch_id=first_batch.batch_id, viewed_at=21 * DAY)
    _print_look(first.report)

    _print_header("第 2 次查看：第 24 天")
    second = service.view(as_of=24 * DAY, batch_id=first_batch.batch_id, viewed_at=24 * DAY)
    _print_look(second.report)

    _print_header("第 3 次查看：第 31 天补数后")
    third = service.view(as_of=31 * DAY, batch_id=first_batch.batch_id, viewed_at=31 * DAY)
    _print_look(third.report)

    _print_header("补数复盘：第 1 次查看 vs 第 3 次查看")
    _print_reconciliation(service.compare(first, third))

    _print_header("计划终期：登记停测")
    stop = service.stop(at=31 * DAY, reason="计划查看次数用尽，按预登记终期结束", look_id=third.look.look_id, by="运营负责人")
    print(f"停测时间 {stop.stopped_at}，依据查看 {stop.look_id}，理由：{stop.reason}，登记人：{stop.by}")
    print("注意：停测理由是按计划终期结束，而不是“已证明差异”。")
    print("停测后任何新增正式查看都会被拒绝，只能做标注为探索性的事后复核。")

    _print_header("停测后的探索性复核（不消耗 α、不进入正式结论）")
    review = service.exploratory_review(
        as_of=40 * DAY,
        batch_id=first_batch.batch_id,
        note="补数继续到达后，确认结论是否仍然一致",
    )
    print(f"结论标记：{review['decision']}（{review['decision_basis']}）")
    print(f"正式查看：{review['exploratory']['is_formal_look']}；消耗 α：{review['exploratory']['consumes_alpha']}；"
          f"支持决策：{review['exploratory']['supports_decision']}")

    _print_header("查看与停测登记")
    history = service.history()
    for look in history["views"]:
        print(f"  {look['look_id']}  viewed_at={look['viewed_at']:<8} as_of={look['as_of']:<8} "
              f"batch={look['batch_id']}  evidence={look['evidence_digest'][:12]}…  "
              f"chain={look['record_hash'][:12]}…  decision={look['decision']}")
    stop_info = history["monitoring"]["stop_decision"]
    print(f"  停测：{stop_info['stopped_at']}，引用 {stop_info['look_id']}，{stop_info['reason']}")
    print(f"  哈希链校验：{'通过' if history['monitoring']['chain_valid'] else '失败'}")

    _print_header("旧分析的可复核性")
    print(f"旧分析证据摘要未被后续记录改写：{first.report.verify_evidence()}")
    print(f"旧分析报告摘要：{first.report.report_digest[:32]}…")
    print("说明：本演示全部为合成数据，只描述流程与记录，不构成总体效果结论。")


if __name__ == "__main__":
    main()
