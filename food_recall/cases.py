"""召回案件的接收、更正、通知与追补。

原始响应仍由 :mod:`food_recall.archive` 保存。本模块在其之上建立业务层：

* 每次接收先写入 ``observations``，保留该条公告在原始响应中的位置与
  内容摘要。重复观察不会重复生成通知或更正。
* 案件由来源身份 ``(authority, case)`` 标识，``revision`` 只推进状态，
  不参与案件身份。不同地区使用相同编号时是两个案件。
* 缺少批次的公告使案件进入待补状态，不产生可执行范围；同一身份下补
  齐批次后仍推进同一案件，不会重建受众。
* 缺少案件身份（例如只有产品名）或自动证据冲突的观察进入待核队列，
  不会因产品名相同自动合案。人工确认的归属作为证据持久保存，优先于
  来源声明，也不会被后续自动采集覆盖。
* 通知范围按"谁见过旧内容、谁现在受影响"解释：逐受众记录已送达的
  批次；后续只补发差量，不重新通知全体。
* 已撤回批次是单调的：旧页重现、更高修订再次列出都不会自动恢复。

案件状态只依赖观察集合与修订号，重放与到达顺序无关。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Any, Callable, Iterable, Mapping

from .recall import REVIEW_DISPOSITIONS, CaseAccumulator, inspect_announcement

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    capture_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    source_index INTEGER NOT NULL,
    authority TEXT,
    case_id TEXT,
    revision INTEGER,
    product TEXT,
    lots TEXT,
    withdrawn_lots TEXT NOT NULL,
    status TEXT,
    content_digest TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'none',
    review_reason TEXT,
    manual_authority TEXT,
    manual_case_id TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (capture_id, source_index)
);
CREATE INDEX IF NOT EXISTS observations_identity ON observations(authority, case_id);

CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY,
    authority TEXT NOT NULL,
    case_id TEXT NOT NULL,
    product TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    current_revision INTEGER,
    seen_revision INTEGER,
    awaiting_lots INTEGER NOT NULL DEFAULT 0,
    effective_lots TEXT NOT NULL DEFAULT '[]',
    withdrawn_lots TEXT NOT NULL DEFAULT '[]',
    basis_observation_id INTEGER,
    review_status TEXT NOT NULL DEFAULT 'none',
    review_reason TEXT,
    confirmed_by TEXT,
    confirmed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (authority, case_id)
);

CREATE TABLE IF NOT EXISTS case_decisions (
    id INTEGER PRIMARY KEY,
    case_uid TEXT NOT NULL,
    observation_id INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    revision INTEGER,
    content_digest TEXT NOT NULL,
    kind TEXT NOT NULL,
    disposition TEXT NOT NULL,
    reason TEXT,
    effective_lots TEXT NOT NULL,
    withdrawn_lots TEXT NOT NULL,
    state_after TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (case_uid, observation_id)
);
CREATE INDEX IF NOT EXISTS decisions_case ON case_decisions(case_uid, ordinal);

CREATE TABLE IF NOT EXISTS stores (
    id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS inventory (
    id INTEGER PRIMARY KEY,
    store_id TEXT NOT NULL,
    product TEXT NOT NULL,
    lot TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS inventory_store ON inventory(store_id, active);

CREATE TABLE IF NOT EXISTS recipients (
    id INTEGER PRIMARY KEY,
    store_id TEXT NOT NULL,
    contact TEXT NOT NULL,
    UNIQUE (store_id, contact)
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE,
    case_uid TEXT NOT NULL,
    store_id TEXT NOT NULL,
    recipient TEXT NOT NULL,
    kind TEXT NOT NULL,
    revision INTEGER NOT NULL,
    observed_revision INTEGER,
    observed_digest TEXT,
    supersedes INTEGER,
    effective_lots TEXT NOT NULL,
    withdraw_lots TEXT NOT NULL,
    reason TEXT,
    basis_observation_id INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS notifications_case ON notifications(case_uid, id);

CREATE TABLE IF NOT EXISTS deliveries (
    notification_id INTEGER PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS delivery_attempts (
    id INTEGER PRIMARY KEY,
    notification_id INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT,
    at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""


def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


def _digest(*parts: Any) -> str:
    return hashlib.sha256("\x1f".join(str(part) for part in parts).encode("utf-8")).hexdigest()


def _order_key(observation: Mapping[str, Any], arrival: int) -> tuple:
    """按修订号重放；无修订号的观察最后处理并单独待核。"""
    revision = observation.get("revision")
    if revision is None:
        return (1, 0, arrival)
    return (0, revision, arrival)


def _product_only(rows) -> bool:
    """受众是否只收到过产品级公告，从未拿到批次口径。

    办了结（closure）之后不再视为产品级：门店已经被告知不受影响，除非
    范围再次变化，否则不重复通知。
    """
    return bool(rows) and all(row["kind"] == "advisory" for row in rows)


class CaseService:
    """在原始归档之上维护召回案件、通知与追补关系。"""

    def __init__(self, connection: sqlite3.Connection, sleeper: Callable[[float], None] = time.sleep):
        self.connection = connection
        self.connection.executescript(SCHEMA)
        self._sleep = sleeper

    # --------------------------------------------------------------- 身份
    @staticmethod
    def case_uid(authority: str, case: str) -> str:
        return f"{authority}\u0000{case}"

    @staticmethod
    def split_uid(case_uid: str) -> tuple[str, str]:
        return case_uid.split("\u0000", 1)

    # --------------------------------------------------------------- 接收
    def ingest(self, capture_id: int, source: str, items: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        """接收一次原始响应中的全部公告观察。对同一 capture 重复调用是安全的。"""
        items = list(items)
        accepted: list[dict[str, Any]] = []
        invalid: list[dict[str, Any]] = []
        touched: set[str] = set()
        for index, raw in enumerate(items):
            try:
                announcement = inspect_announcement(raw)
            except ValueError as error:
                invalid.append({"source_index": index, "error": str(error)})
                continue
            stored = self._store_observation(capture_id, source, index, announcement)
            accepted.append(stored)
            if stored["case_uid"] is not None:
                touched.add(stored["case_uid"])
        for case_uid in sorted(touched):
            self._rebuild(case_uid)
        return {
            "capture_id": capture_id,
            "source": source,
            "items": len(items),
            "accepted": accepted,
            "invalid": invalid,
        }

    def _store_observation(self, capture_id: int, source: str, index: int, announcement) -> dict[str, Any]:
        identity = announcement.identity
        case_uid = self.case_uid(*identity) if identity else None
        reason = None
        if case_uid is None:
            reason = "missing_case_identity"
        elif not announcement.product:
            reason = "missing_product"
        elif not announcement.complete:
            reason = "missing_lots"
        pending = reason in ("missing_case_identity", "missing_product")
        with self.connection:
            cursor = self.connection.execute(
                """INSERT OR IGNORE INTO observations
                   (capture_id, source, source_index, authority, case_id, revision,
                    product, lots, withdrawn_lots, status, content_digest,
                    review_status, review_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    capture_id, source, index,
                    announcement.authority, announcement.case, announcement.revision,
                    announcement.product,
                    _encode(list(announcement.lots) if announcement.lots is not None else None),
                    _encode(list(announcement.withdrawn_lots)),
                    announcement.status, announcement.content_digest,
                    "pending_review" if pending else "none", reason,
                ),
            )
            created = cursor.rowcount > 0
            row = self.connection.execute(
                "SELECT * FROM observations WHERE capture_id=? AND source_index=?",
                (capture_id, index),
            ).fetchone()
        return {
            "observation_id": row["id"],
            "source_index": index,
            "case_uid": case_uid,
            "review_reason": reason,
            "content_digest": announcement.content_digest,
            "created": created,
        }

    # ------------------------------------------------------- 观察归属与重放
    def _attribution_uid(self, row) -> str | None:
        """一条观察当前归属于哪个案件；人工确认优先于来源声明。"""
        if row["manual_authority"] and row["manual_case_id"]:
            return self.case_uid(row["manual_authority"], row["manual_case_id"])
        if row["authority"] and row["case_id"]:
            return self.case_uid(row["authority"], row["case_id"])
        return None

    def _observation_dict(self, row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "authority": row["authority"],
            "case": row["case_id"],
            "revision": row["revision"],
            "product": row["product"],
            "lots": _decode(row["lots"]),
            "withdrawn_lots": _decode(row["withdrawn_lots"], []),
            "status": row["status"],
            "content_digest": row["content_digest"],
            "manual": bool(row["manual_authority"] and row["manual_case_id"]),
        }

    def _observation_rows(self, case_uid: str) -> list[dict[str, Any]]:
        authority, case = self.split_uid(case_uid)
        rows = self.connection.execute(
            """SELECT * FROM observations
               WHERE CASE WHEN manual_authority IS NOT NULL AND manual_case_id IS NOT NULL
                          THEN manual_authority=? AND manual_case_id=?
                          ELSE authority=? AND case_id=? END
               ORDER BY id""",
            (authority, case, authority, case),
        ).fetchall()
        return [self._observation_dict(row) for row in rows]

    def _replay(self, case_uid: str) -> tuple[list[dict[str, Any]], CaseAccumulator]:
        """按修订号重放案件观察，返回每步处置与最终累加器。

        人工确认过的观察即使缺少修订号也可以推进案件：它按已知最大修订
        定位，因此后续真正更高的修订仍能取代它。
        """
        observations = self._observation_rows(case_uid)
        known = [item["revision"] for item in observations if item["revision"] is not None]
        fallback = max(known) if known else 0
        for observation in observations:
            if observation["revision"] is None and observation["manual"]:
                observation["revision"] = fallback
        ordered = sorted(observations, key=lambda item: _order_key(item, item["id"]))
        accumulator = CaseAccumulator()
        steps = []
        for observation in ordered:
            disposition, reason = accumulator.feed(observation)
            steps.append(
                {
                    "observation": observation,
                    "disposition": disposition,
                    "reason": reason,
                    "effective": list(accumulator.effective),
                    "withdrawn": list(accumulator.withdrawn),
                    "state": accumulator.state,
                }
            )
        return steps, accumulator

    def _rebuild(self, case_uid: str) -> dict[str, Any]:
        existing = self._case_row(case_uid)
        steps, accumulator = self._replay(case_uid)
        review_reasons: list[str] = []
        for step in steps:
            observation = step["observation"]
            if observation["manual"]:
                review_status = "confirmed"
            elif step["disposition"] in REVIEW_DISPOSITIONS:
                review_status = "pending_review"
                if step["reason"] not in review_reasons:
                    review_reasons.append(step["reason"])
            else:
                review_status = "none"
            step["review_status"] = review_status
        # 待补只表示缺批次，不是待核：同案身份下补齐即可推进。
        with self.connection:
            attributed = [step["observation"]["id"] for step in steps]
            if attributed:
                placeholders = ",".join("?" * len(attributed))
                # 观察若经人工改判到别案，旧案件里的决定必须一并退出，
                # 否则台账会同时声称两个案件都推进过。
                self.connection.execute(
                    f"DELETE FROM case_decisions WHERE case_uid=? AND observation_id NOT IN ({placeholders})",
                    (case_uid, *attributed),
                )
            else:
                self.connection.execute("DELETE FROM case_decisions WHERE case_uid=?", (case_uid,))
            for ordinal, step in enumerate(steps):
                observation = step["observation"]
                self.connection.execute(
                    """UPDATE observations SET review_status=?, review_reason=?
                       WHERE id=?""",
                    (
                        step["review_status"],
                        step["reason"] if step["review_status"] == "pending_review" else None,
                        observation["id"],
                    ),
                )
                if step["disposition"] in ("duplicate", "stale"):
                    # 重复与过期页面只更新观察状态，不写入决定台账，也不
                    # 产生通知；台账只记录真正推动案件的动作。
                    continue
                self.connection.execute(
                    """INSERT INTO case_decisions
                       (case_uid, observation_id, ordinal, revision, content_digest, kind,
                        disposition, reason, effective_lots, withdrawn_lots, state_after)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(case_uid, observation_id) DO UPDATE SET
                         ordinal=excluded.ordinal, disposition=excluded.disposition,
                         reason=excluded.reason, effective_lots=excluded.effective_lots,
                         withdrawn_lots=excluded.withdrawn_lots,
                         state_after=excluded.state_after""",
                    (
                        case_uid, observation["id"], ordinal, observation.get("revision"),
                        observation.get("content_digest"),
                        "manual" if observation["manual"] else "auto",
                        step["disposition"], step["reason"],
                        _encode(step["effective"]), _encode(step["withdrawn"]), step["state"],
                    ),
                )
        confirmed = bool(existing and existing["confirmed_at"]) or any(
            step["observation"]["manual"] for step in steps
        )
        review_status = "confirmed" if confirmed else ("pending_review" if review_reasons else "none")
        review_reason = None if confirmed else (review_reasons[0] if review_reasons else None)
        state = accumulator.state if steps else "pending_review"
        with self.connection:
            self.connection.execute(
                """INSERT INTO cases
                   (id, authority, case_id, product, state, current_revision, seen_revision,
                    awaiting_lots, effective_lots, withdrawn_lots, basis_observation_id,
                    review_status, review_reason, confirmed_by, confirmed_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                   ON CONFLICT(id) DO UPDATE SET
                     product=excluded.product, state=excluded.state,
                     current_revision=excluded.current_revision,
                     seen_revision=excluded.seen_revision,
                     awaiting_lots=excluded.awaiting_lots,
                     effective_lots=excluded.effective_lots,
                     withdrawn_lots=excluded.withdrawn_lots,
                     basis_observation_id=excluded.basis_observation_id,
                     review_status=excluded.review_status,
                     review_reason=excluded.review_reason,
                     updated_at=excluded.updated_at""",
                (
                    case_uid, *self.split_uid(case_uid), accumulator.product or "", state,
                    accumulator.applied_revision, accumulator.seen_revision,
                    1 if accumulator.awaiting_lots else 0,
                    _encode(list(accumulator.effective)),
                    _encode(list(accumulator.withdrawn)),
                    accumulator.basis_observation_id,
                    review_status, review_reason,
                    existing["confirmed_by"] if existing else None,
                    existing["confirmed_at"] if existing else None,
                ),
            )
        return self._case_dict(self._case_row(case_uid))

    # --------------------------------------------------------------- 待核
    def review_queue(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM observations WHERE review_status='pending_review' ORDER BY id"
        ).fetchall()
        return [
            {
                "observation_id": row["id"],
                "capture_id": row["capture_id"],
                "source": row["source"],
                "source_index": row["source_index"],
                "declared_authority": row["authority"],
                "declared_case": row["case_id"],
                "revision": row["revision"],
                "product": row["product"],
                "lots": _decode(row["lots"]),
                "content_digest": row["content_digest"],
                "reason": row["review_reason"],
                "candidate_cases": self._candidate_cases(row["product"]),
            }
            for row in rows
        ]

    def _candidate_cases(self, product: str | None) -> list[dict[str, Any]]:
        """同产品名的其他案件只作为候选提示，不用于自动合案。"""
        if not product:
            return []
        rows = self.connection.execute(
            "SELECT id, authority, case_id, product, state FROM cases WHERE product=? ORDER BY id",
            (product,),
        ).fetchall()
        return [
            {
                "case_uid": row["id"],
                "authority": row["authority"],
                "case": row["case_id"],
                "product": row["product"],
                "state": row["state"],
            }
            for row in rows
        ]

    def resolve_review(
        self, observation_id: int, authority: str, case: str, *, actor: str, reason: str | None = None
    ) -> dict[str, Any]:
        """人工确认一条证据不足的公告属于哪个案件。

        人工结论持久保存：之后相同产品名的自动观察不会把案件合走，该
        观察也不会再回到待核。
        """
        authority = self._require_text(authority, "authority")
        case = self._require_text(case, "case")
        actor = self._require_text(actor, "actor")
        row = self.connection.execute("SELECT * FROM observations WHERE id=?", (observation_id,)).fetchone()
        if row is None:
            raise KeyError(observation_id)
        target = self.case_uid(authority, case)
        previous = self._attribution_uid(row)
        with self.connection:
            self.connection.execute(
                """UPDATE observations SET review_status='confirmed', review_reason=NULL,
                   manual_authority=?, manual_case_id=? WHERE id=?""",
                (authority, case, observation_id),
            )
            self.connection.execute(
                """INSERT INTO cases(id, authority, case_id, state, review_status, confirmed_by, confirmed_at)
                   VALUES (?,?,?,'pending_review','confirmed',?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                   ON CONFLICT(id) DO UPDATE SET review_status='confirmed', review_reason=NULL,
                     confirmed_by=excluded.confirmed_by, confirmed_at=excluded.confirmed_at,
                     updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
                (target, authority, case, actor),
            )
        rebuilt = self._rebuild(target)
        if previous is not None and previous != target and self._case_row(previous) is not None:
            self._rebuild(previous)
        return {
            "observation_id": observation_id,
            "case_uid": target,
            "attribution": "manual",
            "actor": actor,
            "reason": reason,
            "case": rebuilt,
        }

    @staticmethod
    def _require_text(value: Any, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} is required")
        return value.strip()

    # ------------------------------------------------------------- 门店资料
    def add_store(self, store_id: str, contacts: Iterable[str] = ()) -> None:
        store_id = self._require_text(store_id, "store_id")
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO stores(id) VALUES (?)", (store_id,))
            for contact in contacts:
                contact = self._require_text(contact, "contact")
                self.connection.execute(
                    "INSERT OR IGNORE INTO recipients(store_id, contact) VALUES (?,?)",
                    (store_id, contact),
                )

    def set_inventory(self, store_id: str, product: str, lots: Iterable[str]) -> None:
        """替换门店在某产品上的当前库存；旧记录保留为历史但不再有效。"""
        store_id = self._require_text(store_id, "store_id")
        product = self._require_text(product, "product")
        values: list[str] = []
        for lot in lots:
            lot = self._require_text(lot, "lot")
            if lot not in values:
                values.append(lot)
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO stores(id) VALUES (?)", (store_id,))
            self.connection.execute(
                "UPDATE inventory SET active=0, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE store_id=? AND product=? AND active=1",
                (store_id, product),
            )
            for lot in values:
                self.connection.execute(
                    "INSERT INTO inventory(store_id, product, lot, active) VALUES (?,?,?,1)",
                    (store_id, product, lot),
                )

    def inventory(self, store_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT product, lot FROM inventory WHERE store_id=? AND active=1 ORDER BY id", (store_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def recipients(self, store_id: str) -> list[str]:
        rows = self.connection.execute(
            "SELECT contact FROM recipients WHERE store_id=? ORDER BY id", (store_id,)
        ).fetchall()
        return [row["contact"] for row in rows]

    def _store_ids(self) -> list[str]:
        return [row["id"] for row in self.connection.execute("SELECT id FROM stores ORDER BY id")]

    def _held_lots(self, store_id: str, product: str | None) -> list[str]:
        if not product:
            return []
        result: list[str] = []
        for item in self.inventory(store_id):
            if item["product"] == product and item["lot"] not in result:
                result.append(item["lot"])
        return result

    # --------------------------------------------------------------- 案件图
    def _case_row(self, case_uid: str):
        return self.connection.execute("SELECT * FROM cases WHERE id=?", (case_uid,)).fetchone()

    @staticmethod
    def _case_dict(row) -> dict[str, Any]:
        return {
            "case_uid": row["id"],
            "authority": row["authority"],
            "case": row["case_id"],
            "product": row["product"] or None,
            "state": row["state"],
            "review_status": row["review_status"],
            "review_reason": row["review_reason"],
            "current_revision": row["current_revision"],
            "seen_revision": row["seen_revision"],
            "awaiting_lots": bool(row["awaiting_lots"]),
            "effective_lots": _decode(row["effective_lots"], []),
            "withdrawn_lots": _decode(row["withdrawn_lots"], []),
            "basis_observation_id": row["basis_observation_id"],
            "confirmed_by": row["confirmed_by"],
            "confirmed_at": row["confirmed_at"],
        }

    def list_cases(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM cases ORDER BY authority, case_id").fetchall()
        return [self._case_dict(row) for row in rows]

    def effective_lots(self, case_uid: str) -> list[str]:
        row = self._case_row(case_uid)
        if row is None:
            raise KeyError(case_uid)
        return self._case_dict(row)["effective_lots"]

    def case_scope(self, case_uid: str) -> dict[str, Any]:
        row = self._case_row(case_uid)
        if row is None:
            raise KeyError(case_uid)
        case = self._case_dict(row)
        affected = []
        for store_id in self._store_ids():
            held = self._held_lots(store_id, case["product"])
            matched = [lot for lot in case["effective_lots"] if lot in held]
            if matched:
                affected.append({"store_id": store_id, "lots": matched})
        return {**case, "affected_stores": affected}

    def query_scope(self, store_id: str) -> list[dict[str, Any]]:
        """门店当前有效的召回范围。撤回批次、待补案件与待核案件不会出现。

        人工确认过关联的案件仍然是可执行范围；``confirmed`` 只表示归属已
        核对，不等于证据不足。
        """
        result = []
        for case in self.list_cases():
            if case["state"] != "active":
                # 待补、待核与已撤回的案件都没有可执行范围。
                continue
            if case["review_status"] == "pending_review":
                # 证据不足的案件不进入门店范围，等待人工核对。
                continue
            held = self._held_lots(store_id, case["product"])
            effective = [lot for lot in case["effective_lots"] if lot in held]
            if not effective:
                continue
            result.append(
                {
                    "case_uid": case["case_uid"],
                    "authority": case["authority"],
                    "case": case["case"],
                    "product": case["product"],
                    "current_revision": case["current_revision"],
                    "effective_lots": effective,
                    "withdrawn_lots": [lot for lot in case["withdrawn_lots"] if lot in held],
                }
            )
        return result

    def decision_ledger(self, case_uid: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM case_decisions WHERE case_uid=? ORDER BY ordinal, id", (case_uid,)
        ).fetchall()
        return [
            {
                "decision_id": row["id"],
                "observation_id": row["observation_id"],
                "ordinal": row["ordinal"],
                "revision": row["revision"],
                "content_digest": row["content_digest"],
                "kind": row["kind"],
                "disposition": row["disposition"],
                "reason": row["reason"],
                "effective_lots": _decode(row["effective_lots"], []),
                "withdrawn_lots": _decode(row["withdrawn_lots"], []),
                "state_after": row["state_after"],
            }
            for row in rows
        ]

    # --------------------------------------------------------------- 通知
    def _sent_rows(self, case_uid: str, store_id: str, recipient: str):
        """该受众真正收到过的通知；未送达的通知不算"见过"。"""
        return self.connection.execute(
            """SELECT n.* FROM notifications n
               JOIN deliveries d ON d.notification_id=n.id AND d.status='sent'
               WHERE n.case_uid=? AND n.store_id=? AND n.recipient=? ORDER BY n.id""",
            (case_uid, store_id, recipient),
        ).fetchall()

    def _pending_rows(self, case_uid: str, store_id: str, recipient: str):
        """已生成但尚未送达的通知，补发时重试这些而不是新建更正。"""
        return self.connection.execute(
            """SELECT n.* FROM notifications n
               LEFT JOIN deliveries d ON d.notification_id=n.id
               WHERE n.case_uid=? AND n.store_id=? AND n.recipient=?
                 AND n.status!='superseded' AND (d.status IS NULL OR d.status!='sent')
               ORDER BY n.id""",
            (case_uid, store_id, recipient),
        ).fetchall()

    @staticmethod
    def _delivered(rows) -> list[str]:
        """重放某受众已收到的通知，得到其当前仍被要求召回的批次。"""
        delivered: list[str] = []
        for row in rows:
            if row["kind"] == "advisory":
                # 产品级公告不含批次，不改变批次口径。
                continue
            for lot in _decode(row["withdraw_lots"], []):
                if lot in delivered:
                    delivered.remove(lot)
            for lot in _decode(row["effective_lots"], []):
                if lot not in delivered:
                    delivered.append(lot)
        return delivered

    def advisory_plan(self, case_uid: str) -> list[dict[str, Any]]:
        """产品级公告的受众计划。

        批次未知时可以先发只含产品名的公告，让门店开始排查。这类通知不
        声称任何批次，之后补齐批次时按"谁见过这一版"计算追补。
        """
        row = self._case_row(case_uid)
        if row is None:
            raise KeyError(case_uid)
        case = self._case_dict(row)
        if not case["product"]:
            raise ValueError("case has no product to announce")
        if case["state"] == "withdrawn":
            raise ValueError("case is withdrawn")
        revision = case["seen_revision"] or 0
        items = []
        for store_id in self._store_ids():
            held = self._held_lots(store_id, case["product"])
            if not held:
                continue
            for recipient in self.recipients(store_id):
                if self._sent_rows(case_uid, store_id, recipient):
                    continue
                outstanding = [
                    row for row in self._pending_rows(case_uid, store_id, recipient)
                    if row["kind"] == "advisory" and row["status"] != "superseded"
                ]
                if outstanding:
                    # 已有待送达的产品级公告：重试它，不因修订号变化再建一条。
                    continue
                items.append(
                    self._plan_item(
                        case_uid, store_id, recipient, "advisory", [], [],
                        revision, None, None,
                        "product_level_notice",
                        "batch details not published yet; product-level notice only",
                        held,
                    )
                )
        return items

    def dispatch_advisory(self, case_uid: str) -> list[dict[str, Any]]:
        """生成产品级公告。重复调用不会重复生成。"""
        row = self._case_row(case_uid)
        if row is None:
            raise KeyError(case_uid)
        created = []
        for item in self.advisory_plan(case_uid):
            key = _digest(
                item["case_uid"], item["store_id"], item["recipient"], item["kind"],
                item["revision"],
            )
            with self.connection:
                cursor = self.connection.execute(
                    """INSERT OR IGNORE INTO notifications
                       (event_key, case_uid, store_id, recipient, kind, revision,
                        observed_revision, observed_digest, supersedes, effective_lots,
                        withdraw_lots, reason, basis_observation_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        key, item["case_uid"], item["store_id"], item["recipient"],
                        item["kind"], item["revision"], None, None, None,
                        _encode([]), _encode([]), item["reason"],
                        row["basis_observation_id"],
                    ),
                )
                if cursor.rowcount == 0:
                    continue
                notification_id = cursor.lastrowid
            created.append(
                {
                    "notification_id": notification_id,
                    "case_uid": case_uid,
                    "store_id": item["store_id"],
                    "recipient": item["recipient"],
                    "kind": "advisory",
                    "revision": item["revision"],
                    "effective_lots": [],
                    "withdraw_lots": [],
                    "reason": item["reason"],
                    "supersedes": None,
                }
            )
        return created

    def plan(self, case_uid: str) -> list[dict[str, Any]]:
        """解释每个受众是否应收到首发、追补、更正或撤回通知。

        补发只针对差量：新增批次要求门店当前持有，撤回批次只需门店曾收
        到过旧范围。已经收到产品级公告（尚无批次）的受众，在批次补齐后
        收到追补；补齐后不受影响则收到一次办结通知，避免继续按旧页排查。
        """
        row = self._case_row(case_uid)
        if row is None:
            raise KeyError(case_uid)
        case = self._case_dict(row)
        if case["state"] == "pending_lots" or case["current_revision"] is None:
            raise ValueError("case has no executable scope yet; waiting for lot details")
        effective = case["effective_lots"]
        items: list[dict[str, Any]] = []
        for store_id in self._store_ids():
            held = self._held_lots(store_id, case["product"])
            additions_all = [lot for lot in effective if lot in held]
            for recipient in self.recipients(store_id):
                seen = self._sent_rows(case_uid, store_id, recipient)
                delivered = self._delivered(seen)
                additions = [lot for lot in additions_all if lot not in delivered]
                withdrawals = [lot for lot in delivered if lot not in effective]
                pending = [
                    pending_row
                    for pending_row in self._pending_rows(case_uid, store_id, recipient)
                    # 已被同一受众的后续内容取代的通知不再重试，否则会把
                    # 撤回后的旧范围重新发给门店。
                    if not any(
                        later["supersedes"] == pending_row["id"] for later in seen
                    )
                ]
                if not seen:
                    if not additions:
                        continue
                    item = self._plan_item(
                        case_uid, store_id, recipient, "initial", additions, [],
                        case["current_revision"], pending[-1]["id"] if pending else None, None,
                        "first_notice", "never received a notice; currently holds affected lots",
                        held,
                    )
                elif _product_only(seen):
                    # 门店只见过产品名那版：批次补齐后按当前持有情况追补
                    # 或办结，不把未见过批次的门店当成已通知。
                    kind = "supplement" if additions else "closure"
                    reason = (
                        "lots_now_available"
                        if additions
                        else "not_affected_after_supplementation"
                    )
                    last = seen[-1]
                    item = self._plan_item(
                        case_uid, store_id, recipient, kind, additions, [],
                        case["current_revision"], last["id"], last["revision"],
                        reason, "saw the product-level notice; lot details added later", held,
                    )
                    item["observed_digest"] = last["observed_digest"]
                elif not additions and not withdrawals:
                    continue
                else:
                    if not additions:
                        kind, reason = "withdrawal", "prior_scope_withdrawn"
                    elif withdrawals:
                        kind, reason = "correction", "scope_amended"
                    else:
                        kind, reason = "correction", "newly_affected_scope"
                    last = seen[-1]
                    item = self._plan_item(
                        case_uid, store_id, recipient, kind, additions, withdrawals,
                        case["current_revision"], last["id"], last["revision"],
                        reason, "saw earlier content; only the delta is sent", held,
                    )
                    item["observed_digest"] = last["observed_digest"]
                # 已有同内容未送达通知时，补发应重试它而不是再建一条更正。
                # 只比较送达内容，不看修订号：中间修订若不影响该门店范
                # 围，重试原通知即可，不必产生新的更正。
                same = [
                    pending_row for pending_row in pending
                    if pending_row["kind"] == item["kind"]
                    and pending_row["effective_lots"] == _encode(item["effective_lots"])
                    and pending_row["withdraw_lots"] == _encode(item["withdraw_lots"])
                ]
                if same:
                    # 同内容通知已在队列中但未送达：重试原通知，不新增更正。
                    item["pending_notification_id"] = same[-1]["id"]
                    item["retry"] = True
                    item["revision"] = same[-1]["revision"]
                items.append(item)
        return items

    def _plan_item(
        self,
        case_uid: str,
        store_id: str,
        recipient: str,
        kind: str,
        additions: list[str],
        withdrawals: list[str],
        revision: int,
        supersedes: int | None,
        observed_revision: int | None,
        reason: str,
        explain: str,
        held_now: list[str],
    ) -> dict[str, Any]:
        return {
            "case_uid": case_uid,
            "store_id": store_id,
            "recipient": recipient,
            "kind": kind,
            "revision": revision,
            "effective_lots": additions,
            "withdraw_lots": withdrawals,
            "reason": reason,
            "explain": explain,
            "supersedes": supersedes,
            "observed_revision": observed_revision,
            "observed_digest": None,
            "held_now": held_now,
            "pending_notification_id": None,
            "retry": False,
        }

    def dispatch(self, case_uid: str) -> list[dict[str, Any]]:
        """为计划中的受众生成通知并持久化。重复调用不会生成重复更正。"""
        row = self._case_row(case_uid)
        if row is None:
            raise KeyError(case_uid)
        created = []
        for item in self.plan(case_uid):
            if item["retry"] and item["pending_notification_id"] is not None:
                # 同内容通知已在队列中但未送达：重试原通知，不新增更正。
                created.append(
                    {
                        "notification_id": item["pending_notification_id"],
                        "reused": True,
                        "case_uid": item["case_uid"],
                        "store_id": item["store_id"],
                        "recipient": item["recipient"],
                        "kind": item["kind"],
                        "revision": item["revision"],
                        "effective_lots": item["effective_lots"],
                        "withdraw_lots": item["withdraw_lots"],
                        "reason": item["reason"],
                        "supersedes": item["supersedes"],
                    }
                )
                continue
            key = _digest(
                item["case_uid"], item["store_id"], item["recipient"], item["kind"],
                item["revision"], _encode(item["effective_lots"]), _encode(item["withdraw_lots"]),
                item["supersedes"] or "",
            )
            with self.connection:
                cursor = self.connection.execute(
                    """INSERT OR IGNORE INTO notifications
                       (event_key, case_uid, store_id, recipient, kind, revision,
                        observed_revision, observed_digest, supersedes, effective_lots,
                        withdraw_lots, reason, basis_observation_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        key, item["case_uid"], item["store_id"], item["recipient"],
                        item["kind"], item["revision"], item["observed_revision"],
                        item["observed_digest"], item["supersedes"],
                        _encode(item["effective_lots"]), _encode(item["withdraw_lots"]),
                        item["reason"], row["basis_observation_id"],
                    ),
                )
                if cursor.rowcount == 0:
                    continue
                notification_id = cursor.lastrowid
                if item["supersedes"]:
                    self.connection.execute(
                        "UPDATE notifications SET status='superseded' WHERE id=?",
                        (item["supersedes"],),
                    )
            created.append(
                {
                    "notification_id": notification_id,
                    "case_uid": item["case_uid"],
                    "store_id": item["store_id"],
                    "recipient": item["recipient"],
                    "kind": item["kind"],
                    "revision": item["revision"],
                    "effective_lots": item["effective_lots"],
                    "withdraw_lots": item["withdraw_lots"],
                    "reason": item["reason"],
                    "supersedes": item["supersedes"],
                }
            )
        return created

    def notification(self, notification_id: int) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM notifications WHERE id=?", (notification_id,)).fetchone()
        if row is None:
            raise KeyError(notification_id)
        return {
            "notification_id": row["id"],
            "event_key": row["event_key"],
            "case_uid": row["case_uid"],
            "store_id": row["store_id"],
            "recipient": row["recipient"],
            "kind": row["kind"],
            "revision": row["revision"],
            "observed_revision": row["observed_revision"],
            "observed_digest": row["observed_digest"],
            "supersedes": row["supersedes"],
            "effective_lots": _decode(row["effective_lots"], []),
            "withdraw_lots": _decode(row["withdraw_lots"], []),
            "reason": row["reason"],
            "status": row["status"],
            "basis": self._notification_basis(row["basis_observation_id"]),
        }

    def _notification_basis(self, observation_id: int | None) -> dict[str, Any] | None:
        if observation_id is None:
            return None
        row = self.connection.execute(
            "SELECT * FROM observations WHERE id=?", (observation_id,)
        ).fetchone()
        if row is None:
            return None
        return {
            "observation_id": row["id"],
            "source": row["source"],
            "capture_id": row["capture_id"],
            "source_index": row["source_index"],
            "authority": row["authority"],
            "case": row["case_id"],
            "revision": row["revision"],
            "content_digest": row["content_digest"],
        }

    def notifications_for(
        self, case_uid: str | None = None, store_id: str | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT id FROM notifications"
        clauses, params = [], []
        if case_uid is not None:
            clauses.append("case_uid=?")
            params.append(case_uid)
        if store_id is not None:
            clauses.append("store_id=?")
            params.append(store_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        rows = self.connection.execute(query + " ORDER BY id", params).fetchall()
        return [self.notification(row["id"]) for row in rows]

    # --------------------------------------------------------------- 投递
    def idempotency_key(self, notification_id: int) -> str:
        """通知内容决定幂等键：重试复用，内容不同才换键。"""
        row = self.connection.execute(
            "SELECT event_key FROM notifications WHERE id=?", (notification_id,)
        ).fetchone()
        if row is None:
            raise KeyError(notification_id)
        return "recall:" + row["event_key"]

    def deliver(
        self,
        notification_id: int,
        sender: Callable[[str, dict[str, Any]], Any],
        *,
        max_attempts: int = 3,
        backoff: Callable[[int], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> dict[str, Any]:
        """投递一条通知。失败重试复用同一幂等键，不新增更正记录。"""
        notification = self.notification(notification_id)
        delivery = self.connection.execute(
            "SELECT * FROM deliveries WHERE notification_id=?", (notification_id,)
        ).fetchone()
        if delivery is not None and delivery["status"] == "sent":
            return {"notification_id": notification_id, "status": "sent",
                    "attempts": delivery["attempts"], "reused": True,
                    "idempotency_key": delivery["idempotency_key"]}
        if notification["status"] == "superseded":
            # 已被后续内容取代的旧通知不再投递，否则门店会收到过时范围。
            return {"notification_id": notification_id, "status": "superseded",
                    "attempts": 0, "reused": False,
                    "idempotency_key": self.idempotency_key(notification_id)}
        key = self.idempotency_key(notification_id)
        if delivery is None:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO deliveries(notification_id, idempotency_key) VALUES (?,?)",
                    (notification_id, key),
                )
            attempts_done = 0
        else:
            attempts_done = delivery["attempts"]
        pause = sleep if sleep is not None else self._sleep
        message = {
            "notification_id": notification_id,
            "case_uid": notification["case_uid"],
            "store_id": notification["store_id"],
            "recipient": notification["recipient"],
            "kind": notification["kind"],
            "revision": notification["revision"],
            "effective_lots": notification["effective_lots"],
            "withdraw_lots": notification["withdraw_lots"],
            "supersedes": notification["supersedes"],
            "basis": notification["basis"],
        }
        last_error = None
        while attempts_done < max_attempts:
            attempt = attempts_done + 1
            try:
                sender(key, message)
            except Exception as error:  # 供应商失败需要留痕并重试
                last_error = str(error)
                attempts_done = attempt
                with self.connection:
                    self.connection.execute(
                        """UPDATE deliveries SET status='failed', attempts=?, last_error=?,
                           updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                           WHERE notification_id=?""",
                        (attempts_done, last_error, notification_id),
                    )
                    self.connection.execute(
                        """INSERT INTO delivery_attempts
                           (notification_id, attempt, idempotency_key, outcome, detail)
                           VALUES (?,?,?,?,?)""",
                        (notification_id, attempt, key, "failed", last_error),
                    )
                if attempts_done >= max_attempts:
                    break
                if backoff is not None:
                    pause(backoff(attempt))
                continue
            attempts_done = attempt
            with self.connection:
                self.connection.execute(
                    """UPDATE deliveries SET status='sent', attempts=?, last_error=NULL,
                       updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                       WHERE notification_id=?""",
                    (attempts_done, notification_id),
                )
                self.connection.execute(
                    """INSERT INTO delivery_attempts
                       (notification_id, attempt, idempotency_key, outcome, detail)
                       VALUES (?,?,?,?,?)""",
                    (notification_id, attempt, key, "sent", None),
                )
            return {"notification_id": notification_id, "status": "sent",
                    "attempts": attempts_done, "reused": False, "idempotency_key": key}
        return {"notification_id": notification_id, "status": "failed",
                "attempts": attempts_done, "reused": False,
                "idempotency_key": key, "last_error": last_error}

    def delivery_attempts(self, notification_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM delivery_attempts WHERE notification_id=? ORDER BY attempt, id",
            (notification_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # --------------------------------------------------------------- 审计
    def audit_case(self, case_uid: str) -> dict[str, Any]:
        """一条案件的全部自动/人工决定、批次变化、通知与投递记录。"""
        row = self._case_row(case_uid)
        if row is None:
            raise KeyError(case_uid)
        notifications = self.notifications_for(case_uid)
        for item in notifications:
            item["deliveries"] = self.delivery_attempts(item["notification_id"])
        pending_ids = {
            row["id"]
            for row in self.connection.execute(
                "SELECT id FROM observations WHERE review_status='pending_review'"
            ).fetchall()
        }
        return {
            **self._case_dict(row),
            "notifications": notifications,
            "decisions": self.decision_ledger(case_uid),
            "pending_review": [
                item for item in self.review_queue()
                if item["observation_id"] in pending_ids
                and self._observation_case(item["observation_id"]) == case_uid
            ],
        }

    def _observation_case(self, observation_id: int) -> str | None:
        row = self.connection.execute(
            "SELECT * FROM observations WHERE id=?", (observation_id,)
        ).fetchone()
        return self._attribution_uid(row) if row is not None else None

    def export_case(self, case_uid: str) -> str:
        return json.dumps(self.audit_case(case_uid), ensure_ascii=False, indent=2)
