import json
import os
import random
import socket
import time
from datetime import datetime, timezone

from kafka import KafkaProducer

# Kafka connection details can be overridden from Docker Compose environment variables.
BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "broker:29092")
TOPIC = os.getenv("KAFKA_TOPIC", "web-logs")

METHODS = ["GET", "POST", "PUT", "DELETE"]
ENDPOINTS = [
    "/",
    "/login",
    "/logout",
    "/health",
    "/api/users",
    "/api/users/123",
    "/api/orders",
    "/api/orders/456",
    "/api/products",
    "/api/cart",
]

# Weighted response statuses: successful responses are most common, server errors are rare.
STATUS_CODES = [200, 200, 200, 200, 201, 204, 301, 400, 401, 403, 404, 429, 500, 502, 503]


def random_ip() -> str:
    """Generate a realistic public IPv4-like address."""
    return ".".join(str(random.randint(1, 254)) for _ in range(4))


def make_event() -> dict:
    """Create one fake web log event payload."""
    status_code = random.choice(STATUS_CODES)
    response_time = (
        random.randint(40, 500)
        if status_code < 500
        else random.randint(400, 1800)
    )

    return {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "ip": random_ip(),
        "method": random.choice(METHODS),
        "endpoint": random.choice(ENDPOINTS),
        "status_code": status_code,
        "response_time_ms": response_time,
    }


def wait_for_kafka(timeout_seconds: int = 120) -> None:
    """Wait until Kafka broker is reachable before starting the producer."""
    first_broker = BOOTSTRAP_SERVERS.split(",")[0].strip()
    host, port = first_broker.rsplit(":", 1)
    port = int(port)
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                print(f"Kafka is reachable at {BOOTSTRAP_SERVERS}")
                return
        except OSError:
            print(f"Waiting for Kafka at {BOOTSTRAP_SERVERS}...")
            time.sleep(2)

    raise TimeoutError(f"Kafka not reachable at {BOOTSTRAP_SERVERS} within {timeout_seconds} seconds")


def main() -> None:
    """Produce fake logs continuously at ~5-10 events per second."""
    wait_for_kafka()

    producer = KafkaProducer(
        bootstrap_servers=BOOTSTRAP_SERVERS,
        value_serializer=lambda value: json.dumps(value).encode("utf-8"),
        retries=10,
    )

    while True:
        event = make_event()
        producer.send(TOPIC, value=event)
        producer.flush()
        print(f"Produced: {event}")

        # Sleep 0.1-0.2s => roughly 5-10 events per second.
        time.sleep(random.uniform(0.1, 0.2))


if __name__ == "__main__":
    main()
