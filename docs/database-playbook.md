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
6. Kafka *(coming next)*
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

*Next up: Kafka.*
