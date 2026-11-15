import argparse
import json
import sqlite3
import tempfile
from pathlib import Path

from .archive import Archive
from .cases import CaseService
from .client import Client, collect
from .decoding import records
from .intake import RecallIngestor
from .origin import serve


def capture(base_url, archive_path, source, path):
    archive = Archive(archive_path)
    try:
        capture_id, response = collect(Client(base_url), archive, source, path)
        result = {"capture_id": capture_id, "status": response.status, "bytes": len(response.body), "url": response.url}
        if response.status == 200:
            result["source_records"] = records(response.body, response.headers.get("content-type", ""))
        result["archived_observations"] = len(archive.captures())
        return result
    finally:
        archive.close()


def open_cases(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection, CaseService(connection)


def ingest_command(args):
    archive = Archive(args.archive)
    connection, cases = open_cases(args.cases)
    try:
        ingestor = RecallIngestor(archive, cases)
        if args.capture is not None:
            results = [ingestor.ingest_capture(args.capture)]
        else:
            results = ingestor.ingest_source(args.source)["captures"]
        return {"results": results, "cases": cases.list_cases()}
    finally:
        connection.close()
        archive.close()


def store_command(args):
    connection, cases = open_cases(args.cases)
    try:
        cases.add_store(args.store, args.contact or [])
        if args.product is not None:
            cases.set_inventory(args.store, args.product, args.lot or [])
        return {
            "store_id": args.store,
            "recipients": cases.recipients(args.store),
            "inventory": cases.inventory(args.store),
        }
    finally:
        connection.close()


def scope_command(args):
    connection, cases = open_cases(args.cases)
    try:
        if args.case:
            authority, case = args.case.split("/", 1)
            return cases.case_scope(cases.case_uid(authority, case))
        if not args.store:
            raise ValueError("--case or --store is required")
        return {"store_id": args.store, "scope": cases.query_scope(args.store)}
    finally:
        connection.close()


def review_command(args):
    connection, cases = open_cases(args.cases)
    try:
        if args.resolve:
            observation_id, target = args.resolve.split(":", 1)
            authority, case = target.split("/", 1)
            return cases.resolve_review(int(observation_id), authority, case, actor=args.actor, reason=args.reason)
        return {"queue": cases.review_queue()}
    finally:
        connection.close()


def dispatch_command(args):
    connection, cases = open_cases(args.cases)
    try:
        authority, case = args.case.split("/", 1)
        case_uid = cases.case_uid(authority, case)
        if args.advisory:
            planned = cases.advisory_plan(case_uid)
            if not args.send:
                return {"plan": planned}
            created = cases.dispatch_advisory(case_uid)
        else:
            planned = cases.plan(case_uid)
            if not args.send:
                return {"plan": planned}
            created = cases.dispatch(case_uid)
        deliveries = []
        for item in created:
            delivery = cases.deliver(
                item["notification_id"],
                _null_sender,
                max_attempts=args.attempts,
                backoff=lambda attempt: 0,
                sleep=lambda seconds: None,
            )
            deliveries.append(delivery)
        return {"created": created, "deliveries": deliveries}
    finally:
        connection.close()


def _null_sender(key, message):
    """CLI 演练用：记录投递键，不连接真实供应商。"""
    return {"key": key, "notification_id": message["notification_id"]}


def audit_command(args):
    connection, cases = open_cases(args.cases)
    try:
        if args.case:
            authority, case = args.case.split("/", 1)
            return cases.audit_case(cases.case_uid(authority, case))
        return {"notifications": cases.notifications_for()}
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description="召回公告接收、案件追补与通知审计")
    subcommands = parser.add_subparsers(dest="command", required=True)

    demo = subcommands.add_parser("demo")

    collector = subcommands.add_parser("capture")
    collector.add_argument("--base-url", required=True)
    collector.add_argument("--archive", type=Path, required=True)
    collector.add_argument("--source", required=True)
    collector.add_argument("--path", default="/feed")

    server = subcommands.add_parser("serve")
    server.add_argument("--scenario", type=Path)

    ingest = subcommands.add_parser("ingest")
    ingest.add_argument("--archive", type=Path, required=True)
    ingest.add_argument("--cases", type=Path, required=True)
    ingest.add_argument("--source")
    ingest.add_argument("--capture", type=int)

    scope = subcommands.add_parser("scope")
    scope.add_argument("--cases", type=Path, required=True)
    scope.add_argument("--store")
    scope.add_argument("--case", help="authority/case")

    store = subcommands.add_parser("store")
    store.add_argument("--cases", type=Path, required=True)
    store.add_argument("--store", required=True)
    store.add_argument("--contact", action="append")
    store.add_argument("--product")
    store.add_argument("--lot", action="append")

    review = subcommands.add_parser("review")
    review.add_argument("--cases", type=Path, required=True)
    review.add_argument("--resolve", help="observation_id:authority/case")
    review.add_argument("--actor", default="operator")
    review.add_argument("--reason")

    dispatch = subcommands.add_parser("dispatch")
    dispatch.add_argument("--cases", type=Path, required=True)
    dispatch.add_argument("--case", required=True, help="authority/case")
    dispatch.add_argument("--send", action="store_true")
    dispatch.add_argument("--advisory", action="store_true", help="批次未知时的产品级公告")
    dispatch.add_argument("--attempts", type=int, default=3)

    audit = subcommands.add_parser("audit")
    audit.add_argument("--cases", type=Path, required=True)
    audit.add_argument("--case", help="authority/case")

    args = parser.parse_args()
    if args.command == "demo":
        with tempfile.TemporaryDirectory() as temporary, serve() as origin:
            root = Path(temporary)
            result = capture(origin.base_url, root / "raw.sqlite3", "local-source", "/feed")
            archive = Archive(root / "raw.sqlite3")
            connection, cases = open_cases(root / "cases.sqlite3")
            try:
                ingestor = RecallIngestor(archive, cases)
                ingested = ingestor.ingest_capture(result["capture_id"])
            finally:
                connection.close()
                archive.close()
            print(json.dumps(
                {"operation": "raw_capture", **result, "ingest": ingested},
                ensure_ascii=False,
                indent=2,
            ))
        return
    if args.command == "capture":
        print(json.dumps(capture(args.base_url, args.archive, args.source, args.path), ensure_ascii=False, indent=2))
    elif args.command == "ingest":
        print(json.dumps(ingest_command(args), ensure_ascii=False, indent=2))
    elif args.command == "store":
        print(json.dumps(store_command(args), ensure_ascii=False, indent=2))
    elif args.command == "scope":
        print(json.dumps(scope_command(args), ensure_ascii=False, indent=2))
    elif args.command == "review":
        print(json.dumps(review_command(args), ensure_ascii=False, indent=2))
    elif args.command == "dispatch":
        print(json.dumps(dispatch_command(args), ensure_ascii=False, indent=2))
    elif args.command == "audit":
        print(json.dumps(audit_command(args), ensure_ascii=False, indent=2))
    else:
        from .origin import load_scenario
        with serve(load_scenario(args.scenario)) as origin:
            print(json.dumps({"base_url": origin.base_url}, ensure_ascii=False), flush=True)
            try:
                import threading
                threading.Event().wait()
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
