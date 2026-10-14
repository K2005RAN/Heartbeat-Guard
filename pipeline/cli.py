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


STATE_FILE_PATH = "/tmp/mayacardio_state.json"


def write_daemon_state(mode: str, session_id: Optional[str] = None):
    try:
        data = {"mode": mode, "session_id": session_id, "timestamp": time.time()}
        with open(STATE_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def read_daemon_state() -> dict:
    try:
        if os.path.exists(STATE_FILE_PATH):
            with open(STATE_FILE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"mode": "offline", "session_id": None}


def cmd_status(store: DeviceStore, cloud_url: Optional[str] = None):
    """Prints current mode, samples saved, samples in the cloud, and backlog as ONE JSON line."""
    state = read_daemon_state()
    mode = state.get("mode", "offline")
    session_id = state.get("session_id")

    saved, cloud, backlog = store.get_counts(session_id)
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
            token = os.environ.get("MAYACARDIO_AUTH_TOKEN", "mayacardio_secret_token_2026")
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


def cmd_results(store: DeviceStore, session_id: str, cloud_url: Optional[str] = None, token: Optional[str] = None):
    """Outputs window results as one JSON line per window."""
    results = store.get_results(session_id)
    if results:
        for r in results:
            print(r)
        return

    # If no local results exist (e.g. session was processed online by cloud), fetch from cloud
    import urllib.request
    cloud_url = cloud_url or os.environ.get("MAYACARDIO_CLOUD_URL", "http://127.0.0.1:8000")
    token = token or os.environ.get("MAYACARDIO_AUTH_TOKEN", "mayacardio_secret_token_2026")
    url = f"{cloud_url.rstrip('/')}/results?session={session_id}"
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results_list = data.get("results", []) if isinstance(data, dict) else data
            for item in results_list:
                print(json.dumps(item) if not isinstance(item, str) else item)
    except Exception as e:
        pass


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


def cmd_run(mock_host: str, mock_port: int, db_path: str, offline_only: bool,
            cloud_url: Optional[str], token: Optional[str]):
    """Runs the complete Bedside Pipeline daemon."""
    import threading
    from pipeline.collector import Collector
    from pipeline.uploader import Uploader
    from pipeline.netmon import NetworkMonitor
    from pipeline.local_processor import LocalProcessor

    store = DeviceStore(db_path)
    cloud_url = cloud_url or os.environ.get("MAYACARDIO_CLOUD_URL", "http://127.0.0.1:8000")
    token = token or os.environ.get("MAYACARDIO_AUTH_TOKEN", "mayacardio_secret_token_2026")

    collector = Collector(mock_host, mock_port, store)
    local_proc = LocalProcessor(store)
    uploader = None
    netmon = None

    current_mode = ["offline"]

    def on_mode_change(new_mode: str):
        current_mode[0] = new_mode
        local_proc.set_offline_state(new_mode == "offline")
        write_daemon_state(new_mode, collector.current_session_id)
        sys.stderr.write(f"[PIPELINE DAEMON] Active mode: {new_mode}\n")

    threads = []
    c_th = threading.Thread(target=collector.run_loop, daemon=True)
    lp_th = threading.Thread(target=local_proc.run_loop, daemon=True)
    threads.extend([c_th, lp_th])

    if not offline_only:
        uploader = Uploader(cloud_url, token, store)
        netmon = NetworkMonitor(cloud_url, on_mode_change=on_mode_change)
        u_th = threading.Thread(target=uploader.run_loop, daemon=True)
        nm_th = threading.Thread(target=netmon.run_loop, daemon=True)
        threads.extend([u_th, nm_th])
    else:
        local_proc.set_offline_state(True)
        write_daemon_state("offline", None)

    for th in threads:
        th.start()

    sys.stderr.write("[PIPELINE DAEMON] Pipeline daemon started successfully\n")
    try:
        while True:
            time.sleep(1.0)
            write_daemon_state(current_mode[0], collector.current_session_id)
    except KeyboardInterrupt:
        sys.stderr.write("[PIPELINE DAEMON] Shutting down...\n")
        collector.stop()
        local_proc.stop()
        if uploader:
            uploader.stop()
        if netmon:
            netmon.stop()
        store.close()
        sys.stderr.write("[PIPELINE DAEMON] Stopped cleanly\n")


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
    p_results.add_argument("--cloud-url", default=None, help="Cloud service URL")
    p_results.add_argument("--token", default=None, help="Cloud auth token")

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

    if args.command == "run":
        cmd_run(
            mock_host=args.mock_host,
            mock_port=args.mock_port,
            db_path=db_path,
            offline_only=args.offline_only,
            cloud_url=args.cloud_url,
            token=args.token
        )
        return

    store = DeviceStore(db_path)
    try:
        if args.command == "status":
            cmd_status(store)
        elif args.command == "export":
            cmd_export(store, args.session, args.from_source, getattr(args, "cloud_url", None), getattr(args, "token", None))
        elif args.command == "results":
            cmd_results(store, args.session, getattr(args, "cloud_url", None), getattr(args, "token", None))
        elif args.command == "verify":
            code = cmd_verify(store, args.session)
            sys.exit(code)
    finally:
        store.close()


if __name__ == "__main__":
    main()
