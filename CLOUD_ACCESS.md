# Cloud Service Access Guide for Judges

This document outlines how evaluation judges can connect to and interact with the MayaCardio Cloud Service.

---

## 1. Cloud Service Endpoint

- **Base URL**: `https://mayacardio-cloud-eval.fly.dev` (or participant's hosted endpoint; fallback simulator: `http://<device-ip>:8000`)
- **Transport**: HTTPS / HTTP
- **Authentication**: Bearer Token via standard HTTP header:
  ```http
  Authorization: Bearer <EVALUATION_AUTH_TOKEN>
  ```
  *(Note: In accordance with repository security rules, the actual token value is supplied separately in the candidate submission form/email and is NOT committed to this repository).*

---

## 2. Available Endpoints

### 2.1 Health Check (Unauthenticated)
```http
GET /health
```
- **Response**: `200 OK`
- **Body**: `{"status": "ok", "service": "MayaCardio Cloud", "time": 1728900000.0}`

### 2.2 Query Held State (Ranges & Losses)
```http
GET /state?session=<SESSION_ID>
```
- **Response**: `200 OK`
- **Body**:
  ```json
  {
    "session_id": "abc12345",
    "ranges_1": [[0, 399], [400, 799]],
    "ranges_2": [[0, 399], [400, 799]],
    "losses_1": [],
    "losses_2": []
  }
  ```

### 2.3 Query Window Results
```http
GET /results?session=<SESSION_ID>
```
- **Response**: `200 OK`
- **Body**:
  ```json
  {
    "session_id": "abc12345",
    "results": [
      {
        "session_id": "abc12345",
        "window": 0,
        "mode": "online",
        "missing_1": 0,
        "missing_2": 0,
        "complete": true,
        "mean_1": [720.0, 1200.0, 1560.0],
        "min_1": [700, 1150, 1500],
        "max_1": [750, 1250, 1600],
        "magnitude": 427215.3,
        "heart_rate": 60.0,
        "cloud_received_time": 1728900001.2
      }
    ]
  }
  ```

### 2.4 Export Session Data (Standard JSON-Lines)
```http
GET /export?session=<SESSION_ID>
```
- **Response**: `200 OK` with Content-Type `application/x-ndjson`.
- Streams samples line-by-line followed by loss records:
  ```json
  {"sensor": 1, "index": 0, "values": [0, 1, 2]}
  {"sensor": 2, "from_index": 4000, "to_index": 4399, "reason": "buffer_overflow"}
  ```

---

## 3. Local Evaluation Simulator

Judges can also run the self-contained cloud simulator locally:
```bash
python3 -m cloud.service --port 8000 --db-path /tmp/eval_cloud.db --token <EVALUATION_AUTH_TOKEN>
```
