# The Database Playbook

A working reference for picking storage at scale — written for engineers who already know what an index is, and want to think one rung higher.

The goal is not to teach databases. It is to teach the **shape of decisions** senior engineers make when the wrong choice costs millions, and to put names to the patterns that show up in every architecture review.

---

## How to read this doc

Every database section follows the same template, so you can scan-compare across systems:

| Section | What it tells you |
|---|---|
| **Identity** | One sentence: what this thing actually is, stripped of marketing. |
| **Mental model** | The metaphor that makes it click. |
| **Sweet spot** | Where it is the obviously correct answer. |
| **Don't use it for** | The anti-patterns that wreck careers. |
| **How it breaks at scale** | The cliff at 10× or 100× growth that surprises people. |
| **What seniors watch for** | Heuristics from architecture review. |
| **Cost & ops burden** | Rough signal. Not a quote. |
| **War stories** | Real companies, named, public sources. |

A scenarios chapter at the end works the other direction: starting from the **problem** (payments, Black Friday, search, observability), it walks you to the right stack.

---

## Table of contents

1. [Frame: CAP, PACELC, and why this is the wrong question](#frame)
2. [PostgreSQL](#postgresql)
3. ClickHouse *(coming next)*
4. Cassandra
5. Redis
6. Elasticsearch
7. Prometheus
8. Kafka
9. Apache Iceberg
10. ZooKeeper
11. Scenarios — payments, e-commerce, Black Friday, search, observability, data lake, leaderboards, fraud, recommendations, multi-tenant SaaS
12. Decision flowchart
13. Anti-pattern hall of fame

---

<a id="frame"></a>
## Frame: CAP, PACELC, and why this is the wrong question

CAP is the diagram every engineer sees first and the one most stop at. It says: under a network partition, choose Consistency or Availability. True, but underpowered. Real systems spend 99.9% of their lives **not** partitioned, and the interesting trade lives there.

**PACELC** extends it: *if Partition, choose A or C; else, choose Latency or Consistency.* Spanner is PC/EC — consistent always, paid for in latency. DynamoDB is PA/EL — fast always, eventually consistent. That second clause is what you live with daily.

But picking a database is not really a CAP question at all. It is a **workload shape** question:

- What is the unit of work? (one row, one document, one billion rows scanned)
- What is the read/write ratio?
- Does the data mutate, or is it append-only?
- Do you need joins? Transactions? Secondary indexes?
- What does failure look like to the user — wrong number, or no number?
- Who runs this thing at 3 a.m. when it pages?

Hold those questions in mind as you read. The CAP corner each system occupies is a *consequence* of those design choices, not the starting point.

---

<a id="postgresql"></a>
## PostgreSQL

### Identity
A single-node, row-oriented, ACID relational database with a planner so good that it has quietly eaten most of the OLTP world. The default answer when no other answer is obviously correct.

### Mental model
A **library with a meticulous head librarian**. Every book has a place, every reference is checked at the door, every transaction is logged in ink. The librarian is fast and never lies, but there is exactly one of them, and the library has walls. To grow beyond the building, you either build annexes (read replicas), photocopy by category (sharding), or hire a chain (Aurora, Spanner-likes).

### Sweet spot
- Application state of record: users, accounts, orders, inventory, subscriptions.
- Anything where **a single wrong row is a bug**, not a rounding error.
- Workloads under roughly **50 TB and 50K writes/sec per primary** — the zone where one well-tuned cluster handles it without heroics.
- Mixed workloads: OLTP with occasional analytical queries, JSON documents alongside relational tables, geospatial via PostGIS, full-text via `tsvector`, vector search via `pgvector`. Postgres is the Swiss Army knife that is genuinely good at most blades.
- Anywhere you want **the database to enforce correctness** — foreign keys, `CHECK` constraints, unique indexes, exclusion constraints, transactional DDL.

### Don't use it for
- **High-cardinality analytical scans over billions of rows.** Row storage punishes you. ClickHouse, BigQuery, Snowflake exist for this. Engineers who try to "just add an index" on a 10B-row table learn this on a Tuesday.
- **Write-heavy time-series at firehose scale** (millions of points/sec). Use TimescaleDB extension if you must stay in Postgres-land, otherwise a real TSDB.
- **Global multi-region active-active writes.** Postgres replication is primary-to-replica. Multi-master setups (BDR, pglogical) exist and are operationally treacherous. Reach for Spanner/CockroachDB/Yugabyte if this is a hard requirement.
- **Storing raw event blobs you never query individually** (logs, metrics, raw JSON dumps). Object storage + Iceberg/Parquet is 1/100th the cost.
- **Queue / pub-sub workloads at scale.** `SELECT FOR UPDATE SKIP LOCKED` is a charming trick that works until it doesn't. Use Kafka, SQS, or a real broker once throughput matters.

### How it breaks at scale
The Postgres scaling story has predictable cliffs. Knowing them is the difference between a senior and a staff engineer.

1. **Connection storm (the first cliff, hits early).** Each connection is a forked OS process holding ~10 MB. At 500+ connections the system thrashes. The fix is non-negotiable: **PgBouncer** (or pgcat) in transaction-pooling mode, sitting between every app and the database. Teams that skip this re-discover the problem at 2 a.m. during a launch.

2. **Vacuum and bloat.** MVCC means updates and deletes leave dead tuples that `VACUUM` cleans up. On large, hot tables the autovacuum cannot keep up; bloat eats disk and slows queries. Worst case: **transaction ID wraparound**, which can take the database offline. Sentry famously hit this; their post-mortem ("Transaction ID Wraparound: A Postgres Horror Story") is required reading.

3. **The single-primary write ceiling.** Read replicas scale reads, not writes. When one primary cannot ingest fast enough, you face the hardest decision in Postgres-land: **shard, or migrate.** Sharding Postgres is a real engineering project — months, not weeks.

4. **Long-running transactions and replication lag.** A forgotten `BEGIN` in a worker holds vacuum back across the whole cluster. Replicas drift. Failover gets dangerous.

5. **Schema migrations at scale.** `ALTER TABLE ADD COLUMN NOT NULL DEFAULT ...` rewrites the table and takes a lock. On a 500 GB table that is an outage. GitHub built `gh-ost` and Shopify built `lhm` precisely for this; in Postgres-land the equivalents are `pg_repack` and careful migration patterns (add nullable, backfill, then add constraint).

### What seniors watch for
- **"We'll just add an index"** on a write-hot table. Every index slows every write and bloats faster. Indexes are not free.
- **`SELECT *` in production code paths.** Row stores read whole rows; wide tables make this painful.
- **JSONB used as a schema dodge.** Fine for genuinely variable payloads; a disaster when used to avoid the migration conversation. The data drifts, the queries get untestable, and three years later nobody knows the shape.
- **No PgBouncer.** Instant red flag in any review.
- **Sync replication enabled without understanding the latency cost** — a single slow replica freezes commits.
- **Migrations that lock big tables.** Look for `CONCURRENTLY` on index creation, online-migration tooling for column changes.
- **Cross-shard queries** in a sharded setup. If you find yourself writing them often, the shard key is wrong.

### Cost & ops burden
- **Self-managed:** cheapest per GB, highest ops cost. Needs a DBA-shaped human or team once you cross ~10 TB or HA-critical territory.
- **Managed (RDS, Cloud SQL, Azure):** ~2–3× the raw infra cost; eliminates 80% of the operational pain. The default sane choice for most teams under ~100 engineers.
- **Aurora Postgres:** ~3–5× self-managed cost. Buys you a distributed storage layer (6-way replication across 3 AZs) and faster failover. Worth it for revenue-critical workloads until you are huge enough to build the equivalent yourself.
- **CockroachDB / Yugabyte:** Postgres-compatible distributed SQL. Pay 2–4× the latency and significantly more in license/infra. Buy them when global multi-region writes or unbounded horizontal scale is a hard requirement, not before.

### War stories
- **Notion — sharding Postgres (2021).** Outgrew a single primary holding their block storage. Sharded into 32 logical shards (later 96, then 480) using a deterministic hash on workspace ID. Public post: *"Herding elephants: lessons learned from sharding Postgres at Notion."* The lesson everyone quotes: **shard before you have to, because sharding under fire is the worst project of your year.**
- **Figma — sharding Postgres (2023).** Took a different path: **vertical partitioning first** (split tables across databases by feature area), then horizontal sharding only on the largest table. Public post: *"How Figma's databases team lived to tell the scale."* The lesson: **the shard key is the most expensive decision you will make for the next decade.** They spent months choosing.
- **GitLab's October 2017 incident.** A tired engineer ran `rm -rf` on the wrong primary during a replication issue. Backups were broken in five different ways. They lost ~6 hours of production data and live-streamed the recovery. Lesson: **untested backups are not backups**, and Postgres ops is a discipline, not a checkbox.
- **Basecamp / 37signals.** Famously runs the entire business on boring MySQL/Postgres setups, no microservices, modest replica counts. Public stance: most companies do not have Google's problems and should stop pretending they do. A useful counterweight to the "we need CockroachDB" instinct.

---

*Next up: ClickHouse. Tell me if the depth, tone, and structure land — too long, too short, too opinionated, too dry — and I'll calibrate before continuing.*
