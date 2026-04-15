# Kafka + Spark Streaming Log Analyzer

A beginner-friendly mini project that runs fully on Docker Compose and performs real-time log analytics with Kafka + PySpark Structured Streaming.

## Project structure

```text
kafka-spark-log-analyzer/
├── docker-compose.yml
├── producer/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── log_producer.py
├── spark-job/
│   ├── Dockerfile
│   └── log_analyzer.py
└── README.md
```

## Prerequisites

- Docker Desktop for Mac
- Docker Compose v2 (bundled with Docker Desktop)

## Start everything

From the repository root:

```bash
docker compose up --build
```

This starts:
- Zookeeper
- Kafka broker (`localhost:9092` from host, `broker:29092` inside Docker network)
- Kafdrop UI (`http://localhost:9000`) — browse topics, partitions, and messages
- Spark master (`http://localhost:8080`)
- Spark worker
- Python log producer (writes to `web-logs`)
- Spark streaming analyzer job

## Observe output

- Spark job streaming output:

```bash
docker compose logs -f spark-job
```

- Producer output:

```bash
docker compose logs -f producer
```

- Spark UI:
  - Open `http://localhost:8080`

## Tear down

```bash
docker compose down -v
```

## Architecture diagram

```text
                 ┌──────────────┐
                 │  Zookeeper   │
                 │    :2181     │
                 └──────┬───────┘
                        │
                        ▼
┌──────────────┐   ┌──────────────┐        ┌──────────────┐
│   Producer   │──▶│    Kafka     │───────▶│   spark-job  │
│   (Python)   │   │  web-logs    │  read  │  (Structured │
│ ~5-10 ev/sec │   │   :29092     │ Stream │   Streaming) │
└──────────────┘   └──────────────┘        └──────┬───────┘
                                                  │ spark-submit
                                                  ▼
                                        ┌──────────────────┐
                                        │   spark-master   │
                                        │  :7077, UI :8080 │
                                        └────────┬─────────┘
                                                 │
                                                 ▼
                                        ┌──────────────────┐
                                        │   spark-worker   │
                                        │     UI :8081     │
                                        └──────────────────┘

   Streaming queries (output to console / docker logs):
     1. Error rate per 1-minute window
     2. Top 5 endpoints + avg response time
     3. Slow requests (> SLOW_REQUEST_THRESHOLD_MS)
```

## Architecture summary

1. The **producer** generates realistic fake web log events as JSON and publishes to Kafka topic `web-logs`.
2. Kafka runs on the shared Docker network and exposes:
   - `broker:29092` for internal containers
   - `localhost:9092` for host tools
3. The **Spark Structured Streaming** job consumes Kafka events, parses JSON with schema, applies a 10-second watermark, and runs 3 parallel streaming analytics queries:
   - Error rate per 1-minute window
   - Top endpoints (top 5) with average response time
   - Slow request alerts (`response_time_ms > 1000`)
