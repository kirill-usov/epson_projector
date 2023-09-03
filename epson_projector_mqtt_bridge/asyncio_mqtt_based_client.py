import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from random import randrange
from asyncio_mqtt import Client, MqttError
import json
import logging
import os

import epson_projector as epson
from epson_projector.const import (
    EPSON_KEY_COMMANDS, 
    EPSON_CONFIG_RANGES,
    EPSON_OPTIONS,
    EPSON_READOUTS,
    PWR_OFF_STATE,
    PWR_ON_STATE,
)

BASE_TOPIC = os.environ.get('MQTT_BASE_TOPIC', 'epson')
MQTT_HOST = os.environ.get('MQTT_HOST')
MQTT_USERNAME = os.environ.get('MQTT_USERNAME', None)
MQTT_PASSWORD = os.environ.get('MQTT_PASSWORD', None)
EPSON_HOST = os.environ.get('EPSON_IP')
EPSON_NAME = os.environ.get('EPSON_NAME', EPSON_HOST)

LOGGING_LEVEL = str.upper(os.environ.get('LOGGING_LEVEL', 'DEBUG'))

if not MQTT_HOST or not EPSON_HOST:
    raise Exception('Missing environment config! Please make sure MQTT_HOST and EPSON_IP environment variables are set.')

_LOGGER = logging.getLogger(__name__)

logging.getLogger("asyncio").setLevel(LOGGING_LEVEL)

console_handler = logging.StreamHandler()
console_handler.setFormatter(   
    logging.Formatter("%(asctime)s - [%(threadName)s] - %(name)s - %(levelname)s - %(message)s")
)
_LOGGER.addHandler(console_handler)
_LOGGER.setLevel(LOGGING_LEVEL)

async def epson_projector_bridge():
    async with AsyncExitStack() as stack:
        tasks = set()
        stack.push_async_callback(cancel_tasks, tasks)

        client = Client(
            hostname=MQTT_HOST,
            username=MQTT_USERNAME,
            password=MQTT_PASSWORD
        )
        await stack.enter_async_context(client)

        projector = epson.Projector(host=EPSON_HOST, type='tcp')

        await publish_homeassistant_discovery_config(projector, client)

        manager = client.filtered_messages(f"{BASE_TOPIC}/command/#")
        messages = await stack.enter_async_context(manager)
        task = asyncio.create_task(process_commands(messages, projector, client))
        tasks.add(task)

        # Subscribe to topic(s)
        # 🤔 Note that we subscribe *after* starting the message
        # loggers. Otherwise, we may miss retained messages.
        await client.subscribe(f"{BASE_TOPIC}/command/#")

        task =  asyncio.create_task(poll_projector_status(client, projector))

        tasks.add(task)

        # Wait for everything to complete (or fail due to, e.g., network
        # errors)
        await asyncio.gather(*tasks)


async def poll_projector_status(client, projector):
    while True:
        try:
            powerStatus = await projector.get_power()
            if powerStatus == PWR_OFF_STATE:
                await publish_message(client, f"{BASE_TOPIC}/state/{EPSON_NAME}_power", "OFF")

            if powerStatus == PWR_ON_STATE:
                # These aren't mutally exclusive, during initial startup may give weird codes which then breaks fetching
                # the rest of the config values -- only fetch them if we know it's on
                await publish_message(client, f"{BASE_TOPIC}/state/{EPSON_NAME}_power", "ON")
                await get_all_config_values(client, projector)


        except Exception as inst:
            _LOGGER.warning(f"Exception thrown: {inst}")

        await asyncio.sleep(10)


async def get_all_config_values(client, projector):
    for key_name in EPSON_CONFIG_RANGES:
        try:
            value = await projector.read_config_value(key_name)

            await publish_message(client, f"{BASE_TOPIC}/state/{key_name}", int(value))
        except Exception as inst:
            _LOGGER.warning(f"Exception thrown: {inst}")

    for key_name in EPSON_READOUTS:
        try:
            value = await projector.read_config_value(key_name)

            await publish_message(client, f"{BASE_TOPIC}/state/{key_name}", int(value))
        except Exception as inst:
            _LOGGER.warning(f"Exception thrown: {inst}")

    await get_all_option_values(client, projector)


async def get_all_option_values(client, projector):
    for key_name, config in EPSON_OPTIONS.items():
        try:
            raw_value = await projector.get_property(config['epson_command'])
            for option in config['options']:
                if raw_value == option[2]:
                    await publish_message(client, f"{BASE_TOPIC}/state/{key_name}", option[0])
                    break
        except Exception as inst:
            _LOGGER.warning(f"Exception thrown: {inst}")


async def publish_message(client, topic, message):
    _LOGGER.debug(f"Publishing to MQTT: {topic} -- {message}\n")
    await client.publish(topic, message, retain=True)


async def process_commands(messages, projector, client):
    async for message in messages:
        # 🤔 Note that we assume that the message paylod is an
        # UTF8-encoded string (hence the `bytes.decode` call).
        command = message.topic[len(f"{BASE_TOPIC}/command/"):]
        value = message.payload.decode()

        _LOGGER.info(f"Execute command '{command}' with value of '{value}'")
        try:
            if command in EPSON_CONFIG_RANGES:
                await projector.send_config_value(command, value)

                # new_value = await projector.read_config_value(command)
                # await publish_message(client, f"{BASE_TOPIC}/state/{command}", int(new_value))

            elif command in EPSON_KEY_COMMANDS:
                await projector.send_command(command)
            elif command in EPSON_OPTIONS:
                for option in EPSON_OPTIONS[command]['options']:
                    if value == option[0]:
                        await projector.send_command(option[1])
                        break
            elif command == "power":
                if value == 'OFF':
                    await projector.send_command("PWR OFF")
                else:
                    await projector.send_command("PWR ON")
            else:
                _LOGGER.error(f"Unknown command {command}")
        except Exception as inst:
            _LOGGER.warning(f"---- Exception thrown: {inst}")


async def publish_homeassistant_discovery_config(projector, client):
    await publish_message(client, f"homeassistant/switch/{BASE_TOPIC}/{EPSON_NAME}_power/config",
                          json.dumps({
                              "name": f"{EPSON_NAME} - Epson Projector Power",
                              "unique_id": f"{EPSON_NAME}_pwr",
                              "command_topic": f"{BASE_TOPIC}/command/power",
                              "state_topic": f"{BASE_TOPIC}/state/{EPSON_NAME}_power"
                          })
                          )

    for key_name, config in EPSON_CONFIG_RANGES.items():
        await publish_message(client,
                              f"homeassistant/number/{BASE_TOPIC}/{EPSON_NAME}_{key_name.lower()}/config",
                              json.dumps({
                                  "name": f"{EPSON_NAME} - {config['human_name']}",
                                  "unique_id": f"{EPSON_NAME}_{key_name.lower()}",
                                  "command_topic": f"{BASE_TOPIC}/command/{key_name}",
                                  "state_topic": f"{BASE_TOPIC}/state/{key_name}",
                                  "min": min(config['humanized_range']),
                                  "max": max(config['humanized_range']),
                                  "step": (1, 5)[config['value_translator'] == '50-100'],
                                  "unit_of_measurement": ('', '%')[config['value_translator'] == '50-100'],
                                  "availability_topic": f"{BASE_TOPIC}/state/{EPSON_NAME}_power",
                                  "payload_available": "ON",
                                  "payload_not_available": "OFF",
                              })
                              )

    for key_name, config in EPSON_OPTIONS.items():
        await publish_message(client,
                              f"homeassistant/select/{BASE_TOPIC}/{EPSON_NAME}_{key_name.lower()}/config",
                              json.dumps({
                                  "name": f"{EPSON_NAME} - {config['human_name']}",
                                  "unique_id": f"{EPSON_NAME}_{key_name.lower()}",
                                  "command_topic": f"{BASE_TOPIC}/command/{key_name}",
                                  "state_topic": f"{BASE_TOPIC}/state/{EPSON_NAME}_{key_name}",
                                  "options": [
                                      x[0] for x in config['options']
                                  ],
                                  "availability_topic": f"{BASE_TOPIC}/state/{EPSON_NAME}_power",
                                  "payload_available": "ON",
                                  "payload_not_available": "OFF",
                              })
                              )

    for i in range(1, 11):
        await publish_message(client,
                              f"homeassistant/button/{BASE_TOPIC}/{EPSON_NAME}_lens_memory_{i}/config",
                              json.dumps({
                                  "name": f"{EPSON_NAME} - Load Lens Memory #{i}",
                                  "unique_id": f"{EPSON_NAME}_lens_memory_{i}",
                                  "command_topic": f"{BASE_TOPIC}/command/LENS_MEMORY_{i}",
                                  "availability_topic": f"{BASE_TOPIC}/state/{EPSON_NAME}_power",
                                  "payload_available": "ON",
                                  "payload_not_available": "OFF",
                              })
                              )

        await publish_message(client,
                              f"homeassistant/button/{BASE_TOPIC}/{EPSON_NAME}_image_memory_{i}/config",
                              json.dumps({
                                  "name": f"{EPSON_NAME} - Load Image Memory #{i}",
                                  "unique_id": f"{EPSON_NAME}_image_memory_{i}",
                                  "command_topic": f"{BASE_TOPIC}/command/MEMORY_{i}",
                                  "availability_topic": f"{BASE_TOPIC}/state/{EPSON_NAME}_power",
                                  "payload_available": "ON",
                                  "payload_not_available": "OFF",
                              })
                              )

    for key_name, config in EPSON_READOUTS.items():
        await publish_message(client,
                              f"homeassistant/sensor/{BASE_TOPIC}/{EPSON_NAME}_{key_name.lower()}/config",
                              json.dumps({
                                  "name": f"{EPSON_NAME} - {config['human_name']}",
                                  "unique_id": f"{EPSON_NAME}_{key_name.lower()}",
                                  "state_topic": f"{BASE_TOPIC}/state/{key_name}",
                                  "availability_topic": f"{BASE_TOPIC}/state/{EPSON_NAME}_power",
                                  "payload_available": "ON",
                                  "payload_not_available": "OFF",
                              })
                              )


async def cancel_tasks(tasks):
    for task in tasks:
        if task.done():
            continue
        try:
            task.cancel()
            await task
        except asyncio.CancelledError:
            pass


async def main():
    # Run the epson_projector_bridge indefinitely. Reconnect automatically
    # if the connection is lost.
    reconnect_interval = 3  # [seconds]
    while True:
        try:
            await epson_projector_bridge()
        except MqttError as error:
            _LOGGER.error(f'{error} | Reconnecting in {reconnect_interval} seconds.')
        finally:
            await asyncio.sleep(reconnect_interval)


asyncio.run(main(), debug=True)