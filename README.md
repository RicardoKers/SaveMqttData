# SaveMqttData — Version 0.9.6

This document describes how to configure, run, and use **SaveMqttData**, a Python program that collects MQTT data, stores it in per-topic JSON files, and provides both a **WebSocket** and a **REST** server for data queries. It includes data retention, downsampling (periodic saving), in-memory caching, Argon2-based passkey protection, and more.

---

## 1. Introduction

SaveMqttData performs the following tasks:

1. **Receives messages** from an MQTT broker (subscribe to one or more topics).
2. **Saves** the data into per-topic files (JSON format), applying retention of older records and optional in-memory caching.
3. **Provides** two optional servers:
    - A **WebSocket** server (if `websocket_port != 0`),
    - A **REST** server (if `API_Rest_port != 0`).

### Downsampling vs. Immediate Saving

- **`sampling_interval = 0`** (per topic) ⇒ **immediate** save upon message arrival.
- **`sampling_interval > 0`** ⇒ **downsampling** (data is buffered in memory and saved periodically).

---

## 2. Configuration (config.json)

The application reads settings from `config.json`. Below is an **example**:

```json
{
    "broker": "mqtt.example.com",
    "port": 1883,
    "username": "mqtt_user",
    "password": "secure_password",
    "client_id": "save-mqtt-client",
    "topics": [
        {
            "topic": "/devices/temperature",
            "sampling_interval": 0,
            "retention_days": 1,
            "enable_in_memory_cache": true
        },
        {
            "topic": "/devices/humidity",
            "sampling_interval": 60,
            "retention_days": 7,
            "enable_in_memory_cache": true
        },
        {
            "topic": "/devices/pressure",
            "sampling_interval": 120,
            "retention_days": 30,
            "enable_in_memory_cache": false
        }
    ],
    "websocket_port": 8081,
    "API_Rest_port": 8080,
    "passkeyHash": "$argon2id$v=19$m=65536,t=3,p=4$examplehash"
}
```

### Explanation of Fields

- **`broker`, `port`**: MQTT broker address and port.
- **`username`, `password`**: MQTT credentials (optional).
- **`client_id`**: Base name for the MQTT client ID (random suffix is appended).
- **`topics`**: List of MQTT topics to subscribe, with individual configurations:
    - **`sampling_interval`**: Interval (in seconds) for saving messages:
        - `0`: Messages saved immediately.
        - `>0`: Messages buffered and saved periodically.
    - **`retention_days`**: Retention duration (in days) for saved messages.
    - **`enable_in_memory_cache`**: Boolean to enable caching of the entire data file in memory for improved performance.
- **`websocket_port`**: Port for the WebSocket server (set `0` to disable WebSocket).
- **`API_Rest_port`**: Port for the REST server (set `0` to disable REST server).
- **`passkeyHash`**: Argon2 hash of your passkey, used to validate client connections.

> [!NOTE] Important
When "enable_in_memory_cache" = True, the data file is cached in memory. The program has no mechanism to control the size of memory used. The user must select appropriate "sampling_interval" and "retention_days" values to ensure that memory usage does not exceed available memory.

---

## 3. Running the Program

1. Install Python 3 and the required libraries:
    
    ```bash
    pip install paho-mqtt websockets aiohttp argon2-cffi
    ```
    
2. Edit `config.json` to match your MQTT broker, topics, intervals, etc.
    
3. Run the Python script:
    
    ```bash
    python SaveMqttData.py
    ```
    
4. Program output logs will appear in the console. You should see:
    
    - Connection messages (e.g., “Connected to MQTT Broker!”)
    - Subscription confirmations (e.g., “Subscribed to topic: /devices/temperature”)
    - Initialization of WebSocket and/or REST servers if their ports are nonzero
    - Periodic messages about saved data, retention, and downsampling checks

---

## 4. Data Storage & Retention

### Per-Topic Files

Each MQTT topic is stored in a separate file:

```
Data_<topic_with_slash_replaced_by_dash>.json
```

Inside each file, new records are appended as:

```json
{ "T": "<timestamp_ISO8601>", "V": "<value_or_payload>" }
```

### Retention

Whenever new data is saved:

1. The program reads the existing JSON file (if not cached).
2. Removes records older than `retention_days`.
3. Writes back the updated JSON file.

---

## 5. MQTT Callbacks

- **`on_connect`**: Subscribes to the listed topics on successful broker connection.
- **`on_message`**:
    - Decodes each message payload.
    - Checks the topic’s `sampling_interval`.
    - If caching is enabled, keeps the entire file in memory for improved performance.
    - A background loop (`check_intervals`) periodically saves buffered data to disk.

---

## 6. WebSocket Server

If `websocket_port` is not `0`, the program starts a WebSocket server at:

```
ws://<HOST>:<websocket_port>
```

### Actions

#### Health Check

**Request:**

```json
{
    "action": "health",
    "passkey": "<your_plaintext_password>"
}
```

**Response Example:**

```json
{
    "status": "ok",
    "version": "0.9.3",
    "timestamp": "2025-01-15T12:00:00Z",
    "mem_total_mb": 2048.0,
    "mem_used_mb": 512.0,
    "mem_percent": 25.0,
    "disk_total_mb": 100000.0,
    "disk_used_mb": 40000.0,
    "disk_percent": 40.0,
    "data_files_size_bytes": 12345,
    "packets_count": 47,
    "uptime": "1d, 02:15:30",
    "cache_mem_total_bytes": 1024,
    "topics_info": [
        {
            "name": "/devices/temperature",
            "oldest_timestamp": "...",
            "newest_timestamp": "...",
            "total_samples": 100,
            "sampling_interval": 0,
            "retention_days": 1,
            "file_size_bytes": 2345,
            "memory_usage_bytes": 512
        }
    ]
}
```

#### get_data

**Request Example:**

```json
{
    "action": "get_data",
    "passkey": "<your_password>",
    "topic": "/devices/temperature",
    "start_time": "2025-01-01T00:00:00Z",
    "end_time": "2025-01-10T23:59:59Z"
}
```

---

## 7. REST API Server

If `API_Rest_port` is not `0`, the program also starts a REST server on:

```
http://<HOST>:<API_Rest_port>
```

### Endpoints

#### GET /health

Example:

```
http://<HOST>:8080/health?passkey=<your_password>
```

Returns the same JSON structure as the WebSocket `health` action.

#### GET /data

Example:

```
http://<HOST>:8080/data?passkey=<your_password>&topic=/devices/temperature&start_time=2025-01-01T00:00:00Z&end_time=2025-01-10T23:59:59Z
```

---

## 8. Security (Argon2 Passkey)

- The file `config.json` stores `passkeyHash` (the Argon2 hash).
- At runtime, clients must provide a matching plaintext passkey in WebSocket or REST requests.

To generate the hash from the plain text password, use:

```bash
python GenerateHash.py <password>
```

---

## 9. Logging & Troubleshooting

- Python’s `logging` module is used.
- By default, it logs `ERROR` and above. You can change it to `DEBUG`, `INFO`, or `WARNING`.
- Console output is your first place to look for errors.

---

## 10. Security Limitations

Data exchanged through the WebSocket and REST endpoints is **not encrypted** by default. To secure data in transit, use a reverse proxy with TLS/SSL.

---

## 11. Conclusion

SaveMqttData offers:

- MQTT data collection
- Downsampling per topic, with immediate or periodic saving
- In-memory caching of the entire data file for efficient resource usage
- Data retention to keep file sizes manageable
- Two optional servers for queries: WebSocket and REST
- Argon2 passkey-based security