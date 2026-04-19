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
4. [Redis](#redis)
5. [Prometheus](#prometheus)
6. [Kafka](#kafka)
7. [ZooKeeper](#zookeeper)
8. [Apache Iceberg](#iceberg)
9. [Elasticsearch](#elasticsearch)
10. [Cassandra](#cassandra)
11. [Scenarios](#scenarios) — payments, e-commerce, Black Friday, search, observability, data lake, leaderboards, fraud, recommendations, multi-tenant SaaS
12. [Decision flowchart](#decision-flowchart)
13. [Anti-pattern hall of fame](#anti-pattern-hall-of-fame)

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

---

<a id="redis"></a>
## Redis

### Identity
A single-threaded, in-memory **data structure server** that happens to be the world's most popular cache. The "key-value store" label is the single biggest misnomer in databases — Redis ships strings, hashes, lists, sets, sorted sets, bitmaps, hyperloglogs, streams, and geospatial indexes, all addressable by key, all manipulated atomically. The cache is the appetizer; the data structures are the meal.

### Mental model
A **giant chalkboard with a single, very fast scribe**. Every command is one operation by one hand. Because there is exactly one scribe, every command is atomic — no locks needed, no race conditions inside a command. The chalkboard lives in RAM, so reads and writes are nanoseconds. The same property is also the ceiling: one scribe means one CPU core's worth of work per shard, no matter how big the machine.

### What Redis actually offers (the cheat sheet most engineers skip)

| Data structure | Killer use case | Common command |
|---|---|---|
| **String** | Cache values, counters, feature flags | `GET`, `SET`, `INCR` |
| **Hash** | Session objects, sparse user records | `HGET`, `HSET`, `HINCRBY` |
| **List** | Job queues (small scale), recent-items feeds | `LPUSH`, `RPOP`, `BRPOP` |
| **Set** | Tags, unique visitor tracking, set algebra | `SADD`, `SINTER`, `SISMEMBER` |
| **Sorted set (ZSET)** | **Leaderboards, time-series, priority queues, rate limiting** | `ZADD`, `ZRANGEBYSCORE` |
| **Bitmap** | Active-user bitmasks, A/B test buckets | `SETBIT`, `BITCOUNT` |
| **HyperLogLog** | Cardinality estimation (unique counts) at tiny memory cost | `PFADD`, `PFCOUNT` |
| **Stream** | Append-only log, lightweight Kafka-alike | `XADD`, `XREADGROUP` |
| **Geo** | "Find pubs within 2 km" | `GEOADD`, `GEOSEARCH` |
| **Pub/Sub** | Fire-and-forget broadcast | `PUBLISH`, `SUBSCRIBE` |

The sorted set alone is responsible for half the senior-engineer "Redis is magic" reactions. A leaderboard with rank, range, and score queries in O(log N) is one line of code.

### Sweet spot
- **Caching.** The 80% case. Cache-aside pattern, TTLs everywhere, hit ratio in the high 90s.
- **Session stores.** Hash per session, TTL aligned with session lifetime. Stateless app servers, horizontal scale, no sticky sessions needed.
- **Rate limiting.** `INCR` + `EXPIRE`, or sorted-set sliding windows. The standard pattern at every API gateway.
- **Leaderboards and ranked feeds.** Sorted sets are unbeatable. Twitter timelines, gaming leaderboards, "top stories" widgets.
- **Distributed locks** *(with care — see anti-patterns)*. Short-lived mutual exclusion across processes.
- **Real-time counters and metrics** that don't need durability — page views, active users, "X people are looking at this hotel right now."
- **Pub/Sub for fire-and-forget broadcasts** at modest scale.
- **Streams for lightweight queueing** when Kafka is overkill but in-memory queues aren't enough.

### Cache-aside pattern (the canonical Redis architecture)

```text
   ┌─────────┐    1. GET key             ┌─────────┐
   │   App   │ ───────────────────────▶  │  Redis  │
   │         │ ◀───── HIT (value) ──────  │  cache  │
   └────┬────┘                           └─────────┘
        │ MISS
        ▼
   ┌─────────┐    2. SELECT ...          ┌─────────┐
   │ Postgres│ ◀───────────────────────  │   App   │
   │  (truth)│ ───── row ──────────────▶ │         │
   └─────────┘                           └────┬────┘
                                              │
                                              │ 3. SET key value EX 300
                                              ▼
                                         ┌─────────┐
                                         │  Redis  │
                                         └─────────┘
```

Three rules nobody writes down but everyone learns the hard way:
1. **TTL everything.** A cache without expiry is a memory leak with extra steps.
2. **Cache the result, not the query.** Cache by canonical key, not by user input string.
3. **Invalidate on write, not on read.** And accept that cache invalidation is, as Phil Karlton said, one of the two hard problems in computer science.

### Don't use it for
- **Source of truth.** Redis is RAM. Persistence (RDB snapshots, AOF log) exists, but the design center is "if I lost everything, the system would heal." Treat it as accelerator, not vault.
- **Datasets bigger than RAM.** Redis on Flash and Redis Enterprise tiered storage exist but blunt the speed advantage. If your working set won't fit in memory across a reasonable cluster, use a different tool.
- **Complex queries, joins, secondary indexes.** Not a database in that sense. RediSearch module adds some of this, but if you need real query capability, reach for Postgres.
- **Durable queues at scale.** Lists and Streams work, but losing messages on failover is a real risk. SQS, Kafka, and RabbitMQ exist for a reason.
- **Pub/Sub for anything that must not be lost.** It is fire-and-forget — subscribers offline at publish time miss the message forever. (Streams give you durability; classic pub/sub does not.)
- **Cross-key transactions you actually trust.** `MULTI/EXEC` is atomic on a single shard but provides no isolation across shards in Redis Cluster.
- **Big values.** A 100 MB string blocks the single thread for tens of milliseconds. Tail latency dies. Keep values under ~100 KB.

### How it breaks at scale

```text
   scale →   1 GB RAM     10 GB RAM    100 GB+      multi-node     multi-region
              │             │             │             │             │
  cliff:   ┌──┴──┐       ┌──┴──┐       ┌──┴──┐       ┌──┴──┐       ┌──┴──┐
           │ Hot │       │ Big │       │Single│      │Cluster│     │Cross-│
           │ key │       │ key │       │thread│      │ resh-│      │region│
           │      │      │block │      │ceiling│     │arding│      │ async│
           └─────┘       └──────┘      └──────┘      └──────┘      └─────┘
   fix:  Local cache    Chunk values  Shard via     Plan slot     Region-
         (L1) +         or move to    Cluster;      moves         local;
         hash-tag       Postgres      add KeyDB/    carefully;    accept
         distribution                 Dragonfly     downtime      eventual
                                      for multi-                  per region
                                      core
```

1. **The hot key (the first cliff, hits early).** A single key getting 100K req/s pins one core on one node. The rest of the cluster sits idle while one shard melts. **Fix: read replicas with `READONLY` mode, an L1 in-process cache in front, or hash-tag distribution to spread the load across multiple keys.** The hot-key problem is the most common Redis production incident.

2. **Big-value blocking.** Single-threaded means one slow command stalls everything. `KEYS *` on a million-key DB. `SMEMBERS` on a million-member set. A 50 MB `GET`. Tail latency goes from 1 ms to 5 seconds. **Fix: `SCAN` instead of `KEYS`, `SSCAN` instead of `SMEMBERS`, value size limits enforced in code, `SLOWLOG` monitored.**

3. **The single-thread ceiling.** One Redis process uses one core. At ~100K ops/sec per shard you are done. **Fix: Redis Cluster (16384 hash slots distributed across nodes), or alternatives like KeyDB / Dragonfly that are multi-threaded and Redis-protocol-compatible.** Dragonfly in particular is gaining traction for being 25× more memory-efficient on the same hardware.

4. **Cluster resharding pain.** Adding or removing nodes requires moving slots, which moves keys, which is slow and error-prone under load. Most teams over-provision Cluster from day one to avoid resharding parties.

5. **Persistence trade-off surprises.** RDB snapshots fork the process — at 100 GB RAM that fork is slow and memory-spikes the host. AOF fsync-every-write halves throughput. Most teams pick AOF + `everysec` and accept up to 1 second of data loss on hard crash. Defaults are not sane.

6. **The Redlock debate.** Antirez (Redis creator) published the Redlock algorithm for distributed locks; Martin Kleppmann published a famous critique arguing it is fundamentally unsafe under realistic failure modes. The honest engineering answer: **if correctness depends on the lock, do not use Redis locks. Use ZooKeeper, etcd, or a database with real fencing tokens.** Redis locks are fine for "best effort, prevent thundering herd" — not for "must not double-charge a credit card."

### What seniors watch for
- **`KEYS *` in production code.** Career-limiting move. Always use `SCAN`.
- **Caches without TTLs.** Eviction policy (`maxmemory-policy`) becomes the only thing standing between you and OOM.
- **No `SLOWLOG` monitoring.** A single 200 ms command tells you something is wrong; without monitoring it, you'll never know.
- **Lua scripts that loop over many keys.** Atomic, yes — and atomically blocking. Long Lua scripts are a stealthy way to create a hot key.
- **Using Redis as the only place a piece of data lives.** Even with persistence, you should be able to rebuild the cache. If you can't, you've made it a database.
- **Distributed locks for correctness-critical operations.** See Redlock debate above.
- **Pub/Sub used as a message queue.** Subscribers offline = messages gone. Use Streams or a real broker.
- **Storing large blobs** (images, files, big JSON). Object storage costs less and doesn't block the event loop.
- **`MGET` of thousands of keys in one round-trip.** Atomic and convenient — also blocks the thread for the duration.
- **Keys without consistent naming convention.** `user:123:session` vs `session_user_123` — sounds petty, becomes painful at scale when you need to debug, scan, or migrate.

### Cost & ops burden

| Flavor | Cost (relative) | Ops burden | When it's the right answer |
|---|---|---|---|
| **Self-managed OSS** | 1× | Medium — Sentinel/Cluster setup, persistence tuning, version upgrades | Have ops muscle; cost-sensitive |
| **AWS ElastiCache (Redis)** | 2–3× | Low — managed failover, backups, patching | Default for AWS shops under significant scale |
| **AWS MemoryDB** | 4–6× | Low — multi-AZ durable Redis with strong consistency | Need Redis API + durability as primary store (rare; usually a smell) |
| **Redis Enterprise / Redis Cloud** | 3–6× | Low — adds active-active geo, modules (Search, JSON, TimeSeries, Bloom) | Need modules or active-active across regions |
| **KeyDB / Dragonfly (alternatives)** | 1× infra; new ops | Medium — newer, smaller community | Single-shard hitting CPU ceiling, want multi-threading without sharding |

The honest summary: **the engine is free; the operations and the RAM are not.** Memory is the dominant cost — a 100 GB Redis cluster on AWS runs five figures per month before you blink. Senior teams are ruthless about TTLs and value sizes for exactly this reason.

### War stories
- **Twitter — timeline fan-out (2010s).** Pioneered the "Redis lists per user" pattern for celebrity tweets — push the tweet ID into millions of follower timelines at write time, read O(1) at scroll time. Public talks describe the architecture. Lesson: **Redis turned a read-heavy, query-heavy problem into a precomputed-write problem. The data structure was the design.**
- **Stack Overflow — radically minimal infra.** Famously runs the world's most-trafficked Q&A site on a tiny number of servers, with Redis as the central cache. Public posts (Nick Craver's "Stack Overflow: How We Do Deployment / How We Do Caching") show how aggressive caching + good schema beats microservices. Lesson: **Redis-in-front-of-Postgres scales further than most teams imagine before they "need" anything fancier.**
- **GitHub — Sidekiq + Redis for job processing.** Background jobs for billions of webhook deliveries, notifications, and async work run through Redis-backed Sidekiq queues. Public engineering posts discuss the scale and the failure modes. Lesson: **Redis is a fine queue at GitHub's scale, but only because they treat it as ephemeral and design jobs to be retryable.**
- **Discord — early stack heavy on Redis** for presence, sessions, ephemeral state. Their later move to Cassandra/Scylla for messages is well-documented; Redis stayed for what it was good at. Lesson: **right tool per workload — message storage and presence have different shapes.**
- **Shopify — Black Friday rate limiting and inventory.** Public posts describe Redis as the rate-limiter and short-term inventory cache backing the world's largest single-day e-commerce events. Lesson: **at peak load, the cache is not optional, it is the system.**
- **The cautionary tale — Robinhood (2020 outage).** A cascading failure that included Redis hot-key issues during the GameStop trading frenzy. Public reporting points to systems unable to handle the surge, with caching layers contributing. Lesson: **load tests with realistic key-distribution skew; uniform synthetic load hides hot keys.**

---

---

<a id="prometheus"></a>
## Prometheus

### Identity
A pull-based, single-binary, time-series database purpose-built for **operational metrics and alerting**. Born at SoundCloud, modeled on Google's Borgmon, donated to the CNCF, and now the de facto monitoring layer of the cloud-native world. If your service runs in Kubernetes, Prometheus is monitoring it.

### Mental model
A **polling robot with a notebook of curves**. Every 15 seconds the robot walks down a list of addresses, knocks on each `/metrics` endpoint, copies the numbers into its notebook, and goes home. It does not care who you are or what you do — it cares that you exposed a number it could read. The notebook is shaped for one job: storing numeric time series and answering "what did this curve look like?" fast.

### Pull, not push (and why this matters)

The single most distinctive design decision in Prometheus, and the one that confuses every engineer coming from StatsD or DataDog.

```text
PUSH MODEL (StatsD, DataDog agent)        PULL MODEL (Prometheus)
──────────────────────────────────        ────────────────────────

  ┌──────┐                                   ┌──────────────┐
  │ App  │ ──── metric ───▶ ┌─────────┐      │  Prometheus  │
  └──────┘                  │ collector│     │   server     │
  ┌──────┐                  │          │     └──────┬───────┘
  │ App  │ ──── metric ───▶ │ accepts  │            │ scrape every 15s
  └──────┘                  │ whatever │            │ GET /metrics
  ┌──────┐                  │ shows up │            ▼
  │ App  │ ──── metric ───▶ │          │     ┌──────┐  ┌──────┐  ┌──────┐
  └──────┘                  └─────────┘     │ App  │  │ App  │  │ App  │
                                            │ /met │  │ /met │  │ /met │
  - apps know about collector              └──────┘  └──────┘  └──────┘
  - bad app can flood you
  - hard to know who is "alive"           - server discovers targets
                                          - missing scrape = up{}=0 alert
                                          - apps are passive
```

Implications:
- **Liveness comes free.** A target that doesn't respond is, by definition, in trouble. The `up` metric is the easiest alert in the system.
- **Service discovery becomes the configuration.** Kubernetes, Consul, EC2 tags — Prometheus pulls the target list from the orchestrator and adapts as pods come and go.
- **Bad services cannot DDoS your monitoring.** They can only fail to be scraped.
- **Short-lived jobs are awkward.** A batch job that lives for 30 seconds may never be scraped. Solution: the **Pushgateway** (push to a holding cell, Prometheus scrapes the cell) — for **short-lived jobs only**, not as a general push mechanism.

### Sweet spot
- **Infrastructure and application metrics.** CPU, memory, request rate, error rate, latency (the four golden signals).
- **Kubernetes monitoring.** Native integration, label-based service discovery. Prometheus + kube-state-metrics + node-exporter is the standard stack.
- **Alerting on operational health.** Alertmanager handles routing, grouping, silencing. The combination is what most companies actually use to wake up on-call.
- **SLI/SLO measurement.** Histograms + recording rules + Grafana = the SRE workflow Google's book describes.
- **Short-to-medium retention** (15 days default, comfortable to 30–90 days on a single node with tuning).
- **Pull-friendly environments** where targets expose `/metrics` and live long enough to be scraped.

### Don't use it for
- **Logs.** Prometheus stores numbers, not text. Use Loki, Elasticsearch, or ClickHouse for logs.
- **Distributed traces.** Use Jaeger, Tempo, or Honeycomb. Prometheus has no concept of a span.
- **Business analytics.** "How much revenue did we make in Q3?" is not a Prometheus question. The data model (downsampled, dropped on retention boundary, lossy at ingest) is wrong for finance and BI.
- **Event data.** Prometheus stores **sampled** values at scrape intervals — if a counter ticks 1,000 times between scrapes, you see a delta of 1,000, not 1,000 events. For per-event analytics, use a real event store.
- **Long-term storage at scale** (years of metrics across thousands of services). Single-node Prometheus is not designed for this; use **Thanos, Cortex, Mimir, or VictoriaMetrics** as the long-term/global layer.
- **High-cardinality dimensions.** This is *the* anti-pattern. Read on.
- **Push-from-everywhere** workloads. Pushgateway is for batch jobs only; using it as a general push endpoint defeats every Prometheus design assumption.

### How it breaks at scale

```text
   scale →   100 services  1K services  10K targets   100M series   long-term
              │             │             │             │             │
  cliff:   ┌──┴──┐       ┌──┴──┐       ┌──┴──┐       ┌──┴──┐       ┌──┴──┐
           │Cardi│       │ WAL │       │Federa│      │Single│      │Reten-│
           │nality│      │replay│      │tion  │      │node  │      │tion +│
           │explo│       │slow  │      │pain  │      │OOM   │      │global│
           │sion │       │      │      │      │      │      │      │view  │
           └─────┘       └──────┘      └──────┘      └──────┘      └─────┘
   fix:  Audit labels   Tune WAL,    Hierarchical   Shard by      Thanos /
         + relabel      faster disk  federation     team/cluster; Mimir /
         drops          (NVMe)       OR skip to     vertical-     VictoriaMetrics
                                     Thanos/Mimir   scale node
```

#### The cardinality bomb (the cliff that wrecks every team once)

Every unique combination of label values is a separate time series, stored as its own file group on disk and held in memory. The math is unforgiving:

```text
metric: http_requests_total{method, status, endpoint, ...}

  3 methods × 10 statuses × 50 endpoints     = 1,500 series       ✓ fine
  + 100 instances                            = 150,000 series     ✓ ok
  + region label (5 regions)                 = 750,000 series     ⚠ getting heavy
  + customer_id label (10,000 customers)     = 7,500,000,000 series  💥 dead
```

Adding `customer_id`, `user_id`, `request_id`, or any unbounded label to a metric is the single most common way teams kill their Prometheus. Memory goes vertical, queries time out, restarts take hours replaying the WAL. **High-cardinality dimensions belong in logs or traces, not metrics.** Repeat this in every code review.

1. **Cardinality explosion (the first cliff, hits early).** Audit labels constantly. Use `metric_relabel_configs` to drop bad labels at scrape time. If your `prometheus_tsdb_head_series` metric is climbing without bound, you have a problem now, not later.

2. **WAL replay on restart.** Prometheus rebuilds memory state from the write-ahead log on startup. At hundreds of millions of series, this can take 30+ minutes. Tune WAL size, use NVMe, run HA pairs so one is always up.

3. **Federation pain.** "Just have a top-level Prometheus scrape the others" works at small scale and falls apart at large scale — the federated server becomes a bottleneck, label collisions appear, and global queries are slow. Most large deployments skip federation entirely and go to Thanos/Mimir/VictoriaMetrics.

4. **Single-node ceiling.** Prometheus is intentionally single-node — no clustering. You scale by **sharding**: separate Prometheus per team, per cluster, per environment. Vertical scaling (more CPU/RAM/NVMe) buys time, but the ceiling is real around ~10M active series per server.

5. **Long-term retention requires another system.** Default retention is 15 days. To keep months or years of metrics queryable, you need an object-storage backend: **Thanos** (Improbable, open source), **Cortex / Mimir** (Grafana Labs), or **VictoriaMetrics** (drop-in replacement, often faster). Pick one early; bolting it on later is migration pain.

### What seniors watch for
- **Any new label whose values are not bounded by a small set known at deploy time.** `status="200|400|500"` is fine; `customer_id` is a fireable offense.
- **Histograms with too many buckets** — each bucket is a series. 50 buckets × 100 endpoints × 100 instances = 500K series for one metric.
- **Summaries used where histograms should be.** Summaries cannot be aggregated across instances (the percentile is computed locally and is mathematically wrong to add). Histograms can. Default to histograms.
- **Recording rules missing for expensive PromQL.** A `rate(http_requests_total[5m])` over 1M series, evaluated every dashboard refresh, is wasted compute. Pre-compute it as a recording rule.
- **No HA pair.** Prometheus is single-node. Production HA = run two identical Prometheus servers scraping the same targets, with deduplication at the query layer (Thanos Querier, Promxy).
- **Alerts that fire on raw scrape gaps** instead of `for: 5m`. Single missed scrape during a deploy → page → trust in alerts erodes.
- **Pushgateway used as a general push API.** It is designed for batch jobs and breaks the pull model's liveness signal.
- **Scrape interval set to 1 second "for accuracy."** You just multiplied your storage and CPU by 15. 15 seconds is the right default; faster is rarely worth it.
- **Direct PromQL in Grafana dashboards** that other dashboards also use. Move it to a recording rule once it's shared.

### Cost & ops burden

| Flavor | Cost (relative) | Ops burden | When it's the right answer |
|---|---|---|---|
| **Self-managed Prometheus** | 1× | Low–medium — single binary, but you own scaling and HA | Default for any team running Kubernetes |
| **Self-managed + Thanos / Mimir / VictoriaMetrics** | 1.5–2× | Medium-high — multi-component distributed system | Need long-term retention or global query view |
| **Grafana Cloud (managed Mimir)** | 3–6× | Low — they run the storage and global query layer | Want managed; already on Grafana stack |
| **Amazon Managed Prometheus (AMP)** | 3–5× | Low — AWS-native, integrates with IAM | AWS-only, want managed without leaving the cloud |
| **Chronosphere** | 5–10× | Low — high-end managed M3-based | Massive scale (>100M active series), cardinality controls as a feature |
| **VictoriaMetrics (self or managed)** | 0.5–1× of Prometheus storage | Low–medium — drop-in remote write target | Cost-sensitive, single-binary preference, faster query than Prometheus on big data |

The honest summary: **Prometheus itself is free and easy. The ecosystem you bolt around it for retention and global view is where cost and complexity live.** VictoriaMetrics is the under-the-radar pick that more senior teams reach for when they want Prometheus semantics without Thanos's operational weight.

### War stories
- **SoundCloud — birthplace (2012).** Built Prometheus to replace a homegrown StatsD-style system that couldn't handle their service-oriented architecture. Donated to CNCF in 2016. Public posts and the *Site Reliability Engineering* book (Google) describe the inspiration from Borgmon. Lesson: **the pull model wasn't a preference, it was a response to a specific operational pain — services that were hard to inventory and easy to flood.**
- **Cloudflare — metrics at edge scale.** Runs Prometheus across hundreds of edge locations with VictoriaMetrics for long-term storage. Public engineering blog covers their scale challenges and the cardinality discipline they enforce. Lesson: **at edge scale, VictoriaMetrics often beats Prometheus + Thanos on both cost and query speed.**
- **Uber — built M3 instead.** Outgrew Prometheus's single-node model and built **M3DB** as a distributed metrics platform. Public posts describe handling tens of billions of metrics. Lesson: **Prometheus is excellent until you need a true distributed metrics database; at that point, you either adopt one (Mimir/Thanos/M3) or write one. Most companies should adopt.**
- **Shopify — Thanos at scale.** Public posts describe their Prometheus + Thanos setup handling Black Friday spikes and multi-region observability. Lesson: **the pattern is "Prometheus per cluster, Thanos for global query and long-term store" — it is the closest thing to a default architecture in cloud-native ops.**
- **GitLab — public monitoring stack.** GitLab.com publishes their entire Prometheus + Thanos config and dashboards. Lesson: **reading other teams' monitoring config is one of the highest-leverage learning moves an engineer can make. GitLab's is open.**
- **The cautionary tale — every team, once.** The single most common Prometheus incident is the same one: an engineer adds a label like `request_id` or `customer_email` to a high-traffic metric, ships, and the Prometheus host OOMs within hours. There is no famous public post-mortem because every team has lived this story privately. Make it part of your code-review checklist.

---

---

<a id="kafka"></a>
## Kafka

### Identity
A distributed, partitioned, replicated, **append-only commit log** designed at LinkedIn in 2011 to move billions of events per day between services. Donated to Apache, now the de facto backbone of event-driven architectures and stream processing. The label "message broker" undersells it; Kafka is closer to "a database whose only API is `INSERT INTO log`."

### Mental model
A **bank of distributed tape recorders**. Producers append events to the end of the tape. The tapes (topics) are split into parallel reels (partitions) spread across machines, each reel mirrored on N machines for durability. Consumers can press play anywhere on any reel — at the end for live data, in the middle to replay history, at the beginning to rebuild state from scratch. The tape stays around for as long as you tell it to (seven days by default; forever if you want).

The single most important reframe: **Kafka is not a queue. It is a log.**

```text
QUEUE (RabbitMQ, SQS)                    LOG (Kafka)
─────────────────────                    ───────────

  ┌────────┐                               ┌──────────────────────────┐
  │  msg   │ ── consumed ──▶ gone          │ msg msg msg msg msg msg  │ ← tape
  │  msg   │                               └──────────────────────────┘
  │  msg   │                                ▲       ▲       ▲
  │  msg   │                                │       │       │
  └────────┘                            consumer  consumer  consumer
                                          A       B       C
                                       (offset 0) (offset 4) (offset 6)
  - one consumer per message            - many consumers, independent positions
  - message gone after ack              - messages stay for retention period
  - "did this get processed?"           - "what was the world like at offset N?"
  - good for work distribution          - good for event distribution + replay
```

This distinction explains every Kafka design choice. Replay, multiple independent consumer groups, exactly-once semantics, stream processing, event sourcing — all flow from "the log is the source of truth, consumers are projections."

### How partitions actually work (the picture every Kafka engineer carries)

```text
TOPIC: orders   (replication.factor=3, partitions=4)
                                              ┌──────────────────┐
producer ──┐                                  │ consumer group A │
           │   ┌──────────────────────────┐   │  (each partition │
           │   │ partition 0   ▶ broker 1 │ ◀─┤   to one         │
           │   │              + broker 2  │   │   consumer)      │
           │   │              + broker 3  │   │  c1 → p0,p1      │
           │   ├──────────────────────────┤   │  c2 → p2,p3      │
           ├──▶│ partition 1   ▶ broker 2 │ ◀─┘                  │
key=user42 │   │              + broker 3  │   ┌──────────────────┐
hash → 1   │   │              + broker 1  │   │ consumer group B │
           │   ├──────────────────────────┤ ◀─┤ (independent     │
           │   │ partition 2   ▶ broker 3 │   │  offsets, can    │
           │   │              + broker 1  │   │  replay history) │
           │   │              + broker 2  │   └──────────────────┘
           │   ├──────────────────────────┤
           │   │ partition 3   ▶ broker 1 │
           │   │              + broker 2  │
           │   │              + broker 3  │
           │   └──────────────────────────┘
           │
       partition = hash(key) % num_partitions
       same key → same partition → ordered for that key
       no key → round-robin → no ordering guarantee
```

Two facts to memorize:
1. **Ordering is per-partition, never per-topic.** Two events with different keys may arrive at consumers in any relative order. Engineers who assume global ordering ship subtle bugs that surface months later.
2. **A consumer group has at most one consumer per partition.** Want more parallelism? Add partitions. Adding consumers beyond partition count gives you idle consumers, not more throughput.

### Sweet spot
- **Event backbone between services.** The "central nervous system" pattern: producers fire events, any number of consumers subscribe independently, services decouple cleanly.
- **Stream processing source.** Spark Streaming, Flink, Kafka Streams, ksqlDB all consume from Kafka as the canonical input.
- **Change data capture (CDC).** Debezium reads Postgres/MySQL WAL and emits change events to Kafka. Downstream systems (search index, cache, warehouse) update without polling the source database.
- **Log aggregation at firehose scale.** Application logs, metrics, audit trails — collect into Kafka, fan out to S3/Iceberg, ClickHouse, Elasticsearch.
- **Event sourcing.** The log is the source of truth; current state is a projection. Replay rebuilds state.
- **Decoupling producers from consumer cadence.** Producer can fire 1M events/sec; slow consumer reads at 10K/sec; Kafka holds the difference for days.

### Don't use it for
- **Request/response.** Kafka is async by design. RPC over Kafka exists but is a misuse — use gRPC, HTTP, or a proper RPC framework.
- **Small workloads.** Three services exchanging a thousand messages a day? Kafka is overkill. SQS, Redis Streams, RabbitMQ, or even a Postgres table are simpler.
- **Per-message workflows with priorities, delays, dead-letter routing.** Kafka has no native priority, no delayed delivery, and DLT (dead-letter topic) is a pattern you build yourself. Real queues — RabbitMQ, SQS, ActiveMQ — are better at queue semantics.
- **Strict global ordering.** Single-partition topics give you ordering at the cost of zero parallelism. Pick one.
- **As a database for queries.** Kafka cannot answer "what is the current value for key X" without scanning the log. Compacted topics give you the latest value per key but no query capability beyond that. Project to a real database.
- **Storing data forever as the only copy.** Tiered storage (Confluent, Apache 3.6+) helps, but for long-term analytical history, land it in Iceberg/Parquet on object storage and let Kafka be the hot path.
- **Tiny clusters at large companies.** Below ~100 MB/s sustained, you are paying Kafka's operational tax for nothing.

### How it breaks at scale

```text
   scale →   1 MB/s        100 MB/s     1 GB/s        multi-cluster  multi-region
              │             │             │             │             │
  cliff:   ┌──┴──┐       ┌──┴──┐       ┌──┴──┐       ┌──┴──┐       ┌──┴──┐
           │ Hot │       │Conu-│       │ ZK / │      │Rebal-│      │Mirror│
           │ par-│       │mer  │       │KRaft │      │ance  │      │Maker │
           │tition│      │ lag │       │ load │      │storms│      │cost  │
           └─────┘       └──────┘      └──────┘      └──────┘      └─────┘
   fix:  Better key,   Scale conc,   KRaft mode    Cooperative-   Cluster
         more parts,   add parts,    (no ZK),      sticky         Linking
         hash-mod      faster        bigger ZK     assignor,      (Confluent)
         partition     processing    ensemble      static membr   or accept
                                                                  per-region
```

1. **Hot partitions (the first cliff, hits early).** Partition is `hash(key) % num_partitions`. Pick a key that skews — say `customer_id` where one customer is 50% of traffic — and one broker melts while the others idle. **Fix: choose keys with high cardinality and even distribution. If you must use a skewed key, salt it (`key = original_key + random_suffix`) and accept the loss of per-original-key ordering.**

2. **Consumer lag spirals.** A slow consumer falls behind, the lag grows, the lag triggers an alert, the team adds consumers — and discovers consumers beyond partition count do nothing. **Fix: lag is a partition-count problem and a processing-speed problem, not a consumer-count problem. Plan partition counts for peak throughput from day one (changing them later is painful).**

3. **ZooKeeper bottleneck (legacy) → KRaft transition (modern).** Classic Kafka used ZooKeeper for metadata; ZK became the bottleneck above ~200K partitions per cluster. KIP-500 replaced ZK with **KRaft** (Kafka's own Raft-based metadata layer). New deployments should be KRaft from day one; existing deployments face a migration project. ZK removal is **complete in Apache Kafka 4.0**.

4. **Rebalance storms.** When a consumer joins or leaves, the group rebalances — and historically all consumers stopped consuming during the rebalance. At high churn (Kubernetes pod restarts, scaling events), throughput tanks. **Fix: cooperative-sticky assignor (Kafka 2.4+) does incremental rebalances; static membership avoids rebalance on planned restarts.**

5. **Cross-region pain.** MirrorMaker 2 replicates topics between clusters but doubles your storage cost, doubles network egress (the real cost), and offsets are not preserved across clusters by default. **Fix: Confluent Cluster Linking, or accept region-local clusters with application-level routing. Or switch to alternatives like Pulsar that have geo-replication built in.**

6. **The "we use Kafka as a database" trap.** Compacted topics give you "latest value per key" forever, which feels like a database. It is not. No secondary indexes, no point queries beyond key lookup via a separate consumer rebuild, no transactional updates across keys. Teams that build on this discover the limits at the worst time.

### What seniors watch for
- **Partition key choice.** This is the single most consequential schema decision in Kafka. Bad keys cause hot partitions; ordering assumptions on the wrong key cause subtle correctness bugs. Reviewed line by line in mature teams.
- **Partition count chosen casually.** Default of 1 or 3 is almost always wrong. Calculate from peak throughput (~10 MB/s per partition is a reasonable rule of thumb) and consumer parallelism needs. Plan for 12 months of growth.
- **`acks=1` in production.** That means the producer waits for the leader to ack, not the replicas. A leader crash loses data. **Production default: `acks=all` + `min.insync.replicas=2` + `replication.factor=3`. The durability triangle.** Trading any of these for throughput should be a deliberate, documented decision.
- **No Schema Registry.** Producers and consumers evolve schemas independently; the log fills with incompatible payloads; a downstream consumer breaks at 3 a.m. when an old message reappears. Avro/Protobuf + Schema Registry is the standard answer.
- **Exactly-once semantics misunderstood.** Kafka's EOS works *within* Kafka (transactional producer + consumer with `read_committed`). It does **not** automatically extend to your database write. Idempotent consumer logic is still your job.
- **No dead-letter topic strategy.** Poison messages crash consumers in a loop. Standard pattern: try N times, then publish to `<topic>.dlt`, alert, and continue.
- **Topic naming chaos.** No conventions = no ownership = no cleanup. Mature shops enforce `<domain>.<entity>.<event>` or similar.
- **Confluent license drift.** Many "Kafka" components (KSQL, Schema Registry, some connectors) are under the Confluent Community License, not Apache 2.0. Read the license before building on them; alternatives exist (Apicurio Registry, Karapace).
- **Tiered storage assumptions.** Open-source tiered storage (Apache Kafka 3.6+) is newer than Confluent's. Test the failover paths before betting durability on them.

### Cost & ops burden

| Flavor | Cost (relative) | Ops burden | When it's the right answer |
|---|---|---|---|
| **Self-managed Apache Kafka** | 1× | Very high — brokers, ZK/KRaft, monitoring, upgrades, rebalances | Have dedicated streaming/data platform team |
| **AWS MSK** | 2–3× | Medium — AWS handles brokers, you handle topics/clients | AWS-native, want managed brokers without leaving the cloud |
| **Confluent Cloud** | 4–8× | Low — full managed, includes Schema Registry, Connect, ksqlDB | Want everything managed; can pay for it |
| **Redpanda** | 1–2× infra | Low–medium — single binary, no ZK, Kafka API-compatible | Want simpler ops, lower latency, smaller footprint |
| **WarpStream / Bufstream (S3-backed)** | 0.2–0.5× egress savings | Low — stateless brokers, data in S3 | Cost-sensitive, ok with higher latency, multi-AZ egress is your big bill |
| **Apache Pulsar** | 1× | High — different architecture (BookKeeper + brokers) | Need built-in multi-tenancy, geo-replication, queue semantics + log semantics |

The honest summary: **Kafka's operational cost is the dominant factor.** A self-managed cluster needs a team that knows it. Below ~100 MB/s sustained and a real engineering investment, **managed (MSK, Confluent Cloud) almost always wins on TCO.** The newer S3-backed alternatives (WarpStream, Bufstream, Confluent's Freight tier) are reshaping the cost curve specifically by killing inter-AZ replication egress, which is often 70%+ of a cloud Kafka bill.

### War stories
- **LinkedIn — birthplace (2011).** Built Kafka to replace a tangle of point-to-point service integrations and batch ETL pipelines. Jay Kreps's foundational essay *"The Log: What every software engineer should know about real-time data's unifying abstraction"* is required reading. Lesson: **the log abstraction unifies messaging, integration, and storage. Internalize this and most distributed systems get easier.**
- **Netflix — event-driven backbone.** Runs Kafka at multi-trillion events/day across video playback telemetry, A/B testing, and microservice events. Public posts cover their Kafka-on-AWS optimizations and the move toward open-source contributions. Lesson: **Kafka is the data plane for telemetry-heavy companies; trying to use a relational database here would be malpractice.**
- **Uber — uReplicator and the cross-region story.** Open-sourced uReplicator after MirrorMaker 1 couldn't keep up with cross-region needs. Public posts describe the operational complexity. Lesson: **cross-region Kafka is hard. If you need it, plan for it as a distinct project, not a config flag.**
- **Pinterest — Singer (log forwarder) → Kafka → MemQ.** Built Singer to ship logs reliably into Kafka, then later built MemQ as an S3-backed Kafka alternative for cost reasons. Public posts cover the migration. Lesson: **at huge scale, the inter-AZ network bill becomes the deciding factor in the architecture.**
- **Slack — outage from a Kafka misconfiguration (2021).** Public post-mortems describe how Kafka issues compounded into broader platform failures. Lesson: **Kafka sits in the critical path of more things than you think. Treat it as Tier-0 infrastructure.**
- **Confluent's KIP-500 (2019–2024).** Five-year project to remove ZooKeeper from Kafka, replaced by KRaft. The fact that it took five years and dominated the Kafka roadmap tells you how deeply ZooKeeper was woven into the system. Lesson: **architectural debt in distributed systems is paid in years, not sprints. New deployments: KRaft from day one.**
- **The cautionary tale — Robinhood (2020 surge).** During the GameStop trading frenzy, multiple infrastructure layers struggled, including event pipelines. Public reporting points to Kafka-related backpressure as one of several factors. Lesson: **load-test your event pipeline at 5× peak, not 1.5×. The hot-partition surprise lives in tail traffic, not average traffic.**

---

---

<a id="zookeeper"></a>
## ZooKeeper

### Identity
A small, strongly-consistent, distributed **coordination service** built at Yahoo! and donated to Apache. Stores tiny pieces of shared state (typically a few KB per node) that an entire cluster of machines can agree on, with strict ordering, durable updates, and a notification mechanism. Not a database in the sense the rest of this doc means; closer to a **distributed kernel primitive** for building distributed systems.

### Mental model
A **tiny, very reliable filesystem that the entire cluster sees identically**. Paths are called znodes; they form a hierarchy like `/services/payments/leader`. Any client can read, write, watch, or delete a znode, and every client sees writes in the same order. The filesystem is replicated across an odd number of servers (an ensemble) using the **ZAB (ZooKeeper Atomic Broadcast)** protocol, a Paxos-flavored consensus algorithm. Latency is the cost; correctness is the product.

### Why coordination is its own category

The first instinct of every engineer encountering ZooKeeper is "why don't I just use Postgres / Redis / a key-value store for this?" The answer lives in the failure modes:

| Problem | Naive solution | Why it fails |
|---|---|---|
| **Leader election** across 10 service instances | "Whoever inserts a row first wins" in Postgres | Network partition: two leaders elected, both think they won, split-brain |
| **Distributed lock** for a critical section | `SET NX` in Redis | Redlock debate; clock skew + GC pauses cause double-ownership |
| **Cluster membership** (who is alive?) | Heartbeat table in MySQL | Race conditions on join/leave; stale entries; thundering herd on changes |
| **Configuration that must update atomically across nodes** | Push from a script | Some nodes get old config, some get new; no ordering guarantee |
| **Notification when shared state changes** | Polling | Latency vs load trade-off; never converges nicely |

ZooKeeper exists because **getting these primitives right requires consensus, and consensus is hard.** Building "leader election in Postgres" once is a bug; building it correctly is a research project. ZooKeeper does it correctly so you don't have to.

### Watches and ephemerals (the magic ingredients)

Two features make ZooKeeper uniquely suited to coordination:

```text
EPHEMERAL ZNODES                          WATCHES
─────────────────                         ───────

  client A creates                          client A reads /config
  /workers/A (ephemeral)                    AND sets a watch
        │                                         │
        │ session alive                           │ value is "v1"
        ▼                                         ▼
   znode exists                              waiting...
        │                                         │
        │ client A crashes / network drops        │ client B writes "v2"
        ▼                                         ▼
   session expires                           ZK fires watch event
        │                                    to client A
        ▼                                         │
   znode auto-deleted                             ▼
                                            client A re-reads → "v2"

  → "alive members" = list znodes under     → no polling, no missed updates
    /workers (auto-cleaned on death)        → watches are one-shot, must
                                              re-register after firing
```

Ephemerals turn liveness into a primitive. Watches turn change notification into a primitive. Together they make leader election, cluster membership, and config distribution into a few dozen lines of code instead of a research paper.

### Sweet spot
- **Leader election** in distributed systems that need a single coordinator (e.g., Kafka controller historically, HBase HMaster, Solr Cloud overseer).
- **Cluster membership** with automatic failure detection via ephemeral znodes.
- **Distributed configuration** that must update atomically across all nodes, with notifications.
- **Distributed locks with fencing tokens** — ZooKeeper's `zxid` (transaction id) is monotonically increasing and can be used as a fencing token, the property that makes a lock actually safe under failures.
- **Service discovery** in pre-Kubernetes architectures (Twitter Finagle's ServerSet, Hadoop's HA, etc.).
- **Sequencer / counter** for globally-ordered IDs across a cluster.
- **As a dependency of other systems** that picked it: Kafka (until KRaft), HBase, HDFS HA, Solr Cloud, Mesos, Druid, Pinot.

### Don't use it for
- **General-purpose key-value storage.** Znode size limit is 1 MB, recommended <few KB. Total dataset should fit comfortably in memory across the ensemble. Storing user data here is malpractice.
- **High write throughput.** Every write is a Paxos round across the ensemble. ~10K writes/sec is a reasonable upper bound; exceed it and consensus latency dominates.
- **High read throughput from a single znode.** Reads are local to each follower, so reads scale with ensemble size, but a single hot znode with thousands of watchers triggers **watch storms** on every change.
- **As a database.** No queries beyond path lookup. No secondary indexes. No transactions across paths. No JOIN. The wrong tool for almost any data problem.
- **Storing data that doesn't need consensus.** If "eventually consistent" is acceptable, use Redis or Cassandra. ZooKeeper is paying for consistency you don't need.
- **New greenfield systems in 2026.** Increasingly, **etcd** is the better choice — gRPC API, simpler operations, kubernetes-native, no JVM. ZooKeeper persists in legacy ecosystems (Kafka pre-KRaft, Hadoop) but is rarely the first pick for a new design.

### How it breaks at scale

```text
   ensemble:     3 nodes       5 nodes       7 nodes       9+ nodes
                  │             │             │             │
   throughput: ┌──┴──┐       ┌──┴──┐       ┌──┴──┐       ┌──┴──┐
               │ Best│       │Good │       │ Slow│       │BAD  │
               │write│       │write│       │write│       │write
               │perf │       │perf │       │perf │       │perf │
               └─────┘       └──────┘      └──────┘      └─────┘
   tolerates:    1 fail        2 fail        3 fail        4 fail

   counter-intuitive truth: more nodes does NOT mean more throughput.
   it means more durability and more consensus latency. 5 is the
   default sweet spot; 3 for small, 7 only if 2-failure tolerance
   isn't enough.
```

1. **Ensemble sizing surprise.** New users assume "more nodes = more capacity." Wrong. Every write must be acknowledged by a majority. A 9-node ensemble needs 5 acks; a 3-node ensemble needs 2. Larger ensembles are *slower for writes*, more durable, and harder to operate. **Default to 5; drop to 3 only for dev; never exceed 7 without a specific reason.**

2. **JVM GC pauses.** ZooKeeper is written in Java. A long GC pause on the leader looks like a network partition to followers, triggers a leader election, and stalls every client during the ~few seconds it takes. Tuning the JVM (G1GC, heap sizing) is non-negotiable for production ZK.

3. **Snapshot and transaction log bloat.** ZK persists state via periodic snapshots and a transaction log between snapshots. Without housekeeping (`autopurge.snapRetainCount`, `autopurge.purgeInterval`), disks fill and recovery slows. A ZK with millions of znodes can take many minutes to recover after a crash.

4. **Watch storms.** A znode with 10,000 watchers receives a write; ZK must notify all 10,000 clients. The notification fanout pins network and CPU. **Pattern: structure data so watches are on parent znodes (children-changed) with a small fanout, not on individual children.**

5. **Multi-DC deployments.** ZAB consensus crosses the WAN; latency makes writes painfully slow. **Standard advice: do not stretch ZK across data centers.** Run a per-DC ensemble and replicate at the application layer if needed.

6. **Session expiry under load.** Clients keep their session alive with heartbeats. Under network or GC stress, heartbeats are missed, sessions expire, ephemeral znodes vanish, leadership is reassigned, and a brief blip becomes a leadership churn event. This is the **classic Kafka-controller-flapping incident**: a slow ZK GC takes the Kafka cluster offline for minutes.

### What seniors watch for
- **ZK used as application storage.** Anything beyond coordination metadata is a smell. Read znode counts and sizes; if they trend up, intervene.
- **Single, oversized ensemble shared by every system in the org.** A misbehaving client takes everyone down. Pattern: per-system ensembles, sized small.
- **No backup / recovery drill.** ZK can lose state on cascading failures. Snapshots + transaction logs must be backed up; restore must be tested.
- **Clients with no exponential backoff on session expiry.** Reconnect storms after a brief ZK outage cause a second outage.
- **Watches re-registered incorrectly** (they are one-shot — fire-once, then must be re-set). Buggy clients miss updates silently.
- **Choosing ZK in 2026 for a greenfield system.** Defensible only when integrating with an ecosystem that already requires it. Otherwise, evaluate etcd or Consul first.
- **Using ZK as a service discovery layer** in a Kubernetes environment. Kubernetes already does this with etcd. Stacking ZK on top is duplication.

### Cost & ops burden

| Flavor | Cost (relative) | Ops burden | When it's the right answer |
|---|---|---|---|
| **Self-managed Apache ZooKeeper** | 1× | High — JVM tuning, ensemble ops, snapshot management | Required by your stack (Kafka pre-KRaft, HBase, Solr) |
| **ClickHouse Keeper** | 0× extra | Low — embedded in ClickHouse, drop-in ZK protocol replacement | Running ClickHouse at scale; replaces external ZK |
| **etcd (alternative)** | 1× | Low–medium — Go binary, simpler than ZK, gRPC API | New systems needing coordination; Kubernetes-native shops |
| **HashiCorp Consul (alternative)** | 1× | Medium — adds service discovery, KV, mesh; more features | Want coordination + service discovery + multi-DC in one tool |

The honest summary: **nobody starts a new project with ZooKeeper in 2026.** It is the coordination layer of an earlier generation of distributed systems and persists where those systems live. The strategic move is **understanding ZooKeeper deeply because the systems on top of it (Kafka, HBase, Solr) are deeply shaped by it**, then defaulting to **etcd** for new coordination needs.

### War stories
- **Yahoo! — birthplace (2008).** Built ZooKeeper to replace a tangle of one-off coordination code across Hadoop and other distributed systems. The original paper *"ZooKeeper: Wait-free coordination for Internet-scale systems"* is short and worth reading. Lesson: **coordination is a horizontal concern; extracting it into one well-tested service prevents every team from re-implementing leader election badly.**
- **Apache Kafka — 12-year ZK dependency, then KIP-500 (2019–2024).** Kafka used ZK from inception until the KRaft migration. The fact that removing ZK was a five-year project across multiple major releases tells you how deeply coordination weaves into a system's architecture. Lesson: **picking a coordination service is a decade-long commitment. Choose with that horizon in mind.**
- **Hadoop / HBase / HDFS HA.** The entire Hadoop ecosystem leans on ZK for HA, leader election, and cluster state. Public docs and incident reports across Cloudera, Hortonworks, MapR all share the same ZK operational lessons. Lesson: **ZK is the unsung Tier-0 service in many data platforms; treat it as such operationally.**
- **Twitter — Finagle's ServerSet.** Built service discovery on ZooKeeper across thousands of services pre-Kubernetes. Public posts describe scale challenges and the eventual move toward more specialized infrastructure. Lesson: **service discovery on ZK works at scale but is not its sweet spot; modern environments (Kubernetes, Consul) handle it better.**
- **Pinterest — running ZooKeeper for the Kafka fleet.** Public posts describe operational practices for ZK ensembles supporting massive Kafka deployments, including the playbook for ZK incidents that cascade into Kafka outages. Lesson: **the most common Kafka outage in the ZK era was not a Kafka bug — it was a ZK GC pause.**
- **Kubernetes choosing etcd over ZooKeeper (2014).** When Google built Kubernetes, they explicitly chose etcd over ZooKeeper for the cluster state store, citing simpler operations, gRPC, and a smaller surface area. Lesson: **the industry's most consequential vote against ZK for new systems came from the people who knew distributed coordination best.**
- **The cautionary tale — every Kafka cluster, once.** A long ZK GC pause triggers a Kafka controller failover; partition leadership reshuffles; producers see retriable errors; downstream systems back up. The ZK pause was 4 seconds; the Kafka recovery took 20 minutes. There is no famous post-mortem because every team has lived this story. The lesson: **monitor ZK GC pauses as a Kafka health metric, not just a ZK health metric.**

---

---

<a id="iceberg"></a>
## Apache Iceberg

### Identity
An **open table format** for petabyte-scale analytic datasets on object storage. Not a database, not a query engine, not a storage system — a **specification** for how to lay out Parquet files plus metadata so that any engine (Spark, Trino, Snowflake, BigQuery, DuckDB, ClickHouse) can read and write the same table with ACID guarantees, schema evolution, and time travel. Born at Netflix (2017) to fix the operational nightmare that was the Hive table format, donated to Apache, now the de facto standard of the modern lakehouse.

### Mental model
**Git for big tables on S3.** A folder of Parquet files in object storage by itself is just a folder — no atomicity, no schema evolution, no concurrent-write safety. Iceberg adds a layer of metadata files that turn that folder into a **real table with commits, branches, snapshots, and history**. Every change creates a new snapshot pointed to by a metadata file; the catalog atomically swaps the pointer; readers always see a consistent table state. Old snapshots stick around until you expire them, giving you free time travel.

### Why a "table format" is its own category

Engineers coming from databases find Iceberg's existence confusing. The clearest framing:

| Layer | Role | Examples |
|---|---|---|
| **Storage** | Where bytes live | S3, GCS, ADLS, MinIO, HDFS |
| **File format** | How a single file is structured | Parquet, ORC, Avro |
| **Table format** | How many files behave as one table | **Iceberg, Delta Lake, Apache Hudi** |
| **Catalog** | Where the "current pointer" for each table is stored | REST, Glue, Nessie, Polaris, Hive Metastore, Unity |
| **Engine** | What reads/writes the table | Spark, Trino, Flink, Snowflake, BigQuery, DuckDB, ClickHouse |

Before table formats, a "table" in a data lake was *"this folder, by convention"* — the Hive way. Adding a column meant rewriting partitions. Concurrent writes corrupted state. There were no transactions. Iceberg, Delta, and Hudi all emerged 2017–2018 to solve this same set of problems, with slightly different designs and now overlapping ecosystems.

### How Iceberg actually works (the metadata stack)

The single most important diagram for understanding Iceberg. Internalize this and the rest of the system makes sense.

```text
                              ┌─────────────────┐
                              │     CATALOG     │  ← atomic pointer swap on commit
                              │ (REST/Glue/etc) │     (this is the consistency primitive)
                              └────────┬────────┘
                                       │ "current metadata.json for table T is..."
                                       ▼
                          ┌────────────────────────┐
                          │    metadata.json       │  ← snapshot history, schema,
                          │  (one per table state) │     partition spec, properties
                          └────────────┬───────────┘
                                       │ points to
                                       ▼
                          ┌────────────────────────┐
                          │   manifest list (avro) │  ← which manifests are in
                          │   = one snapshot       │     this snapshot
                          └────────────┬───────────┘
                                       │ points to many
                          ┌────────────┼────────────┐
                          ▼            ▼            ▼
                    ┌──────────┐ ┌──────────┐ ┌──────────┐
                    │ manifest │ │ manifest │ │ manifest │  ← per-file stats
                    │  (avro)  │ │  (avro)  │ │  (avro)  │     (min/max/null counts)
                    └────┬─────┘ └────┬─────┘ └────┬─────┘
                         │            │            │
                  ┌──────┴──────┐  ...  ...
                  ▼      ▼      ▼
             ┌───────┐ ┌───────┐ ┌───────┐
             │parquet│ │parquet│ │parquet│  ← actual data files
             │ file  │ │ file  │ │ file  │     (immutable, append-only at this layer)
             └───────┘ └───────┘ └───────┘
```

Implications worth burning in:
- **Commits are O(1) at the catalog.** Whether you wrote 10 files or 10,000, the commit is one atomic pointer swap. This is the durability primitive.
- **Reads use the metadata to skip whole files.** Per-file min/max stats let the engine prune files without reading them. This is why Iceberg queries can be fast over petabytes.
- **Snapshots are free until you expire them.** Each commit creates a new snapshot; old ones share data files. Time travel costs metadata storage, not data storage.
- **The catalog choice is consequential.** It is the consistency boundary. Glue, Nessie, Polaris, REST catalog, Hive Metastore — each has different operational and feature trade-offs. Pick deliberately.

### Hidden partitioning (the killer feature)

In Hive, partitioning was a physical layout concern that leaked into every query: forget to filter on the partition column and you scanned the whole table. Iceberg decouples logical and physical:

```text
HIVE                                      ICEBERG
────                                      ───────

CREATE TABLE events (                     CREATE TABLE events (
  ts TIMESTAMP,                             ts TIMESTAMP,
  user_id BIGINT,                           user_id BIGINT,
  ...                                       ...
)                                         ) USING iceberg
PARTITIONED BY (                          PARTITIONED BY (
  dt STRING  ← physical partition           days(ts)   ← derived from ts
)                                         )

writer must compute dt and write          writer just writes ts
SELECT must filter on dt or               SELECT WHERE ts > '...'
scan everything:                          automatically prunes:
                                          → engine reads partition spec
WHERE dt = '2025-04-15'  ✓ fast           → derives partition from ts
WHERE ts > '2025-04-15'  ✗ full scan      → prunes files

partition is part of the schema           partition is metadata; can change
forever; can't change without             without rewriting data
rewriting everything                      (partition evolution)
```

This is why Netflix built Iceberg in the first place: **the Hive partitioning model cost them years of pain, and "WHERE on the wrong column" full-scans were a recurring outage cause.**

### Sweet spot
- **Lakehouse architecture.** One copy of data in object storage, read by Spark for ETL, Trino for ad-hoc, DuckDB on the analyst's laptop, Snowflake for BI. No copies, no sync, no vendor lock-in.
- **Long-term raw event archive.** Years of logs, clickstream, telemetry — Iceberg + Parquet + S3 is the cheapest durable analytical storage available.
- **Slowly-changing dimensional data** with the need for time-travel queries: "what did this customer's profile look like on Jan 1?"
- **Reprocessing and backfills.** Bug in your aggregation job? Replay from Iceberg, write to a new snapshot, atomic cutover. No lost data.
- **Compliance and audit.** Snapshots give you an auditable history. GDPR delete-by-key is supported via copy-on-write or merge-on-read.
- **Multi-engine futures.** You don't know what query engine you'll want in 5 years. Iceberg lets you change engines without migrating data.

### Don't use it for
- **OLTP.** Not a database. No row-level locking, no point lookups, no transactions across tables in the OLTP sense.
- **Sub-second queries on small data.** Overhead of metadata parsing dominates for tiny tables. Postgres or DuckDB beats Iceberg under ~1 GB.
- **Streaming source of truth.** Iceberg supports streaming writes (Flink, Spark Structured Streaming) but the **commit cadence trade-off** is real — too frequent and metadata explodes; too infrequent and freshness suffers. Kafka stays the source-of-truth log; Iceberg is the analytical projection.
- **Frequent row-level mutations.** Updates and deletes are supported via copy-on-write (rewrite affected files) or merge-on-read (write delete files, reconcile at read). Both work; both are expensive at high mutation rates. If you need many updates per second, you have picked the wrong layer.
- **Replacing your transactional database.** Iceberg sits *downstream* of OLTP, not in place of it.
- **High-concurrency dashboards on raw tables.** Use ClickHouse or a query engine cache layer. Iceberg shines on ad-hoc and batch, not on thousand-QPS dashboards.

### How it breaks at scale

```text
   scale →   1 TB         100 TB        1 PB          10 PB+        multi-region
              │             │             │             │             │
  cliff:   ┌──┴──┐       ┌──┴──┐       ┌──┴──┐       ┌──┴──┐       ┌──┴──┐
           │Small│       │Snapsht│     │Mani- │      │Concurr│     │Catalog│
           │file │       │ bloat │     │fest  │      │write  │     │ +     │
           │probl│       │       │     │bloat │      │conflct│     │locality│
           │em   │       │       │     │      │      │       │     │       │
           └─────┘       └──────┘      └──────┘      └──────┘      └──────┘
   fix:  rewrite_data_  expire_       partial         retry +      Per-region
         files (compact) snapshots    manifest        partitioned  catalogs;
         + adaptive     + remove_orph compaction      writes;      cross-region
         streaming      an_files                      Nessie       reads via
         batching                                     branching    object store
                                                                   replication
```

1. **The small file problem (the first cliff, hits early).** Streaming writes commit small batches → many small Parquet files. Reading 10,000 small files is dramatically slower than reading 100 large ones. **Fix is mandatory: scheduled compaction (`rewrite_data_files`), target file size 128–512 MB. Treat this like vacuum in Postgres — not optional, just maintenance.**

2. **Snapshot bloat.** Every commit is a snapshot. Without expiration, snapshot history grows forever, and so does the metadata. **Fix: `expire_snapshots` policy (typically retain 7–30 days), `remove_orphan_files` periodically. Most teams set this up too late.**

3. **Manifest bloat.** Within a snapshot, manifests can become thousands of files. Query planning slows because the engine reads every manifest. **Fix: `rewrite_manifests` to consolidate. Newer Iceberg versions handle this better; still requires monitoring.**

4. **Concurrent write conflicts.** Iceberg uses optimistic concurrency at the catalog. Two writers committing to the same table at the same time → one wins, the other retries (or fails). High write concurrency requires partitioned writes (each writer owns a partition) or branching strategies (Nessie).

5. **Catalog as the bottleneck.** All commits funnel through the catalog. Hive Metastore can struggle at high commit rates; REST catalog and Polaris/Nessie are the modern answer. **Catalog choice is one of the most consequential operational decisions.**

6. **Cross-region cost.** S3 cross-region reads are expensive. Iceberg metadata is small but data is not. Most large deployments run **per-region tables with replication**, not single global tables.

### What seniors watch for
- **No compaction strategy in place from day one.** A streaming pipeline writing to Iceberg without compaction is a time bomb. Set it up before turning on the firehose.
- **Snapshot expiration disabled or absent.** "We might want to time-travel back to last year" sounds nice; storage cost and metadata performance say otherwise. Pick a retention window and enforce it.
- **Catalog choice made by accident** (whatever the first tutorial used). Glue is fine for AWS-only shops; Nessie for Git-like branching workflows; Polaris for Snowflake interop; REST catalog as the open standard. Choose with intent.
- **Partition spec chosen casually.** Over-partitioning (one file per partition) creates the small-file problem instantly. Under-partitioning makes pruning useless. Common pattern: `days(ts)` or `bucket(N, key)`, not raw high-cardinality columns.
- **Multi-engine claims untested.** "Iceberg works with everything" is true *in principle*. In practice, write support, advanced features, and version compatibility vary. Test the engines you actually use before committing.
- **Format-war ignorance.** Iceberg vs Delta vs Hudi is a real strategic decision in 2026. Understand the trade-offs and the political landscape (see war stories below).
- **Treating Iceberg as a streaming sink without thinking through commit cadence.** Every commit is a snapshot; commit every 10 seconds and you have 8,640 snapshots per day. Batch writes; commit per minute or per partition.
- **Forgetting that schema evolution has limits.** Iceberg handles add/drop/rename columns gracefully; it does **not** automatically migrate semantically incompatible changes. Rename a column from `total_cents` (BIGINT) to `total` (DOUBLE) without thinking → silent corruption.

### Iceberg vs Delta vs Hudi (the format wars in one table)

| Dimension | Iceberg | Delta Lake | Hudi |
|---|---|---|---|
| **Origin** | Netflix (2017), Apache | Databricks (2017), Linux Foundation | Uber (2017), Apache |
| **Engine neutrality** | Strongest — true multi-engine spec | Historically Databricks-centric, opening up | Spark-centric, expanding |
| **Streaming writes** | Good (Flink, Spark) | Good (Spark Structured Streaming) | **Strongest — designed for it** |
| **Updates/deletes** | Copy-on-write or merge-on-read | Copy-on-write or merge-on-read | Copy-on-write or merge-on-read |
| **Catalog options** | Many (REST, Glue, Nessie, Polaris, Hive) | Unity Catalog (Databricks-led), others emerging | Hive Metastore primarily |
| **Industry momentum (2024–2026)** | **Winning the open standard war** | Strong; merging with Iceberg via UniForm | Niche; strong where it fits |

The big 2024 events that shaped the landscape:
- **Databricks acquired Tabular (Iceberg's commercial backer) for ~$1B**, signaling that even Databricks accepts Iceberg's win. UniForm now lets Delta tables be read as Iceberg.
- **Snowflake released Polaris** as an open Iceberg catalog, ending its closed-table era.
- **AWS announced S3 Tables** — Iceberg as a first-class S3 storage type with built-in optimization.
- **Cloudflare R2 added an Iceberg catalog** for egress-free lakehouse on R2.

The strategic read in 2026: **Iceberg is the open table format winner.** Pick it for new lakehouse work unless you have a specific reason to choose Delta (deep Databricks investment) or Hudi (heavy update workload, already on it).

### Cost & ops burden

| Flavor | Cost (relative) | Ops burden | When it's the right answer |
|---|---|---|---|
| **Self-managed (S3 + Glue/Nessie/Polaris)** | object storage cost only (~$23/TB-month) | Medium — compaction, expiration, catalog ops | Default for cost-sensitive lakehouse |
| **AWS S3 Tables** | small premium over raw S3 | Low — AWS handles compaction and maintenance | AWS-centric, want managed Iceberg without running it |
| **Snowflake managed Iceberg** | 1.5–2× normal Snowflake | Low — Snowflake runs the catalog and tables | Already Snowflake-heavy; want lakehouse interop |
| **Databricks managed (Unity + Iceberg/Delta UniForm)** | normal Databricks cost | Low — Unity Catalog handles governance | Databricks-heavy organization |
| **Tabular (acquired by Databricks)** | premium SaaS | Low — turnkey Iceberg | Want an opinionated managed Iceberg experience |
| **Cloudflare R2 + Iceberg Catalog** | R2 storage (no egress fees) | Medium — newer ecosystem | Cost-sensitive, multi-cloud reads, want zero egress |

The honest summary: **the engine is free, the storage is cheap, and the operational burden is in the maintenance jobs and the catalog.** Iceberg's cost story is what makes it disruptive to traditional warehouses — petabytes for thousands of dollars per month, queryable by any engine. The hidden cost is the discipline to run compaction and expiration jobs reliably.

### War stories
- **Netflix — birthplace (2017–2018).** Built Iceberg to escape Hive's operational pain: full-table scans from missing partition filters, atomic-rename-based commits that broke under S3 eventual consistency, schema evolution that required rewriting partitions. Public posts and the original paper *"Apache Iceberg: An Architectural Look Under the Covers"* are foundational. Lesson: **the data lake's biggest historical problems were not about data, they were about metadata.**
- **Apple — large-scale Iceberg adopter.** Public talks at Iceberg Summit describe deploying Iceberg across enormous internal datasets, including patterns for partition evolution and concurrent writes. Lesson: **at petabyte scale, partition evolution without rewriting data is not a nice-to-have; it is the difference between a one-week change and a six-month migration.**
- **Stripe — Iceberg for analytical events.** Public posts cover the architecture: services emit events to Kafka, Spark/Flink land them in Iceberg, downstream BI and ML read from there. Lesson: **Kafka + Iceberg is the modern default for the "central nervous system" of a data platform — Kafka for hot, Iceberg for warm/cold.**
- **Pinterest — moving off Hive.** Public posts describe migrating large Hive-partitioned tables to Iceberg, with the small-file problem being the dominant operational lesson. Lesson: **migrating to Iceberg is straightforward; running Iceberg without compaction is worse than staying on Hive.**
- **Adobe — Iceberg for Adobe Experience Platform.** Public engineering posts describe choosing Iceberg early (2019) and the operational maturity required at scale. Lesson: **early adopters paid a tax in tooling that mid-adopters in 2025+ don't have to pay. The ecosystem is now mature.**
- **Databricks ↔ Tabular acquisition (2024, ~$1B).** Databricks bought Tabular (founded by Iceberg's original creators at Netflix) for around a billion dollars. The acquisition signaled that even Iceberg's biggest commercial competitor accepted that Iceberg had won the open table format war. Lesson: **format wars in data infra resolve in 5–7 years; the winner becomes infrastructure. Iceberg in 2026 is what Parquet was in 2018.**
- **AWS S3 Tables (re:Invent 2024).** AWS made Iceberg a first-class S3 storage type with managed compaction. Lesson: **when the largest cloud provider builds something into the storage layer itself, the format has crossed from "trend" to "default."**
- **The cautionary tale — every team running streaming writes once.** Set up Spark Structured Streaming → Iceberg → forget compaction → six weeks later, queries are 100× slower than they should be, S3 list operations dominate the bill, and nobody understands why. Lesson: **compaction and expiration are not optional cron jobs; they are the contract.**

---

---

<a id="elasticsearch"></a>
## Elasticsearch

### Identity
A distributed, JSON-document-oriented **search and analytics engine** built on Apache Lucene. Created in 2010 by Shay Banon, now maintained by Elastic (SSPL/Elastic License since 2021, not Apache). The default answer when the requirement contains the word "search" — full-text, fuzzy, faceted, autocomplete, log search, or "find documents that match this complex set of conditions." Also widely (and sometimes wrongly) used as an analytics database and log store.

### Mental model
A **distributed library card catalog that can answer almost any question about the cards, fast**. Every document (card) is analyzed at write time — broken into tokens, stemmed, lowercased, n-grammed — and filed into an **inverted index**: a map from every term to every document that contains it. A search query is a lookup in this inverted index, not a scan. This is why search over 100M documents returns in 50 ms while a Postgres `LIKE '%term%'` on the same data takes minutes — fundamentally different data structure.

### How an inverted index actually works

The core difference from every other system in this doc. Every database scans forward through rows; Elasticsearch looks up terms backward to find documents.

```text
FORWARD INDEX (Postgres row store)            INVERTED INDEX (Elasticsearch)
──────────────────────────────────            ─────────────────────────────

doc_id → content                              term → doc_ids
                                              
1 → "kafka streaming log analyzer"           "kafka"      → [1, 4]
2 → "spark structured streaming job"         "streaming"  → [1, 2]
3 → "redis cache aside pattern"              "log"        → [1]
4 → "kafka consumer group rebalance"         "analyzer"   → [1]
                                             "spark"      → [2]
                                             "structured" → [2]
query: "kafka streaming"                     "job"        → [2]
                                             "redis"      → [3]
postgres: scan ALL rows,                     "cache"      → [3]
  check each for both words                  "aside"      → [3]
  → O(N) where N = total rows               "pattern"    → [3]
                                             "consumer"   → [4]
elasticsearch: look up "kafka" → [1,4]       "group"      → [4]
               look up "streaming" → [1,2]   "rebalance"  → [4]
               intersect → [1]
               → O(1) per term, then set intersection
               fast regardless of total document count
```

This is why Elasticsearch feels magical for search and why it is wrong for analytics — the data structure is optimized for "which documents match?" not "what is the average across all documents?"

### Sweet spot
- **Full-text search** across millions or billions of documents: product catalog search, site search, knowledge base, documentation search.
- **Log search** (the ELK stack: Elasticsearch + Logstash + Kibana). "Show me all error logs from service X containing 'timeout' in the last hour."
- **Autocomplete and typeahead** using n-gram tokenizers and prefix queries.
- **Faceted navigation** — "filter by brand, color, size, price range" on an e-commerce site. Aggregations on bucketed fields are fast and natural.
- **Fuzzy and phonetic search** — misspellings, synonyms, stemming, multilingual.
- **Security event search** (SIEM use case). Splunk's open-source alternative stack is often Elasticsearch + Kibana.
- **Geospatial search** with full-text — "restaurants near me matching 'sushi'."
- **Any workload where the query is "find me the best matches" rather than "give me an exact answer."**

### Don't use it for
- **Source of truth.** Elasticsearch is an index, not a database. The primary data should live in Postgres, Kafka, or S3. Rebuild the index when things go wrong, not the other way around.
- **OLTP.** No transactions, no foreign keys, no real UPDATE (delete + re-index). Updating a single field re-indexes the entire document.
- **Exact-answer analytics over billions of rows.** ClickHouse is 10–50× faster and cheaper for "count/sum/avg grouped by X." Elasticsearch can aggregate, but the inverted index adds overhead that column stores don't pay.
- **As a primary log store for cost-sensitive, long-retention scenarios.** Elasticsearch keeps data hot (on SSD/RAM); cold-tier exists but is operationally heavier than landing logs directly in Iceberg/Parquet on S3. Uber's public post on moving from Elasticsearch to ClickHouse for logs cites 10× cost reduction.
- **High write throughput of structured data that nobody searches.** If you're writing metrics or events and only querying with `GROUP BY`, you're paying the index-build cost for nothing.
- **Storing large binary blobs.** The index grows; search doesn't improve. Use object storage.
- **When you actually need a relational join.** Elasticsearch has no joins. Nested objects and parent-child relationships exist but are limited and expensive.

### How it breaks at scale

```text
   scale →   10 GB         100 GB        1 TB/index    10 TB+        multi-cluster
              │             │             │             │             │
  cliff:   ┌──┴──┐       ┌──┴──┐       ┌──┴──┐       ┌──┴──┐       ┌──┴──┐
           │Mappi│       │Shard │      │Merge │      │ JVM │       │Cross-│
           │ng   │       │ size │      │storm │      │heap │       │clustr│
           │explo│       │ pain │      │      │      │wall │       │search│
           │sion │       │      │      │      │      │     │       │      │
           └─────┘       └──────┘      └──────┘      └──────┘      └─────┘
   fix:  Strict mapping  Target 20-   Tune merge    31 GB heap    CCS / CCR
         + dynamic:false 50 GB/shard  policy;       max (no       (Elastic),
         from day 1      + ILM for    index         compressed    or per-region
                         lifecycle    templates     oops JVM      clusters
                                                    limit)
```

1. **Mapping explosion (the first cliff, hits early).** Elasticsearch auto-detects field types on first encounter. Throw in uncontrolled JSON and you get thousands of fields, each with its own inverted index, stored fields, and doc values. Memory and disk bloat, cluster refuses new fields past the limit. **Fix: `dynamic: false` or `dynamic: strict` from day one. Define your mapping explicitly. This is the Prometheus-cardinality-equivalent for Elasticsearch.**

2. **Shard sizing.** Shards are the unit of parallelism and the unit of operational pain. Too many small shards → high per-shard overhead (each shard is a Lucene index, holding heap memory). Too few large shards → recovery takes hours, and one hot shard is the bottleneck. **Sweet spot: 20–50 GB per shard. Use index lifecycle management (ILM) to roll over time-based indices automatically.**

3. **Merge storms.** Lucene segments are immutable; updates create new segments; background merges consolidate them. Under heavy write load, merges fall behind, search latency spikes, and the cluster looks "stuck." **Fix: tune `merge.scheduler.max_thread_count`, use time-based indices so old indices stop merging, and design for append-mostly workflows.**

4. **The 31 GB JVM heap wall.** Elasticsearch is Java. The JVM's compressed ordinary object pointers (compressed oops) work up to ~31 GB heap. Set heap to 32 GB or more and you *lose* memory efficiency — the effective usable heap drops. **Default rule: 31 GB max heap, 50% of machine memory, the other 50% for OS page cache (which Lucene relies on heavily).** This means a single node is practically capped at ~64 GB RAM. Scale-out is the only path beyond that.

5. **Cross-cluster complexity.** Cross-cluster search (CCS) and cross-cluster replication (CCR) exist but are Elastic-licensed features with operational overhead. Most teams run independent clusters per region or use case and accept the boundary.

### What seniors watch for
- **`dynamic: true` in production mappings.** Automatic field creation is the mapping-explosion fuse. Turn it off.
- **No ILM policy for time-series indices.** Indices grow forever, shards multiply, cluster degrades. Rollover + warm/cold/delete tiers are not optional for logs.
- **Heap set above 31 GB.** Instant flag in a review. The compressed-oops cliff is not intuitive but is well-documented.
- **Using Elasticsearch as the primary data store** with no rebuild path. The cluster corrupts, the index needs rebuilding, and the source data doesn't exist. This is the number one Elasticsearch war story.
- **Text fields used where keyword fields should be.** `text` fields get analyzed (tokenized, stemmed); `keyword` fields are exact. Filtering on a `text` field triggers full-text scoring when you just wanted a filter. Performance and relevance both suffer.
- **Deep pagination** (`from: 10000, size: 10`). Elasticsearch must score and sort all 10,010 documents, then throw away 10,000. Use `search_after` or the scroll API for deep pagination.
- **Giant documents** (>1 MB). Each field is analyzed and indexed; large documents chew through CPU and memory at index time. Chunk or exclude large fields.
- **Aggregation queries over high-cardinality fields** (e.g., "count distinct user_ids across 1B logs"). Works, but uses significant heap. At high cardinalities, ClickHouse or HyperLogLog approximations are better.
- **Reindex operations without capacity planning.** Reindexing a 5 TB index hammers the cluster; schedule it, throttle it, or use aliases to swap indices.
- **Choosing Elasticsearch for analytics because "we already have it for logs."** The sunk-cost trap. Logs search and analytics have different cost profiles and query patterns. Layering analytics on the log cluster works until it doesn't.

### Elasticsearch vs OpenSearch (the fork)

In 2021, Elastic changed Elasticsearch's license from Apache 2.0 to SSPL + Elastic License, prohibiting cloud providers from offering it as a service. AWS forked the last Apache-licensed version (7.10) into **OpenSearch**. The landscape in 2026:

| Dimension | Elasticsearch (Elastic) | OpenSearch (AWS/community) |
|---|---|---|
| **License** | Elastic License / SSPL (not open-source by OSI definition) | Apache 2.0 (open-source) |
| **Managed service** | Elastic Cloud | AWS OpenSearch Service, self-hosted |
| **Features** | Advanced ML, vector search, security analytics (proprietary) | Community-driven, growing feature set |
| **Compatibility** | Original; plugins are Elastic-ecosystem | Fork; some community plugins, diverging API surface |
| **When to pick** | Want latest features, willing to pay Elastic | AWS-native, need OSS license, or cost-sensitive |

Both are "Elasticsearch" under the hood (Lucene). The codebase diverged in 2021. **For new projects, pick based on cloud and licensing constraints, not technical superiority.**

### Cost & ops burden

| Flavor | Cost (relative) | Ops burden | When it's the right answer |
|---|---|---|---|
| **Self-managed OSS (OpenSearch)** | 1× | High — cluster ops, capacity planning, JVM tuning, ILM, upgrades | Cost-sensitive; have ops expertise |
| **AWS OpenSearch Service** | 2–4× | Low–medium — managed nodes, but shard/mapping design still yours | AWS-native; logs and search workloads |
| **Elastic Cloud** | 3–6× | Low — Elastic handles cluster, auto-scaling, upgrades | Want latest Elastic features; search is core to the product |
| **Alternatives for logs — Loki** | 0.2–0.5× ES cost | Low — log-native, stores labels + chunks on S3 | Logs only, don't need full-text search, Grafana-centric |
| **Alternatives for logs — ClickHouse** | 0.3–0.5× ES cost | Medium — see ClickHouse section | Need analytics over logs more than search |
| **Alternatives for search — Meilisearch, Typesense** | 1× | Low — single binary, simpler | Product search at smaller scale; don't need distributed cluster |

The honest summary: **Elasticsearch is expensive to run because data is indexed in RAM-backed structures, replicated across nodes, and Lucene segments need beefy SSDs.** The per-GB storage cost is 5–20× that of Parquet on S3. This is fine for search workloads (the value is in the index, not the storage). It is not fine for "we put all our logs here because Kibana looks nice" — that is the single biggest Elasticsearch cost trap in the industry.

### War stories
- **Wikipedia — search at scale.** Runs Elasticsearch (via CirrusSearch plugin for MediaWiki) powering search across hundreds of millions of articles in hundreds of languages. Public documentation covers the architecture. Lesson: **full-text search with multilingual analysis, fuzzy matching, and relevance tuning is Elasticsearch's home territory. Nothing else is close for this use case.**
- **GitHub — code search (2023).** Built a custom search infrastructure on Elasticsearch (later augmented with Blackbird, a Rust-based indexer) to search 200M+ repositories. Public engineering post: *"The technology behind GitHub's new code search."* Lesson: **Elasticsearch anchors the search workflow, but extreme-scale code search requires domain-specific augmentation — Elasticsearch alone isn't the whole answer at GitHub's scale.**
- **Uber — moving logs off Elasticsearch (2020).** Migrated from an Elasticsearch-based logging stack to ClickHouse, citing 10× cost reduction and better query performance for analytical log queries. Public post: *"Fast and Reliable Schema-Agnostic Log Analytics Platform."* Lesson: **"search" and "analytics" are different workloads with different cost profiles. Using a search engine for analytics over logs burns money.**
- **Netflix — Elasticsearch for distributed tracing and search.** Public posts describe using Elasticsearch for searching traces and metadata across their microservices fleet. Lesson: **the "find me the needle" workload is where Elasticsearch earns its keep. The "show me the haystack's statistics" workload belongs elsewhere.**
- **Grafana Labs — building Loki as the anti-Elasticsearch.** Loki deliberately stores log lines unindexed in object storage, indexing only labels. Public posts and talks describe the motivation: Elasticsearch for logs was 10× too expensive for most teams. Lesson: **the full-text index is a luxury. If your "search" is really "filter by service + grep for a keyword," Loki does it at a fraction of the cost.**
- **Elastic ↔ AWS fork (2021).** Elastic changed the license; AWS forked OpenSearch; the community split. Public blog posts from both sides document the arguments. Lesson: **open-source licensing is an infrastructure dependency. If your build depends on a specific license, track it like you track a breaking API change.**
- **The cautionary tale — mapping explosion.** A team ships a new microservice that logs request headers as top-level JSON fields. Each unique header name becomes a new field in the mapping. Within days: thousands of fields, heap exhaustion, cluster degradation, cascading failures in the logging pipeline. No single famous post-mortem because every team that has run Elasticsearch at scale has a version of this story. Lesson: **`dynamic: strict` is the seatbelt. Wear it.**

---

*Next up: Cassandra.*

---

<a id="cassandra"></a>

## Cassandra

### Identity
A masterless, wide-column **distributed database** designed for massive write throughput and linear horizontal scale across commodity hardware. Originally built at Facebook for inbox search (2007), open-sourced, now an Apache project. The defining trade-off: Cassandra sacrifices read flexibility and strong consistency to achieve write-anywhere availability across data centers. If your workload is "write a lot, read by known keys, never go down," Cassandra is the obvious answer. If your workload includes "I'd like to query this flexibly," you will suffer.

### Mental model
A **distributed hash ring of identical nodes where every write is an append**. There is no master, no leader election, no single point of failure. Data is partitioned by a hash of the partition key and replicated to N nodes clockwise around the ring. Any node can accept any write for any partition — it just forwards to the correct replicas. This is why it writes so fast: no coordination, no consensus, just "accept the mutation, replicate it, sort it out later." The "sort it out later" part is eventual consistency, and it is both the superpower and the price you pay.

### How the write path actually works

The core reason Cassandra writes faster than almost anything else at scale. Every write is sequential I/O — no random disk seeks, no read-before-write.

```text
CLIENT WRITE
    │
    ▼
┌────────────┐   1. Append to commit log     ┌──────────────┐
│ Coordinator │ ─────────────────────────────▶│  Commit Log  │
│   (any node)│                               │ (sequential) │
└─────┬──────┘                                └──────────────┘
      │
      │  2. Write to memtable (in-memory sorted structure)
      ▼
┌────────────┐
│  Memtable  │──── sorted by clustering key
│   (RAM)    │
└─────┬──────┘
      │  3. Flush when full
      ▼
┌────────────┐
│  SSTable   │──── immutable, sorted, on disk
│  (on disk) │
└────────────┘
      │  4. Background compaction merges SSTables
      ▼
┌────────────┐
│  Merged    │──── fewer files, tombstones cleaned
│  SSTable   │
└────────────┘

Read path (the expensive one):
  → Check memtable
  → Check bloom filters on each SSTable
  → Read matching SSTables
  → Merge results, resolve conflicts by timestamp (last-write-wins)
  → Return to client
```

Writes touch only sequential I/O (commit log append) and RAM (memtable). Reads must potentially consult multiple SSTables and merge. **This is why Cassandra is a write-optimized system that trades read efficiency for write speed.** Every data model decision you make is about keeping the read path short.

### The data modeling paradigm (query-first design)

This is the thing that trips up every engineer coming from the relational world. In Postgres, you model the entities, normalize, and write any query you want. In Cassandra, you **start with the queries and design the tables backward from them**. One query = one table. Denormalization is not a smell, it is the architecture.

```text
RELATIONAL THINKING                    CASSANDRA THINKING
──────────────────                     ──────────────────

"What entities exist?"                 "What queries will the app run?"
     │                                      │
     ▼                                      ▼
 Normalize into tables               One table per query pattern
     │                                      │
     ▼                                      ▼
 Write any query (JOIN, WHERE,        Partition key = equality filter
  GROUP BY, subqueries)               Clustering key = range/sort
     │                                      │
     ▼                                      ▼
 Optimize later with indexes          No JOINs, no ad-hoc queries,
                                      no GROUP BY across partitions
```

**Partition key** determines which node stores the data. **Clustering key** determines sort order within a partition. If your query doesn't align with these keys, you either do a full-cluster scatter query (slow, defeats the purpose) or you create another table that does align. This is why Cassandra clusters often have 3–5× the table count of the equivalent Postgres schema.

### Sweet spot
- **Time-series event data at massive write scale.** IoT sensor data, user activity streams, application event logs where writes are 10–100× reads. The append-only write path and time-based clustering keys are purpose-built for this.
- **Messaging and inbox-style workloads.** The original use case at Facebook. Partition by user, cluster by timestamp, read the most recent N. Discord used Cassandra (later Scylla) for trillions of messages.
- **Write-heavy workloads across multiple data centers.** Masterless replication means every DC can accept writes independently with no cross-DC latency on the write path. Active-active multi-region for real.
- **Personalization and user profile stores** at scale. Partition by user_id, denormalize the profile + preferences + recent activity. Netflix stores billions of subscriber records this way.
- **Device and fleet management state.** Millions of devices reporting telemetry, each identified by device_id. Apple reportedly runs one of the largest Cassandra deployments for iCloud and device-state workloads.
- **Any workload where "never go down" matters more than "always consistent."** Cassandra at RF=3 with `LOCAL_QUORUM` reads/writes survives node failures, rack failures, and even a DC failure without human intervention.

### Don't use it for
- **Anything that needs ad-hoc queries.** If the product manager might say "can we also filter by X?" next quarter, and X isn't in your partition key, you're either building a new table or suffering. Postgres or Elasticsearch handle this effortlessly.
- **Small datasets.** Below 100 GB or a few hundred thousand writes per second, Cassandra's operational complexity isn't justified. Postgres with read replicas will do fine and give you real queries.
- **Workloads requiring strong consistency or transactions.** Lightweight transactions (LWT / Paxos) exist but are 4–10× slower than normal writes. If every write needs a compare-and-set, Cassandra is fighting its own design.
- **Analytics and aggregations.** No GROUP BY across partitions, no JOINs, no window functions. Export to Spark or ClickHouse for analytics. Using Cassandra for OLAP is burning money and developer sanity.
- **Workloads with frequent updates and deletes.** Cassandra doesn't update in place — it writes a new timestamped version. Deletes write a tombstone that lingers until compaction. Heavy update/delete patterns create tombstone storms that degrade reads to the point of timeouts.
- **Secondary index-heavy access patterns.** Cassandra's secondary indexes (SASI, SAI, or the legacy kind) are local to each node, meaning a secondary-index query must scatter to every node. This is fine for low-cardinality filtering on small result sets; it is catastrophic at scale.
- **Anything that needs referential integrity.** No foreign keys, no constraints, no cascading deletes. Your application owns all consistency.

### How it breaks at scale

```text
   scale →   100 GB       1 TB          10 TB         100 TB+       multi-DC
              │             │             │             │             │
  cliff:   ┌──┴──┐       ┌──┴──┐       ┌──┴──┐       ┌──┴──┐       ┌──┴──┐
           │Tomb-│       │Parti│       │Compa-│       │ JVM │       │Confl-│
           │stone│       │tion │       │ction │       │ GC  │       │ict   │
           │storm│       │ hot │       │ IO   │       │pause│       │resol-│
           │     │       │spots│       │storm │       │     │       │ution │
           └─────┘       └──────┘      └──────┘      └──────┘      └─────┘
   fix:  Tune gc_grace  Bucket by     Choose         Off-heap     LWTs for
         + avoid        time or       compaction     + G1GC +     critical
         delete-heavy   shard large   strategy       heap < 31G   paths; or
         patterns       partitions    (LCS vs        or move      switch to
                                      STCS vs        to Scylla    CRDT-based
                                      TWCS)          (no JVM)     resolution
```

1. **Tombstone storms (the first cliff, hits early).** Deletes in Cassandra don't remove data — they write a tombstone marker that says "this is dead." Tombstones accumulate until compaction runs. If a read hits a partition with thousands of tombstones, it must scan through all of them before returning results. At the default `gc_grace_seconds` of 10 days, tombstones pile up. Teams that delete frequently (TTL-based expiry on dense partitions, queue-like patterns) hit read timeouts. **Fix: use `TWCS` compaction for time-series (drops whole SSTables when they expire), keep `gc_grace_seconds` aligned with your repair schedule, and fundamentally avoid delete-heavy access patterns.**

2. **Partition hot spots.** All data for a partition key lives on the same set of nodes. Pick a bad partition key (e.g., `country` when 80% of traffic is US, or `date` when today's partition gets all the writes) and you have a hot node while the rest of the ring is idle. **Fix: add a bucketing dimension to the partition key. `(sensor_id, date_bucket)` instead of just `sensor_id`. The trade-off: reads now need to query multiple partitions and merge client-side.**

3. **Compaction I/O storms.** Compaction merges SSTables in the background. Under heavy write load, compaction falls behind, pending SSTables pile up, read latency degrades (more files to consult), and a sudden compaction burst saturates disk I/O. **Fix: choose the right compaction strategy — TWCS for time-series, LCS for read-heavy with updates, STCS (default) for write-heavy. Provision SSDs, never spinning disks. Monitor `pending compaction` in nodetool.**

4. **JVM GC pauses (the Java tax).** Cassandra is Java. Large heaps mean long GC pauses. Long GC pauses mean the node looks dead to the cluster, triggers hinted handoff or streaming, and creates a cascading mess. **Fix: keep heap at 8–16 GB (never above 31 GB for the same compressed-oops reason as Elasticsearch), use G1GC, and let the OS page cache handle the rest. Or migrate to ScyllaDB — a C++ Cassandra-compatible rewrite that eliminates the JVM entirely and claims 10× throughput per node.**

5. **Multi-DC conflict resolution surprises.** With active-active writes to multiple data centers, conflicts are resolved by last-write-wins (highest timestamp). If clocks are skewed (even by milliseconds), the "wrong" write can silently win. NTP drift is the silent killer. **Fix: use NTP with tight sync, prefer `LOCAL_QUORUM` consistency level (per-DC quorum without cross-DC latency), and design the data model so concurrent writes to the same cell are rare. For truly conflict-sensitive operations, use LWTs (Paxos) — but accept the latency cost.**

6. **Scaling down is harder than scaling up.** Adding nodes is straightforward (the ring rebalances). Removing nodes requires streaming data off them, which is slow and I/O-heavy. Decommissioning nodes from a large cluster can take days. **Most teams over-provision and accept the cost rather than attempt to shrink.**

### What seniors watch for
- **Partition key design that doesn't match query patterns.** The single most impactful decision. Get it wrong and you rebuild the table. Every code review of a Cassandra schema should start with "what queries does this serve?"
- **Unbounded partition growth.** A partition key of `user_id` for a messaging app means one user's partition grows forever. Eventually the partition exceeds the practical limit (~100 MB, hard limit 2 GB) and reads degrade. **Bucket by time: `(user_id, month)` or `(user_id, conversation_id)`.**
- **`ALLOW FILTERING` in production queries.** This keyword means "I know this will do a full-cluster scan, do it anyway." It exists for dev convenience. In production it is a time bomb.
- **`SELECT *` across partitions without `LIMIT`.** Unbounded scatter-gather queries that fan out to every node and return unbounded data. Kills the coordinator node.
- **Lightweight transactions (LWT) used for hot paths.** LWTs use Paxos: 4 round trips per operation. If your hot write path needs LWT, your data model is wrong for Cassandra.
- **Consistency level `ALL` used casually.** `ALL` means every replica must respond — a single slow or dead node fails the query. Use `LOCAL_QUORUM` (majority in the local DC) for the balance of consistency and availability.
- **No `nodetool repair` schedule.** Anti-entropy repair is how Cassandra ensures replicas converge. Without regular repairs, data drifts silently. Must run at least every `gc_grace_seconds` (default 10 days) or risk resurrecting deleted data (zombie rows).
- **Mixing workload types on the same cluster.** Batch analytics queries on a cluster serving real-time traffic. The analytics queries saturate disk I/O and compaction resources, spiking P99 for the latency-sensitive path.
- **TTL on large partitions without TWCS.** TTL-based expiry generates tombstones. Without TWCS (which drops entire SSTables once all data has expired), those tombstones accumulate and degrade reads.
- **Running repairs during peak traffic.** Repair is I/O and network heavy. Schedule it during off-peak, or use incremental repairs with sub-range partitioning.

### Cassandra vs ScyllaDB (the C++ rewrite)

ScyllaDB is a drop-in Cassandra replacement written in C++ (Seastar framework), eliminating the JVM and its GC pauses. The landscape:

| Dimension | Cassandra (Apache) | ScyllaDB |
|---|---|---|
| **Language** | Java (JVM) | C++ (Seastar, shard-per-core) |
| **GC pauses** | Yes — the defining operational headache | None — no JVM |
| **Throughput per node** | Baseline | 5–10× higher (fewer nodes needed) |
| **Compatibility** | Original; CQL, SSTable format | CQL-compatible; most drivers work unchanged |
| **Compaction** | Background threads, can spike latency | Incremental compaction, more predictable latency |
| **Community / ecosystem** | Massive; 15+ years, battle-tested | Growing; Discord, Expedia, Zillow among public users |
| **Managed service** | AWS Keyspaces, DataStax Astra, Instaclustr | ScyllaDB Cloud |
| **When to pick** | Existing Cassandra expertise; proven at scale | Greenfield; want fewer nodes, lower tail latency |

**For new projects with Cassandra-shaped workloads, ScyllaDB is increasingly the default recommendation** — same data model, same CQL, but fewer nodes and no JVM tuning. For existing Cassandra clusters, migration is feasible but non-trivial.

### Cost & ops burden

| Flavor | Cost (relative) | Ops burden | When it's the right answer |
|---|---|---|---|
| **Self-managed Apache Cassandra** | 1× | High — ring management, compaction tuning, repair scheduling, JVM tuning, capacity planning | Deep expertise; cost-sensitive; need full control |
| **DataStax Astra (managed Cassandra)** | 3–5× | Low — serverless or provisioned, auto-scaling | Want Cassandra semantics without the ops |
| **AWS Keyspaces** | 2–4× | Low — serverless CQL, pay-per-request | Low-throughput CQL workloads on AWS; limited feature set |
| **ScyllaDB Cloud** | 2–3× (but fewer nodes) | Low–medium — managed, but schema design still yours | Cassandra workload shape; want better price/performance |
| **Self-managed ScyllaDB** | 0.3–0.5× (via node reduction) | Medium — no JVM, but still ring ops, compaction, repair | Ops team exists; want to cut Cassandra costs 3–5× |

The honest summary: **Cassandra is cheap per GB stored but expensive per engineer-hour operated.** The database itself is free; the humans who tune compaction, schedule repairs, right-size partitions, and get paged at 3 AM for GC pauses are not. ScyllaDB removes one dimension of pain (JVM) but the data-modeling complexity — which is the real cost — remains identical.

### War stories
- **Discord — trillions of messages on Cassandra, then migration to ScyllaDB (2023).** Originally chose Cassandra for the write-heavy, partition-per-channel model. At trillions of messages, GC pauses caused cascading latency spikes. Migrated to ScyllaDB, same data model, dramatically better tail latency. Public blog post: *"How Discord Stores Trillions of Messages."* Lesson: **Cassandra's data model was right; the JVM was the bottleneck. ScyllaDB inherits the model without the GC tax.**
- **Netflix — personalization and subscriber state.** One of the largest public Cassandra deployments. Stores subscriber activity, viewing history, and personalization data across multiple regions with active-active replication. Public talks at Cassandra Summit describe multi-DC consistency strategies. Lesson: **masterless replication across regions is Cassandra's killer feature. Netflix designs for eventual consistency at the application layer and avoids LWTs on hot paths.**
- **Apple — reportedly the largest Cassandra deployment (2015 figure: 150K+ nodes).** Powers iCloud and other services. Scale is the point — nothing else runs a ring that large. Lesson: **Cassandra's linear scale-out is real, but operating at this size requires dedicated database teams, custom tooling, and deep JVM expertise.**
- **Instagram — migration away from Cassandra to their own storage.** Initially used Cassandra for the feed, then built custom systems as access patterns evolved beyond what query-first modeling could serve efficiently. Lesson: **when your queries change faster than you can rebuild tables, Cassandra's rigidity becomes a liability. This is the anti-pattern: choosing Cassandra for a product whose access patterns are still evolving.**
- **Uber — Cassandra for driver/rider matching state.** Public talks describe using Cassandra for real-time geospatial state in the dispatch system, with careful partition key design around geohash buckets. Lesson: **creative partition key design can make Cassandra work for workloads it wasn't originally designed for — but it requires deep understanding of the access pattern.**
- **The cautionary tale — tombstone storm.** A team builds a queue on Cassandra: write a row, read it, delete it. Classic queue pattern. Reads become progressively slower. P99 read latency goes from 10 ms to 10 seconds. Cluster appears healthy, CPU is low, disk is fine. The root cause: millions of tombstones in each partition that every read must skip over. The fix required changing the data model to use TTLs with TWCS compaction and abandoning the queue pattern entirely. No single famous post-mortem because this is the most common Cassandra anti-pattern — **do not build a queue on Cassandra.**

---

<a id="scenarios"></a>

## Scenarios

The database sections above work top-down: here is a system, here is what it's good for. This chapter works **bottom-up**: here is a problem, here is the stack a senior engineer would reach for, and here is why. Each scenario names the dominant workload shape, the obvious pick, the alternatives, and the trap that gets teams in trouble.

---

### 1. Payments

**The workload shape:** Low-throughput, high-correctness writes. Every transaction must be ACID. Auditability is a regulatory requirement. Reads are a mix of point lookups ("show me this payment") and analytical queries ("total refunds this quarter"). Double-charging a customer is a career-ending, lawsuit-inviting event.

**The stack:**

```text
┌─────────────────────────────────────────────────────┐
│                   Application                       │
└──────────┬──────────────────────┬───────────────────┘
           │ writes               │ reads
           ▼                      ▼
   ┌──────────────┐       ┌──────────────┐
   │  PostgreSQL  │──────▶│  PostgreSQL  │
   │   (primary)  │  WAL  │  (read replica│
   │              │ repli │   for reports)│
   └──────┬───────┘ cation└──────────────┘
          │
          │ CDC (Debezium / WAL)
          ▼
   ┌──────────────┐       ┌──────────────┐
   │    Kafka     │──────▶│  ClickHouse  │
   │  (event log) │       │ (analytics)  │
   └──────────────┘       └──────────────┘
```

**Why Postgres:** Transactions, foreign keys, constraints, CHECK clauses, serializable isolation. The data model is relational by nature (users, accounts, transactions, ledger entries). Postgres's `SERIALIZABLE` isolation level prevents the phantom reads that cause double-charges. The WAL is your audit log's backbone.

**Why not:**
- **Cassandra** — no transactions, no joins, last-write-wins conflict resolution. A payment system on Cassandra is a lawsuit in incubation.
- **Redis** — not a source of truth. Caching payment status is fine; storing it is not.
- **MongoDB** — multi-document transactions exist now, but the relational model is a better fit for the inherently relational domain of accounting.

**The trap:** Running analytics directly on the OLTP primary. The monthly reconciliation query that table-scans 500M rows and pins CPU at 100% for 20 minutes, degrading checkout latency. **Fix: CDC to Kafka → ClickHouse for analytics. The OLTP database does OLTP; the analytics database does analytics.**

---

### 2. E-commerce (product catalog + orders)

**The workload shape:** Mixed. The product catalog is read-heavy with complex queries (filter by category, price, brand, text search, facets). The order pipeline is write-heavy with strict consistency needs. Inventory is a hot counter. These are three different workload shapes pretending to be one system.

**The stack:**

```text
   ┌────────────┐
   │  Product   │──▶ Elasticsearch (search, facets, autocomplete)
   │  Catalog   │──▶ Postgres (source of truth, admin CRUD)
   └────────────┘

   ┌────────────┐
   │   Orders   │──▶ Postgres (ACID, relational: orders → line_items → payments)
   └────────────┘

   ┌────────────┐
   │ Inventory  │──▶ Redis (hot counter for "in stock" during checkout)
   │            │──▶ Postgres (source of truth, reconciled async)
   └────────────┘

   ┌────────────┐
   │  Sessions  │──▶ Redis (hash per session, TTL = session timeout)
   └────────────┘
```

**Why this split:** Each sub-problem has a different shape. Search is an Elasticsearch problem (inverted index, fuzzy matching, facets). Orders are a Postgres problem (transactions, referential integrity). Inventory at checkout speed is a Redis problem (atomic `DECR`, sub-millisecond). Trying to serve all of these from one system creates a mediocre experience everywhere.

**The trap:** Using Elasticsearch as the source of truth for the product catalog because "we already index there." The index corrupts, you rebuild — from what? **Postgres is the truth; Elasticsearch is the index. Always rebuild-able.**

---

### 3. Black Friday (flash-sale / extreme-burst traffic)

**The workload shape:** Normal traffic 364 days a year, 50–100× spike for hours. The spike is write-heavy (orders, cart updates, inventory decrements) and read-heavy simultaneously (product pages, search, recommendations). The system that survives Monday can't survive Friday without preparation.

**The stack:**

```text
   Normal path (364 days):
   App → Postgres → done

   Black Friday path:
   ┌────────┐     ┌───────┐     ┌──────────┐     ┌──────────┐
   │  CDN   │────▶│ Redis │────▶│ Kafka    │────▶│ Postgres │
   │(static)│     │(cache,│     │(order    │     │(eventual │
   │        │     │ rate  │     │ queue,   │     │ write)   │
   │        │     │ limit)│     │ decouple)│     │          │
   └────────┘     └───────┘     └──────────┘     └──────────┘
```

**The key insight:** You don't scale the database for Black Friday. You **put things in front of it** so it doesn't see Black Friday. CDN for static assets. Redis for session, rate limiting, and inventory cache. Kafka as a buffer between the burst and the database — accept the order into Kafka immediately (fast, durable, horizontally scalable), process it into Postgres asynchronously.

**Why Kafka is the hero here:** It absorbs the write burst. Postgres can process orders at its own pace from the Kafka topic. The user gets "order confirmed" when it hits Kafka, not when it hits Postgres. Eventual consistency between "order accepted" and "order in database" is measured in seconds, which is acceptable.

**The trap:** Trying to scale Postgres horizontally for the spike. Read replicas help reads, but writes still hit the primary. Vertical scaling has a ceiling. **The answer is not a bigger database, it is a queue in front of the same database.**

---

### 4. Search

**The workload shape:** Read-heavy. Users type partial queries and expect results in under 200 ms. The data is semi-structured text (product descriptions, articles, user-generated content). Requirements include fuzzy matching, typo tolerance, faceted filtering, relevance ranking, highlighting, and autocomplete.

**The stack:**

```text
   ┌──────────┐    CDC / sync     ┌───────────────┐
   │ Postgres │ ────────────────▶ │ Elasticsearch  │
   │ (truth)  │                   │ (search index) │
   └──────────┘                   └───────┬────────┘
                                          │
                                   ┌──────┴──────┐
                                   │   App /     │
                                   │  Search UI  │
                                   └─────────────┘
```

**Why Elasticsearch:** The inverted index is the right data structure. Full-text search, fuzzy matching, n-gram tokenizers for autocomplete, aggregations for faceted navigation — all first-class. Postgres `tsvector` / `pg_trgm` handles simple full-text search surprisingly well up to ~10M rows. Beyond that, or when you need relevance tuning, language-aware analysis, or facets, Elasticsearch is the standard answer.

**Alternatives by scale:**
- **< 1M documents, simple search:** Postgres `tsvector` + `GIN` index. No extra system.
- **< 10M documents, product search:** Meilisearch or Typesense — single binary, simpler ops, good relevance out of the box.
- **> 10M documents, complex requirements:** Elasticsearch or OpenSearch. The ops burden is justified.

**The trap:** Building search on Cassandra or Redis. Cassandra has no full-text search capability. Redis has RediSearch, which works at small scale but is not Elasticsearch. The inverted index exists for a reason — use a system built around it.

---

### 5. Observability (logs, metrics, traces)

**The workload shape:** Extremely high write throughput, append-only, time-series. Reads are either "find a needle" (log search) or "show me a dashboard" (metrics aggregation). Retention is days to months, not years. Cost dominates — observability data is the single largest storage line item at most companies.

**The stack:**

```text
   ┌───────────────────────────────────────────────────────┐
   │                    Ingest tier                        │
   │   Apps → OpenTelemetry Collector → Kafka (buffer)     │
   └──────────────┬──────────────┬──────────────┬─────────┘
                  │              │              │
                  ▼              ▼              ▼
           ┌──────────┐  ┌──────────┐   ┌──────────┐
           │  Logs    │  │ Metrics  │   │  Traces  │
           │          │  │          │   │          │
           │ Loki /   │  │Prometheus│   │  Tempo / │
           │ ES /     │  │  / Mimir │   │  Jaeger  │
           │ClickHouse│  │          │   │          │
           └──────────┘  └──────────┘   └──────────┘
                  │              │              │
                  └──────────────┴──────────────┘
                              │
                        ┌──────────┐
                        │ Grafana  │
                        │ (query)  │
                        └──────────┘
```

**The three pillars, three different systems:**
- **Metrics** → Prometheus (or Mimir/Thanos for multi-cluster). Time-series, pre-aggregated, tiny storage footprint. PromQL for dashboards and alerts.
- **Logs** → Loki (cheap, label-indexed, grep-based) or Elasticsearch (expensive, full-text indexed, powerful search). ClickHouse is emerging as the middle ground — cheaper than ES, more query-capable than Loki.
- **Traces** → Tempo or Jaeger. Append-only, read-by-trace-ID. Object storage (S3) as the backend keeps costs low.

**Why not one system for all three:** Different query patterns. Metrics are "aggregate across time"; logs are "find matching text"; traces are "follow a request ID." A system optimized for one is mediocre at the others. The industry tried "put everything in Elasticsearch" and learned the cost lesson.

**The trap:** Elasticsearch for everything. It works, but at 10× the cost of purpose-built alternatives. Uber's public post on moving logs from ES to ClickHouse cites 10× cost reduction. Grafana Labs built Loki specifically because ES for logs was too expensive for most teams.

---

### 6. Data lake (analytics on raw, heterogeneous data)

**The workload shape:** Ingest everything — structured, semi-structured, raw — at massive scale. Query it later with SQL. Schema evolves over time. Storage costs dominate. Query latency is seconds to minutes, not milliseconds. The users are analysts and data scientists, not end-user applications.

**The stack:**

```text
   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
   │  App DBs     │  │  Event       │  │  Third-party │
   │  (CDC)       │  │  streams     │  │  APIs        │
   └──────┬───────┘  └──────┬───────┘  └──────┬───────┘
          │                 │                 │
          └────────────┬────┘─────────────────┘
                       ▼
               ┌──────────────┐
               │    Kafka     │
               │  (ingest bus)│
               └──────┬───────┘
                      │
                      ▼
               ┌──────────────┐
               │  S3 / GCS   │ ◀── Parquet / ORC files
               │  (storage)  │
               └──────┬───────┘
                      │
               ┌──────┴───────┐
               │ Apache Iceberg│ ◀── table format (schema, partitioning,
               │  (metadata)  │      time travel, ACID on object storage)
               └──────┬───────┘
                      │
          ┌───────────┼───────────┐
          ▼           ▼           ▼
   ┌──────────┐ ┌──────────┐ ┌──────────┐
   │  Spark   │ │  Trino   │ │ ClickHouse│
   │ (batch)  │ │(ad-hoc SQL│ │(fast OLAP)│
   └──────────┘ └──────────┘ └──────────┘
```

**Why Iceberg on S3:** Storage is separated from compute. S3 costs ~$23/TB/month. Iceberg adds ACID semantics, schema evolution, partition evolution, and time travel on top of dumb object storage. You store once, query from any engine (Spark, Trino, Flink, ClickHouse). This is the modern data lake architecture that has largely replaced HDFS-based Hadoop.

**Why not a traditional database:** At 100 TB+, no database is cost-competitive with Parquet files on S3. ClickHouse is excellent for structured analytics but costs 10–50× more per TB than object storage. The lake stores everything cheaply; ClickHouse or Trino query the parts you care about.

**The trap:** Building a "data lake" that is actually a data swamp — dumping raw JSON into S3 with no schema, no partitioning, no catalog. Six months later, nobody can find or query anything. **Iceberg (or Delta Lake, or Hudi) is the difference between a lake and a swamp. The table format is not optional.**

---

### 7. Leaderboards

**The workload shape:** High-frequency score updates, real-time rank queries. "What is player X's rank?" and "Who are the top 100?" must both be fast. Updates arrive at thousands per second during peak gaming hours.

**The stack:**

```text
   Game server → Redis ZADD(leaderboard, score, player_id)

   Queries:
     Top 100        → ZREVRANGE leaderboard 0 99 WITHSCORES
     Player rank     → ZREVRANK leaderboard player_id
     Player score    → ZSCORE leaderboard player_id
     Players near me → ZREVRANGE leaderboard (rank-5) (rank+5)
```

**Why Redis sorted sets:** This is the canonical answer. `ZADD` is O(log N). `ZREVRANK` is O(log N). A sorted set with 100M members fits in ~6 GB of RAM and answers any of these queries in under 1 ms. No other system comes close for this access pattern.

**Persistent backing:** Redis is RAM. Persist the leaderboard to Postgres on a schedule (every minute or on game-end). If Redis dies, rebuild from Postgres. The leaderboard is a materialized view, not the source of truth.

**Scaling beyond one sorted set:**
- **Multiple leaderboards** (daily, weekly, all-time): one sorted set per board, TTLs on the daily/weekly ones.
- **Hundreds of millions of players:** shard by game or region. Redis Cluster distributes sorted sets across nodes.
- **Historical leaderboards:** snapshot to Postgres or ClickHouse nightly.

**The trap:** Building a leaderboard in Postgres with `SELECT *, RANK() OVER (ORDER BY score DESC) FROM scores`. Works beautifully at 10K rows. At 10M rows, the window function scans the entire table on every query. Redis eliminates this by maintaining the sorted order incrementally.

---

### 8. Fraud detection

**The workload shape:** Real-time decisioning on streaming events. Every transaction must be scored before it's approved — latency budget is 50–200 ms total. The scoring logic needs access to historical aggregates ("how many transactions has this card done in the last hour?") and pattern matching ("is this IP associated with known fraud rings?"). Write-heavy on the event side, read-heavy on the feature side.

**The stack:**

```text
   ┌──────────┐     ┌──────────┐     ┌────────────────┐     ┌──────────┐
   │Transaction│────▶│  Kafka   │────▶│ Flink / Spark  │────▶│ Decision │
   │  event   │     │ (stream) │     │ Streaming      │     │ API      │
   └──────────┘     └──────────┘     │ (feature calc, │     │(approve/ │
                                     │  rule engine)  │     │ decline) │
                                     └───────┬────────┘     └──────────┘
                                             │
                              ┌──────────────┼──────────────┐
                              ▼              ▼              ▼
                       ┌──────────┐   ┌──────────┐   ┌──────────┐
                       │  Redis   │   │ Postgres │   │ClickHouse│
                       │(real-time│   │ (rules,  │   │(historical│
                       │ counters,│   │ case mgmt│   │ analysis) │
                       │ features)│   │ review)  │   │          │
                       └──────────┘   └──────────┘   └──────────┘
```

**Why this combination:**
- **Kafka** buffers and distributes the event stream to multiple consumers (scoring, logging, analytics).
- **Flink or Spark Streaming** computes real-time features: velocity counters ("5 transactions in 2 minutes"), geo-distance checks, pattern matching.
- **Redis** stores pre-computed features (sliding-window counters, device fingerprint caches, blacklists) for sub-millisecond lookups during scoring.
- **Postgres** stores the rules configuration, investigation cases, and analyst workflow. Relational and transactional — exactly what case management needs.
- **ClickHouse** runs historical analysis: "show me all transactions matching this fraud pattern over the last 90 days." OLAP over billions of events.

**The trap:** Trying to do real-time scoring by querying Postgres directly. A `SELECT COUNT(*) FROM transactions WHERE card_id = X AND created_at > now() - interval '1 hour'` is correct but slow under load. **Pre-compute the features into Redis via the stream processor. The scoring path should be lookups, not queries.**

---

### 9. Recommendations ("users who bought X also bought Y")

**The workload shape:** Read-heavy at serving time (every page load triggers a recommendation request), batch-heavy at training time (process the entire interaction history to generate models). The serving path needs sub-100 ms latency. The training path can take hours. These two paths have completely different database needs.

**The stack:**

```text
   TRAINING PATH (batch, hourly/daily):
   ┌──────────┐     ┌──────────┐     ┌──────────────┐     ┌──────────┐
   │ User     │────▶│ Data Lake│────▶│ Spark / ML   │────▶│ Model    │
   │ events   │     │ (Iceberg │     │ pipeline     │     │ artifacts│
   │ (Kafka → │     │  on S3)  │     │ (ALS, neural │     │ (S3)     │
   │  S3)     │     │          │     │  collab filt) │     │          │
   └──────────┘     └──────────┘     └──────┬───────┘     └──────────┘
                                            │
                                            │ precomputed recs
                                            ▼
   SERVING PATH (real-time, per request):
   ┌──────────┐     ┌──────────┐     ┌──────────┐
   │ App      │────▶│  Redis   │────▶│ Response │
   │ request  │     │ (lookup  │     │ (top 20  │
   │          │     │  user →  │     │  items)  │
   │          │     │  recs)   │     │          │
   └──────────┘     └──────────┘     └──────────┘
```

**Why this split:**
- **Data lake (Iceberg/S3)** stores the raw interaction history (clicks, purchases, ratings). Cheap, queryable, schema-evolving.
- **Spark** runs collaborative filtering or trains an embeddings model on the full interaction matrix. Batch job, runs nightly or hourly.
- **Redis** serves the precomputed results. The ML pipeline writes `user_id → [item_1, item_2, ..., item_20]` as a Redis list or sorted set. The serving path is a single `LRANGE` — sub-millisecond.

**For real-time recommendations** (react to what the user is doing right now): add a streaming layer. Kafka captures live events, Flink or Spark Streaming updates feature vectors in Redis, and the serving path blends batch recommendations with real-time signals.

**The trap:** Querying the ML model or the interaction history at serving time. A recommendation request during a page load cannot afford a 500 ms round trip to Spark or a table scan of the interaction history. **Pre-compute and cache. The serving path is a lookup, never a computation.**

---

### 10. Multi-tenant SaaS

**The workload shape:** Many customers (tenants) sharing infrastructure. Each tenant's data must be isolated. Tenants vary wildly in size — the largest 1% generates 50%+ of load. Query patterns are OLTP (the app) plus lightweight analytics (tenant dashboards). Compliance often requires data residency (EU tenant data stays in EU).

**The stack (three models, pick one):**

```text
   MODEL A: Shared schema, tenant_id column (simplest, most common)
   ┌─────────────────────────────────┐
   │         PostgreSQL              │
   │  ┌─────────────────────────┐   │
   │  │ orders                  │   │
   │  │ ├── tenant_id (indexed) │   │
   │  │ ├── order_id            │   │
   │  │ └── ...                 │   │
   │  └─────────────────────────┘   │
   │  Row-level security (RLS)      │
   │  enforces isolation            │
   └─────────────────────────────────┘

   MODEL B: Schema-per-tenant (moderate isolation)
   ┌─────────────────────────────────┐
   │         PostgreSQL              │
   │  ├── tenant_acme.orders        │
   │  ├── tenant_globex.orders      │
   │  └── tenant_initech.orders     │
   └─────────────────────────────────┘

   MODEL C: Database-per-tenant (maximum isolation, highest ops cost)
   ┌──────────┐  ┌──────────┐  ┌──────────┐
   │ PG: acme │  │PG: globex│  │PG:initech│
   └──────────┘  └──────────┘  └──────────┘
```

**How to choose:**
- **< 1,000 tenants, uniform size:** Model A. Shared tables, `tenant_id` column, Postgres RLS. Simplest ops, simplest migrations, lowest cost.
- **1,000–10,000 tenants, some large:** Model A with caching. Redis caches hot tenant data. ClickHouse for tenant-facing analytics dashboards (pre-aggregated per tenant).
- **Noisy-neighbor risk or compliance isolation:** Model B or C. Schema-per-tenant is a middle ground — one database, separate schemas, `pg_dump` per tenant for portability. Database-per-tenant for regulated industries where auditors want physical separation.
- **Data residency (EU, etc.):** Model C with region-specific database instances. Or Model A with a routing layer that directs queries to the correct regional Postgres.

**The supporting cast:**
- **Redis** — cache per-tenant config, session, rate-limit per-tenant API usage.
- **Kafka** — tenant event stream for async processing (billing events, notifications, audit logs). Partition by tenant_id so per-tenant ordering is preserved.
- **Elasticsearch** — tenant-facing search. Index per tenant (for isolation) or shared index with tenant_id routing (for efficiency).

**The trap:** Starting with database-per-tenant because "isolation" sounds safe. At 5,000 tenants, you have 5,000 databases to migrate, patch, back up, and monitor. Schema migrations become a week-long rolling operation. **Start with Model A (shared schema + RLS). Graduate large or regulated tenants to Model B/C only when forced.**

---

<a id="decision-flowchart"></a>

## Decision flowchart

This is the cheat sheet. Start at the top, follow the arrows. It won't replace thinking, but it will get you to the right neighborhood in under a minute — and "right neighborhood" is 90% of the decision.

### The main flow

```text
                         ┌──────────────────────┐
                         │   What is the primary │
                         │   unit of work?       │
                         └──────────┬────────────┘
                                    │
              ┌─────────────────────┼─────────────────────┐
              ▼                     ▼                     ▼
     ┌────────────────┐   ┌────────────────┐    ┌────────────────┐
     │  Single rows / │   │  Streams of    │    │  Billions of   │
     │  documents     │   │  events        │    │  rows, scanned │
     │  (OLTP)        │   │  (streaming)   │    │  (OLAP)        │
     └───────┬────────┘   └───────┬────────┘    └───────┬────────┘
             │                    │                     │
             ▼                    ▼                     ▼
      SEE: OLTP TREE       SEE: STREAMING TREE    SEE: OLAP TREE
```

---

### OLTP tree — "I read and write individual records"

```text
   Need transactions / joins / constraints?
          │
     ┌────┴─────┐
     ▼          ▼
    YES         NO
     │          │
     ▼          │
 ┌────────┐    │    Write-heavy? (>50K writes/sec sustained)
 │Postgres│    │          │
 │        │    │     ┌────┴─────┐
 └────────┘    │     ▼          ▼
               │    YES         NO
               │     │          │
               │     ▼          ▼
               │  Multi-DC     Need flexible
               │  active-      queries?
               │  active?           │
               │     │         ┌────┴─────┐
               │  ┌──┴──┐      ▼          ▼
               │  ▼     ▼     YES         NO
               │ YES    NO     │          │
               │  │     │      ▼          ▼
               │  ▼     ▼   ┌────────┐ ┌──────────┐
               │ ┌─────────┐│Postgres│ │Cassandra │
               │ │Cassandra││(still) │ │/ ScyllaDB│
               │ │/ Scylla ││        │ │          │
               │ └─────────┘└────────┘ └──────────┘
               │
               │  Data fits in RAM? (<100 GB working set)
               │          │
               │     ┌────┴─────┐
               │     ▼          ▼
               │    YES         NO
               │     │          │
               │     ▼          ▼
               │  ┌────────┐  ┌────────┐
               │  │ Redis  │  │Postgres│
               │  │(as     │  │(it     │
               │  │primary │  │handles │
               │  │ store? │  │more    │
               │  │ only if│  │than you│
               │  │ loss is│  │think)  │
               │  │ OK)    │  │        │
               │  └────────┘  └────────┘
               │
               └──── When in doubt: Postgres.
```

**The Postgres default rule:** If you're asking "should I use Postgres?" the answer is almost always yes. Postgres is the wrong answer only when you have a specific, demonstrated reason it can't handle the workload — and that reason is usually one of: (1) write throughput beyond what a single primary can handle, (2) multi-DC active-active, or (3) the workload is pure key-value at extreme scale. Everything else? Postgres.

---

### Streaming tree — "I process events as they arrive"

```text
   What do you need from the stream?
          │
     ┌────┴──────────────┬──────────────────┐
     ▼                   ▼                  ▼
   Durable log,       Lightweight        Coordination,
   replay, high       pub/sub,           leader election,
   throughput          fire-and-forget    config distribution
     │                   │                  │
     ▼                   ▼                  ▼
  ┌────────┐        ┌────────┐         ┌──────────┐
  │ Kafka  │        │ Redis  │         │ZooKeeper │
  │        │        │Pub/Sub │         │/ etcd    │
  └───┬────┘        │or      │         └──────────┘
      │             │Streams │
      │             └────────┘
      │
      │  Need stream processing (windowed aggregations,
      │  joins, pattern matching)?
      │         │
      │    ┌────┴─────┐
      │    ▼          ▼
      │   YES         NO (just produce/consume)
      │    │          │
      │    ▼          ▼
      │  ┌──────────┐  Kafka consumers
      │  │Flink /   │  (simple, sufficient
      │  │Spark     │   for most cases)
      │  │Streaming │
      │  └──────────┘
      │
      │  Need to store the stream long-term?
      │         │
      │    ┌────┴─────┐
      │    ▼          ▼
      │   YES         NO
      │    │          │
      │    ▼          ▼
      │  Kafka →     Kafka with
      │  S3/Iceberg  retention =
      │  (sink       days/weeks
      │  connector)
      │
      └──── Kafka is the default for durable event streaming.
            Redis is the default for ephemeral messaging.
```

---

### OLAP tree — "I scan billions of rows for aggregates"

```text
   What kind of analytical queries?
          │
     ┌────┴───────────────┬──────────────────┐
     ▼                    ▼                  ▼
   Real-time            Ad-hoc SQL         Time-series
   dashboards,          over a data        metrics
   sub-second           lake (many TB+)    (numeric,
   aggregations                            pre-aggregated)
     │                    │                  │
     ▼                    ▼                  ▼
  ┌────────────┐    ┌────────────┐     ┌────────────┐
  │ ClickHouse │    │ Iceberg +  │     │ Prometheus │
  │            │    │ Trino/Spark│     │ / Mimir    │
  └────────────┘    └────────────┘     └────────────┘
                          │
                          │  Also need full-text search
                          │  over the same data?
                          │         │
                          │    ┌────┴─────┐
                          │    ▼          ▼
                          │   YES         NO
                          │    │          │
                          │    ▼          ▼
                          │  ┌──────────┐  Stay with
                          │  │Elastic-  │  Iceberg +
                          │  │search    │  Trino/Spark
                          │  │(for the  │
                          │  │search    │
                          │  │part)     │
                          │  └──────────┘
                          │
   Data volume?
          │
     ┌────┴─────────────┐
     ▼                  ▼
   < 1 TB              > 1 TB
     │                  │
     ▼                  ▼
  Postgres with       ClickHouse or
  good indexes        Iceberg on S3
  might still         (depending on
  be enough           latency needs)
```

---

### The search spur — "I need to find things by text or complex filters"

```text
   What kind of search?
          │
     ┌────┴───────────────┬──────────────────┐
     ▼                    ▼                  ▼
   Full-text,           Exact filters,     Log search
   fuzzy, autocomplete  facets on          (find errors,
   relevance-ranked     structured fields  grep by keyword)
     │                    │                  │
     ▼                    ▼                  ▼
   Scale?              ┌────────────┐     Budget?
     │                 │Elasticsearch│       │
     │                 │(also good   │  ┌────┴─────┐
  ┌──┴──────┐          │for this)   │  ▼          ▼
  ▼         ▼          └────────────┘ Tight       Flexible
< 10M     > 10M                        │          │
docs      docs                         ▼          ▼
  │         │                     ┌────────┐ ┌────────────┐
  ▼         ▼                     │ Loki   │ │Elasticsearch│
┌────────┐ ┌────────────┐         │(cheap, │ │(powerful,  │
│Postgres│ │Elasticsearch│        │label + │ │expensive)  │
│tsvector│ │/ OpenSearch │        │grep)   │ │            │
│+ GIN   │ │            │        └────────┘ └────────────┘
└────────┘ └────────────┘
```

---

### The caching spur — "I need to make something faster"

```text
   What are you caching?
          │
     ┌────┴───────────────┬──────────────────┐
     ▼                    ▼                  ▼
   Query results,       Computed rankings, Static assets,
   sessions, config,    counters, rate     HTML fragments
   hot rows             limits
     │                    │                  │
     ▼                    ▼                  ▼
  ┌────────┐          ┌────────┐         ┌────────┐
  │ Redis  │          │ Redis  │         │  CDN   │
  │(string/│          │(sorted │         │        │
  │ hash)  │          │ set,   │         └────────┘
  └────────┘          │ INCR)  │
                      └────────┘

   Rule: cache is always in front of a source of truth.
         Redis is the accelerator. Postgres/Kafka is the vault.
         If you can't rebuild the cache from the truth, it's not a cache.
```

---

### Quick-reference decision matrix

For the "I don't want to follow a flowchart, just give me the grid" crowd:

| I need to... | First reach for | Add if needed |
|---|---|---|
| Store relational data with transactions | **Postgres** | Read replicas for read scale |
| Write millions of events/sec, read by key | **Cassandra / ScyllaDB** | Spark/Flink for analytics on top |
| Buffer/decouple services, event sourcing | **Kafka** | Flink/Spark for stream processing |
| Cache hot data, sub-ms reads | **Redis** | Postgres as the source of truth behind it |
| Full-text search, autocomplete, facets | **Elasticsearch** | Postgres as the source of truth behind it |
| Dashboard aggregations, real-time OLAP | **ClickHouse** | Kafka for ingestion |
| Collect and alert on numeric metrics | **Prometheus** | Mimir/Thanos for multi-cluster |
| Store 100 TB+ cheaply, query with SQL | **Iceberg on S3** | Trino or Spark as the query engine |
| Coordinate distributed systems | **ZooKeeper / etcd** | — |
| Build a leaderboard | **Redis sorted sets** | Postgres for persistence |
| Build a queue | **Kafka** (not Cassandra, not Redis) | — |

---

### The one rule that matters more than the flowchart

**Start with Postgres. Add systems when Postgres demonstrably can't handle a specific workload — not when you imagine it might not.** Most applications will never outgrow a well-tuned Postgres instance. The second system you add should solve a problem you've measured, not one you've predicted. Every system you add is a system you operate, monitor, back up, upgrade, and debug at 3 a.m.

---

<a id="anti-pattern-hall-of-fame"></a>

## Anti-pattern hall of fame

The database sections above describe system-specific mistakes. This chapter collects the **cross-cutting** anti-patterns — the ones that transcend any single technology and show up in architecture reviews regardless of stack. Each one has been responsible for outages, cost overruns, or multi-quarter rewrites at real companies. They are ordered roughly by how early in a project they strike.

---

### 1. Résumé-Driven Database Selection

**The pattern:** Choosing a database because the team wants to learn it, because it appeared in a conference talk, or because a FAANG company uses it. The workload is 500 requests per second. The team picks Cassandra because Netflix uses Cassandra.

**Why it hurts:** Every database is a trade-off. The FAANG company chose that database because their workload demanded it — yours almost certainly doesn't. You inherit the operational complexity (ring management, JVM tuning, query-first data modeling) without the workload that justifies it. Six months later the team is debugging compaction storms on a 3-node cluster that Postgres would have handled on a single instance.

**The fix:** Start every database decision with the workload shape, not the technology. Ask: "What is the read/write ratio? What is the query pattern? What happens if this is down for 5 minutes?" If the answer is "it's a web app with relational data and a few hundred QPS," the answer is Postgres. It's always Postgres until it provably isn't.

---

### 2. The Premature Polyglot

**The pattern:** Day-one architecture with six databases: Postgres for users, Cassandra for events, Redis for cache, Elasticsearch for search, Kafka for messaging, ClickHouse for analytics. The team has four engineers.

**Why it hurts:** Each database is a separate ops burden — backups, monitoring, upgrades, security patches, connection pooling, failure modes, on-call runbooks. Four engineers cannot operate six databases well. The cognitive overhead of context-switching between data models (relational, wide-column, key-value, document, columnar) is real. Bugs hide at the seams between systems — stale caches, inconsistent denormalized copies, CDC pipelines that silently lag.

**The fix:** One database until it hurts. Two databases when you can name the specific pain. Three is a mature architecture. Six on day one is a distributed systems PhD program disguised as a startup.

---

### 3. Treating the Cache as the Source of Truth

**The pattern:** Redis holds the canonical copy of the data. There is no rebuild path. "We'll add persistence later."

**Why it hurts:** Redis restarts. Nodes fail. Memory fills. Eviction policies kick in. When the cache disappears, so does the data. "We'll add persistence" never happens because the system works fine — until it doesn't, and there is no recovery path. The team discovers at 2 a.m. that `maxmemory-policy allkeys-lru` has been silently evicting data for weeks.

**The fix:** Every cache must be rebuildable from a source of truth. If you cannot run `FLUSHALL` and have the system recover (slowly, but correctly), your cache is a database and you don't have backups.

**Corollary: the phantom cache.** A cache without TTLs is not a cache — it's an unbounded memory allocation. TTL everything. The default TTL should be short (minutes to hours). Long TTLs are earned by proving the data doesn't change.

---

### 4. The Elasticsearch-as-Database Trap

**The pattern:** Elasticsearch is the only place the product catalog / user profiles / order history lives. No Postgres, no S3, no rebuild path.

**Why it hurts:** Elasticsearch is an index, not a database. It has no transactions, no referential integrity, no real update (every update is delete + re-index). Cluster corruption, split-brain, or a bad mapping change requires reindexing — from what? If the source doesn't exist, the data is gone. Elastic's own documentation says "do not use Elasticsearch as a primary data store."

**The fix:** The source of truth is Postgres (or S3, or Kafka). Elasticsearch is the index. Always maintain a pipeline that can rebuild the index from the source. Test the rebuild regularly — an untested rebuild path is the same as no rebuild path.

---

### 5. The Log Store Cost Bomb

**The pattern:** "We'll put all our logs in Elasticsearch because Kibana looks nice." Three months later, the Elasticsearch cluster is the most expensive line item in the AWS bill. Six months later, it costs more than the application infrastructure it's monitoring.

**Why it hurts:** Elasticsearch stores data in RAM-backed inverted indexes on SSDs. Per-GB cost is 5–20× that of object storage. Most log data is never searched — it's written and then it ages out. You're paying the full-text indexing tax for data nobody will ever full-text search.

**The fix:**
- **Ask: "Do we need to search this, or just grep it?"** If grep is enough, Loki on S3 is 10–20× cheaper.
- **Tier your logs.** Hot tier (Elasticsearch, last 24–48 hours) for active debugging. Warm tier (ClickHouse or Loki, last 30 days) for investigation. Cold tier (S3/Parquet, months to years) for compliance.
- **Stop indexing fields nobody queries.** `dynamic: false` in the mapping. Index the 10 fields you filter on; store the rest as a raw JSON blob.

---

### 6. The Unbounded Query

**The pattern:** `SELECT * FROM events` with no `LIMIT`, no `WHERE`, no pagination. Or the Cassandra equivalent: `SELECT * FROM events_by_user` where one user has 50 million rows. Or the Elasticsearch equivalent: `from: 100000, size: 100`.

**Why it hurts:** The database faithfully does what you asked — reads millions of rows, serializes them, ships them over the network. The coordinator node runs out of memory. The connection pool is exhausted. Other queries time out. One bad query takes down the whole system.

**The fix:**
- **Every query has a `LIMIT`.** No exceptions. Default pagination at the API layer.
- **In Cassandra:** bound your partition size. If a partition can grow unbounded, add a bucketing dimension to the partition key.
- **In Elasticsearch:** use `search_after` for deep pagination, never `from` + `size` beyond 10K.
- **In Postgres:** statement timeouts (`statement_timeout = '30s'`) as a safety net. `pg_stat_statements` to find the offenders before they page you.

---

### 7. Schema? What Schema?

**The pattern:** "We'll use a schemaless database so we don't have to think about the schema." Data flows in as arbitrary JSON. Fields appear and disappear. Types drift (`age` is sometimes a string, sometimes an integer). Three months later, every consumer is a maze of null checks and type coercions.

**Why it hurts:** "Schemaless" doesn't mean "no schema" — it means "the schema is implicit, undocumented, and enforced by the application code instead of the database." Every service that reads this data carries its own copy of the schema assumptions, and they drift. The mapping explosion in Elasticsearch. The deserialization failures in Spark. The silent data corruption when a field changes type.

**The fix:** Define your schema explicitly, always, regardless of database:
- **Elasticsearch:** `dynamic: strict` from day one.
- **Kafka:** Schema Registry with Avro or Protobuf. Reject messages that don't conform.
- **MongoDB / document stores:** JSON Schema validation at the collection level.
- **Data lakes:** Iceberg / Delta Lake with enforced schemas and schema evolution rules.

The database doesn't have to enforce the schema (though it should). But the schema must exist, be documented, and be versioned.

---

### 8. The DIY Queue on the Wrong Database

**The pattern:** Building a job queue by polling a Postgres table (`SELECT * FROM jobs WHERE status = 'pending' ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED`). Or building a queue on Cassandra (write row, read row, delete row). Or building a queue on Redis lists without acknowledging the durability trade-off.

**Why it hurts:**
- **Postgres queue:** Works surprisingly well up to ~1,000 jobs/sec. Beyond that, the polling, row locking, and index bloat from constant insert/update/delete cycles degrade the table and compete with your OLTP workload. The `jobs` table becomes the hottest table in the database.
- **Cassandra queue:** Tombstone storm. Every dequeued (deleted) message leaves a tombstone. Reads slow to a crawl. This is the #1 Cassandra anti-pattern.
- **Redis list queue:** Fast, but if Redis restarts, in-flight jobs disappear. `BRPOPLPUSH` with a processing list helps, but at-least-once delivery requires application-level retry logic.

**The fix:** Use a purpose-built system:
- **< 1,000 jobs/sec, already have Postgres:** `SKIP LOCKED` is fine. Accept the trade-off. Isolate the queue in its own table, vacuum aggressively.
- **Durable, high-throughput, ordered:** Kafka. Consumer groups give you exactly-once semantics with offset management.
- **Task queue with retry, scheduling, priorities:** SQS, RabbitMQ, or Celery (which uses Redis/RabbitMQ as a broker but handles the hard parts).

---

### 9. The Cross-Database Join

**The pattern:** User data lives in Postgres. Activity data lives in Cassandra. The product manager asks for a report: "Show me all users who signed up last month and their activity count." The engineer writes code that fetches users from Postgres, then loops over each user querying Cassandra one by one. N+1 queries across two databases over the network.

**Why it hurts:** This is O(N) network round trips to a database that doesn't support the query natively. Latency is dominated by network hops. At 100K users, it takes minutes. At 1M users, it doesn't finish. The "join" logic lives in application code that is fragile, slow, and untested at scale.

**The fix:**
- **If you need to join, the data should be in the same system.** Denormalize into one database, or CDC both sources into a data lake (Iceberg) and join there with Spark or Trino.
- **If the join is for analytics:** this is an OLAP question. Export both datasets to ClickHouse or the data lake. Don't make the OLTP systems do OLAP work.
- **If the join is for real-time serving:** pre-compute and materialize. A Kafka Streams or Flink job that joins the two streams and writes the result to a serving store (Redis, Postgres).

---

### 10. Ignoring the Operational Cost

**The pattern:** The architecture review evaluates databases on features, performance benchmarks, and license cost. Nobody asks: "Who operates this at 3 a.m.? What does the upgrade path look like? How do we back this up? What happens when a node dies?"

**Why it hurts:** The total cost of a database is 20% license/infra and 80% operations. A "free" open-source database that requires a dedicated DBA costs more than a managed service. A self-hosted Elasticsearch cluster that pages the on-call engineer weekly is more expensive than Elastic Cloud, even at 3× the infrastructure cost. The cheapest database is the one your team can operate without heroics.

**The fix:** For every database in the architecture, answer these questions before committing:
- Who is on call for this system?
- What does failover look like? Is it automatic or does someone wake up?
- How long does a version upgrade take? Does it require downtime?
- How do you back up and restore? Have you tested the restore?
- What happens at 2× current load? 10×?
- Can you hire people who know this system?

If the answer to most of these is "we'll figure it out," use the managed service or pick a simpler system.

---

### 11. The Consistency Mismatch

**The pattern:** The application assumes strong consistency but the database provides eventual consistency. Or: the application doesn't need strong consistency but pays the latency cost for it anyway.

**Why it hurts:**
- **Assuming strong on an eventually consistent system:** A user updates their profile in Cassandra with `CL=ONE`, immediately reads it back from a different replica, gets the old value, and sees a "lost update." The bug is intermittent, hard to reproduce, and looks like a ghost. Or: an inventory decrement in a last-write-wins system, where two concurrent decrements both read "5", both write "4", and you've just sold an item you don't have.
- **Paying for strong when you don't need it:** Using `CL=ALL` in Cassandra for a display-name update that could tolerate 2 seconds of staleness. One slow replica and the write fails. Availability sacrificed for consistency that nobody needed.

**The fix:** Map every data path to its consistency requirement:
- **Financial, inventory, authentication:** strong consistency. Use Postgres, or Cassandra with LWTs (and accept the cost).
- **Display data, recommendations, activity feeds:** eventual consistency is fine. Use `LOCAL_QUORUM` or even `ONE` in Cassandra. Cache in Redis with a short TTL.
- **Mixed:** different tables or different services with different consistency levels. The payment service uses Postgres. The activity feed uses Cassandra. They are different systems because they have different requirements.

---

### 12. The Backup That Was Never Tested

**The pattern:** Backups run nightly. They've run for two years. Nobody has ever restored one. The disaster strikes. The backup is corrupt, incomplete, or restores to the wrong schema version. Or: the backup is fine, but the restore takes 48 hours, and the business expected 4.

**Why it hurts:** A backup you haven't restored is a hypothesis. At the moment you need it most — data loss, corruption, ransomware — you discover the hypothesis was wrong. This is not a database-specific problem. It applies to Postgres `pg_dump`, Cassandra snapshots, Elasticsearch snapshots, Redis RDB files, and every other system in this doc.

**The fix:**
- **Test restores quarterly.** Restore to a staging environment. Verify the data is correct and complete. Time the restore.
- **RTO/RPO are not aspirations, they are SLAs.** Recovery Time Objective (how long) and Recovery Point Objective (how much data loss). Measure them. If the RTO is 1 hour but the restore takes 12, you have a problem today, not the day of the disaster.
- **Backups are not disaster recovery.** A backup on the same disk as the database is not a backup. A backup in the same region as the database is not a disaster recovery plan. Geo-replicate.

---

### The meta-lesson

Every anti-pattern in this list has the same root cause: **optimizing for the easy thing (features, performance, novelty) and ignoring the hard thing (operations, failure modes, consistency, cost at scale).** The hard things don't show up in the demo. They show up at 3 a.m. on a Saturday, when the system is down, the runbook doesn't exist, and the person who chose the database left the company two years ago.

The best database decision is the boring one that nobody has to think about again.

---

*End of the Database Playbook.*
