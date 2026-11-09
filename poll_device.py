"""
Legacy TCP Polling Client for ZKTeco Terminals (Port 4370)
Used as an alternative or fallback for older devices that do not feature ADMS HTTP push.

Usage:
    python poll_device.py --ip 192.168.1.201
    python poll_device.py --ip 192.168.1.201 --once
    python poll_device.py --ip 192.168.1.201 --interval 60 --csv punches.csv
"""

import argparse
import csv
import logging
import os
import socket
import sys
import time
from datetime import datetime
from typing import Set

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("zkteco.poll")

# Mapping punch status codes to human-readable labels
STATUS_LABELS = {
    0: "Check In",
    1: "Check Out",
    2: "Break Out",
    3: "Break In",
    4: "Overtime In",
    5: "Overtime Out",
}

VERIFY_LABELS = {
    0: "Password",
    1: "Fingerprint",
    2: "Card",
    15: "Face",
}


def is_device_reachable(ip: str, port: int = 4370, timeout: float = 3.0) -> bool:
    """Check TCP reachability without needing root ICMP permissions."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def load_existing_keys(csv_path: str) -> Set[str]:
    """Read CSV to determine existing record keys (user_id|timestamp|status)."""
    keys = set()
    if not os.path.exists(csv_path):
        return keys

    try:
        with open(csv_path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = f"{row.get('user_id')}|{row.get('timestamp')}|{row.get('status')}"
                keys.add(key)
    except Exception as ex:
        logger.warning("Could not read existing CSV for deduplication: %s", ex)
    return keys


def poll_cycle(ip: str, port: int, password: int, csv_path: str, timeout: int) -> int:
    """Connect to the terminal, read attendance records, and append new ones."""
    try:
        from zk import ZK
    except ImportError:
        logger.error("pyzk library is required for direct TCP polling. Run: pip install pyzk")
        sys.exit(1)

    if not is_device_reachable(ip, port, timeout=3.0):
        logger.warning("Device at %s:%d is unreachable via TCP. Skipping cycle.", ip, port)
        return 0

    zk = ZK(ip, port=port, timeout=timeout, password=password, force_udp=False)
    conn = None
    new_records_count = 0

    try:
        logger.info("Connecting to ZKTeco terminal at %s:%d...", ip, port)
        conn = zk.connect()
        conn.disable_device()

        records = conn.get_attendance()
        logger.info("Downloaded %d raw attendance records from device.", len(records))

        existing_keys = load_existing_keys(csv_path)
        file_exists = os.path.exists(csv_path)

        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            fieldnames = [
                "user_id",
                "timestamp",
                "status",
                "status_label",
                "verify_type",
                "verify_label",
                "work_code",
            ]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()

            for rec in records:
                ts_str = rec.timestamp.strftime("%Y-%m-%d %H:%M:%S")
                rec_key = f"{rec.user_id}|{ts_str}|{rec.status}"

                if rec_key in existing_keys:
                    continue

                writer.writerow({
                    "user_id": rec.user_id,
                    "timestamp": ts_str,
                    "status": rec.status,
                    "status_label": STATUS_LABELS.get(rec.status, f"Status_{rec.status}"),
                    "verify_type": rec.punch,
                    "verify_label": VERIFY_LABELS.get(rec.punch, f"Type_{rec.punch}"),
                    "work_code": getattr(rec, "work_code", 0),
                })
                existing_keys.add(rec_key)
                new_records_count += 1

        logger.info("Cycle complete: %d new records saved to %s", new_records_count, csv_path)
        conn.enable_device()
    except Exception as ex:
        logger.error("Polling error: %s", ex)
    finally:
        if conn:
            try:
                conn.disconnect()
            except Exception:
                pass

    return new_records_count


def main():
    parser = argparse.ArgumentParser(description="ZKTeco TCP 4370 Attendance Polling Client")
    parser.add_argument("--ip", default=os.getenv("DEVICE_IP", "192.168.1.201"), help="Terminal IP address")
    parser.add_argument("--port", type=int, default=int(os.getenv("DEVICE_PORT", "4370")), help="Terminal port (default 4370)")
    parser.add_argument("--password", type=int, default=int(os.getenv("DEVICE_PASSWORD", "0")), help="Comm key (default 0)")
    parser.add_argument("--csv", default=os.getenv("CSV_PATH", "attendance.csv"), help="CSV export destination")
    parser.add_argument("--interval", type=int, default=int(os.getenv("POLL_INTERVAL", "30")), help="Polling loop interval in seconds")
    parser.add_argument("--timeout", type=int, default=15, help="Socket timeout in seconds")
    parser.add_argument("--once", action="store_true", help="Run a single poll cycle and exit")

    args = parser.parse_args()

    if args.once:
        poll_cycle(args.ip, args.port, args.password, args.csv, args.timeout)
        return

    logger.info("Starting polling loop for %s:%d every %d seconds. Press Ctrl+C to stop.", args.ip, args.port, args.interval)
    while True:
        try:
            poll_cycle(args.ip, args.port, args.password, args.csv, args.timeout)
            time.sleep(args.interval)
        except KeyboardInterrupt:
            logger.info("Polling loop stopped by user.")
            break


if __name__ == "__main__":
    main()

