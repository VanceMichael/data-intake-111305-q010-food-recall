"""召回公告的规范化、案件状态推进与范围计算。

这一层只处理来源公告本身的含义，不访问数据库：

* 来源身份 ``(authority, case)`` 是案件的稳定键，``revision`` 只表示
  某一次公告的修订序号，不能当作案件身份。
* 公告可能缺少批次信息。缺少批次时案件处于待补状态，补齐后仍是同一
  案件，不会新建受众。
* 修订按序号单调推进。晚到的旧页、旧页重现不会回退已生效的范围，也
  不会恢复已经撤回的批次。
* 有效批次是跨修订累积的：某个批次只有被公告明确撤回才会退出召回范
  围，不会因为后续页面没有再次列出就悄悄消失。
* 撤回是保守的：已撤批次不会因为更高修订再次列出而自动恢复，只会记
  入待核供人工确认。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

IDENTITY_FIELDS = ("authority", "case", "revision")
WITHDRAWN_STATUSES = frozenset(
    {"withdrawn", "cancelled", "canceled", "retracted", "closed", "revoked"}
)
#: 这些处置结果需要人工核对；``pending_lots`` 是待补，不属于待核。
REVIEW_DISPOSITIONS = frozenset({"conflict", "pending_review"})
#: 公告本身可以推进案件状态的处置结果。
APPLIED_DISPOSITIONS = frozenset({"applied", "withdrawn_applied"})
#: 这些状态表示来源在修订中收窄了范围：未再列出的旧批次视为撤回。
NARROWING_STATUSES = frozenset({"corrected", "amended", "revised"})


def _text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    value = value.strip()
    return value or None


def _revision(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("revision must be a non-negative integer")
    return value


def _lots(value: Any, field_name: str) -> tuple[str, ...] | None:
    """批次字段可以是数组，也接受来源常见的分隔字符串。"""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.replace("\n", ",").replace("、", ",").replace(";", ",")
        if not value.strip():
            return ()
        parts: Iterable[Any] = value.split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        parts = value
    else:
        raise ValueError(f"{field_name} must be a list or string")
    result: list[str] = []
    for part in parts:
        if isinstance(part, bool) or not isinstance(part, (str, int)):
            raise ValueError(f"{field_name} entries must be text")
        text = str(part).strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def content_digest(payload: Mapping[str, Any]) -> str:
    """计算与接收顺序无关的公告摘要。"""
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Announcement:
    """一条已经规范化、但仍保留来源声明的公告观察。"""

    authority: str | None
    case: str | None
    revision: int | None
    product: str | None
    lots: tuple[str, ...] | None
    withdrawn_lots: tuple[str, ...]
    status: str | None
    payload: dict[str, Any]
    content_digest: str

    @property
    def identity(self) -> tuple[str, str] | None:
        if self.authority and self.case:
            return self.authority, self.case
        return None

    @property
    def complete(self) -> bool:
        """是否足以形成可执行范围：有修订号、产品与至少一个批次。"""
        return bool(self.product) and self.revision is not None and bool(self.lots)

    def as_record(self) -> dict[str, Any]:
        return {
            "authority": self.authority,
            "case": self.case,
            "revision": self.revision,
            "product": self.product,
            "lots": list(self.lots) if self.lots is not None else None,
            "withdrawn_lots": list(self.withdrawn_lots),
            "status": self.status,
            "content_digest": self.content_digest,
        }


def inspect_announcement(raw: Mapping[str, Any]) -> Announcement:
    """校验一条来源公告并计算摘要。

    缺少身份字段不会被当作异常：公告可能只给出产品名，这类观察由调用
    方送入待核。给出但格式错误的字段则视为坏记录。
    """
    if not isinstance(raw, Mapping):
        raise ValueError("announcement must be an object")
    payload = dict(raw)
    authority = _text(raw.get("authority"), "authority")
    case = _text(raw.get("case"), "case")
    product = _text(raw.get("product"), "product")
    revision = _revision(raw.get("revision"))
    lots = _lots(raw.get("lots"), "lots")
    withdrawn = _lots(raw.get("withdrawn_lots"), "withdrawn_lots") or ()
    status_value = raw.get("status", "published")
    status = _text(status_value, "status") if status_value is not None else None
    if status is not None:
        status = status.lower()
    digest_payload = {
        "authority": authority,
        "case": case,
        "revision": revision,
        "product": product,
        "lots": list(lots) if lots is not None else None,
        "withdrawn_lots": list(withdrawn),
        "status": status,
    }
    return Announcement(
        authority=authority,
        case=case,
        revision=revision,
        product=product,
        lots=lots,
        withdrawn_lots=withdrawn,
        status=status,
        payload=payload,
        content_digest=content_digest(digest_payload),
    )


def raw_digest(raw: Any) -> str:
    """坏记录也要有稳定摘要，便于审计和去重。"""
    return content_digest({"raw": raw})


@dataclass(frozen=True)
class Observation:
    """基础版逐条观测的规范化结果，保留给既有调用方。"""

    source: str
    key: str
    revision: int
    value: Any


def inspect_record(raw: dict[str, Any]) -> dict[str, Any]:
    """校验并规范化一条来源观测，同时保留来源信息。

    这是基础版的原始观测口径；召回案件使用 :func:`inspect_announcement`
    与 :class:`CaseAccumulator`。
    """
    required = ("source", "key", "revision", "value")
    missing = [name for name in required if name not in raw]
    if missing:
        raise ValueError("missing fields: " + ", ".join(missing))
    source, key = str(raw["source"]).strip(), str(raw["key"]).strip()
    if not source or not key:
        raise ValueError("source and key must be non-empty")
    revision = raw["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("revision must be a non-negative integer")
    item = Observation(source, key, revision, raw["value"])
    return {
        "source": item.source,
        "key": item.key,
        "revision": item.revision,
        "value": item.value,
        "event": "store_notifications",
    }


class CaseAccumulator:
    """按公告修订单调推进一个案件；顺序由修订号决定而不是到达顺序。"""

    def __init__(self) -> None:
        self.product: str | None = None
        self.applied_revision: int | None = None
        self.applied_digest: str | None = None
        self.basis_observation_id: int | None = None
        self.seen_revision: int | None = None
        self.effective: list[str] = []
        self.withdrawn: list[str] = []
        self.closed = False
        self.awaiting_lots = False
        self.review_reasons: list[str] = []

    def _note_review(self, reason: str) -> None:
        if reason not in self.review_reasons:
            self.review_reasons.append(reason)

    def _withdraw(self, lots: Iterable[str]) -> None:
        for lot in lots:
            if lot not in self.withdrawn:
                self.withdrawn.append(lot)
            if lot in self.effective:
                self.effective.remove(lot)

    def _apply_lots(self, lots: Iterable[str]) -> None:
        for lot in lots:
            if lot in self.withdrawn:
                # 已撤批次不会因再次列出而恢复，交人工确认。
                self._note_review("withdrawn_lot_relisted")
                continue
            if lot not in self.effective:
                self.effective.append(lot)

    def feed(self, observation: Mapping[str, Any]) -> tuple[str, str | None]:
        """推进一条观察，返回 ``(处置, 原因)``。

        处置值：

        ``applied`` / ``withdrawn_applied``
            该观察成为当前修订。
        ``duplicate``
            与当前修订内容相同。
        ``stale``
            修订号低于当前修订，旧页或旧页重现，不改变范围。
        ``pending_lots``
            修订更高但缺少批次，案件待补，不改变已有范围。
        ``pending_review`` / ``conflict``
            证据不足或不一致，需人工核对，不改变已有范围。
        ``after_withdrawal``
            案件整体撤回后出现的页面，不恢复范围。
        """
        revision = observation.get("revision")
        product = observation.get("product")
        lots = observation.get("lots")
        withdrawn = tuple(observation.get("withdrawn_lots") or ())
        status = (observation.get("status") or "").lower()
        digest = observation.get("content_digest")

        if revision is not None and (self.seen_revision is None or revision > self.seen_revision):
            self.seen_revision = revision

        if revision is not None and revision == self.applied_revision and digest == self.applied_digest:
            return "duplicate", None

        if self.closed:
            return "after_withdrawal", "case_withdrawn"

        if not product:
            self._note_review("missing_product")
            return "pending_review", "missing_product"
        if self.product and product != self.product:
            self._note_review("product_conflict")
            return "conflict", "product_conflict"
        # 产品名可以在首个修订里先记录，缺少批次或修订时仍不可执行。
        self.product = self.product or product
        if revision is None:
            self._note_review("missing_revision")
            return "pending_review", "missing_revision"
        if self.applied_revision is not None and revision < self.applied_revision:
            return "stale", "older_revision"
        if self.applied_revision is not None and revision == self.applied_revision:
            self._note_review("revision_conflict")
            return "conflict", "revision_conflict"

        whole_case_withdrawal = status in WITHDRAWN_STATUSES
        if (lots is None or len(lots) == 0) and not whole_case_withdrawal:
            # 较高修订仍缺批次时，已生效范围保持不变；案件只是等待补齐。
            self.awaiting_lots = True
            return "pending_lots", "missing_lots"

        # 应用一个更高修订。
        for resolved in ("missing_product", "missing_lots", "missing_revision"):
            if resolved in self.review_reasons:
                self.review_reasons.remove(resolved)
        self.applied_revision = revision
        self.applied_digest = digest
        self.basis_observation_id = observation.get("id")
        self.awaiting_lots = False
        self._withdraw(withdrawn)

        if whole_case_withdrawal:
            # 整案撤回：当前所有有效批次一并撤回。传副本，避免在迭代中
            # 改动被遍历的列表而漏掉元素。
            self._withdraw(list(self.effective))
            if lots:
                self._withdraw(lots)
            self.closed = True
            return "withdrawn_applied", "case_withdrawn"

        if status in NARROWING_STATUSES:
            # 来源在修订中收窄范围：以前有效但本版未列出的批次视为撤回。
            # 没有声明收窄的一般公告不推断，避免把追加批次误当撤回。
            for lot in list(self.effective):
                if lot not in lots:
                    self._withdraw([lot])
        relisted = [lot for lot in lots if lot in self.withdrawn]
        for lot in relisted:
            self._note_review("withdrawn_lot_relisted")
        self._apply_lots(lots)
        if not self.effective:
            self.closed = True
            return "withdrawn_applied", "all_lots_withdrawn"
        return "applied", "withdrawn_lot_relisted" if relisted else None

    @property
    def state(self) -> str:
        if self.closed or (self.applied_revision is not None and not self.effective):
            return "withdrawn"
        if self.applied_revision is None:
            # 已知道产品、只是缺批次：待补。连产品都不确定才是待核。
            if self.product is None:
                return "pending_review"
            return "pending_lots"
        return "active"

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "product": self.product,
            "current_revision": self.applied_revision,
            "seen_revision": self.seen_revision,
            "effective_lots": list(self.effective),
            "withdrawn_lots": list(self.withdrawn),
            "basis_observation_id": self.basis_observation_id,
            "awaiting_lots": self.awaiting_lots,
            "review_reasons": list(self.review_reasons),
        }


def evaluate(observations: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """按给定顺序重放一个案件的所有观察。"""
    accumulator = CaseAccumulator()
    dispositions = []
    for observation in observations:
        disposition, reason = accumulator.feed(observation)
        dispositions.append(
            {"observation_id": observation.get("id"), "disposition": disposition, "reason": reason}
        )
    result = accumulator.as_dict()
    result["dispositions"] = dispositions
    return result


def held_lots(product: str | None, inventory: Iterable[Mapping[str, Any]]) -> list[str]:
    """门店在某产品上当前持有的批次（保持库存记录顺序）。"""
    if not product:
        return []
    result: list[str] = []
    for item in inventory:
        if str(item.get("product", "")).strip() != product:
            continue
        lot = str(item.get("lot", "")).strip()
        if lot and lot not in result:
            result.append(lot)
    return result
