#!/usr/bin/env python3
"""
Eco-Worthy (JBD BMS) Bluetooth -> CSV logger for Raspberry Pi / OpenPlotter.

Setup:
    sudo apt install python3-venv bluez
    python3 -m venv ~/ecoenv && ~/ecoenv/bin/pip install bleak

Find your battery:
    ~/ecoenv/bin/python ecoworthy_logger.py --scan

Run (one or more batteries):
    ~/ecoenv/bin/python ecoworthy_logger.py \
        --battery house1=AA:BB:CC:DD:EE:01 \
        --battery house2=AA:BB:CC:DD:EE:02 \
        --interval 30 --csv ~/battery_log.csv
"""
import argparse
import asyncio
import csv
import datetime as dt
import json
import os
import socket
import struct

from bleak import BleakClient, BleakScanner

CHAR_NOTIFY = "0000ff01-0000-1000-8000-00805f9b34fb"  # BMS -> Pi
CHAR_WRITE = "0000ff02-0000-1000-8000-00805f9b34fb"   # Pi -> BMS

CMD_BASIC = bytes.fromhex("DDA50300FFFD77")  # pack voltage, current, SOC, temps
CMD_CELLS = bytes.fromhex("DDA50400FFFC77")  # individual cell voltages

MAX_CELLS = 4   # LP4 12V batteries are 4S; raise for 24V/48V packs
MAX_TEMPS = 3


def frame_complete(buf: bytearray) -> bool:
    return len(buf) >= 4 and buf[0] == 0xDD and len(buf) >= buf[3] + 7


def checksum_ok(frame: bytes) -> bool:
    length = frame[3]
    payload = frame[2:4 + length]
    expected = (0x10000 - sum(payload)) & 0xFFFF
    got = struct.unpack(">H", frame[4 + length:6 + length])[0]
    return expected == got


class Link:
    """One notification subscription per connection; reused for every command."""

    def __init__(self, client: BleakClient):
        self.client = client
        self.buf = bytearray()
        self.done = asyncio.Event()

    def _on_notify(self, _, data: bytearray):
        if not self.buf and data[:1] != b"\xdd":
            return  # ignore stray bytes that are not the start of a frame
        self.buf.extend(data)
        if frame_complete(self.buf):
            self.done.set()

    async def start(self):
        await self.client.start_notify(CHAR_NOTIFY, self._on_notify)

    async def stop(self):
        try:
            await self.client.stop_notify(CHAR_NOTIFY)
        except Exception:
            pass

    async def request(self, cmd: bytes, timeout: float = 5.0) -> bytes:
        self.buf.clear()
        self.done.clear()
        await self.client.write_gatt_char(CHAR_WRITE, cmd, response=False)
        try:
            await asyncio.wait_for(self.done.wait(), timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"no reply from BMS within {timeout}s "
                f"(got {len(self.buf)} bytes: {bytes(self.buf).hex()})"
            )
        frame = bytes(self.buf[: self.buf[3] + 7])
        if not checksum_ok(frame):
            raise ValueError("bad checksum")
        if frame[2] != 0x00:
            raise ValueError(f"BMS returned error status {frame[2]:#x}")
        return frame[4:4 + frame[3]]


def parse_basic(d: bytes) -> dict:
    voltage, current, remain, nominal, cycles = struct.unpack(">HhHHH", d[0:10])
    protection = struct.unpack(">H", d[16:18])[0]
    soc = d[19]
    fet = d[20]
    n_ntc = d[22]
    temps = []
    for i in range(n_ntc):
        raw = struct.unpack(">H", d[23 + 2 * i:25 + 2 * i])[0]
        temps.append(round((raw - 2731) / 10.0, 1))
    v = voltage / 100.0
    a = current / 100.0
    return {
        "voltage_v": round(v, 2),
        "current_a": round(a, 2),          # + charging, - discharging
        "power_w": round(v * a, 1),
        "soc_pct": soc,
        "remaining_ah": round(remain / 100.0, 2),
        "nominal_ah": round(nominal / 100.0, 2),
        "cycles": cycles,
        "protection_flags": f"0x{protection:04X}",
        "charge_fet": int(bool(fet & 0x01)),
        "discharge_fet": int(bool(fet & 0x02)),
        "temps": temps,
    }


def parse_cells(d: bytes) -> list:
    n = len(d) // 2
    return [struct.unpack(">H", d[2 * i:2 * i + 2])[0] for i in range(n)]  # mV


def fieldnames():
    return (
        ["timestamp", "battery", "voltage_v", "current_a", "power_w", "soc_pct",
         "remaining_ah", "nominal_ah", "cycles", "protection_flags",
         "charge_fet", "discharge_fet"]
        + [f"temp{i + 1}_c" for i in range(MAX_TEMPS)]
        + [f"cell{i + 1}_mv" for i in range(MAX_CELLS)]
        + ["cell_delta_mv"]
    )


async def read_battery(name: str, address: str, retries: int = 3) -> dict:
    last_err = None
    for attempt in range(retries):
        try:
            # Connecting via a freshly discovered device object is far more
            # reliable on BlueZ than connecting by raw MAC address.
            dev = await BleakScanner.find_device_by_address(address, timeout=10.0)
            if dev is None:
                raise RuntimeError(
                    "not seen in BLE scan (out of range, asleep, or already "
                    "connected to another device/phone app)")
            async with BleakClient(dev, timeout=20.0) as client:
                await asyncio.sleep(1.0)  # let the BMS settle after connecting
                link = Link(client)
                await link.start()
                try:
                    basic = parse_basic(await link.request(CMD_BASIC))
                    await asyncio.sleep(0.5)
                    cells = parse_cells(await link.request(CMD_CELLS))
                finally:
                    await link.stop()
            row = {k: v for k, v in basic.items() if k != "temps"}
            for i in range(MAX_TEMPS):
                row[f"temp{i + 1}_c"] = basic["temps"][i] if i < len(basic["temps"]) else ""
            for i in range(MAX_CELLS):
                row[f"cell{i + 1}_mv"] = cells[i] if i < len(cells) else ""
            row["cell_delta_mv"] = (max(cells) - min(cells)) if cells else ""
            row["timestamp"] = dt.datetime.now().isoformat(timespec="seconds")
            row["battery"] = name
            return row
        except Exception as e:  # BLE is flaky; retry
            last_err = e
            print(f"  {name} attempt {attempt + 1}/{retries} failed: {e!r}")
            await asyncio.sleep(3)
    raise RuntimeError(f"{name}: failed after {retries} tries: {last_err!r}")


def send_signalk(sock, addr, row: dict):
    """Send one battery's readings to Signal K as a delta over UDP."""
    bid = row["battery"]
    base = f"electrical.batteries.{bid}"
    values = [
        {"path": f"{base}.voltage", "value": row["voltage_v"]},
        # Signal K: positive current flows OUT of the battery (discharge),
        # negative = charging. The BMS reports the opposite, so flip the sign.
        {"path": f"{base}.current", "value": -row["current_a"]},
        {"path": f"{base}.capacity.stateOfCharge", "value": row["soc_pct"] / 100.0},
        # Joules = Ah * 3600 * V (approximation using present pack voltage)
        {"path": f"{base}.capacity.remaining",
         "value": round(row["remaining_ah"] * 3600 * row["voltage_v"])},
        {"path": f"{base}.capacity.nominal",
         "value": round(row["nominal_ah"] * 3600 * row["voltage_v"])},
        {"path": f"{base}.cycles", "value": row["cycles"]},
    ]
    if row.get("temp1_c") != "":
        values.append({"path": f"{base}.temperature",
                       "value": round(row["temp1_c"] + 273.15, 2)})  # Kelvin
    for i in range(MAX_CELLS):
        mv = row.get(f"cell{i + 1}_mv")
        if mv != "" and mv is not None:
            values.append({"path": f"{base}.cells.{i + 1}.voltage", "value": mv / 1000.0})
    if row.get("cell_delta_mv") != "":
        values.append({"path": f"{base}.cellDelta", "value": row["cell_delta_mv"] / 1000.0})

    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    delta = {
        "context": "vessels.self",
        "updates": [{
            "source": {"label": "ecoworthy-logger", "type": "BLE"},
            "timestamp": ts,
            "values": values,
        }],
    }
    sock.sendto(json.dumps(delta).encode(), addr)


def append_row(path: str, row: dict):
    new_file = not os.path.exists(path) or os.path.getsize(path) == 0
    try:
        f = open(path, "a", newline="")
    except PermissionError:
        # Some FUSE/network/exFAT mounts reject append-mode opens of an
        # existing file. Open for read/write and seek to the end instead.
        f = open(path, "r+", newline="")
        f.seek(0, os.SEEK_END)
    with f:
        w = csv.DictWriter(f, fieldnames=fieldnames())
        if new_file:
            w.writeheader()
        w.writerow(row)


async def scan():
    print("Scanning 10s...")
    devices = await BleakScanner.discover(timeout=10.0, return_adv=True)
    for addr, (dev, adv) in devices.items():
        print(f"{addr}  {dev.name or '?':<24} RSSI {adv.rssi}  {adv.service_uuids}")


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--scan", action="store_true", help="list nearby BLE devices")
    p.add_argument("--battery", action="append", default=[],
                   metavar="NAME=MAC", help="repeatable")
    p.add_argument("--interval", type=int, default=30, help="seconds between polls")
    p.add_argument("--csv", default=os.path.expanduser("~/battery_log.csv"))
    p.add_argument("--signalk-host", default=None,
                   help="send Signal K deltas over UDP to this host (e.g. 127.0.0.1)")
    p.add_argument("--signalk-port", type=int, default=8375,
                   help="UDP port of the Signal K connection (default 8375)")
    args = p.parse_args()

    if args.scan:
        return await scan()
    if not args.battery:
        p.error("give at least one --battery NAME=MAC (or use --scan)")

    batteries = [b.split("=", 1) for b in args.battery]
    sk_sock = sk_addr = None
    if args.signalk_host:
        sk_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk_addr = (args.signalk_host, args.signalk_port)
        print(f"Publishing to Signal K via UDP {args.signalk_host}:{args.signalk_port}")
    print(f"Logging {len(batteries)} battery(ies) to {args.csv} every {args.interval}s")

    while True:
        start = asyncio.get_event_loop().time()
        for name, addr in batteries:  # sequential: BLE adapters handle one connection best
            try:
                row = await read_battery(name, addr)
            except Exception as e:
                print(f"BLUETOOTH ERROR {e}")
                await asyncio.sleep(3)
                continue
            if sk_sock:
                try:
                    send_signalk(sk_sock, sk_addr, row)
                except Exception as e:
                    print(f"SIGNAL K ERROR {e!r}")
            try:
                append_row(args.csv, row)
                print(f"{row['timestamp']} {name}: {row['voltage_v']}V "
                      f"{row['current_a']}A SOC {row['soc_pct']}%")
            except OSError as e:
                print(f"CSV WRITE ERROR for {args.csv}: {e!r}")
                print(f"  (data was read OK: {row})")
            await asyncio.sleep(3)  # let BlueZ settle before the next battery
        elapsed = asyncio.get_event_loop().time() - start
        await asyncio.sleep(max(1, args.interval - elapsed))


if __name__ == "__main__":
    asyncio.run(main())