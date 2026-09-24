# firmware — ESP32 bench rig (Stage 10)

```bash
cd firmware
cp src/secrets.h.example src/secrets.h      # WiFi credentials; never committed
pio run -e tcp -t upload && pio device monitor     # Modbus TCP over WiFi
pio run -e rtu -t upload && pio device monitor     # Modbus RTU over RS-485
```

Then set the two DS18B20 ROM addresses in `src/rig_config.h`: the serial monitor
lists every probe on the bus every ten seconds. Until they are set, both
temperatures report invalid (BadConfigurationError downstream) — deliberately,
because the alternative is guessing which probe is the hub.

**Flashed and run on the bench, 24 September 2026** — the RS-485 build, over
USB, against the real probes, accelerometer and ACS712 (12 V not yet connected;
the WiFi build not yet flashed). First compiled 23 September (PlatformIO 6.2.0,
espressif32 7.1.3, library versions pinned in `platformio.ini`); until then its
eModbus dependency line named a package that does not exist in the PlatformIO
registry, and nothing had caught it. The bench found an MPU-6500 where an
MPU-6050 was expected, an ADC reading 225 mV high at 2.5 V, and a supply
published as 0.57 V with nothing connected. What has and has not been measured
is in `fat/records/stage-10-hardware-rig.md`.

| File | |
|---|---|
| `REGISTER_MAP.md` | the contract between the firmware and the bridge. Read it before changing either |
| `src/main.cpp` | the firmware |
| `src/rig_config.h` | this rig: relay polarity, probe ROM addresses, pins, scaling |
| `src/secrets.h.example` | template for the WiFi credentials (`secrets.h` is git-ignored) |
| `rig_stub.py` | a stand-in Modbus server implementing the register map, for testing the bridge without hardware |

`rig_stub.py` is **not** a substitute for the rig, and Stage 10's acceptance test
is not satisfied by running against it.

The bridge that reads this rig and republishes it over OPC UA is `sim/bridge.py`
— in `sim/` because it is a source of data, and because putting it in
`collector/` would place an OPC UA write inside the package required to have no
write path at all. Run it with the simulator:

```bash
.venv/bin/python -m sim --modbus-host <rig-ip>                          # TCP
.venv/bin/python -m sim --modbus-serial /dev/tty.usbserial-XXXX         # RTU, 19200 8N1
```

Wiring is in `REGISTER_MAP.md`. The points that matter: the relay modules are
**active-low**; the DS18B20 data line needs a **4.7 kΩ pull-up to 3.3 V** and both
probes share it, identified by address; every fan sits downstream of the ACS712
and behind a relay; fan starts are staggered by 500 ms because three starting
together draw about 1.2 A against a 1 A supply.

**Until the relays are fitted** (`RELAYS_FITTED = false`), the fans run whenever
12 V is on, so the current sensor can only be zeroed before it is: **plug in
USB first, then 12 V.** A boot with 12 V already on leaves the current invalid
and prints a warning on the serial monitor. Unplug 12 V and reset the ESP32.
