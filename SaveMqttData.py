# SaveMqttData Version 0.9.7
#
# Added: GZip compression for get_data responses if "compress_data" == true.
# - WebSocket: sends a binary frame with gzipped JSON.
# - REST: sets "Content-Encoding: gzip" and returns gzipped JSON body.
# No other features or comments changed. The program is otherwise identical
# to version 0.9.6.

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
import sys
import gzip  # for GZip compression
from argon2 import PasswordHasher, exceptions as argon2_exceptions
from aiohttp import web

VERSION = "0.9.7"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

file_lock = Lock()
userdata_lock = Lock()

start_time_utc = datetime.now(timezone.utc)
packets_count = 0

GLOBAL_USERDATA = None

# ---------------------------------------------------------------------------
# HELPER FUNCTIONS
# ---------------------------------------------------------------------------

def load_config(file_path):
    """
    Loads JSON config from 'file_path'. Returns dict or None if missing/invalid.
    """
    try:
        with open(file_path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        log.error(f"Config file '{file_path}' not found.")
        return None
    except json.JSONDecodeError:
        log.error(f"Error decoding JSON in '{file_path}'.")
        return None

def get_topic_file_name(topic):
    """
    Converts slashes ('/') into dashes ('-') to build a disk filename for 'topic'.
    Example: '/my/temperature/' -> 'Data_-my-temperature-.json'
    """
    return "Data_" + topic.replace("/", "-") + ".json"

def passkey_valid(passkey):
    """
    Validates 'passkey' using Argon2 hash from config.json.
    Returns True if correct, False otherwise.
    """
    cfg = load_config("config.json")
    if not cfg:
        return False
    stored_hash = cfg.get("passkeyHash", "")
    if not passkey or not stored_hash:
        return False
    ph = PasswordHasher()
    try:
        ph.verify(stored_hash, passkey)
        return True
    except (argon2_exceptions.VerifyMismatchError, argon2_exceptions.VerificationError):
        return False
    except Exception:
        return False

def sys_deep_size(obj, seen=None):
    """
    Recursively computes approximate memory usage of 'obj' in bytes.
    Used in 'health' calls to measure in-memory usage of each topic's data.
    """
    if seen is None:
        seen = set()

    o_id = id(obj)
    if o_id in seen:
        return 0
    seen.add(o_id)

    size = sys.getsizeof(obj)
    if isinstance(obj, dict):
        for k, v in obj.items():
            size += sys_deep_size(k, seen)
            size += sys_deep_size(v, seen)
    elif isinstance(obj, (list, tuple, set)):
        for item in obj:
            size += sys_deep_size(item, seen)
    return size

def apply_retention(data_list, retention_days):
    """
    Removes records older than 'retention_days' from 'data_list' based on T field.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    def fresh(rec):
        try:
            rt = datetime.fromisoformat(rec["T"]).astimezone(timezone.utc)
            return rt >= cutoff
        except:
            return False
    return [r for r in data_list if fresh(r)]

def filter_data_by_time(data_list, start_dt, end_dt):
    """
    Filters data_list (list of { "T": <iso8601>, "V": <val> }) by optional start_dt, end_dt.
    Returns only records in [start_dt, end_dt].
    If both are None => returns entire list.
    """
    if not start_dt and not end_dt:
        return data_list

    results = []
    for r in data_list:
        try:
            rt = datetime.fromisoformat(r["T"]).astimezone(timezone.utc)
            if start_dt and not end_dt:
                if rt >= start_dt:
                    results.append(r)
            elif end_dt and not start_dt:
                if rt <= end_dt:
                    results.append(r)
            else:
                if start_dt <= rt <= end_dt:
                    results.append(r)
        except:
            pass
    return results

# ---------------------------------------------------------------------------
# DISK OPERATIONS
# ---------------------------------------------------------------------------

def overwrite_disk(topic, new_list):
    """
    Overwrites the file for 'topic' with 'new_list'. We assume 'new_list'
    is already retention-applied and sorted. This ensures memory == disk.
    """
    tfile = get_topic_file_name(topic)
    try:
        with file_lock:
            with open(tfile, 'w') as f:
                json.dump(new_list, f, indent=4)
            log.info(f"[overwrite_disk] Wrote {len(new_list)} records to {tfile} for topic='{topic}'.")
    except Exception:
        log.exception(f"Error overwriting disk for topic='{topic}'")

def load_disk(topic):
    """
    Loads entire list of records from disk for 'topic'. Returns a list
    or empty if not found/invalid.
    """
    tfile = get_topic_file_name(topic)
    if not os.path.exists(tfile):
        return []
    try:
        with file_lock:
            with open(tfile, 'r') as f:
                try:
                    data_list = json.load(f)
                    if not isinstance(data_list, list):
                        data_list = []
                    return data_list
                except:
                    return []
    except:
        log.exception(f"Error loading data for topic='{topic}' from disk.")
        return []

# ---------------------------------------------------------------------------
# GET TOPIC INFO
# ---------------------------------------------------------------------------

def get_topic_info(topics, topic_settings):
    """
    Returns an array describing each topic:
      - name
      - oldest_timestamp / newest_timestamp
      - total_samples
      - sampling_interval, retention_days
      - file_size_bytes
      - memory_usage_bytes (if caching is true)
    """
    results = []
    for t in topics:
        stt = topic_settings[t]
        si = stt["sampling_interval"]
        rd = stt["retention_days"]
        c_en = stt.get("enable_in_memory_cache", False)

        tfile = get_topic_file_name(t)
        oldest = None
        newest = None
        total = 0
        file_sz = 0

        with file_lock:
            if os.path.exists(tfile):
                file_sz = os.path.getsize(tfile)
                try:
                    with open(tfile, 'r') as f:
                        disk_data = json.load(f)
                except:
                    disk_data = []
            else:
                disk_data = []

        if disk_data:
            total = len(disk_data)
            disk_data.sort(key=lambda x: x["T"])
            oldest = disk_data[0]["T"]
            newest = disk_data[-1]["T"]

        mem_usage = 0
        if c_en:
            # read from in_memory_data
            mem_list = GLOBAL_USERDATA["in_memory_data"].get(t, [])
            mem_usage = sys_deep_size(mem_list)

        results.append({
            "name": t,
            "oldest_timestamp": oldest,
            "newest_timestamp": newest,
            "total_samples": total,
            "sampling_interval": si,
            "retention_days": rd,
            "file_size_bytes": file_sz,
            "memory_usage_bytes": mem_usage
        })
    return results

# ---------------------------------------------------------------------------
# MQTT CALLBACKS
# ---------------------------------------------------------------------------

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        log.info("Connected to MQTT Broker!")
        for topic in userdata["topics"]:
            client.subscribe(topic)
            log.info(f"Subscribed to topic: {topic}")
    else:
        log.error(f"Failed to connect, return code {rc}")

def on_message(client, userdata, msg):
    """
    For each new MQTT message:
      - if caching => if sampling_interval=0 => immediate flush
                      else store in 'latest_cache_value' for that topic
      - if not caching => if sampling_interval=0 => immediate write
                         else store in 'last_values'
    """
    global packets_count
    try:
        payload = msg.payload.decode()
        topic = msg.topic

        with userdata_lock:
            packets_count += 1
            if topic not in userdata["topics"]:
                log.warning(f"Topic {topic} not found in config.")
                return

            stt = userdata["topic_settings"][topic]
            si = stt["sampling_interval"]
            rd = stt["retention_days"]
            c_en = stt.get("enable_in_memory_cache", False)

            if c_en:
                # For caching
                if si == 0:
                    # immediate => append to memory & flush entire list to disk
                    mem_list = userdata["in_memory_data"].get(topic, [])
                    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                    new_rec = {"T": now_str, "V": payload}
                    mem_list.append(new_rec)

                    # retention
                    mem_list = apply_retention(mem_list, rd)
                    userdata["in_memory_data"][topic] = mem_list

                    # flush entire list to disk
                    overwrite_disk(topic, mem_list)

                else:
                    # sampling_interval>0 => store only the last message in a "latest_cache_value"
                    # we don't append to mem_list now, we do so at interval time
                    userdata["latest_cache_value"][topic] = payload

            else:
                # not caching
                if si == 0:
                    # immediate
                    disk_data = load_disk(topic)
                    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                    disk_data.append({"T": now_str, "V": payload})
                    disk_data = apply_retention(disk_data, rd)
                    overwrite_disk(topic, disk_data)
                else:
                    userdata["last_values"][topic] = payload

    except Exception:
        log.exception("Unhandled exception in on_message")

# ---------------------------------------------------------------------------
# PERIODIC CHECK
# ---------------------------------------------------------------------------

async def check_intervals(userdata):
    """
    For each topic with sampling_interval>0, we flush once the time is up:
      - if caching => we append exactly one record to in_memory_data:
          the "latest_cache_value" or "--" if none
        apply retention, overwrite disk with entire in_memory_data
      - if not caching => we append exactly one record to disk:
          "last_values" or "--" if none
    Then set last_saved_time, so next flush is in the next interval.
    """
    while True:
        now = datetime.now(timezone.utc)
        with userdata_lock:
            for t in userdata["topics"]:
                stt = userdata["topic_settings"][t]
                si = stt["sampling_interval"]
                rd = stt["retention_days"]
                c_en = stt.get("enable_in_memory_cache", False)

                last_save = userdata["last_saved_time"][t]
                if si > 0:
                    elapsed = (now - last_save).total_seconds()
                    if elapsed >= si:
                        if c_en:
                            mem_list = userdata["in_memory_data"].get(t, [])
                            val = userdata["latest_cache_value"].get(t, None)
                            now_str = now.strftime('%Y-%m-%dT%H:%M:%SZ')
                            if val is None:
                                mem_list.append({"T": now_str, "V": "--"})
                            else:
                                mem_list.append({"T": now_str, "V": val})
                            mem_list = apply_retention(mem_list, rd)
                            mem_list.sort(key=lambda x: x["T"])
                            userdata["in_memory_data"][t] = mem_list
                            userdata["latest_cache_value"][t] = None  # reset

                            overwrite_disk(t, mem_list)
                        else:
                            disk_data = load_disk(t)
                            val = userdata["last_values"].get(t, None)
                            now_str = now.strftime('%Y-%m-%dT%H:%M:%SZ')
                            if val is None:
                                disk_data.append({"T": now_str, "V": "--"})
                            else:
                                disk_data.append({"T": now_str, "V": val})
                            disk_data = apply_retention(disk_data, rd)
                            disk_data.sort(key=lambda x: x["T"])
                            overwrite_disk(t, disk_data)
                            userdata["last_values"][t] = None

                        userdata["last_saved_time"][t] = now
                        log.info(f"[Downsampling] topic '{t}' => wrote 1 record to disk. Next flush in {si} sec.")

        await asyncio.sleep(1)

# ---------------------------------------------------------------------------
# WEBSOCKET
# ---------------------------------------------------------------------------

async def handle_request(websocket, path=None):
    global packets_count
    try:
        async for raw_msg in websocket:
            try:
                req = json.loads(raw_msg)
                action = req.get("action")
                passkey = req.get("passkey")
                topic = req.get("topic")
                st = req.get("start_time")
                et = req.get("end_time")

                # NEW param: "compress_data" => if true => send gzipped JSON in binary
                compress_data = req.get("compress_data", False)

                if not passkey_valid(passkey):
                    await websocket.send(json.dumps({"error": "Invalid passkey"}))
                    continue

                if action == "health":
                    file_dir = os.path.dirname(os.path.abspath(__file__)) or '.'
                    disk_use = psutil.disk_usage(file_dir)
                    mem = psutil.virtual_memory()

                    now_utc = datetime.now(timezone.utc)
                    with userdata_lock:
                        uptime_seconds = (now_utc - start_time_utc).total_seconds()
                        all_topics = GLOBAL_USERDATA["topics"]
                        t_sett = GLOBAL_USERDATA["topic_settings"]

                        sum_mem_cache = 0
                        for tp in all_topics:
                            if t_sett[tp].get("enable_in_memory_cache", False):
                                mem_list = GLOBAL_USERDATA["in_memory_data"].get(tp, [])
                                sum_mem_cache += sys_deep_size(mem_list)

                    days, remainder = divmod(uptime_seconds, 86400)
                    hours, remainder = divmod(remainder, 3600)
                    minutes, seconds = divmod(remainder, 60)
                    up_str = f"{int(days)}d, {int(hours):02}:{int(minutes):02}:{int(seconds):02}"

                    cfg = load_config("config.json") or {}
                    disk_sum = 0
                    for o in cfg.get("topics", []):
                        tf = get_topic_file_name(o["topic"])
                        if os.path.exists(tf):
                            disk_sum += os.path.getsize(tf)

                    t_info = get_topic_info(all_topics, t_sett)
                    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

                    info = {
                        "status": "ok",
                        "version": VERSION,
                        "timestamp": now_str,
                        "mem_total_mb": round(mem.total / (1024 * 1024), 2),
                        "mem_used_mb": round(mem.used / (1024 * 1024), 2),
                        "mem_percent": mem.percent,
                        "disk_total_mb": round(disk_use.total / (1024 * 1024), 2),
                        "disk_used_mb": round(disk_use.used / (1024 * 1024), 2),
                        "disk_percent": disk_use.percent,
                        "data_files_size_bytes": disk_sum,
                        "packets_count": packets_count,
                        "uptime": up_str,
                        "cache_mem_total_bytes": sum_mem_cache,
                        "topics_info": t_info
                    }

                    # health is typically small, so we can just do normal JSON
                    await websocket.send(json.dumps(info))

                elif action == "get_data":
                    sdt = None
                    edt = None
                    if st:
                        sdt = datetime.fromisoformat(st).astimezone(timezone.utc)
                    if et:
                        edt = datetime.fromisoformat(et).astimezone(timezone.utc)

                    cfg = load_config("config.json") or {}
                    req_tops = []
                    if topic:
                        req_tops = [topic]
                    else:
                        for ob in cfg.get("topics", []):
                            req_tops.append(ob["topic"])

                    if sdt and edt and sdt > edt:
                        log.warning("start_time> end_time => empty list")
                        await websocket.send(json.dumps([]))
                        continue

                    results = []
                    with userdata_lock:
                        for t in req_tops:
                            s = GLOBAL_USERDATA["topic_settings"].get(t, {})
                            c_en = s.get("enable_in_memory_cache", False)
                            if c_en:
                                mem_list = GLOBAL_USERDATA["in_memory_data"].get(t, [])
                                flt = filter_data_by_time(mem_list, sdt, edt)
                                for rec in flt:
                                    results.append({
                                        "timestamp": rec["T"],
                                        "topic": t,
                                        "value": rec["V"]
                                    })
                            else:
                                disk_data = load_disk(t)
                                flt = filter_data_by_time(disk_data, sdt, edt)
                                for rec in flt:
                                    results.append({
                                        "timestamp": rec["T"],
                                        "topic": t,
                                        "value": rec["V"]
                                    })

                    # If compress_data => GZip the JSON, send as binary
                    if compress_data:
                        # Convert results to JSON, then GZip
                        json_str = json.dumps(results)
                        gzipped = gzip.compress(json_str.encode("utf-8"))
                        # send as binary frame
                        await websocket.send(gzipped)
                        log.info(f"[WebSocket] Sent gzipped {len(results)} records for get_data (binary).")
                    else:
                        # normal JSON text
                        await websocket.send(json.dumps(results))

                else:
                    await websocket.send(json.dumps({"error": "Unknown action"}))

            except Exception:
                log.exception("Error in handle_request (inner)")
                await websocket.send(json.dumps({"error": "Internal error"}))

    except ws_exceptions.ConnectionClosedError as e:
        log.warning(f"WebSocket connection closed: {e}")
    except Exception:
        log.exception("Error in handle_request (outer)")

# ---------------------------------------------------------------------------
# ASYNC TASKS
# ---------------------------------------------------------------------------

async def run_mqtt(client):
    """
    Runs paho-mqtt loop in a separate thread, preventing blocking.
    """
    try:
        await asyncio.to_thread(client.loop_forever)
    except Exception:
        log.exception("Unhandled exception in run_mqtt")

async def start_websocket_server(port):
    try:
        log.info(f"Starting WebSocket server on port {port}...")
        server = await serve(handle_request, "0.0.0.0", port, ping_interval=30, ping_timeout=10)
        log.info(f"WebSocket server running at ws://0.0.0.0:{port}")
        await server.wait_closed()
    except Exception:
        log.exception("Unhandled exception in start_websocket_server")

async def rest_health(request):
    """
    GET /health => system info, memory usage, etc. passkey needed.
    """
    passkey = request.query.get("passkey", "")
    if not passkey_valid(passkey):
        return web.json_response({"error": "Invalid passkey"}, status=403)

    file_dir = os.path.dirname(os.path.abspath(__file__)) or '.'
    disk_use = psutil.disk_usage(file_dir)
    mem = psutil.virtual_memory()

    now_utc = datetime.now(timezone.utc)
    with request.app["userdata_lock"]:
        uptime_sec = (now_utc - start_time_utc).total_seconds()
        all_topics = request.app["userdata"]["topics"]
        t_sett = request.app["userdata"]["topic_settings"]

        sum_mem_cache = 0
        for tp in all_topics:
            if t_sett[tp].get("enable_in_memory_cache", False):
                mem_list = request.app["userdata"]["in_memory_data"].get(tp, [])
                sum_mem_cache += sys_deep_size(mem_list)

    days, remainder = divmod(uptime_sec, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    up_str = f"{int(days)}d, {int(hours):02}:{int(minutes):02}:{int(seconds):02}"

    cfg = load_config("config.json") or {}
    disk_sum = 0
    for o in cfg.get("topics", []):
        tf = get_topic_file_name(o["topic"])
        if os.path.exists(tf):
            disk_sum += os.path.getsize(tf)

    t_info = get_topic_info(all_topics, t_sett)
    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    resp = {
        "status": "ok",
        "version": VERSION,
        "timestamp": now_str,
        "mem_total_mb": round(mem.total / (1024 * 1024), 2),
        "mem_used_mb": round(mem.used / (1024 * 1024), 2),
        "mem_percent": mem.percent,
        "disk_total_mb": round(disk_sum / (1024 * 1024), 2),
        "disk_used_mb": round(disk_use.used / (1024 * 1024), 2),
        "disk_percent": disk_use.percent,
        "data_files_size_bytes": disk_sum,
        "packets_count": packets_count,
        "uptime": up_str,
        "cache_mem_total_bytes": sum_mem_cache,
        "topics_info": t_info
    }
    return web.json_response(resp)

async def rest_get_data(request):
    """
    GET /data => passkey, optional start/end times. If caching => read from memory,
    else from disk. If 'compress_data=true', we GZip the response.
    """
    passkey = request.query.get("passkey", "")
    if not passkey_valid(passkey):
        return web.json_response({"error": "Invalid passkey"}, status=403)

    st = request.query.get("start_time", "")
    et = request.query.get("end_time", "")
    tpc = request.query.get("topic", "")

    # parse "compress_data"
    compress_data_str = request.query.get("compress_data", "false")
    compress_data = (compress_data_str.lower() == "true")

    log.info(f"Compress data param => {compress_data_str}, final bool => {compress_data}")

    cfg = load_config("config.json") or {}

    start_dt = None
    end_dt = None
    if st:
        start_dt = datetime.fromisoformat(st).astimezone(timezone.utc)
    if et:
        end_dt = datetime.fromisoformat(et).astimezone(timezone.utc)

    req_tops = []
    if tpc:
        req_tops = [tpc]
    else:
        for obj in cfg.get("topics", []):
            req_tops.append(obj["topic"])

    if start_dt and end_dt and start_dt > end_dt:
        log.warning("start_time > end_time => returning empty list")
        return web.json_response([])

    results = []
    with request.app["userdata_lock"]:
        for top in req_tops:
            stt = request.app["userdata"]["topic_settings"][top]
            c_en = stt.get("enable_in_memory_cache", False)
            if c_en:
                mem_list = request.app["userdata"]["in_memory_data"].get(top, [])
                filtered = filter_data_by_time(mem_list, start_dt, end_dt)
                for rec in filtered:
                    results.append({
                        "timestamp": rec["T"],
                        "topic": top,
                        "value": rec["V"]
                    })
            else:
                disk_data = load_disk(top)
                filtered = filter_data_by_time(disk_data, start_dt, end_dt)
                for rec in filtered:
                    results.append({
                        "timestamp": rec["T"],
                        "topic": top,
                        "value": rec["V"]
                    })

    if compress_data:
        # GZip the JSON
        json_str = json.dumps(results)
        gzipped = gzip.compress(json_str.encode("utf-8"))
        # Return as binary with "Content-Encoding: gzip"
        log.info(f"Returned gzipped {len(results)} records for get_data (REST).")
        return web.Response(
            body=gzipped,
            headers={
                "Content-Encoding": "gzip",
                "Content-Type": "application/json"
            }
        )
    else:
        # normal JSON
        return web.json_response(results)

async def start_rest_server(port, userdata):
    """
    Starts AIOHTTP server on 0.0.0.0:<port>, mapping /health -> rest_health and
    /data -> rest_get_data.
    """
    try:
        log.info("Starting REST server...")
        app = web.Application()
        app["file_lock"] = file_lock
        app["userdata_lock"] = userdata_lock
        app["userdata"] = userdata

        app.router.add_get("/health", rest_health)
        app.router.add_get("/data", rest_get_data)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()

        log.info(f"REST server is running on http://0.0.0.0:{port}")
        while True:
            await asyncio.sleep(3600)
    except Exception:
        log.exception("Unhandled exception in start_rest_server")

async def main():
    """
    Main async entry point:
      1) Load config
      2) For each topic => load existing data from disk as downsampled history
         if caching => store entire list in memory
         if not caching => we ignore in_memory_data
      3) Initialize last_saved_time
      4) connect to MQTT
      5) launch optional WebSocket & REST
      6) run sampling_intervals in check_intervals
    """
    cfg = load_config("config.json")
    if not cfg:
        return

    broker = cfg.get("broker")
    port = cfg.get("port", 1883)
    username = cfg.get("username")
    password = cfg.get("password")

    topics_conf = cfg.get("topics", [])
    ws_port = cfg.get("websocket_port", 8080)
    rest_port = cfg.get("API_Rest_port", 0)

    if not broker or not topics_conf:
        log.error("Broker and topics must be specified in config.json.")
        return

    topics = []
    topic_settings = {}
    in_memory_data = {}
    last_values = {}  # for non-cached topics
    latest_cache_value = {}  # for cached topics with si>0
    last_saved_time = {}

    now = datetime.now(timezone.utc)
    for obj in topics_conf:
        t = obj["topic"]
        si = obj["sampling_interval"]
        rd = obj["retention_days"]
        c_en = obj.get("enable_in_memory_cache", False)

        topics.append(t)
        topic_settings[t] = {
            "sampling_interval": si,
            "retention_days": rd,
            "enable_in_memory_cache": c_en
        }
        last_values[t] = None
        latest_cache_value[t] = None

        # load existing data from disk
        disk_data = load_disk(t)
        disk_data = apply_retention(disk_data, rd)
        disk_data.sort(key=lambda x: x["T"])

        if disk_data:
            last_ts = disk_data[-1]["T"]
            last_dt = datetime.fromisoformat(last_ts).astimezone(timezone.utc)
            delta = (now - last_dt).total_seconds()
            if si > 0:
                if delta >= si:
                    adj_time = now - timedelta(seconds=si)
                    last_saved_time[t] = adj_time
                else:
                    last_saved_time[t] = last_dt
            else:
                last_saved_time[t] = last_dt
        else:
            last_saved_time[t] = now

        if c_en:
            # store entire disk data in memory
            in_memory_data[t] = disk_data
        else:
            in_memory_data[t] = []  # we won't use it for queries

        log.info(f"[Init] topic='{t}' loaded {len(disk_data)} records from disk, caching={c_en}, si={si}")

    client_id = f"{cfg.get('client_id','mqtt_client')}-{randint(100000,999999)}"
    log.info(f"Client ID: {client_id}")

    client = mqtt.Client(client_id=client_id, transport="tcp")
    client.enable_logger(log)

    global GLOBAL_USERDATA
    userdata_dict = {
        "topics": topics,
        "topic_settings": topic_settings,
        "in_memory_data": in_memory_data,
        "last_values": last_values,
        "latest_cache_value": latest_cache_value,
        "last_saved_time": last_saved_time
    }
    GLOBAL_USERDATA = userdata_dict

    client.user_data_set(userdata_dict)
    client.on_connect = on_connect
    client.on_message = on_message

    if username and password:
        client.username_pw_set(username, password)

    try:
        client.connect(broker, port, 60)
    except Exception:
        log.exception("Could not connect to broker.")
        return

    tasks = []
    tasks.append(run_mqtt(client))
    tasks.append(check_intervals(userdata_dict))

    if ws_port != 0:
        tasks.append(start_websocket_server(ws_port))
    if rest_port != 0:
        tasks.append(start_rest_server(rest_port, userdata_dict))

    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())
