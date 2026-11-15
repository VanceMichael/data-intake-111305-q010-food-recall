"""把原始归档中的响应接入召回案件接收。

原始采集 (:mod:`food_recall.archive`) 与业务解释 (:mod:`food_recall.cases`)
保持独立：本模块只负责读取已归档的响应、解码公告并交给案例服务。它
不重新请求网络，因此可以安全地重放历史捕获。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .archive import Archive
from .cases import CaseService
from .decoding import records


class RecallIngestor:
    def __init__(self, capture_archive: Archive, cases: CaseService):
        self.archive = capture_archive
        self.cases = cases

    def ingest_capture(self, capture_id: int) -> dict[str, Any]:
        """解码一条已归档捕获并送入案件服务。重复调用不会重复建案。"""
        captures = {row["id"]: row for row in self.archive.captures()}
        capture = captures.get(capture_id)
        if capture is None:
            raise KeyError(capture_id)
        body = self.archive.body(capture_id)
        media_type = capture["headers"].get("content-type", "")
        try:
            items = records(body, media_type)
        except ValueError as error:
            return {
                "capture_id": capture_id,
                "source": capture["source"],
                "status": capture["status"],
                "decoded": False,
                "error": str(error),
                "accepted": [],
                "invalid": [],
            }
        result = self.cases.ingest(capture_id, capture["source"], items)
        result["status"] = capture["status"]
        result["decoded"] = True
        return result

    def ingest_source(self, source: str) -> dict[str, Any]:
        """按接收顺序重放某个来源的全部成功捕获。"""
        results = []
        for capture in self.archive.captures(source):
            if not 200 <= capture["status"] < 300:
                results.append(
                    {
                        "capture_id": capture["id"],
                        "source": source,
                        "status": capture["status"],
                        "decoded": False,
                        "skipped": "non-success response",
                    }
                )
                continue
            results.append(self.ingest_capture(capture["id"]))
        return {"source": source, "captures": results}


def open_case_service(database: Path) -> tuple[sqlite3.Connection, CaseService]:
    """打开（必要时创建）案件数据库。"""
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection, CaseService(connection)
