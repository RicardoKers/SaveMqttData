# SaveMqttData Version 0.9.2
#
# This program collects messages from specified MQTT topics, stores them per topic
# in JSON files, applies data retention policies, and optionally provides both a
# WebSocket and a REST API to query the collected data. It also uses Argon2-based
# hashing to protect passkeys for client authentication.

import json
import paho.mqtt.client as mqtt
from datetime import datetime, timedelta, timezone
import asyncio
from threading import Lock
from random import randint
from websockets import serve, exceptions as ws_exceptions
import logging
import os
import psutil
from argon2 import PasswordHasher, exceptions as argon2_exceptions  # Argon2 for password (hash) verification
from aiohttp import web  # AIOHTTP for the REST server

# Program version constant
VERSION = "0.9.2"

# Configure Python logging (only changes log messages, not the logic)
logging.basicConfig(level=logging.ERROR, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

# Global locks:
#   file_lock - ensures exclusive file access (reading/writing JSON)
#   userdata_lock - protects shared dictionaries (e.g. last_values) in memory
file_lock = Lock()
userdata_lock = Lock()

# Used to calculate system uptime (time since the program started)
start_time_utc = datetime.now(timezone.utc)

# Counts how many MQTT messages have been received
packets_count = 0

# Global reference to the userdata dictionary for WebSocket usage,
# because websockets.ServerConnection objects do not hold an 'app' attribute
GLOBAL_USERDATA = None

def load_config(file_path):
    """
    Loads JSON configuration from a specified file path.
    Returns a dict on success, or None if the file is missing/invalid.
    """
    try:
        with open(file_path, 'r') as file:
            return json.load(file)
    except FileNotFoundError:
        log.error(f"Config file '{file_path}' not found.")
        return None
    except json.JSONDecodeError:
        log.error(f"Error decoding JSON in '{file_path}'.")
        return None

def get_topic_file_name(topic):
    """
    Returns a filename to store data for a given MQTT topic,
    by replacing slashes ('/') with dashes ('-'). For example:
    '/my/temperature/' -> 'Data_-my-temperature-.json'
    """
    return "Data_" + topic.replace("/", "-") + ".json"

def save_data(_unused_file_path, data, retention_days):
    """
    Saves incoming data (a dictionary {topic: value}) into per-topic JSON files.
    Applies a retention policy based on 'retention_days' to remove older records.

    _unused_file_path: unused parameter to maintain compatibility with older code.
    data: dict mapping MQTT topic -> the new value to store
    retention_days: number of days to keep data
    """
    try:
        with file_lock:
            for topic, value in data.items():
                topic_file = get_topic_file_name(topic)
                try:
                    with open(topic_file, 'r') as f:
                        try:
                            file_data = json.load(f)
                        except json.JSONDecodeError:
                            file_data = []
                except FileNotFoundError:
                    file_data = []

                # Remove entries older than the retention period
                cutoff_time = datetime.now(timezone.utc) - timedelta(days=retention_days)
                def in_range(entry):
                    entry_time = datetime.fromisoformat(entry["T"]).astimezone(timezone.utc)
                    return entry_time >= cutoff_time

                file_data = [entry for entry in file_data if in_range(entry)]

                # Append the new record with the current timestamp
                timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                file_data.append({"T": timestamp, "V": value})

                # Write the updated data back to the JSON file
                with open(topic_file, 'w') as f:
                    json.dump(file_data, f, indent=4)

                log.info(f"Data saved to {topic_file}.")
    except Exception:
        log.exception("Unhandled exception while saving data.")

def passkey_valid(passkey):
    """
    Validates the provided 'passkey' by comparing it with the Argon2 hash
    stored in 'config.json'. Returns True if valid, False otherwise.
    """
    config = load_config("config.json")
    if not config:
        return False

    passkey_hash = config.get("passkeyHash", "")
    if not passkey or not passkey_hash:
        return False

    ph = PasswordHasher()
    try:
        ph.verify(passkey_hash, passkey)
        return True
    except (argon2_exceptions.VerifyMismatchError, argon2_exceptions.VerificationError):
        return False
    except Exception:
        return False

def get_topic_info(topics, topic_settings):
    """
    Gathers and returns detailed information about each topic in 'topics':
      - name: the topic string
      - oldest/newest_timestamp: the first and last saved data record
      - total_samples: how many records are stored
      - sampling_interval: time in seconds for downsampling
      - retention_days: how long data is kept
      - file_size_bytes: size of the per-topic JSON file on disk
    """
    info_list = []
    for t in topics:
        topic_file = get_topic_file_name(t)
        setting = topic_settings[t]
        si = setting["sampling_interval"]
        rd = setting["retention_days"]

        oldest_timestamp = None
        newest_timestamp = None
        total_samples = 0
        file_size = 0

        # Protect file reading with file_lock
        with file_lock:
            if os.path.exists(topic_file):
                file_size = os.path.getsize(topic_file)
                try:
                    with open(topic_file, 'r') as f:
                        data_list = json.load(f)
                except (json.JSONDecodeError, FileNotFoundError):
                    data_list = []
            else:
                data_list = []

        if data_list:
            total_samples = len(data_list)
            # Sort by the "T" field so we can identify oldest/newest records
            data_list.sort(key=lambda x: x["T"])
            oldest_timestamp = data_list[0]["T"]
            newest_timestamp = data_list[-1]["T"]

        info_list.append({
            "name": t,
            "oldest_timestamp": oldest_timestamp,
            "newest_timestamp": newest_timestamp,
            "total_samples": total_samples,
            "sampling_interval": si,
            "retention_days": rd,
            "file_size_bytes": file_size
        })
    return info_list

def on_connect(client, userdata, flags, rc):
    """
    Callback invoked by paho-mqtt upon connecting to the MQTT broker.
    If rc == 0, the connection is successful, so we subscribe to each topic.
    Otherwise, logs the error code.
    """
    if rc == 0:
        log.info("Connected to MQTT Broker!")
        for topic in userdata.get("topics", []):
            client.subscribe(topic)
            log.info(f"Subscribed to topic: {topic}")
    else:
        log.error(f"Failed to connect, return code {rc}")

def on_message(client, userdata, msg):
    """
    Callback invoked whenever an MQTT message arrives.
    - Decodes the payload
    - Checks sampling_interval for the topic
      -> if 0, saves immediately
      -> if >0, stores the value in memory and is later saved by check_intervals
    """
    global packets_count
    try:
        payload = msg.payload.decode()
        topic = msg.topic

        log.debug(f"Message received on topic {topic}: {payload}")

        with userdata_lock:
            userdata['last_values'][topic] = payload
            packets_count += 1

            if topic not in userdata.get("topics", []):
                log.warning(f"Topic {topic} not found in config topics.")
                return

            topic_settings = userdata.get("topic_settings", {})
            setting = topic_settings.get(topic, None)
            if setting is None:
                log.warning(f"No settings found for topic {topic}.")
                return

            si = setting["sampling_interval"]
            rd = setting["retention_days"]

            # If sampling_interval == 0, save immediately
            if si == 0:
                save_data(None, {topic: payload}, rd)
                log.info(f"Saved data immediately for topic '{topic}' (sampling_interval=0).")

    except Exception:
        log.exception("Unhandled exception in on_message")

async def check_intervals(userdata):
    """
    Periodic loop (runs asynchronously) to check if the sampling interval has
    elapsed for each topic. If so, it saves data from memory to the per-topic file
    and resets the relevant fields.
    """
    while True:
        now = datetime.now(timezone.utc)
        with userdata_lock:
            for t in userdata.get("topics", []):
                setting = userdata["topic_settings"].get(t, None)
                if setting is None:
                    continue

                si = setting["sampling_interval"]
                rd = setting["retention_days"]
                last_save = userdata["last_saved_time"][t]

                # If si != 0, we do periodic saving
                if si != 0:
                    if (now - last_save).total_seconds() >= si:
                        value = userdata["last_values"].get(t, "--")
                        save_data(None, {t: value}, rd)
                        userdata["last_values"][t] = "--"
                        userdata["last_saved_time"][t] = now
                        log.info(f"Downsampling active: saved data for topic '{t}'.")

        # Sleep 1 second before checking again
        await asyncio.sleep(1)

def filter_data_by_time(data_list, start_dt, end_dt):
    """
    Filters data_list (list of records { "T": <iso8601str>, "V": <value> })
    by optional 'start_dt' and 'end_dt'.
    - If both are None, returns all records.
    - If only start_dt is set, returns data from there onward.
    - If only end_dt is set, returns data up to that time.
    - If both exist, returns data in [start_dt, end_dt].
    """
    if (start_dt is None) and (end_dt is None):
        return data_list

    if (start_dt is not None) and (end_dt is None):
        filtered = []
        for entry in data_list:
            try:
                entry_time = datetime.fromisoformat(entry["T"]).astimezone(timezone.utc)
                if entry_time >= start_dt:
                    filtered.append(entry)
            except Exception:
                pass
        return filtered

    if (start_dt is None) and (end_dt is not None):
        filtered = []
        for entry in data_list:
            try:
                entry_time = datetime.fromisoformat(entry["T"]).astimezone(timezone.utc)
                if entry_time <= end_dt:
                    filtered.append(entry)
            except Exception:
                pass
        return filtered

    # If both are provided
    filtered = []
    for entry in data_list:
        try:
            entry_time = datetime.fromisoformat(entry["T"]).astimezone(timezone.utc)
            if start_dt <= entry_time <= end_dt:
                filtered.append(entry)
        except Exception:
            pass
    return filtered

async def handle_request(websocket, path=None):
    """
    WebSocket server handler. It listens for JSON messages containing:
      - 'action' (e.g., 'health', 'get_data')
      - 'passkey' for Argon2-based verification
      - 'topic', 'start_time', 'end_time' for data queries
    """
    global packets_count
    try:
        async for message in websocket:
            try:
                request = json.loads(message)
                action = request.get("action")
                passkey = request.get("passkey")
                topic = request.get("topic")
                start_time_str = request.get("start_time")
                end_time_str = request.get("end_time")

                # Check passkey
                if not passkey_valid(passkey):
                    await websocket.send(json.dumps({"error": "Invalid passkey"}))
                    continue

                if action == "health":
                    # Return system info, memory/disk usage, topics_info, etc.
                    file_dir = os.path.dirname(os.path.abspath(__file__)) or '.'
                    disk_usage = psutil.disk_usage(file_dir)
                    mem = psutil.virtual_memory()

                    now_utc = datetime.now(timezone.utc)
                    with userdata_lock:
                        uptime_seconds = (now_utc - start_time_utc).total_seconds()
                        topics_copy = GLOBAL_USERDATA.get("topics", [])
                        settings_copy = GLOBAL_USERDATA.get("topic_settings", {})

                    days, remainder = divmod(uptime_seconds, 86400)
                    hours, remainder = divmod(remainder, 3600)
                    minutes, seconds = divmod(remainder, 60)
                    uptime_formatted = f"{int(days)}d, {int(hours):02}:{int(minutes):02}:{int(seconds):02}"

                    config = load_config("config.json") or {}
                    files_size_sum = 0
                    for t in config.get("topics", []):
                        topic_file = get_topic_file_name(t)
                        if os.path.exists(topic_file):
                            files_size_sum += os.path.getsize(topic_file)

                    topics_info = get_topic_info(topics_copy, settings_copy)
                    current_ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

                    info = {
                        "status": "ok",
                        "version": VERSION,
                        "timestamp": current_ts,
                        "mem_total_mb": round(mem.total / (1024 * 1024), 2),
                        "mem_used_mb": round(mem.used / (1024 * 1024), 2),
                        "mem_percent": mem.percent,
                        "disk_total_mb": round(disk_usage.total / (1024 * 1024), 2),
                        "disk_used_mb": round(disk_usage.used / (1024 * 1024), 2),
                        "disk_percent": disk_usage.percent,
                        "data_files_size_bytes": files_size_sum,
                        "packets_count": packets_count,
                        "uptime": uptime_formatted,
                        "topics_info": topics_info
                    }
                    await websocket.send(json.dumps(info))

                elif action == "get_data":
                    # Handle data retrieval requests, possibly with time filtering
                    start_dt = None
                    end_dt = None

                    if start_time_str:
                        start_dt = datetime.fromisoformat(start_time_str).astimezone(timezone.utc)
                    if end_time_str:
                        end_dt = datetime.fromisoformat(end_time_str).astimezone(timezone.utc)

                    config = load_config("config.json") or {}
                    requested_topics = [topic] if topic else config.get("topics", [])

                    if (start_dt is not None) and (end_dt is not None) and (start_dt > end_dt):
                        log.warning("start_time > end_time. Returning empty list.")
                        await websocket.send(json.dumps([]))
                        continue

                    all_entries = []
                    with file_lock:
                        for t in requested_topics:
                            topic_file = get_topic_file_name(t)
                            if not os.path.exists(topic_file):
                                continue
                            try:
                                with open(topic_file, 'r') as f:
                                    data_list = json.load(f)
                            except (json.JSONDecodeError, FileNotFoundError):
                                data_list = []

                            filtered_data = filter_data_by_time(data_list, start_dt, end_dt)
                            for entry in filtered_data:
                                all_entries.append({
                                    "timestamp": entry["T"],
                                    "topic": t,
                                    "value": entry["V"]
                                })

                    await websocket.send(json.dumps(all_entries))

                else:
                    await websocket.send(json.dumps({"error": "Unknown action"}))

            except Exception:
                log.exception("Unhandled exception in handle_request (inner)")
                await websocket.send(json.dumps({"error": "Internal error"}))

    except ws_exceptions.ConnectionClosedError as e:
        log.warning(f"WebSocket connection closed: {e}")
    except Exception:
        log.exception("Unhandled exception in handle_request (outer)")

async def run_mqtt(client):
    """
    Runs the paho-mqtt client loop in a separate thread, so it doesn't block
    the main asyncio event loop.
    """
    try:
        await asyncio.to_thread(client.loop_forever)
    except Exception:
        log.exception("Unhandled exception in run_mqtt")

async def start_websocket_server(port):
    """
    Creates and starts the WebSocket server on the specified port,
    listening for JSON-based requests (handle_request).
    """
    try:
        log.info("Starting WebSocket server...")
        server = await serve(
            handle_request,
            "0.0.0.0",
            port,
            ping_interval=30,
            ping_timeout=10
        )
        log.info(f"WebSocket server is running on ws://0.0.0.0:{port}")
        await server.wait_closed()
    except Exception:
        log.exception("Unhandled exception in start_websocket_server")

async def rest_health(request):
    """
    AIOHTTP handler for GET /health.
    Similar to the WebSocket 'health' action, returns system and topics info
    if the 'passkey' is valid, or 403 if invalid.
    """
    passkey = request.query.get("passkey", "")
    if not passkey_valid(passkey):
        return web.json_response({"error": "Invalid passkey"}, status=403)

    file_dir = os.path.dirname(os.path.abspath(__file__)) or '.'
    disk_usage = psutil.disk_usage(file_dir)
    mem = psutil.virtual_memory()

    now_utc = datetime.now(timezone.utc)
    with userdata_lock:
        uptime_seconds = (now_utc - start_time_utc).total_seconds()
        topics_copy = request.app["userdata"].get("topics", [])
        settings_copy = request.app["userdata"].get("topic_settings", {})

    days, remainder = divmod(uptime_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_formatted = f"{int(days)}d, {int(hours):02}:{int(minutes):02}:{int(seconds):02}"

    config = load_config("config.json") or {}
    files_size_sum = 0
    for t in config.get("topics", []):
        topic_file = get_topic_file_name(t)
        if os.path.exists(topic_file):
            files_size_sum += os.path.getsize(topic_file)

    topics_info = get_topic_info(topics_copy, settings_copy)
    current_ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    info = {
        "status": "ok",
        "version": VERSION,
        "timestamp": current_ts,
        "mem_total_mb": round(mem.total / (1024 * 1024), 2),
        "mem_used_mb": round(mem.used / (1024 * 1024), 2),
        "mem_percent": mem.percent,
        "disk_total_mb": round(disk_usage.total / (1024 * 1024), 2),
        "disk_used_mb": round(disk_usage.used / (1024 * 1024), 2),
        "disk_percent": disk_usage.percent,
        "data_files_size_bytes": files_size_sum,
        "packets_count": packets_count,
        "uptime": uptime_formatted,
        "topics_info": topics_info
    }
    return web.json_response(info)

async def rest_get_data(request):
    """
    AIOHTTP handler for GET /data. It retrieves the data for the requested topic(s)
    and optional time range (start_time / end_time), returning a JSON array of objects.
    """
    passkey = request.query.get("passkey", "")
    if not passkey_valid(passkey):
        return web.json_response({"error": "Invalid passkey"}, status=403)

    start_time_str = request.query.get("start_time", "")
    end_time_str = request.query.get("end_time", "")
    topic = request.query.get("topic", "")

    config = load_config("config.json") or {}

    # Convert time parameters (if provided) into datetime objects
    start_dt = None
    end_dt = None

    if start_time_str:
        start_dt = datetime.fromisoformat(start_time_str).astimezone(timezone.utc)
    if end_time_str:
        end_dt = datetime.fromisoformat(end_time_str).astimezone(timezone.utc)

    requested_topics = [topic] if topic else config.get("topics", [])

    # If both start_dt and end_dt exist but are reversed, return empty
    if (start_dt is not None) and (end_dt is not None) and (start_dt > end_dt):
        log.warning("start_time > end_time. Returning empty list.")
        return web.json_response([])

    all_entries = []
    with file_lock:
        for t in requested_topics:
            topic_file = get_topic_file_name(t)
            if not os.path.exists(topic_file):
                continue
            try:
                with open(topic_file, 'r') as f:
                    data_list = json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                data_list = []

            filtered_data = filter_data_by_time(data_list, start_dt, end_dt)
            for entry in filtered_data:
                all_entries.append({
                    "timestamp": entry["T"],
                    "topic": t,
                    "value": entry["V"]
                })

    return web.json_response(all_entries)

async def start_rest_server(port, userdata):
    """
    Sets up the REST server (AIOHTTP) on the specified port, exposing:
      - GET /health
      - GET /data
    Each requires a 'passkey' query param. If valid, returns requested data.
    """
    try:
        log.info("Starting REST server...")
        app = web.Application()
        # Attach references to locks and user data so the request handlers can use them
        app["file_lock"] = file_lock
        app["userdata_lock"] = userdata_lock
        app["userdata"] = userdata

        # Add routes for health and data
        app.router.add_get("/health", rest_health)
        app.router.add_get("/data", rest_get_data)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()

        log.info(f"REST server is running on http://0.0.0.0:{port}")
        # Keep the REST server alive indefinitely
        while True:
            await asyncio.sleep(3600)
    except Exception:
        log.exception("Unhandled exception in start_rest_server")

async def main():
    """
    Main async entry point:
      1. Loads config
      2. Connects to MQTT
      3. Launches optional WebSocket/REST servers
      4. Starts the downsampling checker
    """
    config = load_config("config.json")
    if not config:
        return

    broker = config.get("broker")
    port = config.get("port", 1883)
    username = config.get("username")
    password = config.get("password")
    topics = config.get("topics", [])

    sampling_intervals = config.get("sampling_interval", [])
    retention_days_list = config.get("retention_days", [])
    websocket_port = config.get("websocket_port", 8080)
    api_rest_port = config.get("API_Rest_port", 0)

    # If sampling_intervals or retention_days_list are not lists, replicate them for each topic
    if not isinstance(sampling_intervals, list):
        sampling_intervals = [sampling_intervals] * len(topics)
    if not isinstance(retention_days_list, list):
        retention_days_list = [retention_days_list] * len(topics)

    # Basic consistency checks
    if len(sampling_intervals) != len(topics):
        log.error("The number of 'sampling_intervals' must match the number of topics.")
        return
    if len(retention_days_list) != len(topics):
        log.error("The number of 'retention_days' must match the number of topics.")
        return
    if not broker or not topics:
        log.error("Broker and topics must be specified in the config file.")
        return

    # Append a random suffix to client_id to avoid collisions
    client_id = f"{config.get('client_id', 'mqtt_client')}-{randint(100000, 999999)}"
    log.info(f"Client ID: {client_id}")

    # Prepare dictionaries to track last_values, last_saved_time, and topic_settings
    last_values = {}
    last_saved_time = {}
    topic_settings = {}

    now = datetime.now(timezone.utc)
    for i, t in enumerate(topics):
        last_values[t] = "--"
        topic_settings[t] = {
            "sampling_interval": sampling_intervals[i],
            "retention_days": retention_days_list[i]
        }

        topic_file = get_topic_file_name(t)
        try:
            with open(topic_file, 'r') as f:
                try:
                    file_data = json.load(f)
                except json.JSONDecodeError:
                    file_data = []
        except FileNotFoundError:
            file_data = []

        if file_data:
            # Sort the data to find the last saved entry time
            file_data.sort(key=lambda x: x["T"])
            last_entry_time = datetime.fromisoformat(file_data[-1]["T"]).astimezone(timezone.utc)
            delta = (now - last_entry_time).total_seconds()
            si = topic_settings[t]["sampling_interval"]

            # If the last sample is older than the interval, we schedule the next save immediately
            if si != 0:
                if delta >= si:
                    adj_time = now - timedelta(seconds=si)
                    last_saved_time[t] = adj_time
                    log.info(f"[Init] Topic '{t}': last sample older than interval. Will save next data immediately.")
                else:
                    last_saved_time[t] = last_entry_time
                    log.info(f"[Init] Topic '{t}': continuing from last saved time {last_entry_time}.")
            else:
                last_saved_time[t] = last_entry_time
                log.info(f"[Init] Topic '{t}': sampling_interval=0, immediate save mode.")
        else:
            last_saved_time[t] = now
            log.info(f"[Init] Topic '{t}': no previous data. Starting now.")

    # Create MQTT client and attach callbacks
    client = mqtt.Client(client_id=client_id, transport="tcp")
    client.enable_logger(log)

    userdata_dict = {
        "topics": topics,
        "topic_settings": topic_settings,
        "last_values": last_values,
        "last_saved_time": last_saved_time
    }
    client.user_data_set(userdata_dict)

    global GLOBAL_USERDATA
    GLOBAL_USERDATA = userdata_dict

    client.on_connect = on_connect
    client.on_message = on_message

    # Set MQTT username & password if provided
    if username and password:
        client.username_pw_set(username, password)

    # Attempt to connect to the broker
    try:
        client.connect(broker, port, 60)
    except Exception:
        log.exception("Could not connect to broker.")
        return

    # Start the main tasks asynchronously
    tasks = []
    tasks.append(run_mqtt(client))              # MQTT loop
    tasks.append(check_intervals(userdata_dict))# Periodic saving loop

    # Launch WebSocket server if websocket_port != 0
    if websocket_port != 0:
        tasks.append(start_websocket_server(websocket_port))

    # Launch REST server if api_rest_port != 0
    if api_rest_port != 0:
        tasks.append(start_rest_server(api_rest_port, userdata_dict))

    # Run all tasks concurrently
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())
