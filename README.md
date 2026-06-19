# Inverter Reaction Tester

Measures how fast a battery inverter reacts to a **Modbus TCP power setpoint**, using
an **SMA energy meter (SMA Speedwire)** as an independent observer.

The tool times the gap between *writing the setpoint to the inverter* and *the meter
seeing the power change* — without reading anything from the inverter during the
measurement window.

## Quick start — run on the Linux device

Clone the repo on the device, install once, then run:

```bash
git clone https://github.com/Anthony-SMD/Inverter-Reaction-Testing.git
cd Inverter-Reaction-Testing
bash install.sh                     # builds ./.venv and installs pymodbus (once)

./run.sh --monitor                  # 1) confirm the SMA meter is being received
./run.sh --config default_config.json   # 2) run the reaction test
```

`install.sh` creates a self-contained `./.venv` (so it works on modern Debian/Ubuntu/
Fedora, which block system-wide `pip install`). `run.sh` runs the tool through that venv.

> **If `install.sh` can't set up pip** (`No module named pip`, `ensurepip is not
> available`, or `No module named 'distutils'`) — common on a stripped Python 3.7 —
> that's fine: **pymodbus is optional and the tool runs without it** on the built-in
> Modbus client. `install.sh` will say so, and `./run.sh` works regardless (it falls
> back to the system `python3`).
>
> To install pymodbus anyway (optional), add the system packages once and re-run:
>
> ```bash
> sudo apt update
> sudo apt install -y python3-venv python3-pip python3-distutils
> rm -rf .venv && bash install.sh
> ```

**Always do step 1 first.** `--monitor` should print live `net / import / export` watt
readings. If it shows nothing, the meter multicast isn't reaching the tool — see
[Troubleshooting](#troubleshooting) below (usually a firewall or wrong NIC).

### What `default_config.json` does

| | |
|---|---|
| Inverter | `192.168.101.28:503`, unit **1**, register **1111** |
| Register | **S16** (signed, 1 register), `pct_scale 0.1` → 100% = raw 1000 |
| Setpoint | **percent mode**, **10%** → raw 100 (reset to 0 after each trial) |
| Rated power | inferred from the meter reaction |
| Meter | SMA Speedwire on the **default network interface** |
| Trials | 5 |

Override anything per run without editing the file, e.g. the other direction or a
bigger swing:

```bash
./run.sh --config default_config.json --target-percent -10
./run.sh --config default_config.json --target-percent 25 --trials 3
```

### If it writes OK but the meter never moves

Many inverters ignore a power setpoint until an **external-control-enable** register is
set first — this tool only writes register 1111. Also check the read-back line: it shows
`MISMATCH` if the S16 type / `pct_scale` is wrong for register 1111. See
[Important caveats](#important-caveats).

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

Needs Python **3.7+** and nothing else — the tool ships a **built-in Modbus TCP
client**, so it runs on the standard library alone. `pymodbus` is optional and used
automatically if it's installed; `install.sh` installs it when the system allows
(pymodbus 3.x on Python 3.8+, 2.5.3 on Python 3.7). If pip can't be set up on the
device, the tool still runs on the built-in client.

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

## Picking the right meter (multiple on the network)

If several SMA meters broadcast on the network, **monitor mode lists each one** as it
appears, with the exact flag to lock onto it:

```bash
./run.sh --monitor
```

```
>>> meter detected: 192.168.101.42   serial 1234567890   ->  filter with:  --meter-src-ip 192.168.101.42  (or --meter-serial 1234567890)
  net      210.5 W   import    210.5 W   export      0.0 W   serial 1234567890   from 192.168.101.42
```

Then pin the test to that meter by IP:

```bash
./run.sh --config default_config.json --meter-src-ip 192.168.101.42
```

or set `"meter_src_ip": "192.168.101.42"` in your config. You can filter by **serial**
instead (`--meter-serial` / `"meter_serial"`), which survives DHCP IP changes. If you
run a test with multiple meters present and no filter set, the tool **warns you** — an
unfiltered baseline and detection would mix readings from different meters.

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

## Percent-of-rated-power inverters

Some inverters take the setpoint as a **% of rated power** instead of watts (e.g.
SunSpec `WMaxLimPct`, many SMA models). You can't know the watts up front, so this tool
commands the percent, waits for the reaction, then **infers the rated power** from how
far the meter moved:

```
rated_power = |settled change at the meter| ÷ (percent ÷ 100)
```

```
python inverter_reaction_tester.py \
    --inv-host 192.168.1.20 --inv-unit 3 --inv-register 40023 \
    --mode percent --datatype U16 --pct-scale 0.01 \
    --target-percent -50 --meter-iface 192.168.1.50 --trials 5
```

What happens per session:

1. **First reaction** — rated power is unknown, so the reaction is detected when the
   meter moves past an absolute floor above its noise (`--detect-watts`, `0` = auto:
   `max(50, 4× noise)`). Reaction time is measured at that crossing.
2. **Settle & infer** — after reacting, the tool waits for the meter to settle
   (`--infer-timeout`, `--settle-samples`, `--settle-band-frac`) and back-calculates the
   rated power. It's printed per trial and aggregated in the summary.
3. **Later trials** — now that rated power is known (running mean), detection switches
   to `--fraction` of the *expected* change, exactly like watt mode. Pass a known
   `--rated-w` to get that robustness from the very first trial.

`--pct-scale` is the percent equivalent of `--scale`: **percent per register count**
(`percent = raw × pct-scale`). So a register in whole `%` → `--pct-scale 1.0`; a
register in `0.01 %` (write `5000` for 50 %) → `--pct-scale 0.01`. See
`example_config_percent.json`.

## Key parameters (the reusable bits)

| Option | Meaning |
| --- | --- |
| `--inv-host` / `--inv-port` / `--inv-unit` | Inverter Modbus TCP target (SMA inverters often use unit id **3**) |
| `--inv-register` | Holding-register address of the power setpoint |
| `--mode` | `watt` (default, uses `--target-w`) or `percent` (uses `--target-percent`, infers rated power) |
| `--datatype` | `U16` / `S16` / `U32` / `S32` — match your inverter's register |
| `--word-order` | `big` (high word first, most common) or `little`, for 32-bit values |
| `--scale` | **[watt mode] Watts per register count.** `watts = raw × scale`. `1.0` = register in W, `0.1` = 0.1 W, `10.0` = 10 W |
| `--pct-scale` | **[percent mode] Percent per register count.** `percent = raw × pct-scale`. `1.0` = register in %, `0.01` = 0.01 % |
| `--target-w` | [watt mode] Setpoint in watts (sign per your inverter; e.g. negative = discharge) |
| `--target-percent` | [percent mode] Setpoint as % of rated power (sign per your inverter) |
| `--rated-w` | [percent mode] Known rated power (W) to seed detection; otherwise inferred from the first reaction |
| `--detect-watts` | [percent mode] Meter change (W) counting as "reacted" before rated power is known (`0` = auto) |
| `--fraction` | Meter must move by this fraction of the expected change to count as reacted (`0.5` = 50%) |
| `--infer-timeout` / `--settle-samples` / `--settle-band-frac` | Settle detection used to infer rated power (percent mode) / report achieved power |
| `--measure-settled` | [watt mode] Also report steady-state achieved power (always on in percent mode) |
| `--meter-iface` | Local NIC IP to receive the multicast on |
| `--meter-src-ip` | **Filter to one meter by its IP** — use when several meters are on the network |
| `--meter-serial` | Filter to one meter by serial number (survives DHCP IP changes) |
| `--trials` / `--settle` / `--warmup` | Repeat measurements, settle time between, baseline window |
| `--confirm` | Consecutive crossing samples required (>1 rejects noise spikes) |
| `--no-reset` / `--reset-value` | By default the setpoint is written back to `0` (W or %) after each trial |

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
