"""
pipeline/cli.py - Main Command-Line Interface for MayaCardio Bedside Device.
Python 3.11+ standard library only.

Commands implemented:
  pipeline status
  pipeline export --session ID --from device|cloud
  pipeline results --session ID
  pipeline verify --session ID
  pipeline run
"""

import argparse
import json
import os
import sys
import time
from typing import Optional
from pipeline.store import DeviceStore, unpack_24bit_samples
from pipeline.ledger import verify_session_continuity

DEFAULT_DB_PATH = "pipeline_data.db"
DEFAULT_CONFIG_PATH = "config.toml"


def get_default_db_path() -> str:
    env_path = os.environ.get("MAYACARDIO_DB_PATH")
    if env_path:
        return env_path
    return DEFAULT_DB_PATH


def cmd_status(store: DeviceStore, mode: str = "offline"):
    """Prints current mode, samples saved, samples in the cloud, and backlog as ONE JSON line."""
    saved, cloud, backlog = store.get_counts()
    status_obj = {
        "mode": mode,
        "samples_saved": saved,
        "samples_cloud": cloud,
        "backlog": backlog
    }
    print(json.dumps(status_obj))


def cmd_export(store: DeviceStore, session_id: str, source: str, cloud_url: Optional[str] = None, token: Optional[str] = None):
    """Exports data as JSON lines: one per sample, then one line per loss."""
    if source == "cloud":
        # Stream export from cloud service endpoint
        import urllib.request
        if not cloud_url:
            cloud_url = os.environ.get("MAYACARDIO_CLOUD_URL", "http://127.0.0.1:8000")
        if not token:
            token = os.environ.get("MAYACARDIO_AUTH_TOKEN", "")
        url = f"{cloud_url.rstrip('/')}/export?session={session_id}"
        req = urllib.request.Request(url)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req) as resp:
                for line in resp:
                    sys.stdout.write(line.decode("utf-8"))
            return
        except Exception as e:
            sys.stderr.write(f"Error exporting from cloud: {e}\n")
            sys.exit(1)

    # Source is device database
    cur = store.conn.cursor()

    # 1. Output samples sorted by sensor, first_index
    for sensor in (1, 2):
        channels = 3 if sensor == 1 else 2
        cur.execute("""
            SELECT first_index, count, data FROM blocks
            WHERE session_id = ? AND sensor = ?
            ORDER BY first_index ASC;
        """, (session_id, sensor))
        for first_index, count, data_blob in cur.fetchall():
            samples = unpack_24bit_samples(data_blob, channels)
            for i, vals in enumerate(samples):
                sample_obj = {
                    "sensor": sensor,
                    "index": first_index + i,
                    "values": vals
                }
                print(json.dumps(sample_obj))

    # 2. Output losses
    for sensor in (1, 2):
        cur.execute("""
            SELECT from_index, to_index, reason FROM losses
            WHERE session_id = ? AND sensor = ?
            ORDER BY from_index ASC;
        """, (session_id, sensor))
        for from_idx, to_idx, reason in cur.fetchall():
            loss_obj = {
                "sensor": sensor,
                "from_index": from_idx,
                "to_index": to_idx,
                "reason": reason
            }
            print(json.dumps(loss_obj))


def cmd_results(store: DeviceStore, session_id: str):
    """Outputs window results as one JSON line per window."""
    results = store.get_results(session_id)
    for r in results:
        print(r)


def cmd_verify(store: DeviceStore, session_id: str) -> int:
    """Verifies loss ledger and continuous sample accounting. Exits 0 on success."""
    is_valid, issues = verify_session_continuity(store, session_id)
    if not is_valid:
        print(f"VERIFICATION FAILED for session {session_id}:", file=sys.stderr)
        for issue in issues:
            print(f"  - {issue}", file=sys.stderr)
        return 1
    print(f"VERIFICATION PASSED: Session {session_id} has continuous index space with zero silent loss.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="MayaCardio Bedside Pipeline CLI")
    subparsers = parser.add_subparsers(dest="command")

    # status
    p_status = subparsers.add_parser("status", help="Print pipeline status as one JSON line")
    p_status.add_argument("--db-path", default=None, help="Path to SQLite database")

    # export
    p_export = subparsers.add_parser("export", help="Export stored samples and losses")
    p_export.add_argument("--session", required=True, help="Session ID to export")
    p_export.add_argument("--from", dest="from_source", choices=["device", "cloud"], default="device",
                          help="Export source: device or cloud")
    p_export.add_argument("--db-path", default=None, help="Path to SQLite database")
    p_export.add_argument("--cloud-url", default=None, help="Cloud service URL")
    p_export.add_argument("--token", default=None, help="Cloud auth token")

    # results
    p_results = subparsers.add_parser("results", help="Print window results")
    p_results.add_argument("--session", required=True, help="Session ID")
    p_results.add_argument("--db-path", default=None, help="Path to SQLite database")

    # verify
    p_verify = subparsers.add_parser("verify", help="Verify sample ledger continuity")
    p_verify.add_argument("--session", required=True, help="Session ID")
    p_verify.add_argument("--db-path", default=None, help="Path to SQLite database")

    # run
    p_run = subparsers.add_parser("run", help="Run pipeline daemon")
    p_run.add_argument("--mock-host", default="127.0.0.1", help="Mock TCP host")
    p_run.add_argument("--mock-port", type=int, default=7100, help="Mock TCP port")
    p_run.add_argument("--db-path", default=None, help="Path to SQLite database")
    p_run.add_argument("--offline-only", action="store_true", help="Run without network/cloud streaming")
    p_run.add_argument("--cloud-url", default=None, help="Cloud ingest URL")
    p_run.add_argument("--token", default=None, help="Bearer auth token")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    db_path = getattr(args, "db_path", None) or get_default_db_path()
    store = DeviceStore(db_path)

    try:
        if args.command == "status":
            cmd_status(store)
        elif args.command == "export":
            cmd_export(store, args.session, args.from_source, getattr(args, "cloud_url", None), getattr(args, "token", None))
        elif args.command == "results":
            cmd_results(store, args.session)
        elif args.command == "verify":
            code = cmd_verify(store, args.session)
            sys.exit(code)
        elif args.command == "run":
            # For phase 2 collector run
            from pipeline.collector import Collector
            collector = Collector(args.mock_host, args.mock_port, store)
            try:
                collector.run_loop()
            except KeyboardInterrupt:
                collector.stop()
    finally:
        store.close()


if __name__ == "__main__":
    main()
