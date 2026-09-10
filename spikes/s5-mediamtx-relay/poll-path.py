#!/usr/bin/env python3
"""Poll a MediaMTX API for publisher presence on a path and log a sub-second timeline.

Usage: poll-path.py <api-base> <path-name> <out-csv> <duration-sec> [interval-ms]

Columns: ts_ms, ready(0/1), publish_conn_count, publish_conn_ids, bytes_received
A change in publish_conn_ids means the RTMP session was torn down and remade — on the real
YouTube leg that is a new ingest session, which is exactly what must be avoided.
"""
import json
import sys
import time
import urllib.error
import urllib.request


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def main():
    api, path, out, duration = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
    interval = (float(sys.argv[5]) if len(sys.argv) > 5 else 100.0) / 1000.0
    end = time.time() + duration
    with open(out, "w", buffering=1) as fh:
        fh.write("ts_ms,ready,pub_conns,conn_ids,bytes_received\n")
        while time.time() < end:
            ts = int(time.time() * 1000)
            pinfo = get(f"{api}/v3/paths/get/{path}")
            ready = 1 if (pinfo and pinfo.get("ready")) else 0
            brecv = pinfo.get("bytesReceived", 0) if pinfo else 0
            conns = get(f"{api}/v3/rtmpconns/list") or {}
            ids = sorted(
                c["id"] for c in conns.get("items", []) if c.get("state") == "publish"
            )
            fh.write(f"{ts},{ready},{len(ids)},{'|'.join(ids)},{brecv}\n")
            time.sleep(interval)


if __name__ == "__main__":
    main()
