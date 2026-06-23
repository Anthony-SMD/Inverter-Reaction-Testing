#!/usr/bin/env python3
"""
modbustest.py

Dead-simple Modbus TCP tester: read one register from the device at
192.168.101.190:502, unit id 1, and print the value. Choose between a holding
register (function code 3) and an input register (function code 4). No
third-party packages.

Usage:
  python modbustest.py                  # uses the defaults below
  python modbustest.py --type input     # read an input register instead
  python modbustest.py --type holding --register 8
  python modbustest.py --register 100 --count 10   # read 10 registers
  python modbustest.py --scan-units                # find responding unit ids
"""

import argparse
import socket
import struct
import sys

HOST = "192.168.101.190"
PORT = 502
UNIT = 101
REGISTER = 102
REG_TYPE = "input"                     # "holding" (FC 3) or "input" (FC 4)

# register type -> Modbus function code
FUNCTION_CODES = {"holding": 0x03, "input": 0x04}

# Modbus exception codes -> short name (for friendlier messages)
EXCEPTION_NAMES = {
    1: "Illegal Function",
    2: "Illegal Data Address",
    3: "Illegal Data Value",
    4: "Slave Device Failure",
    5: "Acknowledge",
    6: "Slave Device Busy",
    8: "Memory Parity Error",
    10: "Gateway Path Unavailable",
    11: "Gateway Target Device Failed to Respond",
}


class ModbusError(IOError):
    """A Modbus exception response from the device (carries the numeric code)."""

    def __init__(self, code):
        self.code = code
        name = EXCEPTION_NAMES.get(code, "Unknown")
        super().__init__(f"Modbus exception code {code} ({name})")


def read_register(host, port, unit, address, reg_type="holding", count=1, timeout=3.0):
    """Read `count` consecutive registers; return a list of 16-bit values.

    reg_type is "holding" (function code 3) or "input" (function code 4).
    Raises ModbusError on an exception response, OSError on a socket problem.
    """
    function_code = FUNCTION_CODES[reg_type]
    # Modbus TCP: MBAP header (tid, proto=0, length, unit) + PDU (fc, addr, count)
    tid = 1
    pdu = struct.pack(">BHH", function_code, address, count)
    mbap = struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit)

    with socket.create_connection((host, port), timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(mbap + pdu)

        header = _recv_exactly(sock, 7)                  # tid, proto, length, unit
        _, _, length, _ = struct.unpack(">HHHB", header)
        resp = _recv_exactly(sock, length - 1)           # response PDU

    if resp[0] & 0x80:                                   # exception response
        raise ModbusError(resp[1])
    byte_count = resp[1]
    return list(struct.unpack(">" + "H" * (byte_count // 2), resp[2:2 + byte_count]))


def _recv_exactly(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise IOError("connection closed by peer")
        buf += chunk
    return buf


def scan_units(host, port, address, reg_type, count, first, last, timeout):
    """Probe every unit id in [first, last] and report what each one replies.

    A device is "present" if it answers at all -- whether with data or a Modbus
    exception. Only a socket timeout / no response means nothing is there.
    """
    print(f"Scanning unit ids {first}-{last} on {host}:{port} "
          f"({reg_type} register {address}) ...\n")
    found = []
    for unit in range(first, last + 1):
        try:
            values = read_register(host, port, unit, address, reg_type, count, timeout)
        except ModbusError as exc:
            # The device answered -- it exists, it just refused this request.
            print(f"  unit {unit:3d}: responded with {exc}")
            found.append(unit)
        except (socket.timeout, OSError):
            continue                                     # no device at this id
        else:
            shown = "  ".join(str(v) for v in values)
            print(f"  unit {unit:3d}: OK -> {shown}")
            found.append(unit)

    print()
    if found:
        print(f"Responding unit id(s): {', '.join(map(str, found))}")
    else:
        print("No unit ids responded. Check host/port, wiring, and that something "
              "is listening on Modbus TCP.")
    return 0 if found else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=HOST, help="device IP / hostname")
    parser.add_argument("--port", type=int, default=PORT, help="Modbus TCP port")
    parser.add_argument("--unit", type=int, default=UNIT, help="unit / slave id")
    parser.add_argument("--register", type=int, default=REGISTER, help="register address")
    parser.add_argument("--count", type=int, default=1,
                        help="number of consecutive registers to read")
    parser.add_argument("--type", choices=list(FUNCTION_CODES), default=REG_TYPE,
                        help="register type to read")
    parser.add_argument("--scan-units", action="store_true",
                        help="probe a range of unit ids and report which respond")
    parser.add_argument("--scan-first", type=int, default=1,
                        help="first unit id to probe when scanning")
    parser.add_argument("--scan-last", type=int, default=247,
                        help="last unit id to probe when scanning")
    parser.add_argument("--timeout", type=float, default=1.0,
                        help="socket timeout per request, seconds "
                             "(kept short so scans don't drag)")
    args = parser.parse_args()

    if args.scan_units:
        return scan_units(args.host, args.port, args.register, args.type, args.count,
                          args.scan_first, args.scan_last, args.timeout)

    print(f"Reading {args.count} {args.type} register(s) from {args.register} on "
          f"{args.host}:{args.port} unit {args.unit} ...")
    try:
        values = read_register(args.host, args.port, args.unit, args.register,
                               args.type, args.count, args.timeout)
    except OSError as exc:
        print(f"ERROR: {exc}")
        return 1
    for offset, value in enumerate(values):
        print(f"  register {args.register + offset} = {value:6d}  (0x{value:04X})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
