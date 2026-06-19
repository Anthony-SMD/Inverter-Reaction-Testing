# Inverter Reaction Tester

Measures how fast a battery inverter reacts to a **Modbus TCP power setpoint**, using
an **SMA energy meter (SMA Speedwire)** as an independent observer.

The tool times the gap between *writing the setpoint to the inverter* and *the meter
seeing the power change* — without reading anything from the inverter during the
measurement window.

## How it works (per trial)

1. A background thread continuously receives the SMA energy-meter multicast
   datagrams and timestamps each one on arrival (monotonic clock).
2. A **baseline** is taken = the average meter net power over a short warm-up window.
   This is the reference power *before* the inverter reacts (not zero).
3. The power setpoint is written **once** to the inverter holding register
   (`t0` = right after the write returns).
4. The register is read back **once** to verify the value was stored (verification
   only — not used for timing).
5. The meter samples are watched. The inverter is "reacted" once the meter net power
   has moved from the baseline by **≥ 50%** (configurable) of the absolute target
   setpoint. `t1` = arrival time of that datagram.
6. **Reaction time = t1 − t0.**

## Install

Needs Python 3.8+ and `pymodbus` 3.x.

**Linux / macOS** (creates a self-contained `./.venv`, recommended):

```
bash install.sh
```

(`install.sh` makes `run.sh` executable, so `./run.sh` works afterwards.)

Then run via the wrapper, which uses the venv automatically:

```
./run.sh --monitor --meter-iface 192.168.1.50
```

**Windows** (or any OS, manual):

```
pip install -r requirements.txt
python inverter_reaction_tester.py --help
```

See the [Linux notes](#linux-notes) below for finding your interface IP and opening
the firewall for the meter's multicast.

## The meter is multicast UDP, not TCP

SMA Speedwire energy meters do **not** expose a TCP connection. They broadcast to
the multicast group `239.12.255.254:9522`. So you don't give the meter an IP to dial —
instead the PC running this tool must be on the same LAN/VLAN as the meter, allow
inbound UDP 9522 through its firewall, and (on a multi-NIC machine) you should pass
`--meter-iface <local-adapter-IP>` so it listens on the right network.

**Always run monitor mode first** to confirm reception:

```
python inverter_reaction_tester.py --monitor --meter-iface 192.168.1.50
```

You should see live `net / import / export` watt readings. If not, see Troubleshooting.

## Run a reaction test

```
python inverter_reaction_tester.py \
    --inv-host 192.168.1.20 --inv-unit 3 --inv-register 40149 \
    --datatype S32 --word-order big --scale 1.0 \
    --target-w -3000 --meter-iface 192.168.1.50 --trials 5
```

Or put it all in a JSON file (see `example_config.json`) and run:

```
python inverter_reaction_tester.py --config my_setup.json
```

CLI options always override the config file.

## Key parameters (the reusable bits)

| Option | Meaning |
| --- | --- |
| `--inv-host` / `--inv-port` / `--inv-unit` | Inverter Modbus TCP target (SMA inverters often use unit id **3**) |
| `--inv-register` | Holding-register address of the power setpoint |
| `--datatype` | `U16` / `S16` / `U32` / `S32` — match your inverter's register |
| `--word-order` | `big` (high word first, most common) or `little`, for 32-bit values |
| `--scale` | **Watts per register count.** `watts = raw × scale`. So `1.0` = register in W, `0.1` = register in 0.1 W, `10.0` = register in 10 W |
| `--target-w` | Setpoint in watts (sign per your inverter; e.g. negative = discharge) |
| `--fraction` | Meter must move by this fraction of \|target\| to count as reacted (`0.5` = 50%) |
| `--meter-iface` | Local NIC IP to receive the multicast on |
| `--meter-serial` | Filter to one meter if several are on the network |
| `--trials` / `--settle` / `--warmup` | Repeat measurements, settle time between, baseline window |
| `--confirm` | Consecutive crossing samples required (>1 rejects noise spikes) |
| `--no-reset` / `--reset-value` | By default the setpoint is written back to 0 W after each trial |

## Measurement resolution

The reaction time can only be as precise as the meter's update rate. The tool measures
and prints the meter's update period during warm-up (`meter update period … ms`). The
reaction time is reported with that figure as the resolution. If you need finer
resolution than the meter provides, the meter is the limiting factor — not this tool.

## Important caveats

- **Some inverters need external power control enabled first.** Many inverters
  (including SMA) ignore a power setpoint unless an operating-mode / "external
  setting" register is set first. This tool only writes the one power register you
  specify. If the meter never moves, that enable register is the usual reason.
- **Setpoint watchdogs.** Some inverters revert to default if the setpoint isn't
  refreshed periodically. The reaction may be transient. That's inverter-specific and
  out of scope here.
- **Detection is direction-agnostic.** It triggers on the *magnitude* of change at the
  meter (per your spec). The observed signed delta and direction are printed so you can
  sanity-check the wiring/sign convention.
- **Noise warning.** If the 50% threshold is within ~3× the measured meter noise, the
  tool warns you — use a bigger setpoint or a quieter load.

## Linux notes

The Python code is identical on every OS — only the environment setup differs.

**Find your LAN interface IP** (use it for `--meter-iface`):

```
ip -4 addr            # look for the adapter on the meter's subnet, e.g. 192.168.1.50
```

**Open the firewall for the meter's multicast** (inbound UDP 9522). The meter only
*broadcasts*, so you just need to let that traffic in:

```
# ufw (Ubuntu/Debian)
sudo ufw allow 9522/udp

# firewalld (Fedora/RHEL)
sudo firewall-cmd --add-port=9522/udp        # add --permanent to persist

# nftables/iptables (generic)
sudo iptables -I INPUT -p udp --dport 9522 -j ACCEPT
```

No root or elevated privileges are needed to run the tool itself — port 9522 is
unprivileged and Modbus uses an outbound TCP connection.

**Confirm packets actually arrive** (optional sanity check, needs sudo for tcpdump):

```
sudo tcpdump -ni any host 239.12.255.254 and udp port 9522
```

If you see packets there but `--monitor` shows nothing, the multicast is arriving on a
different interface than the kernel's default — pass that adapter's IP via
`--meter-iface`.

**Run it:**

```
./run.sh --monitor --meter-iface 192.168.1.50          # verify meter first
./run.sh --inv-host 192.168.1.20 --inv-unit 3 --inv-register 40149 \
         --datatype S32 --scale 1.0 --target-w -3000 \
         --meter-iface 192.168.1.50 --trials 5
```

## Troubleshooting

No meter datagrams received:
- Confirm the PC is on the same L2 network/VLAN as the meter.
- Open inbound UDP 9522 (Windows: allow `python.exe` in Windows Defender Firewall;
  Linux: see the [firewall commands above](#linux-notes)).
- On a multi-NIC machine, set `--meter-iface` to the LAN adapter's IP.
- Verify the meter is powered and broadcasting on `239.12.255.254:9522`.

Inverter write succeeds but meter never moves:
- Check the external-control-enable register for your inverter model.
- Verify `--datatype`, `--word-order` and `--scale` match the register map
  (the read-back line will show `MISMATCH` if the stored value differs).
- Confirm `--inv-unit` (slave id) is correct.
