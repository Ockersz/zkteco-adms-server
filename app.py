"""
ZKTeco ADMS (HTTP Push) Server
A production-ready, open-source receiver for ZKTeco biometric time & attendance terminals.

Features:
- Automatic hardware timezone compensation via HTTP Date header shifting
- Zero-configuration SQLite default (or MySQL support)
- Correct ADMS 'OK: <count>' acknowledgment protocol to prevent infinite retry loops
- Real-time punch ingestion and deduplication
- Optional asynchronous webhook forwarding to external HRMS/ERP APIs
- Device heartbeat monitoring and command dispatch channel
"""

import logging
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import formatdate
from typing import List, Dict

import requests
from flask import Flask, Response, request, jsonify

import config
import storage

# ------------------------------------------------------------------------------
# Logging Setup
# ------------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# Silence verbose werkzeug access logging for high-frequency heartbeats
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logger = logging.getLogger("zkteco.adms")

# ------------------------------------------------------------------------------
# Application State
# ------------------------------------------------------------------------------
app = Flask(__name__)

# Map client IP to serial number for requests lacking the SN parameter
_ip_to_sn: Dict[str, str] = {}
_known_devices: Dict[str, dict] = {}
_pending_commands: Dict[str, List[str]] = defaultdict(list)

# Webhook worker synchronization
_webhook_wake = threading.Event()
_webhook_stop = threading.Event()


# ------------------------------------------------------------------------------
# Timezone Compensation Middleware
# ------------------------------------------------------------------------------
@app.after_request
def fix_date_header(response: Response) -> Response:
    """
    ZKTeco terminals calculate internal time by parsing the standard HTTP 'Date'
    header and adding their hardcoded factory timezone offset (often UTC+8).

    To ensure the terminal displays your actual local time, we shift the Date
    header backwards:
        Adjusted_Date = Current_UTC - (Device_Offset - Server_Offset)
    """
    sn = request.args.get("SN")
    if sn:
        _ip_to_sn[request.remote_addr] = sn
    else:
        sn = _ip_to_sn.get(request.remote_addr, "unknown")

    # Determine shift for this specific terminal (fallback to global default)
    shift_hours = config.DATE_HEADER_SHIFT_SN.get(sn, config.DATE_HEADER_SHIFT_HOURS)

    adjusted_utc = datetime.now(timezone.utc) - timedelta(hours=shift_hours)
    response.headers["Date"] = formatdate(adjusted_utc.timestamp(), usegmt=True)
    return response


# ------------------------------------------------------------------------------
# Punch Parsing Logic
# ------------------------------------------------------------------------------
def parse_attlog(raw_body: str, sn: str) -> List[Dict]:
    """
    Parse the tab-separated ATTLOG payload sent by the ZKTeco device.
    Line structure: UserID \t Timestamp \t VerifyType \t Status \t WorkCode \t Reserved
    """
    records = []
    current_year = datetime.now().year

    for line in raw_body.splitlines():
        line = line.strip()
        if not line or line.upper().startswith("ATTLOG"):
            continue

        parts = line.split("\t")
        if len(parts) < 2:
            continue

        user_id = parts[0].strip()
        timestamp_str = parts[1].strip()

        # Validate date parsing
        try:
            dt = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            logger.warning("[%s] Skipped unparseable timestamp: %r", sn, timestamp_str)
            continue

        # Sanity check: discard anomalous timestamps (e.g. year < 2000 or far in future)
        if dt.year < 2000 or dt.year > (current_year + 1):
            logger.warning("[%s] Skipped anomalous timestamp: %s for user %s", sn, timestamp_str, user_id)
            continue

        # Extract remaining fields if available
        verify_type = int(parts[2].strip()) if len(parts) > 2 and parts[2].strip().isdigit() else 1
        status = int(parts[3].strip()) if len(parts) > 3 and parts[3].strip().isdigit() else 0
        work_code = int(parts[4].strip()) if len(parts) > 4 and parts[4].strip().isdigit() else 0

        records.append({
            "user_id": user_id,
            "timestamp": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "verify_type": verify_type,
            "status": status,
            "work_code": work_code,
            "raw_line": line,
        })

    return records


# ------------------------------------------------------------------------------
# ADMS Protocol Endpoints
# ------------------------------------------------------------------------------
@app.route("/iclock/cdata", methods=["GET"])
def device_handshake():
    """
    Handshake & Registration:
    The device contacts this endpoint on boot to establish the connection and
    retrieve sync configurations.
    """
    sn = request.args.get("SN", "unknown")
    logger.info("[%s] Device handshake received from %s", sn, request.remote_addr)

    _known_devices[sn] = {
        "ip": request.remote_addr,
        "last_seen": datetime.now().isoformat(),
        "status": "online",
    }

    # Configuration directives:
    # - Stamp=0: instructs device to upload all local records from beginning
    # - Delay=10: heartbeat interval in seconds
    # - TransFlag: enable real-time attendance log push
    config_payload = (
        "OK\n"
        "ATTLOG,Stamp=0\n"
        "OPERLOG,Stamp=9999999999\n"
        "ATTPHOTO,Stamp=9999999999\n"
        "ErrorDelay=30\n"
        "Delay=10\n"
        "TransTimes=00:00;23:59\n"
        "TransInterval=1\n"
        "TransFlag=TransData AttLog\n"
        "Realtime=1\n"
        "Encrypt=None\n"
    )
    return Response(config_payload, mimetype="text/plain")


@app.route("/iclock/getrequest", methods=["GET"])
def device_heartbeat():
    """
    Device Heartbeat & Command Polling:
    Called periodically by the terminal. If commands are queued, return them.
    Otherwise respond with 'OK'.
    """
    sn = request.args.get("SN", "unknown")
    _known_devices[sn] = {
        "ip": request.remote_addr,
        "last_seen": datetime.now().isoformat(),
        "status": "online",
    }

    # Check for queued commands (e.g. reboot, time sync, options update)
    if _pending_commands[sn]:
        cmd = _pending_commands[sn].pop(0)
        logger.info("[%s] Dispatched queued command: %s", sn, cmd)
        return Response(cmd + "\n", mimetype="text/plain")

    return Response("OK", mimetype="text/plain")


@app.route("/iclock/cdata", methods=["POST"])
def receive_attendance_data():
    """
    Attendance Record Receiver:
    Device POSTs attendance logs in tab-separated format here.

    CRITICAL:
    Must respond with 'OK: <count>' indicating the exact number of records
    processed. Responding with just 'OK' causes the device firmware to assume
    the transmission failed, leading to an infinite retransmission loop.
    """
    sn = request.args.get("SN", "unknown")
    table = request.args.get("table", "").upper()
    stamp = request.args.get("Stamp", "0")

    if table != "ATTLOG":
        # Ignore other tables (OPERLOG, BIODATA, etc.) with a generic OK
        return Response("OK", mimetype="text/plain")

    raw_body = request.data.decode("utf-8", errors="replace")
    records = parse_attlog(raw_body, sn)

    if records:
        inserted, duplicates = storage.save_punches(records, sn)
        logger.info(
            "[%s] ATTLOG Stamp=%s | Total=%d | Saved=%d | Duplicates=%d",
            sn, stamp, len(records), inserted, duplicates
        )

        # Notify webhook forwarder if enabled
        if config.WEBHOOK_ENABLED and inserted > 0:
            _webhook_wake.set()
    else:
        logger.info("[%s] ATTLOG Stamp=%s | No records parsed from payload", sn, stamp)

    # Return the mandatory record count acknowledgement
    return Response(f"OK: {len(records)}", mimetype="text/plain")


@app.route("/iclock/deviceinfo", methods=["POST"])
def receive_device_info():
    """Receive terminal hardware specs, firmware version, and capacity."""
    sn = request.args.get("SN", "unknown")
    info = request.data.decode("utf-8", errors="replace")
    logger.info("[%s] Device specs: %s", sn, info[:200])
    return Response("OK", mimetype="text/plain")


@app.route("/iclock/devicecmd", methods=["POST"])
def receive_device_cmd_response():
    """Device sends execution status of previously dispatched commands."""
    sn = request.args.get("SN", "unknown")
    cmd_response = request.data.decode("utf-8", errors="replace")
    logger.info("[%s] Command Ack: %s", sn, cmd_response.strip())
    return Response("OK", mimetype="text/plain")


# ------------------------------------------------------------------------------
# Management & Health Endpoints
# ------------------------------------------------------------------------------
@app.route("/health", methods=["GET"])
@app.route("/", methods=["GET"])
def server_status():
    """Public status endpoint for monitoring and uptime checks."""
    return jsonify({
        "status": "healthy",
        "service": "ZKTeco ADMS Server",
        "database_engine": config.DB_ENGINE,
        "configured_shift_hours": config.DATE_HEADER_SHIFT_HOURS,
        "active_devices": _known_devices,
        "webhook_forwarding": config.WEBHOOK_ENABLED,
    })


# ------------------------------------------------------------------------------
# Optional Background Webhook Forwarder
# ------------------------------------------------------------------------------
def webhook_worker():
    """Background worker that pushes unsynced attendance punches to an external API."""
    logger.info("Webhook worker started: forwarding to %s", config.WEBHOOK_URL)
    while not _webhook_stop.is_set():
        # Wait for wake event or wake up every 30 seconds for retry
        _webhook_wake.wait(timeout=30.0)
        if _webhook_stop.is_set():
            break
        _webhook_wake.clear()

        try:
            unsynced = storage.get_unsynced_punches(limit=config.WEBHOOK_BATCH_SIZE)
            if not unsynced:
                continue

            headers = {"Content-Type": "application/json"}
            if config.WEBHOOK_BEARER_TOKEN:
                headers["Authorization"] = f"Bearer {config.WEBHOOK_BEARER_TOKEN}"

            payload = {
                "timestamp": datetime.now().isoformat(),
                "records": unsynced,
            }

            resp = requests.post(
                config.WEBHOOK_URL,
                json=payload,
                headers=headers,
                timeout=config.WEBHOOK_TIMEOUT,
            )

            if resp.status_code in (200, 201, 204):
                synced_ids = [r["id"] for r in unsynced]
                storage.mark_punches_synced(synced_ids)
                logger.info("Webhook forwarded %d punches successfully", len(synced_ids))
            else:
                logger.warning("Webhook endpoint returned status %d: %s", resp.status_code, resp.text[:200])
        except Exception as ex:
            logger.error("Webhook forwarding error: %s", ex)


# ------------------------------------------------------------------------------
# Server Entrypoint
# ------------------------------------------------------------------------------
def start_server():
    """Initialize database and start the ADMS receiver."""
    storage.init_db()

    if config.WEBHOOK_ENABLED:
        worker_thread = threading.Thread(target=webhook_worker, daemon=True)
        worker_thread.start()

    logger.info("==================================================================")
    logger.info("ZKTeco ADMS Push Server running on %s:%d", config.SERVER_HOST, config.SERVER_PORT)
    logger.info("HTTP Date Header Shift: %.2f hours", config.DATE_HEADER_SHIFT_HOURS)
    logger.info("Database: %s", config.DB_ENGINE)
    logger.info("Configure your device: Menu > Comm. > Cloud Server (ADMS)")
    logger.info("Server Address: <This Machine's IP>, Port: %d", config.SERVER_PORT)
    logger.info("==================================================================")

    app.run(host=config.SERVER_HOST, port=config.SERVER_PORT, debug=False)


if __name__ == "__main__":
    start_server()

