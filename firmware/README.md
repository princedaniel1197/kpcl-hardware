# firmware — ESP32 (Stage 10)

C++ using the eModbus library, exposing a Modbus TCP server over the bench rig:
ACS712 current, MPU-6050 vibration RMS, DS18B20 temperature, digital run/stop.

A failed DS18B20 read is signalled distinctly. It never returns zero or the last
value.

Empty until Stage 10.
