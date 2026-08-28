# ESP32 + LD2410 presence radar

This integration publishes only the stable presence boolean required by Aria. Moving/still
distance and energy-gate values remain on the device and are not sent to the Hub.

## Wiring

| LD2410 | ESP32 DevKit V1 |
|---|---|
| VCC | 5V/VIN |
| GND | GND |
| TX | GPIO16 (RX2) |
| RX | GPIO17 (TX2) |

Power off both boards before wiring. Confirm the pinout printed on the exact LD2410 variant;
do not feed its 5V supply into a 3.3V pin.

## Provisioning

1. Copy `secrets.example.yaml` to `secrets.yaml` in this directory and replace every value.
2. Keep `device_id` in `ld2410-presence.yaml` identical to `MQTT_DEVICE_ID` in the Hub `.env`.
3. Set the device MQTT username/password to `MQTT_DEVICE_USERNAME` and
   `MQTT_DEVICE_PASSWORD`. The bundled Mosquitto ACL permits this identity to publish only
   `hub/devices/<device_id>/telemetry`.
4. Validate and flash:

   ```bash
   esphome config integrations/esp32_ld2410/ld2410-presence.yaml
   esphome run integrations/esp32_ld2410/ld2410-presence.yaml
   ```

5. Enable the Hub subscriber with `ARIA_MQTT_ENABLED=true` and restart the Compose stack.

## Wire contract

The retained QoS 1 payload is:

```json
{
  "sensor_type": "presence",
  "value": true,
  "timestamp": "2026-08-28T10:15:30+0800"
}
```

The first retained value after Hub startup establishes a baseline and never creates a proactive
message. A later state change must remain stable for five seconds in the Hub before it becomes a
`presence.changed` semantic event. Raw MQTT telemetry remains an L3 in-memory signal; only the
derived L1 semantic event can enter cognition and proactive delivery.

## Acceptance run

- Walk into range after an `absent` baseline; verify one `presence.changed` audit and at most one
  proactive delivery.
- Move near the detection boundary for five minutes; verify short bounces do not create events.
- Restart the ESP32 and Hub; retained baseline messages must not announce a false arrival.
- Disconnect Wi-Fi and power; stale readings may be shown as unavailable but must not trigger a
  delayed proactive message after reconnection.
- Keep the device running for seven days and record false positives, false negatives, quiet-hours
  violations, duplicate deliveries, and reconnect failures.

