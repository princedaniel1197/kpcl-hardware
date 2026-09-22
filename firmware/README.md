# firmware — ESP32 bench rig (Stage 10)

```bash
cd firmware
pio run -t upload && pio device monitor
```

Set `WIFI_SSID` and `WIFI_PASSWORD` in `src/main.cpp` first.

**This firmware has never been compiled or flashed.** It is written from the
component specifications, not from a working board. `fat/records/stage-10-hardware-rig.md`
records what has and has not been measured.

- `REGISTER_MAP.md` — the contract between the firmware and the bridge. Read it
  before changing either.
- `rig_stub.py` — a stand-in Modbus server implementing that register map, for
  testing the bridge without the hardware. It is **not** a substitute for the
  rig, and Stage 10's acceptance test is not satisfied by running against it.

The bridge that reads this rig and republishes it over OPC UA is `sim/bridge.py`
— in `sim/` because it is a source of data, and because putting it in
`collector/` would place a Modbus write capability inside the package required
to have no write path at all.

Wiring notes that matter: the DS18B20 data line needs a **4.7 kΩ pull-up to
3.3 V** and both probes share it by address; all fans sit downstream of the
ACS712 so it measures total load; fan starts are staggered by 500 ms in firmware
because three starting together draw about 1.2 A against a 1 A supply.
