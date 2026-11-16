import argparse
import json
import tempfile
from pathlib import Path

from .archive import Archive
from .cases import CaseBook
from .client import Client, collect
from .decoding import records
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


def followup(args):
    archive = Archive(args.archive)
    book = CaseBook(args.casebook)
    try:
        if args.stores:
            setup = json.loads(args.stores.read_text(encoding="utf-8"))
            for store in setup.get("stores", []):
                book.add_store(store["id"], store.get("name", ""))
                for authority in store.get("authorities", []):
                    book.subscribe(store["id"], authority)
                book.set_inventory(store["id"], store.get("lots", []))
        if args.replay:
            ingested = [book.ingest_capture(archive, capture["id"]) for capture in archive.captures()]
        else:
            capture_id, _response = collect(Client(args.base_url), archive, args.source, args.path)
            ingested = [book.ingest_capture(archive, capture_id)]
        delivery = book.dispatch() if args.dispatch else None
        summary = {
            "ingested": ingested,
            "cases": book.cases(),
            "notifications": book.notifications(),
            "review_queue": book.review_queue(),
            "store_scopes": {store["id"]: book.store_scope(store["id"]) for store in book.stores()},
        }
        if delivery is not None:
            summary["delivery"] = delivery
        return summary
    finally:
        book.close()
        archive.close()


def main():
    parser = argparse.ArgumentParser(description="来源响应接收与归档")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("demo")
    collector = subcommands.add_parser("capture")
    collector.add_argument("--base-url", required=True)
    collector.add_argument("--archive", type=Path, required=True)
    collector.add_argument("--source", required=True)
    collector.add_argument("--path", default="/feed")
    follow = subcommands.add_parser("followup", help="在归档之上接收召回案件并计划追补")
    follow.add_argument("--archive", type=Path, required=True)
    follow.add_argument("--casebook", type=Path, required=True)
    follow.add_argument("--source", default="local-source")
    follow.add_argument("--base-url")
    follow.add_argument("--path", default="/feed")
    follow.add_argument("--stores", type=Path, help="门店、订阅与库存的 JSON 文件")
    follow.add_argument("--replay", action="store_true", help="重放归档中的全部捕获（幂等）")
    follow.add_argument("--dispatch", action="store_true", help="登记送达待处理通知")
    server = subcommands.add_parser("serve")
    server.add_argument("--scenario", type=Path)
    args = parser.parse_args()
    if args.command == "demo":
        with tempfile.TemporaryDirectory() as temporary, serve() as origin:
            result = capture(origin.base_url, Path(temporary) / "archive.sqlite3", "local-source", "/feed")
            print(json.dumps({"operation": "raw_capture", **result}, ensure_ascii=False, indent=2))
    elif args.command == "capture":
        print(json.dumps(capture(args.base_url, args.archive, args.source, args.path), ensure_ascii=False, indent=2))
    elif args.command == "followup":
        if args.replay == bool(args.base_url):
            parser.error("followup 需要 --base-url 或 --replay 之一")
        print(json.dumps(followup(args), ensure_ascii=False, indent=2))
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
