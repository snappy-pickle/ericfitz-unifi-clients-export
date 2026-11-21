# /// script
# requires-python = ">=3.8"
# dependencies = [
#   "requests",
#   "pandas",
#   "python-dotenv",
# ]
# ///

#!/usr/bin/env python3
"""
UniFi Network Client Export Script (API Key Authentication)

Exports currently connected clients and all UniFi devices from a UniFi Network
controller to a CSV file using a read-only API key. With --include-offline,
clients from the controller's history that are not connected are included too.

Compatible with UniFi Network Application v9.5.21+.

Configuration:
  Copy example.env to .env and configure your UniFi controller settings.

Run with: `uv run uce.py` (add `--include-offline` for offline clients,
`-o -` to write the client CSV to stdout; see `--help` for all options)

Automatically installs dependencies via PEP 723 metadata when using `uv`.

Author: Assistant
Date: November 15, 2025
"""

import argparse
import json
import os
import sys
import requests
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

DEFAULT_OUTPUT = "unifi_clients.csv"


def log(*args, **kwargs):
    """Print progress and errors to stderr, so stdout can carry CSV data."""
    print(*args, file=sys.stderr, **kwargs)


def add_timestamp(path, stamp):
    """Insert a timestamp before a file's extension: a.csv -> a_<stamp>.csv"""
    root, ext = os.path.splitext(path)
    return f"{root}_{stamp}{ext}"


# Columns of the client CSV, in order. With --all-fields, every other field
# the API returns is added after these.
STANDARD_COLUMNS = [
    "Type",
    "Name",
    "MAC Address",
    "IP Address",
    "Model",
    "Connection Type",
    "Switch",
    "Port",
    "Last Seen",
    "Status",
]

# Secret API fields never written by --all-fields. The controller returns
# device adoption and management credentials even to read-only API keys;
# by convention their names start with "x_".
SECRET_FIELDS = {"syslog_key", "guest_token"}


def is_secret_field(key):
    return key.startswith("x_") or key in SECRET_FIELDS


def strip_secrets(value):
    """Recursively remove secret fields from API data."""
    if isinstance(value, dict):
        return {
            k: strip_secrets(v) for k, v in value.items() if not is_secret_field(k)
        }
    if isinstance(value, list):
        return [strip_secrets(v) for v in value]
    return value


def add_api_fields(record, api_obj):
    """Add every non-secret API field to a CSV record, without replacing
    the standard columns. Nested values are written as JSON."""
    for key, value in strip_secrets(api_obj).items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value, sort_keys=True)
        record.setdefault(key, value)


def get_config():
    """Load and validate configuration from environment variables."""
    # Required configuration
    controller_url = os.getenv("CONTROLLER_URL")
    api_key = os.getenv("API_KEY")

    # Check required values
    if not controller_url:
        log("ERROR: CONTROLLER_URL is not set.")
        log("   → Copy example.env to .env and configure your settings")
        sys.exit(1)

    if not api_key or api_key == "your-api-key-here":
        log("ERROR: API_KEY is not set or using placeholder value.")
        log("   → Get your API key from: Settings > Control Plane > Applications")
        log("   → Update the API_KEY value in your .env file")
        sys.exit(1)

    # Optional configuration with defaults
    site_id = os.getenv("SITE_ID", "default")

    # Parse VERIFY_SSL boolean
    verify_ssl_str = os.getenv("VERIFY_SSL", "true").lower()
    verify_ssl = verify_ssl_str not in ("false", "0", "no")

    return controller_url, api_key, site_id, verify_ssl


def parse_args():
    """Parse command-line options."""
    parser = argparse.ArgumentParser(
        description="Export UniFi clients and devices to CSV files."
    )
    parser.add_argument(
        "--include-offline",
        action="store_true",
        help="also export clients that are not currently connected, "
        "from the controller's client history",
    )
    parser.add_argument(
        "--history-hours",
        type=int,
        default=8760,
        metavar="HOURS",
        help="with --include-offline, how far back to include offline "
        "clients (default: 8760 = 1 year)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=DEFAULT_OUTPUT,
        metavar="FILE",
        help=f"where to write the client CSV; use '-' for stdout "
        f"(default: {DEFAULT_OUTPUT}). Switch port CSVs are written to "
        "the same directory, or the current directory with '-'",
    )
    parser.add_argument(
        "--timestamp",
        action="store_true",
        help="add the run's date and time to output file names, "
        "e.g. unifi_clients_2025-11-20_154512.csv",
    )
    parser.add_argument(
        "--all-fields",
        action="store_true",
        help="add every field the API returns for each client and device "
        "after the standard columns (nested values as JSON; secret fields "
        "such as device credentials are always left out)",
    )
    args = parser.parse_args()
    if args.history_hours <= 0:
        parser.error("--history-hours must be a positive integer")
    return args


def main() -> None:
    args = parse_args()
    run_stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    to_stdout = args.output == "-"
    output_path = args.output
    if not to_stdout and args.timestamp:
        output_path = add_timestamp(output_path, run_stamp)
    output_dir = "" if to_stdout else os.path.dirname(output_path)

    # Load configuration
    CONTROLLER_URL, API_KEY, SITE_ID, VERIFY_SSL = get_config()
    session = requests.Session()
    session.headers.update(
        {
            "X-API-KEY": API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    )

    # Test sites endpoint
    sites_url = f"{CONTROLLER_URL}/proxy/network/api/self/sites"
    log(f"Testing: {sites_url}")
    try:
        resp = session.get(sites_url, verify=VERIFY_SSL, timeout=15)
        log(f"HTTP {resp.status_code}")
        if resp.status_code == 401:
            log("   → 401 Unauthorized: Invalid API key.")
            return
        if resp.status_code == 404:
            log("   → 404: Trying fallback endpoint...")
            sites_url = f"{CONTROLLER_URL}/api/self/sites"
            resp = session.get(sites_url, verify=VERIFY_SSL, timeout=15)
            log(f"Fallback HTTP {resp.status_code}")
        if resp.status_code != 200:
            log(f"   → Failed: {resp.status_code}. Raw: {resp.text[:500]}")
            return
        sites = resp.json().get("data", [])
        log(f"Found {len(sites)} site(s): {[s.get('desc') for s in sites]}")
    except Exception as e:
        log(f"Connection error: {e}")
        return

    # Fetch devices (switches, APs, etc.) for name mapping
    devices_url = f"{CONTROLLER_URL}/proxy/network/api/s/{SITE_ID}/stat/device"
    log(f"\nFetching devices: {devices_url}")
    devices_map = {}
    devices_data = []
    try:
        resp = session.get(devices_url, verify=VERIFY_SSL, timeout=15)
        if resp.status_code == 404:
            log("   → 404: Trying fallback...")
            devices_url = f"{CONTROLLER_URL}/api/s/{SITE_ID}/stat/device"
            resp = session.get(devices_url, verify=VERIFY_SSL, timeout=15)
        resp.raise_for_status()
        devices_data = resp.json().get("data", [])
        # Build a map of MAC address to device name
        for device in devices_data:
            mac = device.get("mac", "").upper()
            name = device.get("name") or device.get("hostname") or mac
            devices_map[mac] = name
        log(f"Found {len(devices_map)} device(s)")
    except Exception as e:
        log(f"Warning: Could not fetch devices: {e}")
        log("   → Switch names will show as MAC addresses")

    # Fetch clients
    clients_url = f"{CONTROLLER_URL}/proxy/network/api/s/{SITE_ID}/stat/sta"
    log(f"\nFetching clients: {clients_url}")
    try:
        resp = session.get(clients_url, verify=VERIFY_SSL, timeout=15)
        if resp.status_code == 404:
            log("   → 404: Trying fallback...")
            clients_url = f"{CONTROLLER_URL}/api/s/{SITE_ID}/stat/sta"
            resp = session.get(clients_url, verify=VERIFY_SSL, timeout=15)
        resp.raise_for_status()
        clients_data = resp.json().get("data", [])
    except requests.exceptions.HTTPError as e:
        log(f"HTTP error: {e}")
        log(f"Response: {e.response.text[:500]}")
        return
    except Exception as e:
        log(f"Failed: {e}")
        return
    log(f"Found {len(clients_data)} connected client(s)")

    # stat/sta only returns connected clients; the client history also
    # includes clients that are offline.
    history_data = []
    if args.include_offline:
        history_paths = [
            (
                "stat/alluser",
                {"type": "all", "conn": "all", "within": args.history_hours},
            ),
            ("rest/user", None),
        ]
        for path, params in history_paths:
            history_url = f"{CONTROLLER_URL}/proxy/network/api/s/{SITE_ID}/{path}"
            log(f"\nFetching client history: {history_url}")
            try:
                resp = session.get(
                    history_url, params=params, verify=VERIFY_SSL, timeout=30
                )
                if resp.status_code == 404:
                    log("   → 404: Trying fallback...")
                    history_url = f"{CONTROLLER_URL}/api/s/{SITE_ID}/{path}"
                    resp = session.get(
                        history_url, params=params, verify=VERIFY_SSL, timeout=30
                    )
                resp.raise_for_status()
                history_data = resp.json().get("data", [])
                log(f"Found {len(history_data)} known client(s)")
                break
            except Exception as e:
                log(f"Warning: Could not fetch client history: {e}")
        if not history_data:
            log("   → Only currently connected clients will be exported")

    # Merge: connected clients (with live details) take precedence over
    # history entries for the same MAC address
    seen_macs = set()
    merged_clients = []
    for c in clients_data:
        mac = c.get("mac", "").upper()
        if mac:
            seen_macs.add(mac)
        merged_clients.append((c, True))
    for c in history_data:
        mac = c.get("mac", "").upper()
        if not mac or mac in seen_macs:
            continue
        seen_macs.add(mac)  # also de-duplicates history entries
        merged_clients.append((c, False))

    # Process client data
    records = []
    for c, online in merged_clients:
        if online:
            conn = "Wireless" if c.get("ap_mac") else "Wired"
        else:
            # Offline clients have no ap_mac; use the last known link type
            conn = "Wired" if c.get("is_wired") else "Wireless"
        last = (
            datetime.fromtimestamp(c.get("last_seen", 0))
            if c.get("last_seen")
            else None
        )

        # Extract switch and port info for connected wired clients; the
        # controller does not track these for offline clients
        switch_name = ""
        switch_port = ""
        if online and conn == "Wired":
            sw_mac = c.get("sw_mac", "").upper()
            if sw_mac:
                switch_name = devices_map.get(sw_mac, sw_mac)
            sw_port = c.get("sw_port")
            if sw_port is not None:
                switch_port = str(sw_port)

        records.append(
            {
                "Type": "Client",
                "Name": c.get("name") or c.get("hostname") or "Unknown",
                "MAC Address": c.get("mac", "").upper(),
                "IP Address": c.get("ip")
                or c.get("last_ip")
                or c.get("fixed_ip")
                or "",
                "Model": "",
                "Connection Type": conn,
                "Switch": switch_name,
                "Port": switch_port,
                "Last Seen": last.strftime("%Y-%m-%d %H:%M:%S") if last else "",
                "Status": "Online" if online else "Offline",
            }
        )
        if args.all_fields:
            add_api_fields(records[-1], c)

    # Process UniFi device data
    device_type_map = {
        "usw": "Switch",
        "uap": "Access Point",
        "ugw": "Gateway",
        "udm": "Dream Machine",
        "uxg": "Gateway",
        "ubb": "Building Bridge",
        "ulte": "LTE",
        "pdu": "PDU",
    }

    for device in devices_data:
        device_type = device.get("type", "")
        friendly_type = device_type_map.get(device_type, device_type.upper())

        # Determine connection type and switch info
        conn = "Wired"
        switch_name = ""
        switch_port = ""

        # Check if device is connected via uplink
        if device.get("uplink"):
            uplink = device["uplink"]
            uplink_mac = uplink.get("uplink_mac", "").upper()
            if uplink_mac:
                switch_name = devices_map.get(uplink_mac, uplink_mac)
            uplink_port = uplink.get("uplink_remote_port")
            if uplink_port is not None:
                switch_port = str(uplink_port)

        # Get last seen time
        last_seen_time = device.get("last_seen")
        last = datetime.fromtimestamp(last_seen_time) if last_seen_time else None

        # Determine status
        state = device.get("state", 0)
        status = "Online" if state == 1 else "Offline"

        records.append(
            {
                "Type": f"Device - {friendly_type}",
                "Name": device.get("name") or device.get("hostname") or "Unknown",
                "MAC Address": device.get("mac", "").upper(),
                "IP Address": device.get("ip", ""),
                "Model": device.get("model", ""),
                "Connection Type": conn,
                "Switch": switch_name,
                "Port": switch_port,
                "Last Seen": last.strftime("%Y-%m-%d %H:%M:%S") if last else "",
                "Status": status,
            }
        )
        if args.all_fields:
            add_api_fields(records[-1], device)

    if not records:
        log("No clients or devices found.")
        return

    if args.all_fields:
        # object dtype keeps integers as integers when some rows lack a field
        df = pd.DataFrame(records, dtype=object)
        extra_columns = sorted(c for c in df.columns if c not in STANDARD_COLUMNS)
        df = df[STANDARD_COLUMNS + extra_columns]
    else:
        df = pd.DataFrame(records)
    if to_stdout:
        df.to_csv(sys.stdout, index=False)
        sys.stdout.flush()
    else:
        df.to_csv(output_path, index=False)

    # Count clients and devices
    client_count = sum(1 for r in records if r["Type"] == "Client")
    device_count = len(records) - client_count
    destination = "stdout" if to_stdout else output_path
    log(f"\nSuccess: {len(records)} total entries → {destination}")
    log(f"   → {client_count} clients")
    log(f"   → {device_count} UniFi devices")

    # Generate per-switch port information CSV files
    log("\nGenerating switch port CSV files...")
    switches_processed = 0
    for device in devices_data:
        # Only process switches (usw = UniFi Switch)
        if device.get("type") != "usw":
            continue

        device_name = device.get("name") or device.get("hostname") or device.get("mac")
        port_table = device.get("port_table", [])

        if not port_table:
            continue

        # Build port records
        port_records = []
        for port in port_table:
            # Find connected device/client info
            connected_name = ""
            connected_mac = ""
            connected_type = ""
            connected_model = ""

            if port.get("up") and "mac_table_count" in port and port["mac_table_count"] > 0:
                # First check for clients connected to this port
                for client in clients_data:
                    if (client.get("sw_mac", "").upper() == device.get("mac", "").upper() and
                        client.get("sw_port") == port.get("port_idx")):
                        connected_name = client.get("name") or client.get("hostname") or ""
                        connected_mac = client.get("mac", "").upper()
                        connected_type = "Client"
                        break

                # If no client found, check for UniFi devices connected via uplink
                if not connected_name:
                    for dev in devices_data:
                        if dev.get("uplink"):
                            uplink = dev["uplink"]
                            uplink_mac = uplink.get("uplink_mac", "").upper()
                            uplink_port = uplink.get("uplink_remote_port")
                            if (uplink_mac == device.get("mac", "").upper() and
                                uplink_port == port.get("port_idx")):
                                connected_name = dev.get("name") or dev.get("hostname") or ""
                                connected_mac = dev.get("mac", "").upper()
                                connected_model = dev.get("model", "")
                                dev_type = dev.get("type", "")
                                friendly_type = device_type_map.get(dev_type, dev_type.upper())
                                connected_type = f"Device - {friendly_type}"
                                break

            port_records.append({
                "Port": port.get("name") or f"Port {port.get('port_idx', '')}",
                "Port Index": port.get("port_idx", ""),
                "Status": "Up" if port.get("up") else "Down",
                "Speed": f"{port.get('speed', '')} Mbps" if port.get("speed") else "",
                "Full Duplex": "Yes" if port.get("full_duplex") else "No",
                "PoE Enabled": "Yes" if port.get("poe_enable") else "No",
                "PoE Power (W)": port.get("poe_power", ""),
                "PoE Class": port.get("poe_class", ""),
                "Connected Type": connected_type,
                "Connected Name": connected_name,
                "Connected MAC": connected_mac,
                "Connected Model": connected_model,
                "RX Bytes": port.get("rx_bytes", ""),
                "TX Bytes": port.get("tx_bytes", ""),
                "RX Packets": port.get("rx_packets", ""),
                "TX Packets": port.get("tx_packets", ""),
                "RX Errors": port.get("rx_errors", ""),
                "TX Errors": port.get("tx_errors", ""),
            })

        if port_records:
            # Sanitize filename
            safe_name = "".join(c if c.isalnum() or c in (" ", "-", "_") else "_" for c in device_name)
            filename = os.path.join(output_dir, f"switch_{safe_name}.csv")
            if args.timestamp:
                filename = add_timestamp(filename, run_stamp)
            port_df = pd.DataFrame(port_records)
            port_df.to_csv(filename, index=False)
            switches_processed += 1
            log(f"   → {filename} ({len(port_records)} ports)")

    log(f"\nGenerated {switches_processed} switch port CSV files")


if __name__ == "__main__":
    main()
