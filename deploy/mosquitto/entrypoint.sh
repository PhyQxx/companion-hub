#!/bin/sh
set -eu

mkdir -p /mosquitto/config-runtime
case "${MQTT_USERNAME}" in
  *[!A-Za-z0-9_.-]*|'') echo "invalid MQTT_USERNAME" >&2; exit 1 ;;
esac
case "${MQTT_DEVICE_USERNAME}" in
  *[!A-Za-z0-9_.-]*|'') echo "invalid MQTT_DEVICE_USERNAME" >&2; exit 1 ;;
esac
case "${MQTT_DEVICE_ID}" in
  *[!A-Za-z0-9_-]*|'') echo "invalid MQTT_DEVICE_ID" >&2; exit 1 ;;
esac
mosquitto_passwd -b -c /mosquitto/config-runtime/passwords "${MQTT_USERNAME}" "${MQTT_PASSWORD}"
mosquitto_passwd -b /mosquitto/config-runtime/passwords "${MQTT_DEVICE_USERNAME}" "${MQTT_DEVICE_PASSWORD}"
{
  echo "user ${MQTT_USERNAME}"
  echo "topic read hub/devices/+/telemetry"
  echo 'topic read $SYS/broker/version'
  echo "user ${MQTT_DEVICE_USERNAME}"
  echo "topic write hub/devices/${MQTT_DEVICE_ID}/telemetry"
} > /mosquitto/config-runtime/acl
chown -R mosquitto:mosquitto /mosquitto/config-runtime
chmod 640 /mosquitto/config-runtime/passwords /mosquitto/config-runtime/acl
exec /usr/sbin/mosquitto -c /mosquitto/config/mosquitto.conf
