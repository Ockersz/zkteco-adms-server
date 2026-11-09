# ZKTeco ADMS HTTP Push Server

A lightweight, production-ready server implementation for ZKTeco biometric time and attendance terminals using the **ADMS (Automatic Data Master Server)** HTTP push protocol, with built-in hardware timezone compensation, deduplication, and legacy TCP polling support.

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Protocol](https://img.shields.io/badge/Protocol-ZKTeco%20ADMS%20%2F%20Push%20SDK-orange.svg)]()
[![Database](https://img.shields.io/badge/Storage-SQLite%20%7C%20MySQL-lightgrey.svg)]()

---

## Overview: ADMS Push vs. Legacy Polling

Most developers integrate ZKTeco biometric devices using legacy TCP polling over port 4370. While polling works on a local area network, it falls short when devices are behind firewalls, connected via 4G/cellular modems, or distributed across multiple branch offices.

| Feature | Legacy TCP Polling (Port 4370) | ADMS HTTP Push (This Server) |
| :--- | :--- | :--- |
| **Connection Direction** | Server initiates inbound connection to device | Device initiates outbound HTTP request to server |
| **NAT / Firewall Traversal**| Requires port forwarding or VPN at every site | Works seamlessly behind standard NAT and firewalls |
| **Static IP Requirement** | Device requires a static LAN or public IP | Device needs no static IP; only server needs an IP/Domain |
| **Latency** | Delayed by poll interval (e.g., 5-60 minutes) | Instantaneous; pushes punch logs within 1 second |
| **Scalability** | High overhead for hundreds of distributed devices | Highly scalable; stateless HTTP handling |

```
[ ADMS HTTP Push Architecture ]

+-------------------------+                     +---------------------------+
|   ZKTeco Device         |                     |   ADMS Push Server        |
|   (Client on LAN/4G)    |                     |   (Cloud / On-Premise)    |
+-------------------------+                     +---------------------------+
             |                                                |
             | ---- 1. Handshake (GET /iclock/cdata) -------->|
             |<---- 2. Sync Config (Stamp=0, Delay=10) -------|
             |                                                |
             | ---- 3. Heartbeat (GET /iclock/getrequest) --->|
             |<---- 4. Adjusted HTTP Date Header (Timezone) --|
             |                                                |
   [ User Scans Finger ]                                      |
             |                                                |
             | ---- 5. Punch Log (POST /iclock/cdata) ------->|
             |         (Tab-separated ATTLOG payload)         |--> [ SQLite / MySQL ]
             |                                                |--> [ Webhook Forward ]
             |<---- 6. Ack: "OK: <count>" --------------------|
             |                                                |
```

---

## Critical Engineering Discoveries ("Gotchas")

If you have tried building an ADMS server from scratch, you likely encountered one of these two undocumented pitfalls:

### 1. The Hardcoded Factory UTC+8 Timezone Bug

**The Problem:**
Most ZKTeco terminals run firmware with a hardcoded expectation of UTC+8 (China Standard Time). When connected to an ADMS server, the terminal reads the standard HTTP `Date` header (which RFC 7231 specifies must always be in GMT/UTC) and internally adds +8 hours to display local time. Even if you manually configure the device's clock or timezone in its menu, the next HTTP heartbeat will override the terminal's clock with the shifted time.

**The Solution:**
This server intercepts every HTTP response and calculates a compensatory backwards time shift in the `Date` header:

$$\text{Shift} = \text{Device Factory Offset (UTC+8)} - \text{Your Local Offset}$$

For instance, if your server runs in Sri Lanka or India (UTC+5:30):
$$\text{Shift} = 8.0 - 5.5 = 2.5\text{ hours (2 hours, 30 minutes)}$$

The server shifts the HTTP `Date` header backwards by 2.5 hours. When the terminal adds its hardcoded 8 hours, it lands on your exact local time down to the second.

### 2. The "Infinite Retransmission Loop" (`OK: <count>`)

**The Problem:**
When a device uploads punch logs via `POST /iclock/cdata?table=ATTLOG`, standard HTTP documentation suggests returning `200 OK` or plain text `OK`. However, the ZKTeco push firmware specifically expects the body to match:

```text
OK: <number_of_records_saved>
```

For example, if the terminal posts 4 punches, the server response must be:
```text
OK: 4
```

If the server returns plain `OK` or a JSON response, the terminal assumes the server dropped the payload. It will retry sending the same batch indefinitely on every heartbeat, eventually exhausting its memory buffer and crashing the device network stack.

---

## Physical Device Configuration

Configure your ZKTeco device (F22, IN01, K40, uFace, MB20, etc.) through its hardware menu:

1. Press **Menu / M/OK** and authenticate as Administrator.
2. Navigate to: **Comm.** > **Cloud Server Setting** (or **ADMS** / **Web Server Setting**).
3. Set the following fields:
   - **Server Address:** Your server IP address or public domain (e.g., `192.168.1.50` or `attendance.yourcompany.com`)
   - **Server Port:** `8080` (or your configured `SERVER_PORT`)
   - **Enable Domain Name:** Turn **OFF** if using a direct IP address, or **ON** if using a domain name.
   - **Enable Proxy Server:** **OFF**
   - **HTTPS:** **No** (unless using an SSL reverse proxy)
4. Save and reboot the device.
5. Upon reboot, the terminal displays a globe/cloud icon in the status bar indicating successful connection.

---

## Project Structure

```
zkteco-adms-server/
├── app.py                  # Core ADMS HTTP server and timezone middleware
├── config.py               # Configuration loader and environment parser
├── storage.py              # SQLite and MySQL database storage abstraction
├── poll_device.py          # Standalone legacy TCP 4370 fallback polling client
├── requirements.txt        # Python package dependencies
├── .env.example            # Environment variables template
├── Dockerfile              # Container definition
├── docker-compose.yml      # Docker Compose setup
├── systemd/
│   └── zkteco-adms.service # Linux systemd service unit template
└── nginx/
    └── zkteco-adms.conf    # Production Nginx reverse proxy configuration
```

---

## Quickstart (Zero-Setup SQLite)

### 1. Clone & Install Dependencies

```bash
git clone https://github.com/your-username/zkteco-adms-server.git
cd zkteco-adms-server

# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install requirements
pip install -r requirements.txt
```

### 2. Configure Environment

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Adjust the time offsets in `.env` to match your local timezone:

```ini
SERVER_PORT=8080
DEVICE_UTC_OFFSET_HOURS=8.0
SERVER_UTC_OFFSET_HOURS=5.5
DATE_HEADER_SHIFT_HOURS=2.5
DB_ENGINE=sqlite
```

### 3. Start the Server

```bash
python app.py
```

The server automatically initializes `attendance.db` and starts listening on port 8080. When a device scans a card or fingerprint, the punch log is stored immediately in SQLite.

---

## Database Schema

Both SQLite and MySQL use the following schema:

| Column | Type | Description |
| :--- | :--- | :--- |
| `id` | BIGINT / INTEGER | Auto-incrementing primary key |
| `serial_number` | VARCHAR(50) | Terminal serial number (from HTTP request parameter) |
| `user_id` | VARCHAR(50) | Employee enrollment / badge number |
| `timestamp` | DATETIME | Exact punch date and time (`YYYY-MM-DD HH:MM:SS`) |
| `status` | INT | Punch state: `0` (Check In), `1` (Check Out), `2` (Break Out), `3` (Break In) |
| `verify_type` | INT | Verification mode: `1` (Fingerprint), `2` (Card), `0` (Password), `15` (Face) |
| `work_code` | INT | Optional work code entered on terminal |
| `raw_line` | VARCHAR(255) | Original tab-separated payload string |
| `synced` | CHAR / INT | `0` / `'N'` for pending, `1` / `'Y'` for forwarded to external webhook |
| `created_at` | TIMESTAMP | Server reception timestamp |

---

## Webhook Forwarding (HRMS / ERP Integration)

To automatically stream punches to your ERP (such as SAP, Odoo, or custom payroll software), enable the webhook worker in `.env`:

```ini
WEBHOOK_ENABLED=true
WEBHOOK_URL=https://api.yourcompany.com/v1/attendance-punches
WEBHOOK_BEARER_TOKEN=your_jwt_or_api_key
```

When active, the server pushes JSON batches in real-time:

```json
{
  "timestamp": "2026-11-09T16:30:00Z",
  "records": [
    {
      "id": 1042,
      "serial_number": "CJW820390124",
      "user_id": "10024",
      "timestamp": "2026-11-09 08:31:14",
      "status": 0,
      "verify_type": 1,
      "work_code": 0
    }
  ]
}
```

---

## Legacy TCP Polling Fallback (`poll_device.py`)

If you have legacy devices on your network that do not feature ADMS cloud push, use the included polling script:

```bash
# Single test poll
python poll_device.py --ip 192.168.1.201 --once

# Continuous polling loop every 60 seconds with CSV export
python poll_device.py --ip 192.168.1.201 --interval 60 --csv attendance.csv
```

---

## Production Deployment

### 1. Linux Systemd Service

1. Copy the project to `/opt/zkteco-adms-server`:
   ```bash
   sudo cp -r . /opt/zkteco-adms-server
   sudo chown -R ubuntu:ubuntu /opt/zkteco-adms-server
   ```
2. Copy the service unit:
   ```bash
   sudo cp systemd/zkteco-adms.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now zkteco-adms.service
   ```
3. Check status:
   ```bash
   sudo systemctl status zkteco-adms.service
   journalctl -u zkteco-adms.service -f
   ```

### 2. Nginx Reverse Proxy

To run the server behind Nginx with an SSL certificate:

```bash
sudo cp nginx/zkteco-adms.conf /etc/nginx/sites-available/
sudo ln -s /etc/nginx/sites-available/zkteco-adms.conf /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

> **Note:** The Nginx configuration includes `proxy_pass_header Date;` to ensure the server's calculated timezone shift is not overwritten by Nginx.

---

## Contributing

Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/my-feature`)
3. Commit your changes (`git commit -m 'Add new feature'`)
4. Push to the branch (`git push origin feature/my-feature`)
5. Open a Pull Request

---

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

