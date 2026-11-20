"""核验练习分析的口径测试：每条规则都能单独触发、单独解释。"""

import copy
import json
import unittest

from record_reader import read_records
from verification import (
    Ledger,
    assess_submission,
    dispute_impact,
    evaluate,
    latest_state,
    plan_review_slots,
    state_at,
    transfer_pairs,
)


class Base(unittest.TestCase):
    def setUp(self):
        self.tables = read_records()
        self.ledger = Ledger(self.tables)
        self.latest = latest_state(self.ledger.history)

    # -- 便捷构造 ---------------------------------------------------------

    def clone(self):
        return copy.deepcopy(self.tables)

    def ledger_from(self, tables):
        return Ledger(tables)

    def reasons(self, assessment):
        return {reason["code"] for reason in assessment["reasons"]}

    def citation(self, assessment, version):
        return next(item for item in assessment["citations"] if item["version"] == version)


class CitationJudgmentTest(Base):
    def test_same_url_is_not_same_version(self):
        # v1 与 v2 同址，但交卷日 12 只存在 v2；a4 引的是 v1。
        assessment = assess_submission(self.ledger, "a4", self.latest)
        self.assertEqual(assessment["support_count"], 0)
        # v2 判“部分支持”，不能因为网址相同就当成“支持”。
        self.assertEqual(self.citation(assessment, "v2")["credit"], "partial")

    def test_contrary_and_invisible_are_separate_reasons(self):
        assessment = assess_submission(self.ledger, "a8", self.latest)
        citation = self.citation(assessment, "v1")
        codes = {reason["code"] for reason in citation["reasons"]}
        self.assertEqual(citation["judgment"], "相反")
        self.assertIn("judgment_contrary", codes)
        # v1 在交卷日 14 已下线，且同址提供 v2：理由要分开写。
        self.assertIn("version_not_visible", codes)
        self.assertIn("url_served_other_version", codes)
        self.assertEqual(assessment["support_count"], 0)

    def test_partial_is_not_promoted_to_support(self):
        assessment = assess_submission(self.ledger, "a9", self.latest)
        self.assertEqual(assessment["status"], "no_support")
        self.assertIn("judgment_partial", self.reasons(assessment))

    def test_undecided_review_judgment_stays_unresolved(self):
        assessment = assess_submission(self.ledger, "a5", self.latest)
        self.assertEqual(assessment["status"], "unresolved")
        self.assertFalse(assessment["verified"])
        self.assertEqual(assessment["support_count"], 0)

    def test_supplement_added_after_submission_is_not_retroactive(self):
        # a6 引 v6，v6 第 19 日才上线，交卷在第 15 日。
        assessment = assess_submission(self.ledger, "a6", self.latest)
        supplement = self.citation(assessment, "v6")
        self.assertEqual(supplement["credit"], "supplement")
        self.assertFalse(supplement["visible_on_submission_day"])
        codes = {reason["code"] for reason in supplement["reasons"]}
        self.assertIn("supplement_after_submission", codes)
        self.assertEqual(assessment["support_count"], 1)
        self.assertEqual(assessment["independent_support"], ["v4"])


class IndependenceTest(Base):
    def test_two_urls_can_be_one_upstream(self):
        # a2 同时引 v3(=/研究简讯) 与 v4(=/市图展页)；d2 裁定 s4 与 raw1 同源，
        # d1 裁定 s3 与 raw1 同源，因此两条引用只是同一原始证据的转述。
        assessment = assess_submission(self.ledger, "a2", self.latest)
        self.assertEqual(assessment["support_count"], 1)
        self.assertEqual(assessment["independent_support"], ["v3"])
        self.assertEqual(assessment["status"], "single_source")
        duplicate = self.citation(assessment, "v4")
        self.assertEqual(duplicate["credit"], "duplicate")
        self.assertEqual(duplicate["duplicate_of"], "v3")

    def test_pending_relation_does_not_count_as_shared_upstream(self):
        # d4 只是提出，未裁定；不得把 v6 与 v4 并成一组。
        assessment = assess_submission(self.ledger, "a6", self.latest)
        self.assertEqual(assessment["independent_support"], ["v4"])

    def test_discarded_relation_does_not_merge(self):
        # d3 已裁定无同源：raw2 与 raw5 仍算两组。
        state = self.latest
        self.assertEqual(state["d3"]["state"], "discarded")
        groups = self.ledger.groups(state)
        self.assertFalse(groups.same("raw2", "raw5"))


class TransferTest(Base):
    def test_pairing_uses_skill_not_claim_number(self):
        pairs = transfer_pairs(self.ledger, self.latest)
        by_key = {(pair["student"], pair["skill"]): pair for pair in pairs}
        self.assertIn(("s1", "出处"), by_key)
        self.assertEqual(by_key[("s1", "出处")]["stage1_claim"], "p1")
        self.assertEqual(by_key[("s1", "出处")]["stage2_claim"], "p2")

    def test_new_url_does_not_make_late_core_unfamiliar(self):
        # s1 第 4 日在课内见过 v1(raw1)；后段 p2 的核心 s4 经 d2 与 raw1 同源。
        # v1 与 v4 网址不同，但仍是同一上游，配对失去陌生情境资格。
        pair = next(
            item
            for item in transfer_pairs(self.ledger, self.latest)
            if item["student"] == "s1" and item["skill"] == "出处"
        )
        self.assertEqual(pair["status"], "ineligible_exposure")
        self.assertFalse(pair["included_in_confirmed_rate"])
        self.assertTrue(pair["exposure"])

    def test_one_student_keeps_comparable_skill(self):
        # s1 的“出处”因接触失去资格，“比对”没有接触，仍应可比；不能整卷删除。
        pairs = {
            (pair["student"], pair["skill"]): pair
            for pair in transfer_pairs(self.ledger, self.latest)
        }
        self.assertEqual(pairs[("s1", "出处")]["status"], "ineligible_exposure")
        self.assertEqual(pairs[("s1", "比对")]["status"], "eligible")
        self.assertTrue(pairs[("s1", "比对")]["included_in_confirmed_rate"])

    def test_own_citation_does_not_disqualify_the_pair(self):
        # s1 后段 p2 自己引用 v4；不能因为本卷引用了后段核心就算“交卷前已见过”。
        pair = next(
            item
            for item in transfer_pairs(self.ledger, self.latest)
            if item["student"] == "s1" and item["skill"] == "比对"
        )
        self.assertFalse(
            any(event["via"] == "earlier_citation" and event["version"] == "v2"
                for event in pair["exposure"])
        )

    def test_students_without_submission_are_reported_not_dropped(self):
        pairs = {
            (pair["student"], pair["skill"]): pair
            for pair in transfer_pairs(self.ledger, self.latest)
        }
        # s3 未交卷，但上课名单里有两条机会。
        self.assertEqual(pairs[("s3", "出处")]["status"], "missing_submission")
        report = evaluate(self.ledger)
        self.assertIn("s3", report["transfer"]["by_student_skill"])

    def test_unresolved_relation_blocks_confirmed_rate(self):
        # 20 日复核时 d3 仍待定：s1/s2 的“比对”配对都只能是待定，不进入确定率。
        state_at_review = state_at(self.ledger.history, 20)
        pairs = transfer_pairs(self.ledger, state_at_review)
        indeterminate = [pair for pair in pairs if pair["status"] == "indeterminate_relation"]
        self.assertTrue(indeterminate)
        for pair in indeterminate:
            self.assertFalse(pair["included_in_confirmed_rate"])
            self.assertIn("d3", pair["uncertain_relations"])

    def test_confirmed_rate_excludes_uncertain_pairs(self):
        report = evaluate(self.ledger)
        rate = report["transfer"]["confirmed_rate"]
        self.assertEqual(rate["denominator"], len(rate["counted_pairs"]))
        for pair in report["transfer"]["pairs"]:
            if pair["status"] in {"indeterminate_relation", "ineligible_exposure", "missing_submission"}:
                self.assertFalse(pair["included_in_confirmed_rate"])


class DisputeImpactTest(Base):
    def impact(self, dispute):
        return next(row for row in dispute_impact(self.ledger) if row["dispute"] == dispute)

    def test_confirmed_shared_upstream_lists_lost_pairs_and_count_changes(self):
        d2 = self.impact("d2")
        self.assertEqual(d2["state"], "confirmed")
        lost = {(row["student"], row["skill"]) for row in d2["losing_eligibility"]}
        self.assertIn(("s1", "出处"), lost)
        self.assertIn(("s2", "出处"), lost)
        changes = {row["submission"] for row in d2["support_count_changes"]}
        self.assertIn("a2", changes)
        change = next(row for row in d2["support_count_changes"] if row["submission"] == "a2")
        self.assertEqual((change["support_count_before"], change["support_count_after"]), (2, 1))

    def test_confirmed_ruling_names_teachers_to_revisit(self):
        d2 = self.impact("d2")
        submissions = {item["submission"]: item for item in d2["feedback_to_revisit"]}
        self.assertIn("a2", submissions)
        self.assertIn("a6", submissions)
        self.assertEqual(submissions["a2"]["teacher"], "东")
        self.assertEqual(submissions["a6"]["teacher"], "西")
        self.assertEqual(set(d2["review_teachers"]), {"东", "西"})

    def test_feedback_keeps_original_published_basis(self):
        # a2 的反馈在第 15 日发出，当时 d2 尚未裁定；原说明要按当时依据解释得通。
        item = next(
            item
            for item in self.impact("d2")["feedback_to_revisit"]
            if item["submission"] == "a2"
        )
        self.assertEqual(item["basis_then"]["relations_published_on"], 15)
        self.assertEqual(item["basis_then"]["support_count"], 2)
        self.assertEqual(item["basis_now"]["support_count"], 1)
        self.assertTrue(item["unchanged_original_basis"])

    def test_pending_dispute_does_not_write_confirmed_numbers(self):
        d4 = self.impact("d4")
        self.assertEqual(d4["state"], "pending")
        self.assertIn("未计入确定迁移率", d4["effect"])
        self.assertFalse(d4["losing_eligibility"])
        for item in d4["feedback_to_revisit"]:
            self.assertIn("备查", item["action"])

    def test_discarded_dispute_keeps_published_basis(self):
        d3 = self.impact("d3")
        self.assertEqual(d3["state"], "discarded")
        self.assertIn("原公布的依据继续成立", d3["effect"])
        # 仍要说明当初若确认会波及什么，但不能写成已生效。
        self.assertTrue(d3["losing_eligibility"])

    def test_confirmed_merge_is_counted_once(self):
        # d1 与 d2 先后把 s3、s4 并到 raw1 上；同一条 a2 的佐证数变化
        # 不能在两行里各算一次“首次失效”。
        d1 = self.impact("d1")
        change_subs = {row["submission"] for row in d1["support_count_changes"]}
        self.assertNotIn("a2", change_subs)


class ReviewPlanTest(Base):
    def test_locked_slot_is_preserved(self):
        plan = plan_review_slots(self.ledger, 22)
        r1 = next(row for row in plan["assignments"] if row["slot"] == "r1")
        self.assertTrue(r1["locked"])
        self.assertEqual(r1["dispute"], "d3")

    def test_eligibility_disputes_are_scheduled_before_explanations(self):
        plan = plan_review_slots(self.ledger, 22)
        priority = plan["priority_order"]
        # 20 日提出的 d4 只影响单卷解释；提出更早的资格类争议若仍待定应先排。
        tiers = {
            row["dispute"]: (0 if set(row["affects"]) & {"eligibility", "support_count"} else 1)
            for row in plan["priority_detail"]
        }
        for dispute in priority:
            self.assertIn(dispute, tiers)

    def test_free_slot_is_reassigned_by_new_evidence(self):
        # 20 日排席：d3 已在锁定的 r1 上，只剩下单卷解释类的 d4 排到 r3。
        # 21 日冒出一条会影响资格的新争议 d5：r3 应改排给 d5，d4 顺延到 r4。
        before = plan_review_slots(self.ledger, 20)
        before_by_dispute = {row["dispute"]: row["slot"] for row in before["assignments"]}
        self.assertEqual(before_by_dispute.get("d4"), "r3")

        tables = self.clone()
        tables["relations"].append(
            {"dispute": "d5", "left": "raw2", "right": "raw5", "state": "pending", "day": 21}
        )
        ledger = self.ledger_from(tables)
        after = plan_review_slots(ledger, 21)
        after_by_dispute = {row["dispute"]: row["slot"] for row in after["assignments"]}
        self.assertEqual(after_by_dispute.get("d5"), "r3")
        self.assertEqual(after_by_dispute.get("d4"), "r4")
        r1 = next(row for row in after["assignments"] if row["slot"] == "r1")
        self.assertTrue(r1["locked"])
        self.assertEqual(r1["dispute"], "d3")
        # 锁定席上的争议不会被重复排到第二个时段。
        self.assertEqual(
            [row["slot"] for row in after["assignments"] if row["dispute"] == "d3"], ["r1"]
        )

    def test_no_free_slot_reports_unscheduled(self):
        tables = self.clone()
        tables["review_slots"] = [row for row in tables["review_slots"] if row["locked_dispute"]]
        ledger = self.ledger_from(tables)
        plan = plan_review_slots(ledger, 22)
        self.assertIn("d4", plan["unscheduled"])


class RecordedBeforeReviewTest(Base):
    def test_review_day_explanation_uses_only_published_relations(self):
        # 20 日复核时 d3 尚未否定；当时看到的 a7 仍应是单来源，而不是事后改写。
        reviewed = assess_submission(self.ledger, "a7", state_at(self.ledger.history, 20))
        self.assertEqual(reviewed["status"], "single_source")
        now = assess_submission(self.ledger, "a7", self.latest)
        self.assertEqual(now["status"], "single_source")

    def test_later_ruling_does_not_relabel_earlier_submission(self):
        # a2 在 11 日交卷；d2 在 20 日裁定，不能因此说它在交卷时只有一条依据。
        assessment = assess_submission(self.ledger, "a2", self.latest)
        self.assertEqual(
            assessment["as_reviewed"]["support_count_then"],
            2,
            "反馈发出时按当时依据是两条",
        )
        self.assertEqual(assessment["as_reviewed"]["support_count_now"], 1)

    def test_missing_version_is_not_silently_dropped(self):
        tables = self.clone()
        tables["citations"][0]["version"] = "v9"
        tables["versions"].append(
            {"version": "v9", "url": "/新页", "evidence": "raw9",
             "shown_from": 1, "shown_until": None, "kind": "原件"}
        )
        ledger = self.ledger_from(tables)
        result = assess_submission(ledger, "a1", latest_state(ledger.history))
        self.assertEqual(result["citations"][0]["version"], "v9")


class ReportShapeTest(Base):
    def test_report_is_json_serialisable_and_keeps_both_bases(self):
        report = evaluate(self.ledger)
        json.dumps(report, ensure_ascii=False)
        self.assertIn("submissions_as_of_review", report)
        self.assertIn("transfer_as_of_review", report)
        self.assertEqual(report["review_day"], 20)


if __name__ == "__main__":
    unittest.main()


class EdgeCaseTest(Base):
    def test_later_evidence_cannot_retroactively_justify_earlier_check(self):
        # 把 v6 的可访问起点改到交卷前，a6 就多一条当时可见的支持；
        # 反过来，v6 保持 19 日上线时绝不能补进 a6 的当时依据。
        tables = self.clone()
        for row in tables["versions"]:
            if row["version"] == "v1":
                row["shown_until"] = None  # 延长原件
        ledger = self.ledger_from(tables)
        a8 = assess_submission(ledger, "a8", latest_state(ledger.history))
        # 相反判断不因版本变可见而变成支持。
        self.assertEqual(a8["support_count"], 0)
        self.assertEqual(
            {reason["code"] for reason in self.citation(a8, "v1")["reasons"]},
            {"judgment_contrary"},
        )

    def test_citation_of_version_not_yet_online_is_supplement_only(self):
        tables = self.clone()
        tables["citations"].append(
            {"submission": "a3", "version": "v6", "judgment": "支持", "minute": 8}
        )
        ledger = self.ledger_from(tables)
        a3 = assess_submission(ledger, "a3", latest_state(ledger.history))
        self.assertEqual(a3["support_count"], 1)
        self.assertEqual(self.citation(a3, "v6")["credit"], "supplement")

    def test_same_evidence_two_versions_is_not_independent(self):
        tables = self.clone()
        tables["versions"].append(
            {"version": "v7", "url": "/手册-转载", "evidence": "raw5",
             "shown_from": 6, "shown_until": None, "kind": "转载"}
        )
        tables["citations"].append(
            {"submission": "a3", "version": "v7", "judgment": "支持", "minute": 8}
        )
        ledger = self.ledger_from(tables)
        a3 = assess_submission(ledger, "a3", latest_state(ledger.history))
        self.assertEqual(a3["support_count"], 1)
        self.assertEqual(self.citation(a3, "v7")["credit"], "duplicate")

    def test_stage2_submission_does_not_expose_itself(self):
        # s2 后段 p2 引用 v4(s4)，后段核心是 s4；本卷引用不等于交卷前已见。
        pair = next(
            item
            for item in transfer_pairs(self.ledger, self.latest)
            if item["student"] == "s2" and item["skill"] == "出处"
        )
        self.assertFalse(
            any(event.get("submission") == "a6" for event in pair["exposure"])
        )
