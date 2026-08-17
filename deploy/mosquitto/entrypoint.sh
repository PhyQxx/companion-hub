#!/bin/sh
set -eu

mkdir -p /mosquitto/config-runtime
mosquitto_passwd -b -c /mosquitto/config-runtime/passwords "${MQTT_USERNAME}" "${MQTT_PASSWORD}"
chown -R mosquitto:mosquitto /mosquitto/config-runtime
chmod 640 /mosquitto/config-runtime/passwords
exec /usr/sbin/mosquitto -c /mosquitto/config/mosquitto.conf
