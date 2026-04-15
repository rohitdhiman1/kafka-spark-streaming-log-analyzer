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
3. [ClickHouse](#clickhouse)
4. Redis *(coming next)*
5. Prometheus
6. Kafka
7. Apache Iceberg
8. ZooKeeper
9. Elasticsearch
10. Cassandra
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

```text
   scale →    100 GB        1 TB         10 TB        50 TB        100 TB+
              │             │             │             │             │
  cliff:   ┌──┴──┐       ┌──┴──┐       ┌──┴──┐       ┌──┴──┐       ┌──┴──┐
           │ Conn│       │Vacuum│      │Schema│      │Write │      │Shard│
           │storm│       │/bloat│      │migrtn│      │ceiling│      │ or  │
           └─────┘       └──────┘      └──────┘      └──────┘      │bust │
                                                                    └─────┘
   fix:  PgBouncer    Tune autovac   gh-ost-style    Read replicas  Notion/
         (always)     + pg_repack    online migrn    (reads only)   Figma-
                                                                    style
                                                                    sharding
```

1. **Connection storm (the first cliff, hits early).** Each connection is a forked OS process holding ~10 MB. At 500+ connections the system thrashes. The fix is non-negotiable: **PgBouncer** (or pgcat) in transaction-pooling mode, sitting between every app and the database. Teams that skip this re-discover the problem at 2 a.m. during a launch.

   ```text
   Without PgBouncer (breaks ~500 conns)         With PgBouncer (handles 10K+)
   ────────────────────────────────────          ──────────────────────────────
        app pods                                       app pods
   ┌───┐ ┌───┐ ┌───┐ ┌───┐ ... 1000s            ┌───┐ ┌───┐ ┌───┐ ... 1000s
     │     │     │     │                          │     │     │
     └──┬──┴──┬──┴──┬──┴──┐                       └──┬──┴──┬──┴──┐
        ▼     ▼     ▼     ▼                          ▼     ▼     ▼
     ┌─────────────────────┐                      ┌──────────────┐
     │     Postgres        │                      │  PgBouncer   │  ← pools
     │ 1000 forked procs   │ ← 10GB RAM gone      │  100 actual  │    & multiplexes
     │  thrashing          │                      │  conns       │
     └─────────────────────┘                      └──────┬───────┘
                                                         ▼
                                                  ┌──────────────┐
                                                  │   Postgres   │
                                                  │  ~50 procs   │
                                                  └──────────────┘
   ```

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

| Flavor | Cost (relative) | Ops burden | When it's the right answer |
|---|---|---|---|
| **Self-managed** | 1× | High — needs a DBA-shaped human past ~10 TB or HA-critical | Cost-sensitive, in-house expertise exists |
| **RDS / Cloud SQL / Azure** | 2–3× | Low — backups, failover, patching handled | Default sane choice for teams under ~100 engineers |
| **Aurora Postgres** | 3–5× | Low — plus 6-way replicated storage, faster failover | Revenue-critical workloads; not yet huge enough to build the equivalent |
| **CockroachDB / Yugabyte** | 5–10× + license | Medium-high — distributed system ops | Global multi-region active-active is a hard requirement, not a nice-to-have |

The trap is buying the next tier "to be safe." Each step up costs real money and real complexity; only pay for the constraint you actually have.

### War stories
- **Notion — sharding Postgres (2021).** Outgrew a single primary holding their block storage. Sharded into 32 logical shards (later 96, then 480) using a deterministic hash on workspace ID. Public post: *"Herding elephants: lessons learned from sharding Postgres at Notion."* The lesson everyone quotes: **shard before you have to, because sharding under fire is the worst project of your year.**
- **Figma — sharding Postgres (2023).** Took a different path: **vertical partitioning first** (split tables across databases by feature area), then horizontal sharding only on the largest table. Public post: *"How Figma's databases team lived to tell the scale."* The lesson: **the shard key is the most expensive decision you will make for the next decade.** They spent months choosing.
- **GitLab's October 2017 incident.** A tired engineer ran `rm -rf` on the wrong primary during a replication issue. Backups were broken in five different ways. They lost ~6 hours of production data and live-streamed the recovery. Lesson: **untested backups are not backups**, and Postgres ops is a discipline, not a checkbox.
- **Basecamp / 37signals.** Famously runs the entire business on boring MySQL/Postgres setups, no microservices, modest replica counts. Public stance: most companies do not have Google's problems and should stop pretending they do. A useful counterweight to the "we need CockroachDB" instinct.

---

---

<a id="clickhouse"></a>
## ClickHouse

### Identity
A column-oriented, vectorized, distributed OLAP database built at Yandex to crunch web analytics in real time. Open source, written in C++, optimized for scanning billions of rows per second on commodity hardware. The default modern answer when "raw events, ad-hoc analytical queries, sub-second" is the requirement.

### Mental model
A **warehouse with high-speed conveyor belts**. Boxes (rows) are unpacked at the door and their contents (columns) are sorted onto separate conveyors. To answer "what is the average weight of red boxes shipped last month," the warehouse runs only three conveyors — color, weight, ship-date — past a counter at full speed, ignoring everything else. To find one specific box by its barcode, the warehouse is the wrong building entirely; that is what a library (Postgres) is for.

### How storage actually differs

The single most important diagram in this entire doc. Internalize this and you will never confuse OLTP and OLAP again.

```text
ROW STORE (Postgres)                    COLUMN STORE (ClickHouse)
─────────────────────                    ──────────────────────────
disk layout:                             disk layout:

[ts|ip|method|endpoint|status|rt_ms]     ts:        [t1, t2, t3, t4, ...]
[ts|ip|method|endpoint|status|rt_ms]     ip:        [i1, i2, i3, i4, ...]
[ts|ip|method|endpoint|status|rt_ms]     method:    [GET, GET, POST, GET, ...]
[ts|ip|method|endpoint|status|rt_ms]     endpoint:  [/a, /b, /a, /c, ...]
[ts|ip|method|endpoint|status|rt_ms]     status:    [200, 200, 500, 200, ...]
... 1B rows                              rt_ms:     [42, 51, 1800, 38, ...]

query: SELECT avg(rt_ms) FROM logs       query: same
       WHERE endpoint='/a'                       
                                                 
disk reads:                              disk reads:
├─ every column of every row             ├─ endpoint column only (filter)
├─ ~200 GB scanned                       ├─ rt_ms column only (aggregate)
└─ even though you needed 2 columns      ├─ ~4 GB scanned (50× less)
                                         └─ + compression typically 10×
                                            → ~400 MB actually read
```

The 500× difference is not a benchmark trick. It is the architecture. Everything else ClickHouse does — vectorized execution, sparse indexes, data-skipping — is leverage on top of this one decision.

### Sweet spot
- **Observability and analytics over raw events.** Logs, metrics, spans, clickstream, ad impressions, security telemetry. Anything append-only with billions of rows where users want to slice and dice.
- **Real-time dashboards** over recent + historical data: "p99 latency by endpoint, by region, last 30 days, refresh every 5 seconds."
- **Replacing slower OLAP** stacks: Druid, Pinot, Elasticsearch-as-analytics, even Redshift/BigQuery for hot data.
- **Cost-sensitive analytics at scale.** ClickHouse on commodity hardware is typically 5–20× cheaper per query than Snowflake or BigQuery for the same workload, with the trade-off being you run it.

### Don't use it for
- **OLTP.** No real transactions. Single-row updates are async mutations that rewrite parts. A login flow on ClickHouse is malpractice.
- **Point lookups.** `WHERE user_id = 12345` will work, but it scans a granule (~8K rows) minimum. Postgres or Redis returns in 1 ms; ClickHouse takes 50–200 ms and burns CPU.
- **Frequent updates and deletes.** "Mutation" in ClickHouse is a heavyweight async operation. Acceptable for occasional GDPR deletes; catastrophic as a normal pattern. If your data mutates row-by-row, you have picked the wrong tool.
- **Heavy joins between large tables.** The optimizer is weak at this. Pattern: denormalize at write time, or use `Dictionary` for small lookup tables. Engineers from Postgres-land try to recreate their normalized schema and watch queries die.
- **High-concurrency tiny queries.** Each query parallelizes across cores; 5,000 concurrent `SELECT * WHERE id=?` will thrash. ClickHouse expects tens of heavy queries, not thousands of small ones.
- **Tiny datasets.** Below ~100 GB, a well-indexed Postgres outperforms ClickHouse on most queries and costs nothing extra to run.

### How it breaks at scale

```text
   scale →   100M rows     1B rows      10B rows     100B rows     1T rows+
              │             │             │             │             │
  cliff:   ┌──┴──┐       ┌──┴──┐       ┌──┴──┐       ┌──┴──┐       ┌──┴──┐
           │"too │       │ ZK/  │      │ JOIN │      │Mutatn│      │Multi-│
           │many │       │Keeper│      │ pain │      │backlog│      │region│
           │parts"│      │limits│      │      │      │      │      │ pain │
           └─────┘       └──────┘      └──────┘      └──────┘      └─────┘
   fix:  Batch inserts  ClickHouse   Denormalize    Avoid muta-   Sharded
         (10K-100K)     Keeper +     at write,      tions; use    cluster
         + Buffer       larger ZK    Dictionary     ReplacingMT   per region
         engine         ensemble     for lookups    /AggregMT
```

1. **"Too many parts" error (the first cliff, hits early).** ClickHouse stores inserts as immutable parts that merge in the background. Insert one row at a time and you create thousands of tiny parts faster than the merger can keep up. The server then refuses writes with the dreaded `Too many parts` error. **Fix is mandatory: batch inserts of 10K–100K rows, or put a `Buffer` engine / Kafka in front.** This is the #1 newcomer mistake.

2. **ZooKeeper / Keeper as the bottleneck.** Replicated tables coordinate through ZK (or the newer ClickHouse Keeper). Every insert, every merge generates ZK ops. At ~10B rows/day across many tables, ZK becomes the limiter before ClickHouse does. Migration to Keeper (which speaks the same protocol but is C++ and embedded) is now the standard fix.

3. **Joins fall over.** ClickHouse joins broadcast the right table to every node by default. Two 1B-row tables joined this way is a node-killer. Senior teams design schemas to avoid it: pre-join at ingest, denormalize aggressively, use `Dictionary` (in-memory hash table) for small lookups, or use the experimental `GLOBAL JOIN` carefully.

4. **Mutation backlog.** Every `ALTER TABLE ... DELETE WHERE ...` or `UPDATE` queues a mutation that rewrites affected parts. Run a few of these on a 10 TB table and the cluster spends days catching up while query latency degrades. Pattern: model the schema so mutations are unnecessary (use `ReplacingMergeTree`, `CollapsingMergeTree`, or `AggregatingMergeTree` engines that handle "updates" as inserts).

5. **Multi-region pain.** ClickHouse replication is async and chatty. Cross-region clusters work but suffer. Most large deployments run **independent clusters per region** with a query router on top, accepting that data is region-local.

### What seniors watch for
- **Inserts that aren't batched.** Look for `INSERT INTO ... VALUES (single_row)` in the codebase. Instant red flag. The fix is upstream — Kafka + a consumer that batches, or a `Buffer` table.
- **`ORDER BY` keys chosen casually.** This is ClickHouse's most consequential schema decision. The `ORDER BY` clause defines disk layout and the sparse primary index. Wrong choice = data-skipping does not work = full scans on every query. Should match the most common filter columns.
- **Wide schemas with hundreds of columns** that are mostly null. ClickHouse handles it, but query planning slows and disk fills. Often a sign that someone is storing JSON-as-columns when a `Map` or `JSON` type would do.
- **Materialized views used as triggers.** They are powerful and dangerous — they fire on inserts, can fail silently, and recovering from a bad MV is painful. Use them deliberately, document them ruthlessly.
- **Secondary indexes (`skip indexes`) added everywhere.** They are not Postgres B-trees. They prune granules, and only help if cardinality and distribution cooperate. Often added in panic, rarely removed.
- **Distributed table with no `sharding_key` discipline.** Random distribution makes joins worse and breaks data-locality optimizations.

### Cost & ops burden

| Flavor | Cost (relative) | Ops burden | When it's the right answer |
|---|---|---|---|
| **Self-managed OSS** | 1× | High — cluster ops, ZK/Keeper, upgrades, backups all yours | Have ops muscle; cost is the primary constraint |
| **ClickHouse Cloud** | 3–5× | Low — Altinity / ClickHouse Inc. handle the ugly parts | Default for teams that want the engine, not the operations |
| **Altinity.Cloud / managed by 3rd party** | 2–4× | Low–medium | Want managed but on your own AWS account |
| **Alternatives — Druid, Pinot** | similar infra | Higher ops than ClickHouse, lower than self-built | Real-time ingestion + sub-second queries with very high concurrency (Druid/Pinot win here historically) |
| **Alternatives — BigQuery / Snowflake** | 5–20× per query | Near-zero | Spiky workloads; ops budget is zero; data is small enough that per-query cost stays sane |

The honest summary: **ClickHouse is the price-performance leader for analytical workloads**, but you pay for that with operational complexity. A team of fewer than ~15 engineers should think hard before self-hosting.

### War stories
- **Cloudflare — HTTP analytics (2018, ongoing).** Migrated from a Citus + Kafka stack to ClickHouse to handle ~70M+ HTTP events/sec across the edge. Public post: *"HTTP Analytics for 6M requests per second using ClickHouse."* Lesson: **at firehose scale, the column store / row store choice is not a preference, it is the only thing that works.**
- **Uber — Logging platform (2020).** Replaced an Elasticsearch-based logging stack with ClickHouse, citing 10× cost reduction and faster queries on trillions of rows. Public post: *"Fast and Reliable Schema-Agnostic Log Analytics Platform."* Lesson: **Elasticsearch is a search engine; analytics over logs is a different workload, and treating it as search burns money.**
- **eBay — moving off Druid (2022).** Migrated their behavioral analytics platform from Druid to ClickHouse for "simpler ops and better cost." Public post on eBay tech blog. Lesson: **Druid's real-time ingestion superpowers come with operational complexity that ClickHouse increasingly matches at lower burden.**
- **Sentry — error event storage (2019).** Built `Snuba` as a thin service over ClickHouse for searching billions of error events. Public posts and open-source code. Lesson: **don't try to make Postgres serve queries it was never built for; put the right tool behind a service boundary.**
- **GitHub — code search and audit log.** Uses ClickHouse for high-cardinality event analytics where Postgres would melt. The engineering blog covers parts of this publicly.
- **The cautionary tale — ContentSquare (2021).** Public post-mortem describing how careless `INSERT` patterns produced a "too many parts" outage on a multi-billion-row table. Recovery took days. Lesson: **the batching discipline is not optional; it is the contract.**

---

*Next up: Cassandra. Same template, same visual density. Tell me to continue or adjust first.*
