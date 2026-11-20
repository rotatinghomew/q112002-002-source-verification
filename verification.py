"""两阶段来源核验练习：引用判定、迁移资格、佐证独立性与复核席安排。

口径（与 README 对应）：

* 引用按 ``version`` 判定，不按网址判定；同一网址在不同时段可能是不同版本。
* 支持同一主张的多条引用，只有落在不同证据来源组时才算独立佐证。
  来源组由已裁定的 ``confirmed`` 关系合并；``pending`` 不合并，``discarded`` 不合并。
* 一条引用是否算作交卷依据，要看所引版本在交卷日是否可见
  （``shown_from`` 至 ``shown_until`` 闭区间）。交卷后才上线的版本只能算续补材料，
  不能倒过来当作学生当时的依据。
* 迁移配对按 ``student + skill + 两阶段主张``，不看题号。后阶段核心证据若在交卷前
  已经由 ``seen`` 登记过、或已在别的答卷里用过，且与当时接触到的材料同源，则失去
  “陌生情境”资格；网址不同不改变这个判断。本卷自己的引用不算“以前见过”。
* 待定且会改变结论的争议只让相关配对进入“待定”，不进入确定迁移率。
* 后续裁定只更新解释口径，不改写学生当时交卷、当时反馈的既成事实。
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from record_reader import read_records

ROOT = Path(__file__).resolve().parent / "records"

SUPPORT = "支持"
PARTIAL = "部分"
CONTRARY = "相反"
PENDING_JUDGMENT = "待定"

CONFIRMED = "confirmed"
PENDING = "pending"
DISCARDED = "discarded"

# 不同原因分开记录，避免把“部分支持”“同源待定”“证据相反”混成一句。
REASON_TEXT = {
    "judgment_partial": "引用只支持主张的一部分",
    "judgment_contrary": "引用证据与主张相反",
    "judgment_pending": "复核判断仍为待定",
    "version_not_visible": "所引版本在交卷日不可见，不能算作交卷时依据",
    "url_served_other_version": "同一网址在交卷日已换成另一版本，不能按网址改判或补记",
    "supplement_after_submission": "该版本在交卷后才上线，只能列为续补材料",
    "duplicate_upstream": "与另一条依据共用上游，属于重复转述，不增加独立佐证数",
    "relation_pending": "与另一条依据（或后段核心）的同源关系待裁定",
    "no_counted_support": "没有任何一条引用同时满足：交卷日可见、判为支持",
    "late_core_seen_before_submission": "交卷前已接触后段核心证据，不算陌生情境",
    "stage2_not_verified": "配对资格仍在，但后段答卷本身未通过核验（属成绩理由，不是迁移问题）",
    "missing_stage_submission": "两阶段配对缺答卷，该技能不可比",
    "seen_outside_window": "见到登记落在该版本可访问时段之外，需核对登记",
}


def _reason(code: str) -> dict[str, str]:
    return {"code": code, "text": REASON_TEXT.get(code, code)}


# ---------------------------------------------------------------------------
# 版本与来源组


@dataclass(frozen=True)
class Version:
    version: str
    url: str
    evidence: str
    shown_from: int
    shown_until: int | None
    kind: str

    def visible_on(self, day: int) -> bool:
        return self.shown_from <= day and (self.shown_until is None or day <= self.shown_until)


class VersionCatalog:
    """网址与版本分开：同一网址可先后承载不同版本、不同证据。"""

    def __init__(self, rows: Sequence[Mapping[str, Any]]):
        self.by_id: dict[str, Version] = {}
        self.by_url: dict[str, list[Version]] = defaultdict(list)
        for row in rows:
            version = Version(
                version=row["version"],
                url=row["url"],
                evidence=row["evidence"],
                shown_from=int(row["shown_from"]),
                shown_until=None if row["shown_until"] is None else int(row["shown_until"]),
                kind=row["kind"],
            )
            self.by_id[version.version] = version
            self.by_url[version.url].append(version)
        for versions in self.by_url.values():
            versions.sort(key=lambda item: item.shown_from)

    def at_url_on(self, url: str, day: int) -> list[Version]:
        return [version for version in self.by_url.get(url, []) if version.visible_on(day)]

    def served_versions(self, url: str, day: int) -> list[str]:
        return [version.version for version in self.at_url_on(url, day)]


class SourceGroups:
    """按已确认的同源关系合并证据件；未裁定/被否定的关系不合并。"""

    def __init__(self, evidence_ids: Iterable[str], confirmed_pairs: Iterable[tuple[str, str]] = ()):
        self._parent: dict[str, str] = {str(item): str(item) for item in evidence_ids}
        self.confirmed_pairs = list(dict.fromkeys((str(left), str(right)) for left, right in confirmed_pairs))
        for left, right in self.confirmed_pairs:
            self.union(left, right)

    def find(self, item: str) -> str:
        item = str(item)
        self._parent.setdefault(item, item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != item:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, left: str, right: str) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self._parent[right_root] = left_root

    def same(self, left: str, right: str) -> bool:
        return self.find(left) == self.find(right)


def relation_history(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    """每个争议按 (业务日, 登记顺序) 排列；同一争议后一行取代前一行。"""
    history: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for index, row in enumerate(rows):
        history[row["dispute"]].append({**row, "_index": index})
    for items in history.values():
        items.sort(key=lambda item: (int(item["day"]), int(item["_index"])))
    return dict(history)


def state_at(history: Mapping[str, Sequence[Mapping[str, Any]]], day: int) -> dict[str, Mapping[str, Any]]:
    """某业务日已经公布的状态；当日之后才到达的裁定不参与当时解释。"""
    current: dict[str, Mapping[str, Any]] = {}
    for dispute, items in history.items():
        applicable = [item for item in items if int(item["day"]) <= day]
        if applicable:
            current[dispute] = applicable[-1]
    return current


def latest_state(history: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, Mapping[str, Any]]:
    return {dispute: items[-1] for dispute, items in history.items() if items}


def pairs_in_state(state: Mapping[str, Mapping[str, Any]], wanted: str) -> list[tuple[str, str]]:
    return [(row["left"], row["right"]) for row in state.values() if row["state"] == wanted]


def pending_pairs(state: Mapping[str, Mapping[str, Any]]) -> list[tuple[str, str]]:
    return pairs_in_state(state, PENDING)


# ---------------------------------------------------------------------------
# 登记台


class Ledger:
    def __init__(self, tables: Mapping[str, Sequence[Mapping[str, Any]]]):
        self.tables = tables
        self.catalog = VersionCatalog(tables["versions"])
        self.claims = {row["claim"]: row for row in tables["claims"]}
        self.submissions = {row["submission"]: row for row in tables["submissions"]}
        self.history = relation_history(tables["relations"])
        self.feedback = list(tables["feedback"])
        self.slots = list(tables["review_slots"])
        self.citations: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in tables["citations"]:
            self.citations[row["submission"]].append(row)
        self.opportunities: dict[tuple[str, str], Mapping[str, Any]] = {
            (row["student"], row["claim"]): row for row in tables["opportunities"]
        }
        self.seen: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in tables["seen"]:
            self.seen[row["student"]].append(row)
        for rows in self.seen.values():
            rows.sort(key=lambda item: int(item["day"]))
        self._evidence_ids = self._collect_evidence_ids()

    def _collect_evidence_ids(self) -> set[str]:
        ids = {row["evidence"] for row in self.tables["versions"]}
        ids |= {row["core"] for row in self.tables["claims"]}
        for row in self.tables["relations"]:
            ids.add(row["left"])
            ids.add(row["right"])
        return ids

    def groups(self, state: Mapping[str, Mapping[str, Any]]) -> SourceGroups:
        return SourceGroups(self._evidence_ids, pairs_in_state(state, CONFIRMED))

    def hypothetical_groups(self, state: Mapping[str, Mapping[str, Any]]) -> SourceGroups:
        """把待定关系也当作已确认，只用于“若裁定同源会怎样”的测算。"""
        return SourceGroups(
            self._evidence_ids,
            pairs_in_state(state, CONFIRMED) + pending_pairs(state),
        )


# ---------------------------------------------------------------------------
# 单条答卷核验
#
# 顺序：先看引用支持的是哪个说法，再看交卷日能不能看到那个版本，最后看多条
# “支持”之间是否只是同一上游的转述。
#
# * 部分支持、相反、复核待定各有独立理由，不能互相顶替，也不能合成“支持”；
# * 支持但交卷日看不到的版本只算续补材料，不能倒算成当时的依据；
# * 同址在交卷日若已是另一版本，不能凭网址把后补材料当作当时引用；
# * “本题核心证据”只用于迁移资格里的接触判断，不是准入门槛。一条引用是否
#   有效，取决于它支持哪一个主张；是否独立，取决于它和其它引用是否共用上游。
#
# support_count 是结案口径（按最新已裁定的关系）；as_reviewed 是老师实际发出
# 反馈那一刻按当时已公布依据算出的口径。两者并列保留，不互相覆盖。


def _citation_record(
    ledger: Ledger,
    submission: Mapping[str, Any],
    citation: Mapping[str, Any],
) -> dict[str, Any]:
    version = ledger.catalog.by_id[citation["version"]]
    day = int(submission["day"])
    served = ledger.catalog.served_versions(version.url, day)
    return {
        "version": version.version,
        "url": version.url,
        "evidence": version.evidence,
        "kind": version.kind,
        "judgment": citation["judgment"],
        "minute": int(citation["minute"]),
        "visible_on_submission_day": version.visible_on(day),
        "url_served_on_submission_day": served,
        "credit": "not_counted",
        "counts_as_evidence": False,
        "counts_as_independent": False,
        "reasons": [],
    }


def _classify(
    ledger: Ledger,
    submission: Mapping[str, Any],
    citation: Mapping[str, Any],
    record: dict[str, Any],
) -> None:
    version = ledger.catalog.by_id[citation["version"]]
    judgment = citation["judgment"]
    codes: list[str] = []

    if judgment == PARTIAL:
        record["credit"] = "partial"
        codes.append("judgment_partial")
    elif judgment == CONTRARY:
        record["credit"] = "contrary"
        codes.append("judgment_contrary")
        if not record["visible_on_submission_day"]:
            codes.append("version_not_visible")
            if record["url_served_on_submission_day"]:
                codes.append("url_served_other_version")
    elif judgment == PENDING_JUDGMENT:
        record["credit"] = "judgment_pending"
        codes.append("judgment_pending")
    elif not record["visible_on_submission_day"]:
        record["credit"] = "supplement"
        codes.append("version_not_visible")
        if version.shown_from > int(submission["day"]):
            codes.append("supplement_after_submission")
        if record["url_served_on_submission_day"]:
            codes.append("url_served_other_version")
    else:
        record["credit"] = "counted"
        record["counts_as_evidence"] = True

    record["reasons"] = [_reason(code) for code in dict.fromkeys(codes)]


def _assess(
    ledger: Ledger,
    submission_id: str,
    state: Mapping[str, Mapping[str, Any]],
    groups: SourceGroups,
) -> dict[str, Any]:
    submission = ledger.submissions[submission_id]
    claim = ledger.claims[submission["claim"]]
    result: dict[str, Any] = {
        "submission": submission_id,
        "student": submission["student"],
        "claim": claim["claim"],
        "skill": claim["skill"],
        "stage": int(claim["stage"]),
        "day": int(submission["day"]),
        "citations": [],
        "counted_versions": [],
        "independent_support": [],
        "support_count": 0,
        "status": "no_support",
        "verified": True,
        "open_relations": [],
        "reasons": [],
    }
    for citation in ledger.citations.get(submission_id, []):
        record = _citation_record(ledger, submission, citation)
        _classify(ledger, submission, citation, record)
        result["citations"].append(record)

    counted = [item for item in result["citations"] if item["credit"] == "counted"]
    result["counted_versions"] = [item["version"] for item in counted]

    first_by_root: dict[str, dict[str, Any]] = {}
    for item in counted:
        root = groups.find(item["evidence"])
        if root in first_by_root:
            first = first_by_root[root]
            item["credit"] = "duplicate"
            item["reasons"].append(_reason("duplicate_upstream"))
            item["duplicate_of"] = first["version"]
            item["shares_upstream_with"] = first["version"]
        else:
            first_by_root[root] = item
            item["credit"] = "independent"
            item["counts_as_independent"] = True
            result["independent_support"].append(item["version"])
    result["support_count"] = len(first_by_root)

    # 只有可能改变条数的待定关系才让结论暂定；不影响条数的争议不挂在这里，
    # 留给争议影响表去解释。
    if counted:
        alt_groups = ledger.hypothetical_groups(state)
        if len({alt_groups.find(item["evidence"]) for item in counted}) != result["support_count"]:
            result["verified"] = False
            result["status"] = "provisional_relation"
            result["reasons"].append(_reason("relation_pending"))
            cited_evidence = {item["evidence"] for item in counted}
            for dispute, relation in state.items():
                if relation["state"] != PENDING:
                    continue
                sides = {relation["left"], relation["right"]}
                if not sides & cited_evidence:
                    continue
                result["open_relations"].append(dispute)
                for item in counted:
                    if item["evidence"] in sides:
                        item["reasons"].append(_reason("relation_pending"))

    if result["status"] == "provisional_relation":
        pass
    elif any(item["judgment"] == PENDING_JUDGMENT for item in result["citations"]):
        result["verified"] = False
        result["status"] = "unresolved"
    elif result["support_count"] > 1:
        result["status"] = "independent_multi"
    elif result["support_count"] == 1:
        result["status"] = "single_source"
    else:
        result["status"] = "no_support"
        if result["citations"]:
            result["reasons"].append(_reason("no_counted_support"))

    for item in result["citations"]:
        for reason in item["reasons"]:
            if reason not in result["reasons"]:
                result["reasons"].append(reason)
    result["as_reviewed"] = _as_reviewed(ledger, submission_id)
    return result


def assess_submission(
    ledger: Ledger,
    submission_id: str,
    state: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """按交卷日可见性核验一条答卷，并按来源组去重计算独立佐证。

    只合并已裁定的 ``confirmed`` 关系；待定关系若会改变条数，结论标为暂定。
    """
    return _assess(ledger, submission_id, state, ledger.groups(state))


def _support_count_at(
    ledger: Ledger,
    submission_id: str,
    state: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """某一时点按当时公布的关系可以算出的独立佐证数。"""
    submission = ledger.submissions[submission_id]
    groups = ledger.groups(state)
    by_root: dict[str, str] = {}
    visible_support: list[str] = []
    for citation in ledger.citations.get(submission_id, []):
        version = ledger.catalog.by_id[citation["version"]]
        if citation["judgment"] != SUPPORT:
            continue
        if not version.visible_on(int(submission["day"])):
            continue
        visible_support.append(version.version)
        by_root.setdefault(groups.find(version.evidence), version.version)
    return {
        "support_count": len(by_root),
        "supporting_versions": visible_support,
        "independent_versions": list(by_root.values()),
    }


def _as_reviewed(ledger: Ledger, submission_id: str) -> dict[str, Any] | None:
    minutes = [int(row["minute"]) for row in ledger.citations.get(submission_id, [])]
    if not minutes:
        return None
    review_day = max(minutes)
    then = _support_count_at(ledger, submission_id, state_at(ledger.history, review_day))
    now = _support_count_at(ledger, submission_id, latest_state(ledger.history))
    return {
        "review_day": review_day,
        "support_count_then": then["support_count"],
        "support_count_now": now["support_count"],
        "independent_versions_then": then["independent_versions"],
        "independent_versions_now": now["independent_versions"],
        "changed_by_later_relations": then["support_count"] != now["support_count"],
    }


# ---------------------------------------------------------------------------
# 两阶段迁移分析
#
# 配对按 student + skill 取两阶段机会，不看题号。资格只取决于一件事：
# 学生在后段交卷之前，是否已经通过课内登记（seen）或前一次答卷接触过后段
# 核心证据的上游。判定按来源组，不按网址；证据换了网址也可能是同一上游。


def _claims_by_skill(ledger: Ledger) -> dict[str, dict[int, Mapping[str, Any]]]:
    grouped: dict[str, dict[int, Mapping[str, Any]]] = defaultdict(dict)
    for claim in ledger.claims.values():
        grouped[claim["skill"]][int(claim["stage"])] = claim
    return dict(grouped)


def _submission_of(ledger: Ledger, student: str, claim: str) -> Mapping[str, Any] | None:
    for row in ledger.tables["submissions"]:
        if row["student"] == student and row["claim"] == claim:
            return row
    return None


def exposure_events(
    ledger: Ledger,
    student: str,
    late_core: str,
    late_submission_id: str,
    before_day: int,
    groups: SourceGroups,
) -> list[dict[str, Any]]:
    """后段交卷之前，学生实际接触过后段核心上游的证据。

    只认两种接触：课内发放/阅读登记（seen），以及此前其他答卷用过的引用。
    本卷自身的引用不算“以前见过”，否则每条答卷都会被判成已见后段核心。
    """
    events: list[dict[str, Any]] = []
    for row in ledger.seen.get(student, []):
        day = int(row["day"])
        if day > before_day:
            continue
        version = ledger.catalog.by_id[row["version"]]
        if not groups.same(version.evidence, late_core):
            continue
        event = {
            "via": "seen",
            "version": version.version,
            "url": version.url,
            "evidence": version.evidence,
            "day": day,
            "text": "课内登记见过与后段核心同源的材料",
        }
        if not version.visible_on(day):
            # 登记日落在该版本可访问时段之外：仍算接触，但要提示核对登记。
            event["reasons"] = [_reason("seen_outside_window")]
        events.append(event)
    for submission in ledger.tables["submissions"]:
        if submission["student"] != student:
            continue
        if submission["submission"] == late_submission_id:
            continue
        if int(submission["day"]) > before_day:
            continue
        for citation in ledger.citations.get(submission["submission"], []):
            version = ledger.catalog.by_id[citation["version"]]
            # 交卷日看不到的版本不可能是学生当时用过的材料，不能算接触。
            if not version.visible_on(int(submission["day"])):
                continue
            if groups.same(version.evidence, late_core):
                events.append(
                    {
                        "via": "earlier_citation",
                        "submission": submission["submission"],
                        "version": version.version,
                        "url": version.url,
                        "evidence": version.evidence,
                        "day": int(submission["day"]),
                        "text": "前一次答卷已用到同一上游证据",
                    }
                )
    return events


def _pending_exposure_candidates(
    ledger: Ledger,
    student: str,
    late_core: str,
    late_submission_id: str,
    before_day: int,
    state: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    """待定关系若成立，会不会让已接触材料并到后段核心上。只报告会改变资格的。"""
    candidates: list[str] = []
    for dispute, relation in state.items():
        if relation["state"] != PENDING:
            continue
        if late_core not in {relation["left"], relation["right"]}:
            continue
        other = relation["right"] if relation["left"] == late_core else relation["left"]
        touched = False
        for row in ledger.seen.get(student, []):
            if int(row["day"]) > before_day:
                continue
            if ledger.catalog.by_id[row["version"]].evidence == other:
                touched = True
        for submission in ledger.tables["submissions"]:
            if submission["student"] != student or int(submission["day"]) > before_day:
                continue
            if submission["submission"] == late_submission_id:
                continue
            for citation in ledger.citations.get(submission["submission"], []):
                version = ledger.catalog.by_id[citation["version"]]
                if not version.visible_on(int(submission["day"])):
                    continue
                if version.evidence == other:
                    touched = True
        if touched:
            candidates.append(dispute)
    return sorted(set(candidates))


def pair_record(
    ledger: Ledger,
    student: str,
    skill: str,
    early: Mapping[str, Any],
    late: Mapping[str, Any],
    state: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    early_sub = _submission_of(ledger, student, early["claim"])
    late_sub = _submission_of(ledger, student, late["claim"])
    pair: dict[str, Any] = {
        "student": student,
        "skill": skill,
        "stage1_claim": early["claim"],
        "stage2_claim": late["claim"],
        "stage1_submission": early_sub["submission"] if early_sub else None,
        "stage2_submission": late_sub["submission"] if late_sub else None,
        "eligible": False,
        "included_in_confirmed_rate": False,
        "status": "missing_submission",
        "exposure": [],
        "uncertain_relations": [],
        "outcome": None,
        "support_count": None,
        "stage1_status": None,
        "reasons": [],
    }
    if not early_sub or not late_sub:
        pair["reasons"].append(_reason("missing_stage_submission"))
        return pair

    groups = ledger.groups(state)
    before = int(late_sub["day"])
    pair["exposure"] = exposure_events(
        ledger, student, late["core"], late_sub["submission"], before, groups
    )
    pair["uncertain_relations"] = _pending_exposure_candidates(
        ledger, student, late["core"], late_sub["submission"], before, state
    )

    stage2 = assess_submission(ledger, late_sub["submission"], state)
    pair["outcome"] = stage2["status"]
    pair["support_count"] = stage2["support_count"]
    pair["stage2_reasons"] = stage2["reasons"]
    early_assessment = assess_submission(ledger, early_sub["submission"], state)
    pair["stage1_status"] = early_assessment["status"]

    if pair["exposure"]:
        pair["status"] = "ineligible_exposure"
        pair["reasons"].append(_reason("late_core_seen_before_submission"))
    elif pair["uncertain_relations"]:
        pair["status"] = "indeterminate_relation"
        pair["reasons"].append(_reason("relation_pending"))
    else:
        pair["eligible"] = True
        pair["included_in_confirmed_rate"] = True
        pair["status"] = "eligible"
        if stage2["status"] not in {"independent_multi", "single_source"}:
            # 资格还在，只是后段答卷本身没通过核验：这是成绩理由问题，不是迁移问题。
            pair["reasons"].append(_reason("stage2_not_verified"))
    return pair


def transfer_pairs(
    ledger: Ledger,
    state: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    claims_by_skill = _claims_by_skill(ledger)
    students = sorted({row["student"] for row in ledger.tables["opportunities"]})
    return [
        pair_record(ledger, student, skill, stages[1], stages[2], state)
        for student in students
        for skill, stages in sorted(claims_by_skill.items())
        if 1 in stages and 2 in stages
    ]


def analyze_transfer(
    ledger: Ledger,
    state: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    pairs = transfer_pairs(ledger, state)
    students = sorted({pair["student"] for pair in pairs})
    by_student_skill: dict[str, dict[str, Any]] = {}
    for student in students:
        skills: dict[str, Any] = {}
        for skill in sorted({pair["skill"] for pair in pairs if pair["student"] == student}):
            group = [pair for pair in pairs if pair["student"] == student and pair["skill"] == skill]
            comparable = [pair for pair in group if pair["included_in_confirmed_rate"]]
            verified = [
                pair
                for pair in comparable
                if pair["outcome"] in {"independent_multi", "single_source"}
            ]
            skills[skill] = {
                "pairs": len(group),
                "comparable": len(comparable),
                "verified": len(verified),
                "transfer_rate": (len(verified) / len(comparable)) if comparable else None,
                "pair_status": [pair["status"] for pair in group],
                "note": (
                    "有可比较的陌生情境配对"
                    if comparable
                    else "该技能不可比：缺后段答卷或交卷前已接触后段核心证据"
                ),
            }
        by_student_skill[student] = skills

    comparable = [pair for pair in pairs if pair["included_in_confirmed_rate"]]
    verified = [
        pair for pair in comparable if pair["outcome"] in {"independent_multi", "single_source"}
    ]
    return {
        "pairs": pairs,
        "by_student_skill": by_student_skill,
        "confirmed_rate": {
            "numerator": len(verified),
            "denominator": len(comparable),
            "rate": (len(verified) / len(comparable)) if comparable else None,
            "counted_pairs": [
                {"student": pair["student"], "skill": pair["skill"]} for pair in comparable
            ],
        },
        "excluded": [
            {
                "student": pair["student"],
                "skill": pair["skill"],
                "status": pair["status"],
                "reasons": pair["reasons"],
            }
            for pair in pairs
            if not pair["included_in_confirmed_rate"]
        ],
        "pending_relations": [
            dispute for dispute, row in state.items() if row["state"] == PENDING
        ],
    }


# ---------------------------------------------------------------------------
# 争议影响
#
# 每条争议的状态都会变化（提出 -> 确认/否定）。判断“这次裁定改变了什么”，
# 要拿裁定之前的状态与裁定之后的状态比，而不是拿“去掉这条”与“最终状态”比；
# 否则早先已确认的关系会把后来的裁定重复算一遍。
#
# 输出分三类，理由分开写：
#   * losing_eligibility：哪些配对因此失去迁移资格；
#   * support_count_changes：哪些只是改佐证数，不改变资格；
#   * feedback_to_revisit：已发反馈中结论受影响、需要回原老师复议的记录。


def _visible_support_groups(
    ledger: Ledger,
    submission_id: str,
    groups: SourceGroups,
) -> list[tuple[str, list[str]]]:
    """交卷日可见、判为支持、按来源组去重后的依据分组。"""
    submission = ledger.submissions[submission_id]
    by_root: dict[str, list[str]] = {}
    for citation in ledger.citations.get(submission_id, []):
        version = ledger.catalog.by_id[citation["version"]]
        if citation["judgment"] != SUPPORT:
            continue
        if not version.visible_on(int(submission["day"])):
            continue
        by_root.setdefault(groups.find(version.evidence), []).append(version.version)
    return list(by_root.items())


def _diff_states(
    ledger: Ledger,
    before: Mapping[str, Mapping[str, Any]],
    after: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """两种关系状态之间的差别：资格变化、佐证数变化、受影响学生。"""
    before_groups = ledger.groups(before)
    after_groups = ledger.groups(after)
    losing_pairs: list[dict[str, Any]] = []
    count_changes: list[dict[str, Any]] = []
    affected_students: set[str] = set()

    claims_by_skill = _claims_by_skill(ledger)
    for student in sorted({row["student"] for row in ledger.tables["opportunities"]}):
        for skill, stages in sorted(claims_by_skill.items()):
            if 1 not in stages or 2 not in stages:
                continue
            earlier = pair_record(ledger, student, skill, stages[1], stages[2], before)
            later = pair_record(ledger, student, skill, stages[1], stages[2], after)
            if earlier["included_in_confirmed_rate"] and not later["included_in_confirmed_rate"]:
                losing_pairs.append(
                    {
                        "student": student,
                        "skill": skill,
                        "stage1_claim": stages[1]["claim"],
                        "stage2_claim": stages[2]["claim"],
                        "stage1_submission": earlier["stage1_submission"],
                        "stage2_submission": earlier["stage2_submission"],
                        "new_status": later["status"],
                        "exposure": later["exposure"],
                    }
                )
                affected_students.add(student)

    for submission_id in ledger.submissions:
        before_support = _visible_support_groups(ledger, submission_id, before_groups)
        after_support = _visible_support_groups(ledger, submission_id, after_groups)
        if len(after_support) < len(before_support):
            collapsed: list[dict[str, str]] = []
            for _root, versions in after_support:
                kept = versions[0]
                for version in versions[1:]:
                    collapsed.append({"kept": kept, "duplicate": version})
            count_changes.append(
                {
                    "submission": submission_id,
                    "student": ledger.submissions[submission_id]["student"],
                    "claim": ledger.submissions[submission_id]["claim"],
                    "support_count_before": len(before_support),
                    "support_count_after": len(after_support),
                    "collapsed": collapsed,
                }
            )
            affected_students.add(ledger.submissions[submission_id]["student"])

    return {
        "losing_eligibility": losing_pairs,
        "support_count_changes": count_changes,
        "affected_students": sorted(affected_students),
        "affected_submissions": sorted(
            {row["submission"] for row in count_changes}
            | {
                submission_id
                for pair in losing_pairs
                for submission_id in (pair["stage1_submission"], pair["stage2_submission"])
                if submission_id
            }
        ),
    }


def _state_without(
    state: Mapping[str, Mapping[str, Any]], dispute: str
) -> dict[str, Mapping[str, Any]]:
    return {key: value for key, value in state.items() if key != dispute}


def _teachers_for(ledger: Ledger, students: Iterable[str]) -> list[str]:
    teachers: list[str] = []
    for student in sorted(set(students)):
        for opportunity in ledger.opportunities.values():
            if opportunity["student"] == student and opportunity["teacher"] not in teachers:
                teachers.append(opportunity["teacher"])
    return teachers


def _review_teachers(
    ledger: Ledger,
    impact: Mapping[str, Any],
    *,
    left: str,
    right: str,
) -> list[str]:
    """该争议要发到谁手里。

    顺序：受影响的配对/答卷老师；引用到争议任一侧证据的答卷老师；
    最后加上已就该答卷发过反馈的老师。不会为了填满席位而随便点名。
    """
    teachers = _teachers_for(ledger, impact["affected_students"])
    sides = {left, right}
    for submission_id, citations in ledger.citations.items():
        if not any(ledger.catalog.by_id[row["version"]].evidence in sides for row in citations):
            continue
        submission = ledger.submissions[submission_id]
        for opportunity in ledger.opportunities.values():
            if (
                opportunity["student"] == submission["student"]
                and opportunity["claim"] == submission["claim"]
                and opportunity["teacher"] not in teachers
            ):
                teachers.append(opportunity["teacher"])
    return teachers


def _feedback_for_impact(
    ledger: Ledger,
    impact: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """已发反馈里被这次裁定改到结论的，回原老师复议；原说明照旧成立。"""
    count_submissions = {row["submission"] for row in impact["support_count_changes"]}
    changed_pairs = {
        (row["student"], row["skill"]) for row in impact["losing_eligibility"]
    }
    changed_submissions: set[str] = set()
    for row in impact["losing_eligibility"]:
        for key in ("stage1_submission", "stage2_submission"):
            if row[key]:
                changed_submissions.add(row[key])

    results: list[dict[str, Any]] = []
    for feedback in ledger.feedback:
        submission_id = feedback["submission"]
        submission = ledger.submissions.get(submission_id)
        if not submission:
            continue
        skill = ledger.claims[submission["claim"]]["skill"]
        if (
            submission_id not in count_submissions
            and submission_id not in changed_submissions
            and (submission["student"], skill) not in changed_pairs
        ):
            continue
        day = int(feedback["day"])
        then = _support_count_at(ledger, submission_id, state_at(ledger.history, day))
        now_state = latest_state(ledger.history)
        now = _support_count_at(ledger, submission_id, now_state)
        if submission_id in count_submissions:
            reason = "独立佐证数按新裁定改变"
        else:
            reason = "该生此技能的迁移配对资格按新裁定改变"
        results.append(
            {
                "submission": submission_id,
                "student": submission["student"],
                "claim": submission["claim"],
                "skill": skill,
                "teacher": feedback["teacher"],
                "feedback_day": day,
                "note": feedback["note"],
                "basis_then": {
                    "relations_published_on": day,
                    "support_count": then["support_count"],
                    "independent_versions": then["independent_versions"],
                },
                "basis_now": {
                    "support_count": now["support_count"],
                    "independent_versions": now["independent_versions"],
                },
                "unchanged_original_basis": True,
                "explanation": (
                    "反馈发出时可见的依据照旧成立；本次只更新解释口径，"
                    "不等于学生当时重新答卷，也不改写当时反馈。"
                ),
                "reason": reason,
                "action": "发回原反馈老师复议",
            }
        )
    return results


def _accumulated_confirmed(history: Mapping[str, Sequence[Mapping[str, Any]]], day: int) -> dict[str, Mapping[str, Any]]:
    """某日及以前已确认同源的关系（不受后来否定影响）。"""
    accumulated: dict[str, Mapping[str, Any]] = {}
    for dispute, items in history.items():
        confirmed = [item for item in items if item["state"] == CONFIRMED and int(item["day"]) <= day]
        if confirmed:
            accumulated[dispute] = confirmed[-1]
    return accumulated


def dispute_impact(
    ledger: Ledger,
    state: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """每条争议的影响，按 ``state``（默认最新已公布状态）解释。

    “若确认同源”的测算始终相对当前口径计算，不写进确定迁移率。
    """
    latest = dict(state) if state is not None else latest_state(ledger.history)
    rows: list[dict[str, Any]] = []
    for dispute in sorted(latest):
        relation = latest[dispute]
        raised_day = int(ledger.history[dispute][0]["day"])
        ruled_day = int(relation["day"])
        if relation["state"] == CONFIRMED:
            # 以“裁定当天之前已公布的其它关系”为基线，只算这次裁定带来的边际变化，
            # 避免把更早的裁定或后来才到达的裁定重复算入。
            baseline = (
                state_at(ledger.history, ruled_day - 1)
                if state is None
                else state
            )
            before = _state_without(baseline, dispute)
            after = {**before, dispute: relation}
            effect = "已确认同源：下列变化已生效"
        else:
            # 待定或否定：当前口径不合并；测算“若这次成立会改变什么”。
            before = _state_without(latest, dispute)
            after = {**before, dispute: {**relation, "state": CONFIRMED}}
            effect = (
                "待裁定：下列只是若确认同源会发生的测算，未计入确定迁移率"
                if relation["state"] == PENDING
                else "已裁定无同源：原公布的依据继续成立；下列为当初若确认会波及的范围"
            )
        impact = _diff_states(ledger, before, after)

        row: dict[str, Any] = {
            "dispute": dispute,
            "state": relation["state"],
            "left": relation["left"],
            "right": relation["right"],
            "raised_day": raised_day,
            "ruled_day": ruled_day,
            "effect": effect,
            "affects": [],
            "losing_eligibility": impact["losing_eligibility"],
            "support_count_changes": impact["support_count_changes"],
            "affected_students": impact["affected_students"],
            "affected_submissions": impact["affected_submissions"],
            "review_teachers": _review_teachers(
                ledger, impact, left=relation["left"], right=relation["right"]
            ),
            "feedback_to_revisit": [],
            "broken_chains": [],
        }
        if relation["state"] == CONFIRMED:
            row["broken_chains"] = [
                {
                    "left": relation["left"],
                    "right": relation["right"],
                    "text": (
                        f"{relation['left']} 与 {relation['right']} 共用上游；"
                        "只转述同一原始证据的引用不再算独立佐证"
                    ),
                }
            ]
            row["feedback_to_revisit"] = _feedback_for_impact(ledger, impact)
            row["effect"] = "已确认同源：下列变化已生效"
        elif relation["state"] == PENDING:
            row["feedback_to_revisit"] = _feedback_for_impact(ledger, impact)
            for item in row["feedback_to_revisit"]:
                item["action"] = "仅备查：裁定同源后再发回原反馈老师复议"
        if row["losing_eligibility"]:
            row["affects"].append("eligibility")
        if row["support_count_changes"]:
            row["affects"].append("support_count")
        if not row["affects"]:
            row["affects"].append("single_sheet")
        rows.append(row)
    return rows


def accumulated_shared_upstream(ledger: Ledger) -> list[dict[str, Any]]:
    """所有曾裁定同源的关系（含后来被否定的），只用于解释当时公布的依据。

    当前口径仍只合并 ``confirmed``；这张表不会让已否定的关系重新算作同源。
    """
    if not ledger.history:
        return []
    last_day = max(int(item["day"]) for items in ledger.history.values() for item in items)
    accumulated = _accumulated_confirmed(ledger.history, last_day)
    if not accumulated:
        return []
    groups = ledger.groups(accumulated)
    chains: dict[str, set[str]] = defaultdict(set)
    for dispute, relation in accumulated.items():
        chains[groups.find(relation["left"])].add(dispute)
    current = latest_state(ledger.history)
    current_groups = ledger.groups(current)
    result = []
    for root, disputes in chains.items():
        members = sorted(item for item in ledger._evidence_ids if groups.find(item) == root)
        result.append(
            {
                "disputes": sorted(disputes),
                "members": members,
                "still_merged_now": len({current_groups.find(item) for item in members}) == 1,
            }
        )
    return sorted(result, key=lambda row: row["disputes"])


# ---------------------------------------------------------------------------
# 复核席安排
#
# 固定时段内：先排“会改变同一批学生迁移资格或成绩理由”的争议，再排只影响
# 单卷解释的。已锁定的席位不动；空档按新证据重排，不覆盖锁定的争议。


def _priority(row: Mapping[str, Any]) -> tuple[int, int, int, str]:
    """会改变迁移资格或成绩理由的争议排前，只影响单卷解释的排后。"""
    affects = set(row.get("affects") or ["single_sheet"])
    tier = 0 if affects & {"eligibility", "support_count"} else 1
    # 同档里先处理提出更早、能改动更多学生的。
    return (tier, int(row["raised_day"]), -int(row.get("affected_count", 0)), row["dispute"])


def _eligible_slots(slots: Sequence[Mapping[str, Any]], as_of: int) -> list[Mapping[str, Any]]:
    free = [slot for slot in slots if not slot["locked_dispute"]]
    free.sort(key=lambda slot: (int(slot["day"]), slot["slot"]))
    return free


def plan_review_slots(ledger: Ledger, as_of: int) -> dict[str, Any]:
    """固定时段内先排影响资格/成绩理由的争议，再排单卷解释；锁定席位不动。

    只排到 ``as_of`` 当天已公布、仍为 pending 的争议。已确认/已否定的争议
    不再占用新的复核席，但锁定席位原样保留。
    """
    state = state_at(ledger.history, as_of)
    pending = []
    for dispute, relation in state.items():
        if relation["state"] != PENDING:
            continue
        default_state = _state_without(state, dispute)
        would = _diff_states(
            ledger, default_state, {**default_state, dispute: {**relation, "state": CONFIRMED}}
        )
        affects: list[str] = []
        if would["losing_eligibility"]:
            affects.append("eligibility")
        if would["support_count_changes"]:
            affects.append("support_count")
        if not affects:
            affects.append("single_sheet")
        pending.append(
            {
                "dispute": dispute,
                "raised_day": int(relation["day"]),
                "affects": affects,
                "affected_students": would["affected_students"],
                "affected_submissions": would["affected_submissions"],
                "affected_count": len(would["affected_students"]),
                "would_change_eligibility": bool(would["losing_eligibility"]),
                "would_change_support_count": bool(would["support_count_changes"]),
                "teachers": _review_teachers(
                    ledger, would, left=relation["left"], right=relation["right"]
                ),
            }
        )
    pending.sort(key=_priority)

    assignments: list[dict[str, Any]] = []
    for slot in ledger.slots:
        if not slot["locked_dispute"]:
            continue
        assignments.append(
            {
                "slot": slot["slot"],
                "day": int(slot["day"]),
                "teacher": slot["teacher"],
                "dispute": slot["locked_dispute"],
                "locked": True,
                "note": "已安排席位，保留不动",
            }
        )
    used = {row["slot"] for row in assignments}
    covered = {row["dispute"] for row in assignments}
    unscheduled: list[str] = []
    for item in pending:
        if item["dispute"] in covered:
            # 已锁定的席位就是在处理这条争议，不再重复排。
            continue
        candidates = [
            slot
            for slot in _eligible_slots(ledger.slots, as_of)
            if slot["slot"] not in used and int(slot["day"]) >= max(as_of, item["raised_day"])
        ]
        if not candidates:
            unscheduled.append(item["dispute"])
            continue
        same_teacher = [slot for slot in candidates if slot["teacher"] in item["teachers"]]
        chosen = (same_teacher or candidates)[0]
        assignments.append(
            {
                "slot": chosen["slot"],
                "day": int(chosen["day"]),
                "teacher": chosen["teacher"],
                "dispute": item["dispute"],
                "locked": False,
                "note": (
                    "按新证据改排"
                    if chosen["teacher"] in item["teachers"]
                    else "无同科老师空档，暂排最近时段"
                ),
            }
        )
        used.add(chosen["slot"])

    assignments.sort(key=lambda row: (row["day"], row["slot"]))
    return {
        "as_of": as_of,
        "assignments": assignments,
        "unscheduled": unscheduled,
        "priority_order": [item["dispute"] for item in pending],
        "priority_detail": pending,
        "idle_slots": [
            slot["slot"]
            for slot in _eligible_slots(ledger.slots, as_of)
            if slot["slot"] not in used
        ],
    }


# ---------------------------------------------------------------------------
# 总报告


def evaluate(ledger: Ledger, as_of: int | None = None) -> dict[str, Any]:
    latest = latest_state(ledger.history)
    if as_of is None:
        candidates = [int(row["day"]) for row in ledger.tables["submissions"]]
        candidates += [int(row["day"]) for row in ledger.slots]
        candidates += [
            int(ledger.history[dispute][-1]["day"]) for dispute in ledger.history
        ]
        as_of = max(candidates) if candidates else 0

    submissions = {
        submission_id: assess_submission(ledger, submission_id, latest)
        for submission_id in sorted(ledger.submissions)
    }
    # 每条答卷按“自己那次复核到达日”已公布的依据解释；不能拿最后一条
    # 复核的时间统一回溯，否则早发的反馈会被后来的裁定改写。
    review_days = {
        row["submission"]: int(row["minute"]) for row in ledger.tables["citations"]
    }
    review_day = max(review_days.values()) if review_days else 0
    as_reviewed = {
        submission_id: assess_submission(
            ledger, submission_id, state_at(ledger.history, review_days[submission_id])
        )
        if submission_id in review_days
        else None
        for submission_id in sorted(ledger.submissions)
    }
    review_transfer = analyze_transfer(ledger, state_at(ledger.history, review_day))
    return {
        "as_of": as_of,
        "review_day": review_day,
        "submission_review_days": review_days,
        "submissions": submissions,
        "submissions_as_of_review": as_reviewed,
        "transfer": analyze_transfer(ledger, latest),
        "transfer_as_of_review": review_transfer,
        "disputes": dispute_impact(ledger),
        "disputes_as_of_review": dispute_impact(ledger, state_at(ledger.history, review_day))
        if review_days
        else dispute_impact(ledger),
        # 曾被裁定同源、后来才被否定的关系：只为解释“当时公布的依据”，不合并当前口径。
        "shared_upstream_history": accumulated_shared_upstream(ledger),
        "review_plan": plan_review_slots(ledger, as_of),
        "relations_latest": {
            dispute: {key: row[key] for key in ("left", "right", "state", "day")}
            for dispute, row in latest.items()
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="核验练习资料分析")
    parser.add_argument("folder", nargs="?", default=str(ROOT))
    parser.add_argument("--as-of", type=int, default=None, help="按该业务日已公布的依据解释")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = parser.parse_args(argv)
    ledger = Ledger(read_records(Path(args.folder)))
    report = evaluate(ledger, args.as_of)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"复核日={report['review_day']}  结案基准日={report['as_of']}")
    for submission_id, item in report["submissions"].items():
        codes = "、".join(reason["code"] for reason in item["reasons"]) or "—"
        print(
            f"{submission_id} {item['student']} {item['claim']} "
            f"独立佐证={item['support_count']} 状态={item['status']} "
            f"复核时={item['as_reviewed']['support_count_then'] if item['as_reviewed'] else '-'}"
            f"->{item['as_reviewed']['support_count_now'] if item['as_reviewed'] else '-'}"
            f"  原因={codes}"
        )
    for pair in report["transfer"]["pairs"]:
        note = "（不可比）" if not pair["included_in_confirmed_rate"] else ""
        print(f"配对 {pair['student']} {pair['skill']}: {pair['status']} -> {pair['outcome']}{note}")
    print("按学生/技能：")
    for student, skills in report["transfer"]["by_student_skill"].items():
        for skill, info in skills.items():
            print(
                f"  {student} {skill}: 可比 {info['comparable']}/{info['pairs']}，"
                f"已核验 {info['verified']}，{info['note']}"
            )
    rate = report["transfer"]["confirmed_rate"]
    print(f"确定迁移率: {rate['numerator']}/{rate['denominator']}")
    for row in report["disputes"]:
        print(f"争议 {row['dispute']} [{row['state']}] {row['effect']}")
        for pair in row["losing_eligibility"]:
            print(f"  失去资格：{pair['student']} {pair['skill']}")
        for change in row["support_count_changes"]:
            print(
                f"  佐证数变化：{change['submission']} "
                f"{change['support_count_before']}->{change['support_count_after']}"
            )
        for item in row["feedback_to_revisit"]:
            print(f"  反馈复议：{item['teacher']} / {item['submission']}")
    plan = report["review_plan"]
    print(
        "复核席："
        + "；".join(
            f"{row['slot']}({row['day']},{row['teacher']})->{row['dispute']}"
            f"{'[锁定]' if row['locked'] else ''}"
            for row in plan["assignments"]
        )
    )
    if plan["unscheduled"]:
        print("未排入：" + ", ".join(plan["unscheduled"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
