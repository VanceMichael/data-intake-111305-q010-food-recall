"""召回案件接收与追补。

在原始归档（archive.py）之上解释来源观察，把公告变成门店可执行的案件：

- 案件身份只认来源声明的 (authority, case)；不同地区同编号各自成案，
  产品名相同不足以合案。
- 缺关键批次信息时案件保持待补（pending_lots），补齐批次后仍是同一案件。
- 案件修订只前进不回退：旧页重现不会恢复已撤回的批次范围；已撤批次在
  同一案件内不自动回到有效范围。
- 证据不足的观察进入待核（review）；人工确认过的案件关联被保留并优先于
  自动判断。
- 门店库存批次与收件关系可动态变化；补发与更正只发给见过旧内容或当前
  受影响的门店，不对全体重发。
- 通知的创建与发送分离：发送失败可重试，重试不会产生重复更正。
"""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .decoding import records as decode_records

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY,
    authority TEXT NOT NULL,
    case_number TEXT NOT NULL,
    product TEXT,
    state TEXT NOT NULL DEFAULT 'pending_lots',
    max_revision INTEGER NOT NULL DEFAULT -1,
    effective_lots TEXT NOT NULL DEFAULT '[]',
    withdrawn_lots TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (authority, case_number)
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    capture_id INTEGER,
    digest TEXT NOT NULL,
    authority TEXT,
    case_number TEXT,
    revision INTEGER,
    product TEXT,
    lots TEXT,
    withdrawn_lots TEXT,
    status TEXT,
    raw TEXT NOT NULL,
    applied INTEGER NOT NULL DEFAULT 0,
    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS observations_capture_dedup
    ON observations(source, capture_id, digest) WHERE capture_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS observations_direct_dedup
    ON observations(source, digest) WHERE capture_id IS NULL;
CREATE INDEX IF NOT EXISTS observations_identity
    ON observations(authority, case_number, revision);
CREATE TABLE IF NOT EXISTS associations (
    id INTEGER PRIMARY KEY,
    observation_id INTEGER NOT NULL REFERENCES observations(id),
    case_id INTEGER NOT NULL REFERENCES cases(id),
    kind TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (observation_id)
);
CREATE TABLE IF NOT EXISTS review_items (
    id INTEGER PRIMARY KEY,
    observation_id INTEGER NOT NULL REFERENCES observations(id),
    reason TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'open',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS stores (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS subscriptions (
    store_id TEXT NOT NULL REFERENCES stores(id),
    authority TEXT NOT NULL,
    PRIMARY KEY (store_id, authority)
);
CREATE INDEX IF NOT EXISTS subscriptions_authority ON subscriptions(authority);
CREATE TABLE IF NOT EXISTS inventory (
    store_id TEXT NOT NULL REFERENCES stores(id),
    lot TEXT NOT NULL,
    PRIMARY KEY (store_id, lot)
);
CREATE TABLE IF NOT EXISTS change_events (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    revision INTEGER NOT NULL,
    kind TEXT NOT NULL,
    effective_lots TEXT NOT NULL,
    withdrawn_lots TEXT NOT NULL,
    basis_observation_id INTEGER NOT NULL REFERENCES observations(id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES change_events(id),
    case_id INTEGER NOT NULL REFERENCES cases(id),
    store_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    effective_lots TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (event_id, store_id)
);
CREATE INDEX IF NOT EXISTS notifications_case ON notifications(case_id);
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY,
    notification_id INTEGER NOT NULL REFERENCES notifications(id),
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    attempted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""


def _text_or_none(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _json_list(value):
    return json.dumps(list(value), ensure_ascii=False)


class CaseBook:
    """召回案件簿：接收公告观察，维护案件状态，计划并登记追补通知。"""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(SCHEMA)

    def close(self):
        self.connection.close()

    @contextmanager
    def transaction(self):
        with self.connection:
            yield self.connection

    # ---- 门店、收件关系与库存（均可动态变化） ----

    def add_store(self, store_id, name=""):
        store_id = str(store_id).strip()
        if not store_id:
            raise ValueError("store id is required")
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO stores(id,name) VALUES (?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                (store_id, str(name)),
            )

    def subscribe(self, store_id, authority):
        authority = str(authority).strip()
        if not authority:
            raise ValueError("authority is required")
        with self.transaction() as connection:
            self._ensure_store(connection, store_id)
            connection.execute(
                "INSERT OR IGNORE INTO subscriptions(store_id,authority) VALUES (?,?)",
                (str(store_id).strip(), authority),
            )

    def unsubscribe(self, store_id, authority):
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM subscriptions WHERE store_id=? AND authority=?",
                (str(store_id).strip(), str(authority).strip()),
            )

    def set_inventory(self, store_id, lots):
        if not isinstance(lots, (list, tuple, set)) or any(
            not isinstance(lot, str) or not lot.strip() for lot in lots
        ):
            raise ValueError("lots must be non-empty strings")
        with self.transaction() as connection:
            store_id = self._ensure_store(connection, store_id)
            connection.execute("DELETE FROM inventory WHERE store_id=?", (store_id,))
            connection.executemany(
                "INSERT OR IGNORE INTO inventory(store_id,lot) VALUES (?,?)",
                [(store_id, lot.strip()) for lot in lots],
            )

    def stores(self):
        rows = self.connection.execute("SELECT * FROM stores ORDER BY id").fetchall()
        return [{"id": row["id"], "name": row["name"]} for row in rows]

    @staticmethod
    def _ensure_store(connection, store_id):
        store_id = str(store_id).strip()
        if not store_id:
            raise ValueError("store id is required")
        connection.execute("INSERT OR IGNORE INTO stores(id) VALUES (?)", (store_id,))
        return store_id

    # ---- 案件 ----

    def ensure_case(self, authority, case_number, product=None):
        """运营手工建案；已存在时仅补充缺失的产品名。"""
        authority, case_number = str(authority).strip(), str(case_number).strip()
        if not authority or not case_number:
            raise ValueError("authority and case number are required")
        with self.transaction() as connection:
            self._create_case(connection, authority, case_number, _text_or_none(product))
        return self.get_case(authority, case_number)

    def get_case(self, authority, case_number):
        row = self.connection.execute(
            "SELECT * FROM cases WHERE authority=? AND case_number=?",
            (str(authority).strip(), str(case_number).strip()),
        ).fetchone()
        return self._case_dict(row) if row is not None else None

    def cases(self):
        rows = self.connection.execute("SELECT * FROM cases ORDER BY id").fetchall()
        return [self._case_dict(row) for row in rows]

    @staticmethod
    def _case_dict(row):
        return {
            "id": row["id"],
            "authority": row["authority"],
            "case_number": row["case_number"],
            "product": row["product"],
            "state": row["state"],
            "max_revision": row["max_revision"],
            "effective_lots": json.loads(row["effective_lots"]),
            "withdrawn_lots": json.loads(row["withdrawn_lots"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _case_row(self, connection, authority, case_number):
        return connection.execute(
            "SELECT * FROM cases WHERE authority=? AND case_number=?",
            (authority, case_number),
        ).fetchone()

    def _create_case(self, connection, authority, case_number, product=None):
        connection.execute(
            "INSERT OR IGNORE INTO cases(authority,case_number,product) VALUES (?,?,?)",
            (authority, case_number, product),
        )
        if product:
            connection.execute(
                "UPDATE cases SET product=? WHERE authority=? AND case_number=? AND product IS NULL",
                (product, authority, case_number),
            )
        return connection.execute(
            "SELECT id FROM cases WHERE authority=? AND case_number=?",
            (authority, case_number),
        ).fetchone()["id"]

    # ---- 公告观察接收 ----

    def ingest(self, record, *, source, capture_id=None):
        """接收一条来源观察，返回接收结果。

        同一来源、同一捕获（capture_id）的相同内容幂等去重；旧页被重新抓取
        属于新捕获，会登记为新观察并按修订高低分类。

        事件取值：opened / lots_supplied / corrected / applied（已应用但范围
        未变）/ duplicate / stale / already_ingested / pending_review。
        """
        if not isinstance(record, dict):
            raise ValueError("record must be an object")
        source = str(source).strip() if source is not None else ""
        if not source:
            raise ValueError("source is required")
        if capture_id is not None and (isinstance(capture_id, bool) or not isinstance(capture_id, int)):
            raise TypeError("capture_id must be an integer")
        digest = hashlib.sha256(
            json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        with self.transaction() as connection:
            existing = self._find_observation(connection, source, capture_id, digest)
            if existing is not None:
                return {"event": "already_ingested", "observation_id": existing,
                        "case_id": None, "notified": []}
            problem = self._validate(record)
            observation_id = self._insert_observation(connection, source, capture_id, digest, record)
            if problem is not None:
                review_id = self._open_review(
                    connection, observation_id, problem, "来源记录缺少身份字段或字段非法")
                return {"event": "pending_review", "observation_id": observation_id,
                        "review_id": review_id, "review": problem, "case_id": None, "notified": []}
            authority = str(record["authority"]).strip()
            case_number = str(record["case"]).strip()
            revision = record["revision"]
            case = self._case_row(connection, authority, case_number)
            if case is None:
                self._create_case(connection, authority, case_number,
                                  _text_or_none(record.get("product")))
                case = self._case_row(connection, authority, case_number)
            case_id = case["id"]
            product = _text_or_none(record.get("product"))
            if case["product"] and product and product != case["product"]:
                review_id = self._open_review(
                    connection, observation_id, "product_conflict",
                    f"案件产品为「{case['product']}」，观察声称为「{product}」")
                return {"event": "pending_review", "observation_id": observation_id,
                        "review_id": review_id, "review": "product_conflict",
                        "case_id": case_id, "notified": []}
            if revision < case["max_revision"]:
                self._associate(connection, observation_id, case_id, "auto_identity")
                return {"event": "stale", "observation_id": observation_id,
                        "case_id": case_id, "notified": []}
            if revision == case["max_revision"]:
                applied_digest = self._applied_digest(connection, case_id, revision)
                if applied_digest == digest:
                    self._associate(connection, observation_id, case_id, "auto_identity")
                    return {"event": "duplicate", "observation_id": observation_id,
                            "case_id": case_id, "notified": []}
                review_id = self._open_review(
                    connection, observation_id, "revision_conflict",
                    "同一修订号出现了不同内容")
                return {"event": "pending_review", "observation_id": observation_id,
                        "review_id": review_id, "review": "revision_conflict",
                        "case_id": case_id, "notified": []}
            self._associate(connection, observation_id, case_id, "auto_identity")
            return self._apply(connection, self._case_dict(case), record, observation_id)

    def ingest_capture(self, archive, capture_id, *, source=None):
        """把归档中一次捕获的公告记录逐条接收；非 200 响应跳过。"""
        meta = next((c for c in archive.captures() if c["id"] == capture_id), None)
        if meta is None:
            raise KeyError(capture_id)
        if meta["status"] != 200:
            return {"event": "skipped", "capture_id": capture_id,
                    "status": meta["status"], "results": []}
        body = archive.body(capture_id)
        items = decode_records(body, meta["headers"].get("content-type", ""))
        return {
            "event": "ingested",
            "capture_id": capture_id,
            "results": [
                self.ingest(item, source=source or meta["source"], capture_id=capture_id)
                for item in items
            ],
        }

    @staticmethod
    def _validate(record):
        """身份或批次证据不足时返回待核原因，否则返回 None。"""
        authority = record.get("authority")
        case_number = record.get("case")
        revision = record.get("revision")
        if not isinstance(authority, str) or not authority.strip():
            return "missing_identity"
        if (isinstance(case_number, bool) or not isinstance(case_number, (str, int))
                or not str(case_number).strip()):
            return "missing_identity"
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            return "missing_identity"
        for field in ("lots", "withdrawn_lots"):
            value = record.get(field)
            if value is not None and (
                not isinstance(value, list)
                or any(not isinstance(lot, str) or not lot.strip() for lot in value)
            ):
                return "malformed_record"
        for field in ("product", "status"):
            if record.get(field) is not None and not isinstance(record.get(field), str):
                return "malformed_record"
        return None

    @staticmethod
    def _find_observation(connection, source, capture_id, digest):
        if capture_id is not None:
            row = connection.execute(
                "SELECT id FROM observations WHERE source=? AND capture_id=? AND digest=?",
                (source, capture_id, digest),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT id FROM observations WHERE source=? AND capture_id IS NULL AND digest=?",
                (source, digest),
            ).fetchone()
        return row["id"] if row is not None else None

    @staticmethod
    def _insert_observation(connection, source, capture_id, digest, record):
        lots = record.get("lots")
        withdrawn = record.get("withdrawn_lots")
        revision = record.get("revision")
        cursor = connection.execute(
            "INSERT INTO observations(source,capture_id,digest,authority,case_number,"
            "revision,product,lots,withdrawn_lots,status,raw) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                source,
                capture_id,
                digest,
                _text_or_none(record.get("authority")),
                _text_or_none(record.get("case")),
                revision if isinstance(revision, int) and not isinstance(revision, bool) else None,
                _text_or_none(record.get("product")),
                _json_list(lots) if isinstance(lots, list) else None,
                _json_list(withdrawn) if isinstance(withdrawn, list) else None,
                record.get("status") if isinstance(record.get("status"), str) else None,
                json.dumps(record, ensure_ascii=False, sort_keys=True),
            ),
        )
        return cursor.lastrowid

    @staticmethod
    def _associate(connection, observation_id, case_id, kind, note=""):
        connection.execute(
            "INSERT OR IGNORE INTO associations(observation_id,case_id,kind,note) VALUES (?,?,?,?)",
            (observation_id, case_id, kind, note),
        )

    @staticmethod
    def _open_review(connection, observation_id, reason, detail):
        cursor = connection.execute(
            "INSERT INTO review_items(observation_id,reason,detail) VALUES (?,?,?)",
            (observation_id, reason, detail),
        )
        return cursor.lastrowid

    @staticmethod
    def _applied_digest(connection, case_id, revision):
        row = connection.execute(
            "SELECT o.digest FROM observations o "
            "JOIN associations a ON a.observation_id = o.id "
            "WHERE a.case_id=? AND o.revision=? AND o.applied=1 "
            "ORDER BY o.id DESC LIMIT 1",
            (case_id, revision),
        ).fetchone()
        return row["digest"] if row is not None else None

    def _apply(self, connection, case, record, observation_id):
        """应用一条更新的修订：推进案件状态，必要时生成变更事件与通知。"""
        revision = record["revision"]
        old_effective = list(case["effective_lots"])
        old_withdrawn = list(case["withdrawn_lots"])
        withdrawn = sorted(set(old_withdrawn) | set(record.get("withdrawn_lots") or []))
        lots = record.get("lots")
        if isinstance(lots, list):
            effective = sorted({lot.strip() for lot in lots} - set(withdrawn))
            state = "executable"
        else:
            effective, state = [], "pending_lots"
        product = _text_or_none(record.get("product")) or case["product"]
        if case["max_revision"] < 0:
            event_kind = "opened"
        elif case["state"] == "pending_lots" and state == "executable":
            event_kind = "lots_supplied"
        elif state != case["state"] or effective != old_effective or withdrawn != old_withdrawn:
            event_kind = "corrected"
        else:
            event_kind = None
        connection.execute(
            "UPDATE cases SET product=?, state=?, max_revision=?, effective_lots=?,"
            " withdrawn_lots=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
            (product, state, revision, _json_list(effective), _json_list(withdrawn), case["id"]),
        )
        connection.execute("UPDATE observations SET applied=1 WHERE id=?", (observation_id,))
        result = {"event": event_kind or "applied", "observation_id": observation_id,
                  "case_id": case["id"], "notified": []}
        if event_kind is None:
            return result
        if event_kind == "opened":
            notification_kind = "initial"
        elif event_kind == "lots_supplied":
            notification_kind = "supplement"
        elif set(withdrawn) - set(old_withdrawn) or set(old_effective) - set(effective):
            notification_kind = "correction"
        else:
            notification_kind = "supplement"
        event_id = connection.execute(
            "INSERT INTO change_events(case_id,revision,kind,effective_lots,withdrawn_lots,"
            "basis_observation_id) VALUES (?,?,?,?,?,?)",
            (case["id"], revision, event_kind, _json_list(effective),
             _json_list(withdrawn), observation_id),
        ).lastrowid
        recipients = self._plan_recipients(
            connection, case["id"], case["authority"], event_kind, effective)
        notified = []
        for store_id, reason in sorted(recipients.items()):
            cursor = connection.execute(
                "INSERT OR IGNORE INTO notifications(event_id,case_id,store_id,kind,reason,"
                "effective_lots) VALUES (?,?,?,?,?,?)",
                (event_id, case["id"], store_id, notification_kind, reason,
                 _json_list(effective)),
            )
            if cursor.rowcount:
                notified.append({"store": store_id, "kind": notification_kind, "reason": reason})
        result.update({"event_id": event_id, "notified": notified})
        return result

    @staticmethod
    def _plan_recipients(connection, case_id, authority, event_kind, effective):
        """补发范围 = 见过旧内容的门店 ∪ 当前受影响的门店，不对全体重发。"""
        previous = {
            row["store_id"]
            for row in connection.execute(
                "SELECT DISTINCT store_id FROM notifications WHERE case_id=?", (case_id,))
        }
        subscribed = {
            row["store_id"]
            for row in connection.execute(
                "SELECT store_id FROM subscriptions WHERE authority=?", (authority,))
        }
        if effective:
            holders = {
                row["store_id"]
                for row in connection.execute(
                    "SELECT DISTINCT store_id FROM inventory WHERE lot IN (%s)"
                    % ",".join("?" * len(effective)), tuple(effective))
            }
            affected = subscribed & holders
        elif event_kind == "opened":
            # 首版公告只有产品名时，收件关系内的门店先按产品名排查。
            affected = set(subscribed)
        else:
            affected = set()
        recipients = {}
        for store_id in previous | affected:
            if store_id in affected and store_id in previous:
                reason = "affected+previously_notified"
            elif store_id in affected:
                reason = "affected"
            else:
                reason = "previously_notified"
            recipients[store_id] = reason
        return recipients

    # ---- 人工确认与待核 ----

    def confirm_association(self, observation_id, case_id, note=""):
        """人工确认观察属于某案件；关联被保留，待核项随之了结。"""
        with self.transaction() as connection:
            observation = connection.execute(
                "SELECT * FROM observations WHERE id=?", (observation_id,)).fetchone()
            if observation is None:
                raise KeyError(f"observation {observation_id}")
            case = connection.execute(
                "SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
            if case is None:
                raise KeyError(f"case {case_id}")
            linked = connection.execute(
                "SELECT 1 FROM associations WHERE observation_id=?",
                (observation_id,)).fetchone()
            if linked is not None:
                raise ValueError("observation already associated")
            self._associate(connection, observation_id, case_id, "manual", str(note))
            connection.execute(
                "UPDATE review_items SET state='resolved', note=?"
                " WHERE observation_id=? AND state='open'",
                (str(note), observation_id),
            )
            record = json.loads(observation["raw"])
            if self._validate(record) is None and record["revision"] > case["max_revision"]:
                result = self._apply(connection, self._case_dict(case), record, observation_id)
                result["observation_id"] = observation_id
                return result
            return {"event": "associated", "observation_id": observation_id,
                    "case_id": case_id, "notified": []}

    def review_queue(self):
        rows = self.connection.execute(
            "SELECT r.id AS review_id, r.reason, r.detail, r.created_at,"
            " o.id AS observation_id, o.raw, o.capture_id, o.source"
            " FROM review_items r JOIN observations o ON o.id = r.observation_id"
            " WHERE r.state='open' ORDER BY r.id",
        ).fetchall()
        return [
            {
                "review_id": row["review_id"],
                "observation_id": row["observation_id"],
                "reason": row["reason"],
                "detail": row["detail"],
                "record": json.loads(row["raw"]),
                "capture_id": row["capture_id"],
                "source": row["source"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # ---- 通知发送（创建与发送分离，失败可重试） ----

    def notifications(self, case_id=None, store_id=None):
        query, params = "SELECT * FROM notifications", []
        clauses = []
        if case_id is not None:
            clauses.append("case_id=?")
            params.append(case_id)
        if store_id is not None:
            clauses.append("store_id=?")
            params.append(store_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        rows = self.connection.execute(query + " ORDER BY id", params).fetchall()
        return [
            {
                "id": row["id"],
                "event_id": row["event_id"],
                "case_id": row["case_id"],
                "store_id": row["store_id"],
                "kind": row["kind"],
                "reason": row["reason"],
                "state": row["state"],
                "attempts": row["attempts"],
                "effective_lots": json.loads(row["effective_lots"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def dispatch(self, sender=None):
        """发送待处理与失败的通知。

        sender 接收通知字典，抛异常记为失败并留待重试；缺省仅登记送达。
        已送达的通知不会重复发送，也不会因重试产生新的更正。
        """
        outcome = {"sent": [], "failed": []}
        rows = self.connection.execute(
            "SELECT * FROM notifications WHERE state IN ('pending','failed') ORDER BY id"
        ).fetchall()
        for row in rows:
            payload = self._notification_payload(row)
            try:
                if sender is not None:
                    sender(payload)
                state, detail = "sent", ""
            except Exception as error:  # 任何发送失败都登记留待重试
                state, detail = "failed", str(error)
            with self.transaction() as connection:
                connection.execute(
                    "INSERT INTO deliveries(notification_id,outcome,detail) VALUES (?,?,?)",
                    (row["id"], state, detail),
                )
                connection.execute(
                    "UPDATE notifications SET state=?, attempts=attempts+1 WHERE id=?",
                    (state, row["id"]),
                )
            outcome["sent" if state == "sent" else "failed"].append(row["id"])
        return outcome

    def _notification_payload(self, row):
        case = self.connection.execute(
            "SELECT * FROM cases WHERE id=?", (row["case_id"],)).fetchone()
        event = self.connection.execute(
            "SELECT * FROM change_events WHERE id=?", (row["event_id"],)).fetchone()
        return {
            "notification_id": row["id"],
            "store_id": row["store_id"],
            "kind": row["kind"],
            "reason": row["reason"],
            "authority": case["authority"],
            "case_number": case["case_number"],
            "product": case["product"],
            "revision": event["revision"],
            "effective_lots": json.loads(row["effective_lots"]),
            "event_id": row["event_id"],
        }

    # ---- 查询 ----

    def store_scope(self, store_id):
        """门店当前有效范围：其订阅机构下各案件的有效批次与本店命中情况。"""
        store_id = str(store_id).strip()
        if self.connection.execute(
            "SELECT 1 FROM stores WHERE id=?", (store_id,)).fetchone() is None:
            raise KeyError(store_id)
        authorities = [
            row["authority"]
            for row in self.connection.execute(
                "SELECT authority FROM subscriptions WHERE store_id=? ORDER BY authority",
                (store_id,))
        ]
        inventory = {
            row["lot"]
            for row in self.connection.execute(
                "SELECT lot FROM inventory WHERE store_id=?", (store_id,))
        }
        entries = []
        for authority in authorities:
            for row in self.connection.execute(
                "SELECT * FROM cases WHERE authority=? ORDER BY id", (authority,)):
                case = self._case_dict(row)
                notified = self.connection.execute(
                    "SELECT 1 FROM notifications WHERE case_id=? AND store_id=? LIMIT 1",
                    (case["id"], store_id)).fetchone() is not None
                entries.append({
                    "case_id": case["id"],
                    "authority": case["authority"],
                    "case_number": case["case_number"],
                    "product": case["product"],
                    "state": case["state"],
                    "effective_lots": case["effective_lots"],
                    "withdrawn_lots": case["withdrawn_lots"],
                    "inventory_hits": sorted(inventory & set(case["effective_lots"])),
                    "withdrawn_hits": sorted(inventory & set(case["withdrawn_lots"])),
                    "notified": notified,
                })
        return {"store": store_id, "cases": entries}

    def case_timeline(self, authority, case_number):
        """运营审计：案件的观察、变更事件、每次通知及其公告依据。"""
        case = self.get_case(authority, case_number)
        if case is None:
            raise KeyError(f"{authority}/{case_number}")
        observations = []
        for row in self.connection.execute(
            "SELECT o.*, a.kind AS association, a.note AS association_note"
            " FROM observations o JOIN associations a ON a.observation_id = o.id"
            " WHERE a.case_id=? ORDER BY o.id",
            (case["id"],)):
            observations.append({
                "id": row["id"],
                "source": row["source"],
                "capture_id": row["capture_id"],
                "revision": row["revision"],
                "product": row["product"],
                "lots": json.loads(row["lots"]) if row["lots"] else None,
                "withdrawn_lots": json.loads(row["withdrawn_lots"]) if row["withdrawn_lots"] else None,
                "status": row["status"],
                "applied": bool(row["applied"]),
                "association": row["association"],
                "association_note": row["association_note"],
                "received_at": row["received_at"],
            })
        events = []
        for event in self.connection.execute(
            "SELECT * FROM change_events WHERE case_id=? ORDER BY id", (case["id"],)):
            basis = self.connection.execute(
                "SELECT id, capture_id, source, revision FROM observations WHERE id=?",
                (event["basis_observation_id"],)).fetchone()
            notifications = []
            for note_row in self.connection.execute(
                "SELECT * FROM notifications WHERE event_id=? ORDER BY id", (event["id"],)):
                deliveries = [
                    {"outcome": d["outcome"], "detail": d["detail"],
                     "attempted_at": d["attempted_at"]}
                    for d in self.connection.execute(
                        "SELECT * FROM deliveries WHERE notification_id=? ORDER BY id",
                        (note_row["id"],))
                ]
                notifications.append({
                    "id": note_row["id"],
                    "store_id": note_row["store_id"],
                    "kind": note_row["kind"],
                    "reason": note_row["reason"],
                    "state": note_row["state"],
                    "attempts": note_row["attempts"],
                    "effective_lots": json.loads(note_row["effective_lots"]),
                    "deliveries": deliveries,
                })
            events.append({
                "id": event["id"],
                "kind": event["kind"],
                "revision": event["revision"],
                "effective_lots": json.loads(event["effective_lots"]),
                "withdrawn_lots": json.loads(event["withdrawn_lots"]),
                "basis": dict(basis) if basis is not None else None,
                "created_at": event["created_at"],
                "notifications": notifications,
            })
        return {"case": case, "observations": observations, "events": events}
