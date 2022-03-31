"""
   Copyright 2021 Lexi Robinson

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.
"""
from __future__ import annotations

import asyncio
import signal
from pprint import pprint
from typing import Any, Dict, List, Optional, TypedDict

import yaml
from gmqtt import Client as MQTTClient
from pi1wire import OneWireInterface, Pi1Wire


class MQTTConf(TypedDict):
    broker: str
    port: int
    client_id: Optional[str]
    discovery: bool
    discovery_retain: bool
    discovery_prefix: str
    discovery_device: bool
    discovery_device_name: str
    topic_prefix: str
    username: Optional[str]
    password: Optional[str]


class SensorConf(TypedDict):
    name: str
    address: int
    update_interval: Optional[int]


DEFAULT_MQTT_CONFIG: MQTTConf = {
    "broker": "",
    "port": 1883,
    "client_id": None,
    "discovery": True,
    "discovery_retain": True,
    "discovery_prefix": "homeassistant",
    "discovery_device": True,
    "discovery_device_name": "Mystery Pi",
    "topic_prefix": "temps",
    "username": None,
    "password": None,
}
DEFAULT_UPDATE_INTERVAL = 60


STOP = asyncio.Event()

loop = asyncio.get_event_loop()
# I don't like this library but I can't be bothered to write my own
p1w = Pi1Wire()


def _on_signal(*args):
    STOP.set()


loop.add_signal_handler(signal.SIGINT, _on_signal)
loop.add_signal_handler(signal.SIGTERM, _on_signal)


def getserial() -> str:
    with open("/proc/cpuinfo", "r") as f:
        for line in f:
            if line[0:6] == "Serial":
                return line[10:26]

    return "0000000000000000"


class Sensor:
    _sensor: OneWireInterface
    _name: str
    _update_interval: int
    _client: MQTTClient

    def __init__(self, config: SensorConf) -> None:
        if "name" not in config:
            raise ValueError("Missing required 'name' parameter in sensor config!")
        elif "address" not in config:
            raise ValueError("Missing required 'address' parameter in sensor config!")
        self._name = config["name"]
        self._update_interval = (
            config.get("update_interval", None) or DEFAULT_UPDATE_INTERVAL
        )
        address = config["address"]
        self._sensor = p1w.find(f"{address:x}")

    def _create_topic(self, prefix: str, postfix: str) -> str:
        return f"{prefix}/sensor/{self._sensor.mac_address}/{postfix}"

    def _create_autodiscovery(self, config: MQTTConf) -> dict:
        packet: Dict[str, Any] = {
            "device_class": "temperature",
            "name": self._name,
            "unique_id": self._sensor.mac_address,
            "retain": config["discovery_retain"],
            "state_topic": self._create_topic(config["topic_prefix"], "state"),
            "unit_of_measurement": "°C",
        }

        if config["discovery_device"]:
            packet["device"] = {
                "name": config["discovery_device_name"],
                "ids": getserial(),
                "mdl": "Lexi's 1wire",
                "mf": "Lexi Robinson",
            }

        return packet

    def _publish_state(self, client: MQTTClient, config: MQTTConf) -> None:
        topic = self._create_topic(config["topic_prefix"], "state")
        packet = self._sensor.get_temperature()
        client.publish(topic, packet)
        loop.call_later(self._update_interval, self._publish_state, client, config)

    def run(self, client: MQTTClient, config: MQTTConf) -> None:
        if config["discovery"]:
            topic = self._create_topic(config["discovery_prefix"], "config")
            packet = self._create_autodiscovery(config)
            client.publish(topic, packet)
        self._publish_state(client, config)


def on_connect(client, flags, rc, properties):
    print("Connected", flags, rc, properties)


def on_disconnect(client, packet, exc=None):
    print("Disconnected")


def _load_config() -> dict:
    with open("configuration.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _get_mqtt_client(mqtt_conf: MQTTConf) -> MQTTClient:
    if not mqtt_conf["broker"]:
        raise ValueError("Broker not configured!")

    client = MQTTClient(mqtt_conf["client_id"])
    if mqtt_conf["username"] is not None:
        client.set_auth_credentials(mqtt_conf["username"], mqtt_conf["password"])

    # wip
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect

    return client


def _print_sensor_help() -> None:
    print("No sensors configured!")
    print("Detected Sensors:")
    for sensor in p1w.find_all_sensors():
        address = int(sensor.mac_address, 16)
        temp = sensor.get_temperature()
        print(f"0x{address:X}: {temp:.1f}°C")


async def _get_sensor(config: SensorConf) -> Sensor:
    """This is a fairly slow function so let's make it async!"""
    return Sensor(config)


async def _get_sensors(sensor_configs: List[SensorConf]) -> List[Sensor]:
    if len(sensor_configs) == 0:
        _print_sensor_help()

    return await asyncio.gather(*(_get_sensor(config) for config in sensor_configs))


async def main():
    config = _load_config()
    pprint(config)
    mqtt_conf = DEFAULT_MQTT_CONFIG
    mqtt_conf.update(config.get("mqtt", {}))
    pprint(mqtt_conf)

    sensors = await _get_sensors(config.get("sensor", []))
    if len(sensors) == 0:
        return

    # client.on_message = on_message
    # client.on_subscribe = on_subscribe
    client = _get_mqtt_client(mqtt_conf)

    await client.connect(mqtt_conf["broker"])

    for sensor in sensors:
        loop.call_soon(sensor.run, client, mqtt_conf)

    await STOP.wait()
    await client.disconnect()


loop.run_until_complete(main())
