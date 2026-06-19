#!/usr/bin/env python3
"""
inverter_reaction_tester.py

Measure how fast a battery inverter reacts to a Modbus power setpoint, using an
SMA energy meter (SMA Speedwire) as the independent observer.

Per trial the tool does exactly this:

  1. Continuously receive SMA energy-meter datagrams in a background thread and
     timestamp each one on arrival with a monotonic clock.
  2. Establish a *baseline* = average meter net power over a short warm-up window.
     This is the reference power BEFORE the inverter reacts -- NOT zero.
  3. Write the power setpoint ONCE to the inverter holding register over Modbus
     TCP, applying the configured data type / word order / scaling.  t0 is taken
     immediately after the write call returns.
  4. Read the register back ONCE to verify the value was stored. This is for
     verification only; it is not used for timing, and the inverter is never read
     again during the measurement.
  5. Watch the meter samples. The inverter is considered to have "reacted" when
     the meter net power has moved away from the baseline by at least a fraction
     (default 50%) of the absolute target setpoint.  t1 is the *arrival* timestamp
     of the meter datagram that crossed the threshold.
  6. Reaction time = t1 - t0.

Only the meter is read while timing the reaction; the inverter is not polled
during the measurement window.

------------------------------------------------------------------------------
IMPORTANT - the meter is NOT a TCP connection
------------------------------------------------------------------------------
SMA Speedwire energy meters (Energy Meter / Sunny Home Manager 2.0) do not expose
a TCP socket you connect to.  They *broadcast* their measurements as UDP multicast
datagrams to 239.12.255.254 : 9522.  So the meter is configured by multicast
group / port (plus an optional local NIC and an optional serial filter), not by an
IP:port you dial.  The PC running this tool must be on the same L2 network/VLAN as
the meter and must allow inbound UDP 9522 through its firewall.

Run `--monitor` first to confirm the meter datagrams are being received before you
attempt a reaction test.

------------------------------------------------------------------------------
Requirements
------------------------------------------------------------------------------
  Python 3.8+ and pymodbus 3.x     ->   pip install pymodbus

Everything else is from the Python standard library.

------------------------------------------------------------------------------
Examples
------------------------------------------------------------------------------
  # 1) Sanity check: just watch the meter net power
  python inverter_reaction_tester.py --monitor --meter-iface 192.168.1.50

  # 2) Single reaction test: discharge 3000 W, S32 register at addr 40149,
  #    register units are 1 W (scale 1.0)
  python inverter_reaction_tester.py \
      --inv-host 192.168.1.20 --inv-unit 3 --inv-register 40149 \
      --datatype S32 --word-order big --scale 1.0 \
      --target-w -3000 --meter-iface 192.168.1.50

  # 3) Five trials, store everything in a JSON config instead of long CLI
  python inverter_reaction_tester.py --config my_setup.json --trials 5

CLI options always override values from --config.
"""

import argparse
import json
import socket
import statistics
import struct
import sys
import threading
import time

# --------------------------------------------------------------------------- #
# Modbus client import (pymodbus 3.x preferred, 2.x fallback)
# --------------------------------------------------------------------------- #
try:
    from pymodbus.client import ModbusTcpClient            # pymodbus >= 3.0
except ImportError:
    try:
        from pymodbus.client.sync import ModbusTcpClient   # pymodbus 2.x
    except ImportError:
        ModbusTcpClient = None


# --------------------------------------------------------------------------- #
# SMA Speedwire energy-meter datagram parser
# --------------------------------------------------------------------------- #
SMA_PROTOCOL_ENERGY_METER = 0x6069   # protocol id of the energy-meter telegram

# OBIS measurement (index, type) -> meaning. type 4 = 4-byte "current" value.
# Power "current" values are transmitted in 0.1 W units, so divide by 10 -> W.
OBIS_P_IMPORT = (1, 4)   # 1.4.0  active power drawn FROM grid  (W, after /10)
OBIS_P_EXPORT = (2, 4)   # 2.4.0  active power fed INTO grid    (W, after /10)


def parse_sma_em(datagram):
    """Parse an SMA energy-meter Speedwire datagram.

    Returns a dict with serial, susy_id, ticker (ms), p_import, p_export and
    net_power (= p_import - p_export, positive means importing from grid), all in
    watts.  Returns None if the datagram is not a valid energy-meter telegram or
    does not contain both active-power channels.
    """
    if len(datagram) < 28 or datagram[0:4] != b"SMA\x00":
        return None
    if int.from_bytes(datagram[16:18], "big") != SMA_PROTOCOL_ENERGY_METER:
        return None

    data_end = min(int.from_bytes(datagram[12:14], "big") + 16, len(datagram))
    susy_id = int.from_bytes(datagram[18:20], "big")
    serial = int.from_bytes(datagram[20:24], "big")
    ticker = int.from_bytes(datagram[24:28], "big")   # device ms counter (wraps)

    values = {}
    pos = 28
    while pos + 4 <= data_end:
        index = datagram[pos + 1]
        typ = datagram[pos + 2]
        if typ == 4:                       # 4-byte current value
            if pos + 8 > data_end:
                break
            values[(index, typ)] = int.from_bytes(datagram[pos + 4:pos + 8], "big")
            pos += 8
        elif typ == 8:                     # 8-byte counter value
            if pos + 12 > data_end:
                break
            values[(index, typ)] = int.from_bytes(datagram[pos + 4:pos + 12], "big")
            pos += 12
        else:                              # version block / unknown -> 4+4 bytes
            pos += 8

    p_imp = values.get(OBIS_P_IMPORT)
    p_exp = values.get(OBIS_P_EXPORT)
    if p_imp is None or p_exp is None:
        return None

    p_import = p_imp / 10.0
    p_export = p_exp / 10.0
    return {
        "serial": serial,
        "susy_id": susy_id,
        "ticker": ticker,
        "p_import": p_import,
        "p_export": p_export,
        "net_power": p_import - p_export,
    }


# --------------------------------------------------------------------------- #
# Background multicast receiver
# --------------------------------------------------------------------------- #
class MeterReceiver(threading.Thread):
    """Receives SMA energy-meter multicast datagrams and keeps the latest sample.

    Each datagram is timestamped with time.perf_counter() the instant it arrives,
    so the reaction time is measured against datagram arrival, independent of how
    fast the main thread happens to poll.
    """

    def __init__(self, group, port, iface_ip=None, serial_filter=None,
                 src_ip_filter=None):
        super().__init__(daemon=True)
        self.group = group
        self.port = port
        self.iface_ip = iface_ip
        self.serial_filter = serial_filter
        self.src_ip_filter = src_ip_filter
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest = None           # (t_perf, sample_dict, src_ip)
        self._count = 0
        self._sock = None
        self.error = None

    def _open(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass                       # not available / not needed on Windows
        s.bind(("", self.port))
        group_bin = socket.inet_aton(self.group)
        if self.iface_ip:
            mreq = group_bin + socket.inet_aton(self.iface_ip)
        else:
            mreq = group_bin + struct.pack("=I", socket.INADDR_ANY)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        s.settimeout(1.0)
        self._sock = s

    def run(self):
        try:
            self._open()
        except Exception as exc:       # noqa: BLE001 - report any setup failure
            self.error = exc
            return
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            t = time.perf_counter()
            if self.src_ip_filter and addr[0] != self.src_ip_filter:
                continue
            sample = parse_sma_em(data)
            if sample is None:
                continue
            if self.serial_filter and sample["serial"] != self.serial_filter:
                continue
            with self._lock:
                self._latest = (t, sample, addr[0])
                self._count += 1

    def latest(self):
        with self._lock:
            return self._latest

    def count(self):
        with self._lock:
            return self._count

    def stop(self):
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Inverter value encoding / decoding
# --------------------------------------------------------------------------- #
# data type -> (struct format for the whole value, register count, (min, max))
DATA_TYPES = {
    "U16": (">H", 1, (0, 0xFFFF)),
    "S16": (">h", 1, (-32768, 32767)),
    "U32": (">I", 2, (0, 0xFFFFFFFF)),
    "S32": (">i", 2, (-2147483648, 2147483647)),
}


def watts_to_raw(watts, scale):
    """Convert a watt value to the raw register integer.

    `scale` is the value of one register count expressed in watts
    (i.e. watts = raw * scale).  Examples:
        register in 1 W     -> scale = 1.0
        register in 0.1 W   -> scale = 0.1
        register in 10 W    -> scale = 10.0
    """
    return int(round(watts / scale))


def encode_value(raw, data_type, word_order):
    """Encode a raw integer to a list of 16-bit Modbus register values."""
    fmt, nreg, (lo, hi) = DATA_TYPES[data_type]
    if raw < lo or raw > hi:
        raise ValueError(
            f"raw value {raw} is out of range for {data_type} [{lo}, {hi}]. "
            f"Check --scale / --target-w / --datatype."
        )
    packed = struct.pack(fmt, raw)
    regs = [int.from_bytes(packed[i:i + 2], "big") for i in range(0, len(packed), 2)]
    if nreg == 2 and word_order == "little":
        regs.reverse()
    return regs


def decode_registers(regs, data_type, word_order):
    """Decode register values read back from the inverter into a raw integer."""
    fmt, nreg, _ = DATA_TYPES[data_type]
    words = list(regs[:nreg])
    if nreg == 2 and word_order == "little":
        words.reverse()
    packed = b"".join(int(w).to_bytes(2, "big") for w in words)
    return struct.unpack(fmt, packed)[0]


# --------------------------------------------------------------------------- #
# pymodbus version-compatible helpers (slave= for 3.x, unit= for 2.x)
# --------------------------------------------------------------------------- #
def mb_read_holding(client, address, count, unit):
    try:
        return client.read_holding_registers(address, count=count, slave=unit)
    except TypeError:
        return client.read_holding_registers(address, count=count, unit=unit)


def mb_write_single(client, address, value, unit):
    try:
        return client.write_register(address, value, slave=unit)
    except TypeError:
        return client.write_register(address, value, unit=unit)


def mb_write_multi(client, address, values, unit):
    try:
        return client.write_registers(address, values, slave=unit)
    except TypeError:
        return client.write_registers(address, values, unit=unit)


def write_setpoint(client, cfg, watts):
    """Encode `watts` and write it to the inverter. Returns (raw, regs, response)."""
    raw = watts_to_raw(watts, cfg["scale"])
    regs = encode_value(raw, cfg["datatype"], cfg["word_order"])
    if len(regs) == 1:
        resp = mb_write_single(client, cfg["register"], regs[0], cfg["unit"])
    else:
        resp = mb_write_multi(client, cfg["register"], regs, cfg["unit"])
    return raw, regs, resp


# --------------------------------------------------------------------------- #
# Meter sampling helpers
# --------------------------------------------------------------------------- #
def wait_first_sample(rx, timeout):
    """Block until the receiver has at least one parsed sample, or timeout."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if rx.error:
            return None
        if rx.latest() is not None:
            return rx.latest()
        time.sleep(0.01)
    return None


def collect_baseline(rx, seconds):
    """Collect distinct meter samples for `seconds` and summarise them.

    Returns dict with baseline (mean net power), stdev, n, min, max and the mean
    inter-arrival interval (= the meter's effective update period / measurement
    resolution).
    """
    samples = []          # (t_perf, net_power)
    last_t = None
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        latest = rx.latest()
        if latest is not None and latest[0] != last_t:
            last_t = latest[0]
            samples.append((latest[0], latest[1]["net_power"]))
        time.sleep(0.002)

    if not samples:
        return None
    nets = [n for _, n in samples]
    intervals = [samples[i + 1][0] - samples[i][0] for i in range(len(samples) - 1)]
    return {
        "baseline": statistics.mean(nets),
        "stdev": statistics.pstdev(nets) if len(nets) > 1 else 0.0,
        "n": len(nets),
        "min": min(nets),
        "max": max(nets),
        "mean_interval": statistics.mean(intervals) if intervals else float("nan"),
    }


def detect_reaction(rx, baseline, threshold_w, t0, timeout, confirm):
    """Wait for the meter net power to deviate from baseline by >= threshold_w.

    Only samples that arrived at/after t0 are considered. `confirm` consecutive
    crossing samples are required before accepting, but the reported reaction time
    uses the FIRST crossing sample (the moment the reaction actually began).

    Returns (t1, sample, delta) on success, or None on timeout.
    """
    last_t = None
    consec = 0
    first_cross = None
    deadline = t0 + timeout
    while time.perf_counter() < deadline:
        latest = rx.latest()
        if latest is None or latest[0] == last_t:
            time.sleep(0.002)
            continue
        last_t = latest[0]
        t, sample, _ = latest
        if t < t0:                         # stale sample from before the write
            continue
        delta = sample["net_power"] - baseline
        if abs(delta) >= threshold_w:
            if first_cross is None:
                first_cross = (t, sample, delta)
            consec += 1
            if consec >= confirm:
                return first_cross
        else:
            consec = 0
            first_cross = None
    return None


# --------------------------------------------------------------------------- #
# One measurement trial
# --------------------------------------------------------------------------- #
def run_trial(client, rx, cfg, trial_no, total_trials):
    print(f"\n--- Trial {trial_no}/{total_trials} "
          f"-------------------------------------------------")

    # 1. Baseline (reference power before the inverter reacts)
    print(f"  Sampling baseline for {cfg['warmup']:.1f}s ...")
    base = collect_baseline(rx, cfg["warmup"])
    if base is None:
        print("  ERROR: no meter samples during warm-up. Aborting trial.")
        return {"ok": False, "reason": "no_meter_data"}

    baseline = base["baseline"]
    threshold = abs(cfg["target_w"]) * cfg["fraction"]
    res_ms = base["mean_interval"] * 1000.0
    print(f"    baseline net power : {baseline:10.1f} W   "
          f"(noise +/- {base['stdev']:.1f} W over {base['n']} samples)")
    print(f"    meter update period: {res_ms:8.1f} ms  "
          f"(<- reaction-time resolution)")
    print(f"    detection threshold: {threshold:10.1f} W   "
          f"(= {cfg['fraction'] * 100:.0f}% of |{cfg['target_w']:.0f} W|)")
    if threshold < 3 * base["stdev"]:
        print("    WARNING: threshold is within ~3x the meter noise -- result may "
              "false-trigger. Use a larger --target-w or a quieter load.")

    # 2. Write the setpoint  (t0 is captured right after the call returns)
    try:
        raw, regs, resp = write_setpoint(client, cfg, cfg["target_w"])
        t0 = time.perf_counter()
    except ValueError as exc:
        print(f"  ERROR encoding setpoint: {exc}")
        return {"ok": False, "reason": "encode_error"}
    if resp is None or resp.isError():
        print(f"  ERROR: Modbus write failed: {resp}")
        return {"ok": False, "reason": "write_error"}
    print(f"  Wrote setpoint: {cfg['target_w']:.0f} W  ->  raw {raw}  "
          f"regs {regs}  @ register {cfg['register']}")

    # 3. Read back once to verify (verification only, not used for timing)
    try:
        rr = mb_read_holding(client, cfg["register"], len(regs), cfg["unit"])
        if rr is None or rr.isError():
            print(f"  WARNING: read-back failed: {rr}")
        else:
            rb_raw = decode_registers(rr.registers, cfg["datatype"], cfg["word_order"])
            rb_w = rb_raw * cfg["scale"]
            ok = "OK" if rb_raw == raw else "MISMATCH"
            print(f"  Read back     : {rb_w:.0f} W  (raw {rb_raw})  [{ok}]")
    except Exception as exc:               # noqa: BLE001
        print(f"  WARNING: read-back error: {exc}")

    # 4. Watch the meter for the reaction (only the meter is read here)
    print(f"  Waiting for meter to react (timeout {cfg['timeout']:.1f}s) ...")
    hit = detect_reaction(rx, baseline, threshold, t0, cfg["timeout"], cfg["confirm"])

    result = {"ok": False, "baseline": baseline, "threshold": threshold,
              "resolution_ms": res_ms}
    if hit is None:
        print(f"  TIMEOUT: meter did not move by {threshold:.0f} W within "
              f"{cfg['timeout']:.1f}s. Inverter did not react (or needs an "
              f"external-control-enable register set first).")
        result["reason"] = "timeout"
    else:
        t1, sample, delta = hit
        reaction_ms = (t1 - t0) * 1000.0
        direction = "import+" if delta > 0 else "export+"
        print(f"  REACTED: meter net power = {sample['net_power']:.1f} W "
              f"(delta {delta:+.1f} W, {direction})")
        print(f"  >> Reaction time = {reaction_ms:.1f} ms "
              f"(+/- ~{res_ms:.0f} ms meter resolution)")
        result.update({"ok": True, "reaction_ms": reaction_ms,
                       "delta": delta, "net_power": sample["net_power"]})

    return result


def reset_inverter(client, cfg):
    """Write the reset/idle setpoint back to the inverter."""
    try:
        raw, regs, resp = write_setpoint(client, cfg, cfg["reset_value"])
        if resp is None or resp.isError():
            print(f"  WARNING: reset write failed: {resp}")
        else:
            print(f"  Reset setpoint to {cfg['reset_value']:.0f} W (raw {raw}).")
    except Exception as exc:               # noqa: BLE001
        print(f"  WARNING: reset write error: {exc}")


# --------------------------------------------------------------------------- #
# Monitor mode
# --------------------------------------------------------------------------- #
def run_monitor(rx, meter_timeout):
    print("Monitor mode - press Ctrl+C to stop.\n")
    first = wait_first_sample(rx, meter_timeout)
    if rx.error:
        print(f"ERROR opening meter socket: {rx.error}")
        return 1
    if first is None:
        print_no_meter_help(meter_timeout)
        return 1
    last_t = None
    while True:
        latest = rx.latest()
        if latest is not None and latest[0] != last_t:
            last_t = latest[0]
            t, s, src = latest
            print(f"  net {s['net_power']:10.1f} W   "
                  f"import {s['p_import']:9.1f} W   export {s['p_export']:9.1f} W   "
                  f"serial {s['serial']}   from {src}")
        time.sleep(0.05)


def print_no_meter_help(timeout):
    print(f"\nERROR: no SMA energy-meter datagrams received in {timeout:.0f}s.\n"
          "  - Is this PC on the same LAN/VLAN as the meter?\n"
          "  - Allow inbound UDP 9522 through the Windows firewall (python.exe).\n"
          "  - On a multi-NIC PC, set --meter-iface to the LAN adapter's IP.\n"
          "  - Confirm the meter is powered and on 239.12.255.254:9522.")


# --------------------------------------------------------------------------- #
# Configuration / CLI
# --------------------------------------------------------------------------- #
def build_config():
    # First pass: just grab --config so its values become defaults.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config")
    pre_args, _ = pre.parse_known_args()
    file_cfg = {}
    if pre_args.config:
        with open(pre_args.config, "r", encoding="utf-8") as fh:
            file_cfg = json.load(fh)

    def d(key, fallback):
        return file_cfg.get(key, fallback)

    p = argparse.ArgumentParser(
        parents=[pre],
        description="Measure battery-inverter reaction time to a Modbus power "
                    "setpoint, observed via an SMA Speedwire energy meter.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Inverter (Modbus TCP)
    g = p.add_argument_group("inverter (Modbus TCP)")
    g.add_argument("--inv-host", default=d("inv_host", None),
                   help="inverter IP / hostname")
    g.add_argument("--inv-port", type=int, default=d("inv_port", 502),
                   help="inverter Modbus TCP port")
    g.add_argument("--inv-unit", type=int, default=d("inv_unit", 1),
                   help="Modbus unit/slave id (SMA inverters are often 3)")
    g.add_argument("--inv-register", type=int, default=d("inv_register", None),
                   help="holding-register address of the power setpoint")
    g.add_argument("--datatype", choices=list(DATA_TYPES), default=d("datatype", "S32"),
                   help="register data type")
    g.add_argument("--word-order", choices=["big", "little"],
                   default=d("word_order", "big"),
                   help="word order for 32-bit values (big = high word first)")
    g.add_argument("--scale", type=float, default=d("scale", 1.0),
                   help="watts per register count (watts = raw * scale): "
                        "1.0 for W, 0.1 for 0.1 W, 10.0 for 10 W")
    g.add_argument("--modbus-timeout", type=float, default=d("modbus_timeout", 3.0),
                   help="Modbus TCP socket timeout (s)")

    # Setpoint / detection
    g = p.add_argument_group("setpoint / detection")
    g.add_argument("--target-w", type=float, default=d("target_w", None),
                   help="power setpoint to write, in watts "
                        "(sign per inverter convention, e.g. negative = discharge)")
    g.add_argument("--fraction", type=float, default=d("fraction", 0.5),
                   help="meter must move by this fraction of |target_w| to count "
                        "as 'reacted' (0.5 = 50%%)")
    g.add_argument("--timeout", type=float, default=d("timeout", 10.0),
                   help="max time to wait for a reaction (s)")
    g.add_argument("--confirm", type=int, default=d("confirm", 1),
                   help="consecutive crossing samples required (>1 rejects spikes "
                        "but adds up to one meter period of latency)")

    # Meter (SMA Speedwire, multicast UDP)
    g = p.add_argument_group("meter (SMA Speedwire, multicast UDP)")
    g.add_argument("--meter-group", default=d("meter_group", "239.12.255.254"),
                   help="multicast group address")
    g.add_argument("--meter-port", type=int, default=d("meter_port", 9522),
                   help="multicast UDP port")
    g.add_argument("--meter-iface", default=d("meter_iface", None),
                   help="local NIC IP to receive multicast on (recommended on "
                        "multi-NIC / Windows machines)")
    g.add_argument("--meter-serial", type=int, default=d("meter_serial", None),
                   help="only accept datagrams from this meter serial")
    g.add_argument("--meter-src-ip", default=d("meter_src_ip", None),
                   help="only accept datagrams from this sender IP")
    g.add_argument("--meter-timeout", type=float, default=d("meter_timeout", 5.0),
                   help="max time to wait for the first meter datagram (s)")

    # Trial control
    g = p.add_argument_group("trial control")
    g.add_argument("--warmup", type=float, default=d("warmup", 2.0),
                   help="baseline sampling window before each write (s)")
    g.add_argument("--trials", type=int, default=d("trials", 1),
                   help="number of measurement trials")
    g.add_argument("--settle", type=float, default=d("settle", 3.0),
                   help="settle time between trials after reset (s)")
    g.add_argument("--no-reset", action="store_true", default=d("no_reset", False),
                   help="do NOT write the reset value after each trial")
    g.add_argument("--reset-value", type=float, default=d("reset_value", 0.0),
                   help="setpoint (W) written after each trial when resetting")

    # Modes
    g = p.add_argument_group("modes")
    g.add_argument("--monitor", action="store_true", default=d("monitor", False),
                   help="just print live meter net power, then exit (no inverter)")

    args = p.parse_args()

    cfg = {
        "inv_host": args.inv_host, "inv_port": args.inv_port,
        "unit": args.inv_unit, "register": args.inv_register,
        "datatype": args.datatype, "word_order": args.word_order,
        "scale": args.scale, "modbus_timeout": args.modbus_timeout,
        "target_w": args.target_w, "fraction": args.fraction,
        "timeout": args.timeout, "confirm": args.confirm,
        "meter_group": args.meter_group, "meter_port": args.meter_port,
        "meter_iface": args.meter_iface, "meter_serial": args.meter_serial,
        "meter_src_ip": args.meter_src_ip, "meter_timeout": args.meter_timeout,
        "warmup": args.warmup, "trials": args.trials, "settle": args.settle,
        "reset": not args.no_reset, "reset_value": args.reset_value,
        "monitor": args.monitor,
    }
    return cfg


def validate_config(cfg):
    errors = []
    if not cfg["monitor"]:
        if ModbusTcpClient is None:
            errors.append("pymodbus is not installed. Run: pip install pymodbus")
        if not cfg["inv_host"]:
            errors.append("--inv-host is required (or set inv_host in --config)")
        if cfg["register"] is None:
            errors.append("--inv-register is required (or set inv_register in --config)")
        if cfg["target_w"] is None:
            errors.append("--target-w is required (or set target_w in --config)")
        elif cfg["target_w"] == 0:
            errors.append("--target-w must be non-zero")
        if cfg["scale"] == 0:
            errors.append("--scale must be non-zero")
        if not 0 < cfg["fraction"] <= 1:
            errors.append("--fraction must be in (0, 1]")
        if cfg["confirm"] < 1:
            errors.append("--confirm must be >= 1")
    return errors


def print_header(cfg):
    print("=" * 66)
    print(" Inverter reaction-time tester")
    print("=" * 66)
    if not cfg["monitor"]:
        print(f" Inverter : {cfg['inv_host']}:{cfg['inv_port']} unit {cfg['unit']}  "
              f"register {cfg['register']}  {cfg['datatype']}/{cfg['word_order']}  "
              f"scale {cfg['scale']} W/count")
        print(f" Setpoint : {cfg['target_w']:.0f} W   detect at "
              f"{cfg['fraction'] * 100:.0f}% change   timeout {cfg['timeout']:.0f}s   "
              f"trials {cfg['trials']}")
    print(f" Meter    : Speedwire multicast {cfg['meter_group']}:{cfg['meter_port']}"
          + (f"  iface {cfg['meter_iface']}" if cfg["meter_iface"] else "")
          + (f"  serial {cfg['meter_serial']}" if cfg["meter_serial"] else ""))
    print("=" * 66)


def print_summary(results):
    times = [r["reaction_ms"] for r in results if r.get("ok")]
    print("\n" + "=" * 66)
    print(" Summary")
    print("=" * 66)
    print(f"  trials run    : {len(results)}")
    print(f"  reacted       : {len(times)}")
    if times:
        print(f"  min  reaction : {min(times):8.1f} ms")
        print(f"  mean reaction : {statistics.mean(times):8.1f} ms")
        print(f"  median        : {statistics.median(times):8.1f} ms")
        print(f"  max  reaction : {max(times):8.1f} ms")
        if len(times) > 1:
            print(f"  stdev         : {statistics.stdev(times):8.1f} ms")
    print("=" * 66)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    cfg = build_config()
    errors = validate_config(cfg)
    if errors:
        print("Configuration error(s):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 2

    print_header(cfg)

    rx = MeterReceiver(cfg["meter_group"], cfg["meter_port"],
                       iface_ip=cfg["meter_iface"], serial_filter=cfg["meter_serial"],
                       src_ip_filter=cfg["meter_src_ip"])
    rx.start()

    client = None
    try:
        if cfg["monitor"]:
            return run_monitor(rx, cfg["meter_timeout"])

        # Confirm the meter is actually delivering data before touching the inverter.
        print(f"\nWaiting for first meter datagram (timeout "
              f"{cfg['meter_timeout']:.0f}s) ...")
        if wait_first_sample(rx, cfg["meter_timeout"]) is None:
            if rx.error:
                print(f"ERROR opening meter socket: {rx.error}")
            else:
                print_no_meter_help(cfg["meter_timeout"])
            return 1
        print("Meter datagrams are being received.")

        # Connect to the inverter.
        client = ModbusTcpClient(cfg["inv_host"], port=cfg["inv_port"],
                                 timeout=cfg["modbus_timeout"])
        if not client.connect():
            print(f"ERROR: could not connect to inverter at "
                  f"{cfg['inv_host']}:{cfg['inv_port']}")
            return 1

        results = []
        for n in range(1, cfg["trials"] + 1):
            results.append(run_trial(client, rx, cfg, n, cfg["trials"]))
            if cfg["reset"]:
                reset_inverter(client, cfg)
            if n < cfg["trials"]:
                print(f"  Settling {cfg['settle']:.1f}s before next trial ...")
                time.sleep(cfg["settle"])

        print_summary(results)
        return 0

    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        return 130
    finally:
        if client is not None:
            if cfg["reset"] and not cfg["monitor"]:
                reset_inverter(client, cfg)
            client.close()
        rx.stop()


if __name__ == "__main__":
    sys.exit(main())
