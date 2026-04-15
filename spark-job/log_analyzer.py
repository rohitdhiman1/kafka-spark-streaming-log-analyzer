import os

from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import IntegerType, StringType, StructField, StructType, TimestampType

# Kafka settings are loaded from environment for easy Docker Compose wiring.
BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "broker:29092")
TOPIC = os.getenv("KAFKA_TOPIC", "web-logs")
SLOW_REQUEST_THRESHOLD_MS = int(os.getenv("SLOW_REQUEST_THRESHOLD_MS", "1000"))


# Explicit schema for incoming JSON payload from Kafka.
log_schema = StructType(
    [
        StructField("timestamp", TimestampType(), True),
        StructField("ip", StringType(), True),
        StructField("method", StringType(), True),
        StructField("endpoint", StringType(), True),
        StructField("status_code", IntegerType(), True),
        StructField("response_time_ms", IntegerType(), True),
    ]
)


spark = (
    SparkSession.builder.appName("KafkaSparkLogAnalyzer")
    .config("spark.sql.shuffle.partitions", "2")
    .getOrCreate()
)
spark.sparkContext.setLogLevel("WARN")

# Read Kafka stream and keep running even if older offsets are missing.
raw_stream = (
    spark.readStream.format("kafka")
    .option("kafka.bootstrap.servers", BOOTSTRAP_SERVERS)
    .option("subscribe", TOPIC)
    .option("startingOffsets", "latest")
    .option("failOnDataLoss", "false")
    .load()
)

# Parse JSON payload and keep only schema-defined columns.
logs = (
    raw_stream.selectExpr("CAST(value AS STRING) AS json_str")
    .select(F.from_json(F.col("json_str"), log_schema).alias("data"))
    .select("data.*")
    .where(F.col("timestamp").isNotNull())
    .withWatermark("timestamp", "10 seconds")
)

# Query 1: Error rate per 1-minute tumbling window.
error_rate = (
    logs.groupBy(F.window("timestamp", "1 minute").alias("window"))
    .agg(
        F.count("*").alias("total_requests"),
        F.sum(F.when(F.col("status_code") >= 400, 1).otherwise(0)).alias("error_count"),
    )
    .withColumn(
        "error_percentage",
        F.round((F.col("error_count") / F.col("total_requests")) * 100, 2),
    )
)

error_query = (
    error_rate.writeStream.outputMode("update")
    .format("console")
    .option("truncate", "false")
    .trigger(processingTime="30 seconds")
    .queryName("error_rate")
    .start()
)


# Query 2: Top 5 endpoints by hit count (+ avg response time) in 1-minute windows.
def show_top_endpoints(batch_df, batch_id):
    print(f"\n===== Top Endpoints (batch {batch_id}) =====")
    batch_df.orderBy(F.col("hit_count").desc()).limit(5).show(truncate=False)


endpoint_window_agg = logs.groupBy(
    F.window("timestamp", "1 minute").alias("window"),
    F.col("endpoint"),
).agg(
    F.count("*").alias("hit_count"),
    F.round(F.avg("response_time_ms"), 2).alias("avg_response_time_ms"),
)

top_endpoints_query = (
    endpoint_window_agg.writeStream.outputMode("update")
    .foreachBatch(show_top_endpoints)
    .trigger(processingTime="30 seconds")
    .queryName("top_endpoints")
    .start()
)

# Query 3: Slow request alert stream.
slow_requests = logs.filter(F.col("response_time_ms") > SLOW_REQUEST_THRESHOLD_MS)

slow_query = (
    slow_requests.writeStream.outputMode("update")
    .format("console")
    .option("truncate", "false")
    .trigger(processingTime="30 seconds")
    .queryName("slow_requests")
    .start()
)

spark.streams.awaitAnyTermination()
