# SaveMqttData — Version 0.9.2

This manual describes how to configure, run, and use **SaveMqttData**, a Python program that collects MQTT data, stores it in per-topic JSON files, and provides both a **WebSocket** and a **REST** server for data queries. It also includes data retention, downsampling (periodic saving), Argon2-based passkey protection, and more.

---

## 1. Introduction

SaveMqttData performs the following tasks:

1. **Receives messages** from an MQTT broker (subscribe to one or more topics).
2. **Saves** the data into per-topic files (JSON format), applying retention of older records.
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
  "broker": "test.mosquitto.org",
  "port": 1883,
  "username": "",
  "password": "",
  "client_id": "mqtt_client",
  "topics": ["/my/temperature/", "/my/humidity/"],
  "sampling_interval": [60, 120],
  "retention_days": [2, 2],
  "websocket_port": 8080,
  "API_Rest_port": 8081,
  "passkeyHash": "$argon2id$v=19$m=65536,t=3,p=4$..."
}
```

### Explanation of Fields

- **`broker`, `port`**: MQTT broker address and port.
- **`username`, `password`**: MQTT credentials (optional).
- **`client_id`**: Base name for the MQTT client ID (random suffix is appended).
- **`topics`**: List of MQTT topics to subscribe.
- **`sampling_interval`**: A list of values (in seconds), one value for each topic.
  - `0` means every message is saved immediately.
  - `>0` means data is buffered and saved periodically.
- **`retention_days`**: A list of values (in days), one value for each topic. Removes older data beyond this threshold.
- **`websocket_port`**: Port for the WebSocket server (set `0` to disable WebSocket).
- **`API_Rest_port`**: Port for the REST server (set `0` to disable REST server).
- **`passkeyHash`**: Argon2 hash of your passkey, used to validate client connections. (No plain password is stored.)

The program does not handle client-side TLS certificates for MQTT broker.

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
   or
   ```bash
   python3 SaveMqttData.py
   ```

4. Program output logs will appear in the console. You should see:
   - Connection messages (e.g., “Connected to MQTT Broker!”)
   - Subscription confirmations (e.g., “Subscribed to topic: /my/temperature/”)
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

1. The program reads the existing JSON file.
2. It removes records older than `retention_days`.
3. It writes back the new combined JSON file.

---

## 5. MQTT Callbacks

- **`on_connect`**: Subscribes to the listed topics on successful broker connection.
- **`on_message`**:
  - Decodes each message payload.
  - Checks the topic’s `sampling_interval`.
  - If `0`, saves immediately.
  - If `>0`, just stores the latest value in memory.
  - A background loop (`check_intervals`) periodically saves buffered data.

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

**Response example:**

```json
{
  "status": "ok",
  "version": "0.9.2",
  "timestamp": "2025-01-01T12:00:00Z",
  "mem_total_mb": 2048.0,
  "mem_used_mb": 512.0,
  "mem_percent": 25.0,
  "disk_total_mb": 976745.0,
  "disk_used_mb": 320745.21,
  "disk_percent": 32.8,
  "data_files_size_bytes": 12345,
  "packets_count": 47,
  "uptime": "0d, 01:01:23",
  "topics_info": [
    {
      "name": "/my/temperature/",
      "oldest_timestamp": "...",
      "newest_timestamp": "...",
      "total_samples": 100,
      "sampling_interval": 60,
      "retention_days": 2,
      "file_size_bytes": 2345
    },
    {
      "name": "/my/humidity/",
      "oldest_timestamp": "...",
      "newest_timestamp": "...",
      "total_samples": 100,
      "sampling_interval": 60,
      "retention_days": 2,
      "file_size_bytes": 2345
    }
    ...
  ]
}
```

#### get_data

**Request example:**

```json
{
  "action": "get_data",
  "passkey": "<your_password>",
  "topic": "/my/temperature/",
  "start_time": "2025-01-01T00:00:00Z",
  "end_time": "2025-01-10T23:59:59Z"
}
```

- `start_time` and `end_time` are optional.
  - If both are omitted, you get all data.
  - If only `start_time` is provided, returns from that time to newest.
  - If only `end_time` is provided, returns all data up to that time.

**Response:**

```json
[
  {
    "timestamp": "...",
    "topic": "/my/temperature/",
    "value": "..."
  }
  ...
]
```

---

## 7. REST API Server

If `API_Rest_port` is not `0`, the program also starts a REST server on:

```
http://<HOST>:<API_Rest_port>
```

### Endpoints

#### GET /health

**Example:**

```
http://<HOST>:8081/health?passkey=<your_password>
```

Returns the same JSON structure as the WebSocket `health` action.

#### GET /data

**Example:**

```
http://<HOST>:8081/data?passkey=<pw>&topic=/my/temperature/&start_time=2025-01-01T00:00:00Z&end_time=2025-01-10T23:59:59Z
```

- Same rules for time intervals: optional `start/end`, partial, or none.
- Returns an array of `{ "timestamp": "...", "topic": "...", "value": "..." }`.

---

## 8. Security (Argon2 Passkey)

- The file `config.json` stores `passkeyHash` (the Argon2 hash).
- At runtime, clients must provide a matching plaintext passkey in WebSocket or REST requests.
- If the passkey fails verification, an `{ "error": "Invalid passkey" }` is returned.

To generate the hash from the plain text password, a Python script called "GenerateHash.py" is provided.

- Usage: `python GenerateHash.py <password>`

---

## 9. Logging & Troubleshooting

- Python’s `logging` module is used.
- By default, it logs `ERROR` and above. You can change it to `DEBUG`, `INFO`, or `WARNING`.
- Console output is your first place to look for errors (e.g., file not found, JSON decode errors, etc.).

---

## 10. **Security Limitations**

By default, data exchanged through the WebSocket and REST endpoints of this program is **not** encrypted in transit, which means someone intercepting the network traffic could potentially read or modify messages. To address this limitation, you can place a **reverse proxy** (e.g., Nginx, Apache, or Caddy) in front of the application to terminate TLS/SSL. This proxy would handle HTTPS (or WSS) connections from the outside world, encrypting and decrypting traffic, while forwarding unencrypted data locally to SaveMqttData. By doing so, you ensure end-to-end confidentiality of the data and reduce the risk of eavesdropping or tampering.

---

## 11. Conclusion

SaveMqttData offers:

- MQTT data collection
- Downsampling per topic, with immediate or periodic saving
- Data retention to keep file sizes manageable
- Concurrent read/write protection
- Two optional servers for queries: WebSocket and REST
- Argon2 passkey-based security

Feel free to monitor logs, adjust `sampling_interval` and `retention_days` as needed, and integrate the collected data into your automation or analytics workflow.
