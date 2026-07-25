# URL Audit Service — Architecture at Scale

**Scope:** redesign of the URL-audit service to handle 10,000 audits/day sustained, bursts of 500 concurrent requests, and a customer-facing response-time SLA.

---

## 1. Requirements recap

| Requirement | Implication |
|---|---|
| 10,000 audits/day | ~0.12 req/s average — trivial in isolation |
| Bursts of 500 concurrent | The real driver of the design; average load is irrelevant, peak concurrency is what breaks a naive synchronous service |
| Customer-facing SLA | The API must respond fast even when the underlying audit work (fetching a URL, running checks) is slow or fails — so the request/response path and the audit-execution path must be decoupled |

The core design decision falls out of these three lines: **don't do the audit work inline with the HTTP request.** Accept the request fast, do the work asynchronously, and let the client either poll or get pushed a result.

---

## 2. Components

- **API gateway / load balancer** — TLS termination, routes to healthy API pods, absorbs connection-level bursts.
- **FastAPI service tier** — stateless, horizontally autoscaled pods. Validates input, checks the cache, enqueues jobs, and exposes a status/result endpoint. Holds no in-memory state so any pod can serve any request.
- **Redis cache** — stores completed audit results keyed by normalized URL, with a configurable TTL window. This is what makes repeat audits cheap and is the first thing checked on every request.
- **Redis-backed task queue** — durable queue for audit jobs that miss the cache. Redis is reused here (streams or a list-based queue) rather than adding a second broker, since at this volume its throughput and persistence guarantees are more than sufficient.
- **Worker pool** — a separate deployment (not the API pods) that pulls jobs off the queue, performs the actual URL fetch and checks (with timeouts), and writes the result to both Postgres and the cache. Scaled independently from the API tier since audit work (network-bound, variable latency) has a completely different scaling profile than request handling (CPU-light, fast).
- **PostgreSQL** — system of record for job status (queued/running/done/failed) and full audit results. Survives restarts, cache evictions, and Redis failover; this is what the API tier reads if the cache misses on a status check.

## 3. Data flow

1. Client submits an audit request to the API tier via the gateway.
2. The API pod validates the URL, normalizes it, and checks Redis cache.
   - **Cache hit** (audited recently, within the configurable window): return the cached result immediately. This is the fast path and the main lever for meeting the SLA under load, since repeat audits never touch the queue or a worker.
   - **Cache miss**: the API pod writes a `queued` job record to Postgres, pushes a job onto the Redis queue, and returns a `202 Accepted` with a job ID and a status-poll URL. The SLA is satisfied by responding fast to the *enqueue*, not by waiting for the *audit*.
3. A worker picks up the job, marks it `running` in Postgres, executes the audit (fetch + checks, bounded by a timeout), and writes the result to Postgres and Redis on completion (or `failed` with an error reason).
4. The client polls the status endpoint (or receives a webhook, if offered) until the job is `done`, at which point the API tier serves the result — from cache if still warm, otherwise from Postgres.

## 4. Queueing strategy

- One logical queue, partitioned by priority if needed later (not required at this volume), consumed by a worker pool sized to keep queue depth near zero under normal load and to drain a burst within a bounded time.
- **Backpressure, not unbounded queueing**: if queue depth crosses a threshold (meaning workers can't keep up), the API tier stops accepting new audit requests with a `503` and a `Retry-After` header rather than growing the queue indefinitely and letting client-perceived latency blow past the SLA. A slow, honest failure is better than a queue that silently makes every wait time worse.
- At 500 concurrent bursts with a worker pool sized for, say, 50-100 concurrent audits (network-bound, so workers can run many concurrent fetches each), the queue absorbs the burst and drains over seconds, not minutes — this is the number to tune based on real audit latency.

## 5. Where state lives

| State | Location | Why |
|---|---|---|
| Completed audit results (recent) | Redis cache | Fast reads, TTL-bound, disposable |
| Job status + full result history | PostgreSQL | Durable, queryable, survives cache eviction and restarts |
| In-flight job queue | Redis (queue) | Fast enqueue/dequeue; jobs are also mirrored as `queued` rows in Postgres so nothing is lost if Redis restarts |
| Request-scoped state (rate limit counters, etc.) | Redis | Shared across all API pods so limits are enforced service-wide, not per-pod |

No state lives in the API pods themselves — that's what makes them safe to autoscale and recycle freely.

---

## 6. Technology decision record

| Decision | Choice | Rejected alternative | Why rejected |
|---|---|---|---|
| API framework | FastAPI (Python, async) | Flask (sync) | Flask's synchronous request model would tie up a worker thread per in-flight request; FastAPI's async I/O lets one process handle many concurrent slow operations (cache/queue calls) without blocking, which matters directly for the 500-concurrent burst requirement |
| Cache + queue backend | Redis (single service, two uses) | Separate broker (RabbitMQ/SQS) for the queue | At 10K/day + 500-burst scale, Redis's throughput is not the bottleneck, and running one fewer stateful service reduces operational surface area. This would be revisited if job volume grew by orders of magnitude or if jobs needed complex routing/retry topologies that Redis lists don't naturally express |
| Async execution | Custom worker pool consuming Redis | Celery | Celery is a reasonable alternative and would be picked if the team already operates it; a lighter custom consumer was chosen here to avoid Celery's operational overhead (separate result backend config, broker connection quirks) for a queue this simple. This is the closer call of the two — Celery becomes the better choice once retries, scheduling, or chained tasks get complex |
| Durable store | PostgreSQL | MongoDB | The data (job records, status enums, structured audit results) is relational and benefits from transactional writes when marking a job's status; there's no schema-flexibility requirement that would justify a document store here |
| Deployment scaling | Independent autoscaling for API tier vs. worker tier | Single scaling group for both | API pods and workers have different load shapes (API scales with request rate, workers scale with audit-execution time) — coupling them would either starve one or over-provision the other |

## 7. Failure modes and mitigations

1. **Redis unavailability (cache and/or queue).**
   Impact: cache misses go to Postgres directly (slower but correct); queue writes fail, which would otherwise silently drop jobs.
   Mitigation: the API pod writes the `queued` job row to Postgres *before* attempting to push to Redis, and treats a Redis-push failure as retryable rather than fatal — a background reconciler periodically re-enqueues any Postgres-recorded `queued` jobs that never appeared in the queue. Redis itself runs with replication and automatic failover so a single node loss doesn't lose the cache.

2. **Downstream URL targets are slow or hanging** (the sites being audited, not the audit service itself).
   Impact: a worker occupied by a hung fetch stops making progress, and enough hung fetches exhaust worker concurrency, causing queue depth to climb and the client-visible SLA to slip even though the audit service itself is healthy.
   Mitigation: hard per-audit fetch timeouts (connect + read) enforced at the HTTP client level, with the job marked `failed: timeout` rather than left `running` indefinitely. Timeouts are tuned well below the client-facing SLA so a single slow target can never itself become the reason the SLA is missed.

3. **Traffic burst exceeds worker throughput** (the 500-concurrent scenario itself, sustained rather than momentary).
   Impact: queue depth grows unbounded, and by the time a job is processed the result is stale relative to when the client actually needed it.
   Mitigation: worker pool autoscaling keyed off queue depth (not CPU, since workers are I/O-bound), combined with the backpressure behavior in §4 — once queue depth crosses a safety threshold, new requests are rejected with `503 + Retry-After` rather than accepted and left to rot in the queue. This keeps the failure mode "some requests are told to retry" rather than "all requests silently wait past the SLA."

## 8. Observability and rollback

**What to monitor:**
- **Request-path SLA metrics**: p50/p95/p99 latency for the enqueue response (not the full audit), and separately for cache-hit responses — these are the two numbers the customer-facing SLA is actually made of.
- **Queue depth and age of oldest job** — the leading indicator that the worker tier is falling behind, well before customers notice.
- **Worker throughput and failure rate** (per failure reason: timeout, non-2xx target, DNS failure, etc.) — distinguishes "our service is broken" from "the internet is slow."
- **Cache hit ratio** — a silent drop here means load on Postgres and workers is about to spike even if nothing else has changed.
- **Error rates by endpoint and status code**, tagged with the request ID for tracing.

**What to alert on:**
- p95 enqueue latency breaching the SLA threshold for a sustained window (not a single spike).
- Queue depth or oldest-job-age crossing a threshold that predicts SLA breach if it isn't addressed within minutes.
- Worker failure rate crossing a threshold that suggests a systemic issue (e.g., DNS resolution failing broadly) rather than isolated bad target URLs.
- Redis or Postgres unavailability.

Every request carries a request ID generated at the gateway, propagated through structured logs at every hop (API pod, queue push, worker execution) so a single audit's full lifecycle can be reconstructed from logs alone.

**Rollback:**
- Deploys are versioned and rolled out with canary/rolling deployment (a small percentage of API pods on the new version first), watching the SLA and error-rate metrics above during the canary window before completing the rollout.
- Given the API tier is stateless, rollback is a redeploy of the previous image version — no data migration to reverse in the common case.
- Worker deployments roll back independently from the API tier; since job records and their status live in Postgres, a worker-version rollback simply resumes processing the queue from wherever it was, with no job loss.
- If a deploy introduces a state schema change (rare, but possible for the job/result tables), migrations are written to be backward-compatible for at least one release (additive columns, no destructive changes) so a rollback of the application code doesn't require an accompanying reverse migration.

---

## Deliverables checklist
- [x] Architecture document with diagram (§1–§5, diagram above)
- [x] Technology decision record (§6)
- [x] Failure mode analysis (§7)
- [x] Observability and rollback plan (§8)
