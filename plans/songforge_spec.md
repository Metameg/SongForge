# SongForge — System Redesign Specification

> Status: `ready-for-agent`
> Source: grill-me decisions #1–#18 (see `.../memory/redesign-*.md`) + `plans/system_redesig_plan.md`.
> **Kafka is intentionally absent** — dropped in decision #7; the original plan diagram is superseded.

## Problem Statement

A listener wants to open a URL and immediately hear a live, globally-synchronized internet radio
station — the same song at the same position as everyone else — and wants to contribute by typing a
prompt that generates a new song which then plays on the air. Today's implementation (a Flask +
Socket.IO monolith) couples the whole system to sticky WebSocket sessions and eventlet, keeps the
authoritative playback queue in Redis (so a Redis flush loses queued songs), and leans on a
message-broker mental model (Kafka) whose capabilities are dormant given that only one generation may
run at a time. It cannot scale listeners independently of its datastores, cannot survive a coordinator
crash cleanly, and cannot be developed or load-tested without spending real money on the generation
API. The creator also has no guarantee their song survives — audio is referenced from a third party
whose URLs expire.

## Solution

A greenfield rebuild that keeps the *product* (a synchronized, prompt-fed global radio with a static
downtime library) and replaces the *plumbing* with patterns that are individually correct and
internally consistent — this is a portfolio/learning exercise where **demonstrating each distributed-
systems pattern correctly is the spec**.

From the user's perspective:

- **Listeners** connect over plain HTTP + Server-Sent Events (SSE) and hear the exact same song,
  synchronized to a shared server timeline, computed on the client with no per-listener datastore
  load. Playback survives datastore hiccups and coordinator crashes; a listener who reconnects or
  drifts is silently re-synced.
- **Creators** (anonymous or authenticated) submit a prompt; the song is generated, ingested into
  storage the platform owns (so it plays forever and lives in their library), and prioritized onto the
  air. If generation fails, their daily quota is refunded and they are told to retry.
- **The station** never goes silent: user songs take priority, a curated static library fills every
  gap, and a freshly-finished song may cut into a static filler (with a crossfade) but never
  interrupts another user's song.
- **Operators/developers** can run the entire system locally with a fault-injectable fake generation
  API (no cost, deterministic failure testing), and can prove the capacity model with a load test that
  demonstrates the *shape* (flat datastore load vs. listener count, per-instance connection ceiling)
  rather than renting a million connections.

## User Stories

### Listening & global synchronization

1. As an anonymous listener, I want to open the site and immediately hear whatever is currently
   playing, so that the station feels live like a real radio.
2. As a listener, I want to hear the same song at the same position as every other listener, so that
   the experience is a shared broadcast rather than a personal playlist.
3. As a listener, I want the audio to start at the correct offset into the current song when I join
   mid-track, so that I'm synchronized the moment I arrive.
4. As a listener on a device whose clock is slightly wrong, I want my playback position corrected to
   the server's timeline, so that clock drift doesn't desync me.
5. As a listener, I want pressing "play" (required because browsers block autoplay) to compute my
   position locally, so that starting playback costs no server or datastore round-trip.
6. As a listener, I want the next song preloaded before the current one ends, so that transitions are
   gapless.
7. As a listener, I want to be told the instant the song changes, so that I move to the new track
   together with everyone else.
8. As a listener whose network briefly stalls, I want to be caught up to the live position when I
   recover (a small skip), so that I don't fall permanently behind and hear songs truncated at the end.
9. As a listener, I want small timing drift (< ~1s) ignored, so that I'm not constantly nudged for
   imperceptible differences.
10. As a listener who never disconnects but missed a change notification, I want a periodic re-broadcast
    to correct me within ~30s, so that a dropped message doesn't strand me on the wrong song.
11. As a listener whose current audio file ends without a next-song instruction, I want the client to
    immediately re-fetch the current state, so that I'm not left in silence waiting for the next tick.
12. As a listener, I want to keep hearing the current song even if a backend coordinator crashes, so
    that a server failure is invisible to me mid-track.

### Creating songs

13. As an anonymous user, I want to submit a text prompt to generate a song, so that I can contribute
    to the station without making an account.
14. As a creator, I want my finished song prioritized onto the air ahead of the static filler, so that
    my contribution actually gets heard.
15. As a creator whose song finishes while a *static* song is playing, I want it to cut in (with a
    fade), so that I hear my song promptly.
16. As a creator, I want my song to wait for the next boundary if another *user's* song is currently
    playing, so that no one's creative work is ever chopped off.
17. As a creator, I want a visible indication of how many songs I have left today, so that I understand
    my remaining quota.
18. As a creator whose generation fails, I want my daily quota slot refunded and to be notified, so
    that a failure doesn't cost me one of my limited attempts.
19. As a creator whose generation fails, I want to be able to resubmit, so that I can try again after a
    transient or content failure.
20. As a creator, I want my generated song stored permanently, so that I can always access songs I made.
21. As an authenticated creator, I want a higher daily creation limit than anonymous users, so that an
    account is worth making.
22. As an authenticated user, I want a personal library/history of songs I created and favorited, so
    that I can revisit them.

### Identity, rate limiting & abuse

23. As a listener, I want to use the station with no account, so that there is zero friction to listen.
24. As an anonymous user, I want a stable identity via a signed cookie, so that my daily limits and
    (limited) session continuity work without login.
25. As a product owner, I want anonymous creation capped per cookie and (more loosely) per IP, so that
    casual over-use is deterred without hard-walling shared networks.
26. As a product owner, I want authenticated users exempt from the per-IP creation cap and governed by
    their own account limit, so that the IP cap funnels people to sign up rather than blocking NAT'd
    users.
27. As a product owner, I want a bot check on the create action, so that scripted farming is deterred.
28. As a product owner, I want every limit to be config-driven from a single source, so that I can tune
    them via environment without a redeploy and without config drift.
29. As a product owner, I want a single boolean to disable enforcement in development, so that dev
    matches prod by default and "goes unlimited" only deliberately.
30. As a product owner, I want a per-user in-flight concurrency cap, so that one user cannot occupy all
    generation slots at once.

### Generation pipeline

31. As the system, I want a submitted prompt persisted as a job row before anything else, so that the
    job is always recoverable even before an external handle exists.
32. As the system, I want jobs claimed from a durable Postgres queue (not a broker), so that no queued
    work is lost on a cache flush.
33. As the system, I want concurrent generations kept under the API's limit by a best-effort semaphore
    with the API's own 429 as the authoritative backstop, so that I never exceed the generation API's
    concurrency cap (and never overspend even if the semaphore drifts).
34. As the system, I want to be woken immediately when new work arrives or a slot frees (not poll
    constantly), so that dispatch is prompt without wasteful querying.
35. As the system, I want the generation-completion webhook handler to be thin and return fast, so that
    the external API does not time out and retry.
36. As the system, I want the finished audio ingested asynchronously and robustly, so that a slow
    download never blocks the webhook.
37. As the system, I want to recover a completion even if the webhook never arrives, by polling the
    external API by handle, so that a lost webhook doesn't strand a job.
38. As the system, I want a bounded, delayed re-call for a job that may have been charged but never
    persisted a handle, so that the rare double-charge is deferred and infrequent, not immediate.
39. As the system, I want duplicate/late webhooks handled idempotently, so that a song is not enqueued
    twice.
40. As the system, I want all generation timing, retry, and crash recovery owned by a single periodic
    watchdog, so that recovery logic lives in one place.
41. As the system, I want a crashed worker's un-finished jobs automatically re-claimed or swept, so that
    a worker crash self-heals.

### Object storage & audio

42. As the system, I want to ingest the generated audio into storage I own rather than hot-linking the
    generation API's URL, so that songs survive the provider's URL expiry and remain replayable.
43. As the system, I want to refresh an expired download URL via the provider's by-handle lookup, so
    that a fast-expiring URL never costs me the file.
44. As a listener, I want audio served from a CDN edge, so that streaming is fast and the origin is not
    hammered by a million streams.
45. As the system, I want audio objects immutable and long-cached, so that a song re-entering rotation
    is already warm at the edge.
46. As the system, I want one canonical version stored per generation (of the two the API returns), so
    that storage and playback stay simple.
47. As a creator, I want my audio retained indefinitely (storage is negligible next to generation
    cost), so that nothing I made is ever deleted to save pennies.
48. As the system, I want generated and static audio to be indistinguishable playable objects, so that
    the radio treats all songs uniformly.

### Radio coordination, failover & sources

49. As the system, I want exactly one elected coordinator to own the "advance the song" timer, so that
    the station advances exactly once per boundary even with multiple workers.
50. As the system, I want leadership acquired and released automatically via a database lock, so that a
    coordinator crash triggers failover with no heartbeat tuning.
51. As the system, I want a paused/stalled leader to keep leadership (not trigger a false failover), so
    that a GC pause is a late advance, never a split-brain.
52. As the system, I want the advance to be an idempotent conditional write, so that even a momentary
    two-leader overlap cannot double-advance the station.
53. As the system, I want a newly-elected leader to reconstruct the correct current state from the
    database, so that failover resumes cleanly regardless of how long the gap was.
54. As the system, I want catch-up to use live-radio semantics (advance one, start fresh), so that a
    long outage doesn't try to replay songs nobody heard.
55. As the system, I want the station to never be silent — user queue first, else static library — so
    that there is always something on the air.
56. As the system, I want static song selection centralized in the leader with a recent-history window,
    so that shuffle doesn't feel like a short loop and no two instances disagree.
57. As the system, I want a guaranteed fallback (replay the current song) if static selection somehow
    returns nothing, so that silence is impossible.
58. As the system, I want song changes broadcast to every app instance so each relays to its own
    listeners, so that fan-out works across many processes.
59. As the system, I want the station to start eagerly at boot and play to an empty room, so that it is
    an always-on live broadcast, not something a listener has to start.
60. As the system, I want first boot (no pointer yet) handled as the "initialize" branch of normal
    leader startup, so that cold start is not a special subsystem.

### Fairness (v1)

61. As a listener, I want the queue to be first-come-first-served, so that the station behaves like an
    intuitive jukebox.
62. As a product owner, I want fairness bounded by rate limits (not a complex reordering), so that v1
    stays simple while a heavy submitter can still only queue a few songs.
63. As a product owner, I want a documented trigger (large asymmetric caps at thousands of users) after
    which I'd add a per-user in-queue cap, so that the scaling path is known but not prematurely built.

### Scaling & performance (observable behavior)

64. As an operator, I want listener count decoupled from datastore load, so that a million listeners is
    one state read fanned out, not a million reads.
65. As an operator, I want each app instance to serve listeners from a process-local cache invalidated
    by a pub/sub event, so that connect/play never hit the datastores.
66. As an operator, I want the app tier to scale horizontally behind a load balancer, so that I add
    capacity by adding stateless instances.
67. As an operator, I want the write path to sustain the target create-rate at low datastore
    utilization, so that growth is comfortable.
68. As an operator, I want a restart burst (cold instances warming) absorbed by a Redis read, so that a
    thundering herd never lands on Postgres.

### Development, deployment & operations

69. As a developer, I want a fault-injectable fake generation API, so that I can build and test the
    whole pipeline — including every failure path — for free and deterministically.
70. As a developer, I want the fake API to be contract-faithful and swapped in by config (base URL),
    so that code developed against it runs unchanged against the real API.
71. As a developer, I want the fake API's webhook delay configurable (default ~5s), so that I exercise
    the waiting/overdue window, with an instant mode for fast UI iteration.
72. As a developer, I want local infra (Postgres, Redis, object storage, fake API) in one
    docker-compose, so that I can run everything with one command.
73. As a developer, I want object storage abstracted behind the S3 API (MinIO locally, R2 in prod), so
    that dev and prod differ only by endpoint config.
74. As an operator, I want the worker to use a direct database connection for its lock and
    notifications, so that a transaction pooler doesn't silently break leadership.
75. As an operator, I want the row-processing worker loops to run on every worker instance (serialized
    by row-level claim) and only the time-driven coordinator to be single-leader, so that throughput
    scales while the station advances exactly once.
76. As an operator, I want a boot sequence that migrates the schema and seeds the static library before
    starting, so that first boot always has content to play.

### Observability & load testing

77. As an operator, I want the app to always expose a metrics endpoint plus structured logs with a
    correlation ID, so that a single song's journey (submit → webhook → ingest → play) is traceable.
78. As an operator, I want metrics organized by load driver (create-rate, generation-rate, listener),
    so that I can reason about what scales with what.
79. As an operator, I want a load test that measures the per-instance connection ceiling, so that I can
    extrapolate instance count for a target audience.
80. As an operator, I want a graph of datastore ops vs. connection count that stays flat, so that I can
    visually prove listener load is decoupled from the datastores.
81. As an operator, I want to drive the create/generation path at target rate via the fake API, so that
    I can prove write-path headroom without real cost.
82. As an operator, I want a live pipeline view (jobs by state, semaphore utilization, failure/refund
    counts), so that I can see the system working end-to-end and injected faults recovering.
83. As an operator, I want continuous prod metrics via a managed free tier (not a self-hosted 24/7
    stack), so that prod is monitored cheaply while the heavy stack runs on-demand in staging.

### Generation-API rate limiting (429)

84. As a creator, when the generation service is momentarily at capacity, I want to see a graceful
    "high demand — your song is queued and will generate shortly" status rather than an error, so that
    a transient limit never looks like a failure.
85. As the system, I want a 429 from the generation API treated as a no-charge, retriable rejection —
    release the slot, reconcile the semaphore from Postgres, requeue with backoff — so that no song is
    lost and I never overspend even if the semaphore drifts.
86. As an operator, I want 429s surfaced as a metric/alert, so that I can detect semaphore drift or a
    misconfigured concurrency limit.

## Implementation Decisions

### Overall shape (#1–#3)

- **Greenfield repository.** Backend on **FastAPI** (async, native SSE, no eventlet). A **separate
  Python worker** process. **Next.js** frontend. Port domain logic from the current Flask app
  (generation API client, job lifecycle rules, rate rules, simulator harness); discard the transport
  (Socket.IO/eventlet) and Redis-authoritative-queue storage layers.
- **Transport is SSE, not WebSockets.** All client→server traffic is discrete HTTP; only the server
  needs unprompted push (webhook completion, radio advance). This removes sticky sessions and eventlet.
- **No Kafka** (#7). Generation dispatch is a Postgres queue claimed via `FOR UPDATE SKIP LOCKED`
  plus `LISTEN/NOTIFY` wakeups and a Redis semaphore. Revisit only on a genuine multi-consumer
  event-fan-out need.

### Datastores & ownership

- **Postgres = source of truth**: users, songs (catalog + metadata + object key), generation jobs and
  their state machine, the authoritative playback queue/order, playback history, external generation
  IDs, and the single `radio_state` pointer row. **Postgres also holds the leader lock and the
  playback pointer of record.**
- **Redis = derived/ephemeral**: the atomic generation **semaphore** (global-n + per-user in-flight
  counters), rate-limit counters, the hot-head of the upcoming queue, a **capped "recently played"
  list** (serves shuffle anti-repeat + the recently-played ticker), the **cached playback pointer**
  (cold-start/herd shock absorber), and **pub/sub** channels for fan-out. Redis is always rebuildable
  from Postgres; on divergence Postgres wins.
- **Cloudflare R2** (S3-API) stores audio; Postgres stores the object key. MinIO stands in locally via
  the same S3 client.

### Generation job pipeline (#5, #7, #13, #18)

- **Job model:** internal PK is our own UUID `job_id` (the row we can always find). External handles
  (`task_id`, `conversion_id_1/2`) are nullable columns filled after submit; one conversion is
  canonical. State machine, authoritative in Postgres:

  ```
  QUEUED → SUBMITTING → WAITING_FOR_WEBHOOK → INGEST_PENDING → READY
                     ↘──────────────── FAILED ───────────────↙
  ```

- **Dispatch:** worker claims the next `QUEUED` job (`SELECT … FOR UPDATE SKIP LOCKED LIMIT 1`,
  FIFO by a monotonic `seq`), acquires the Redis semaphore (global-n + per-user), transitions to
  `SUBMITTING`, POSTs the generation API, stores handles, transitions to `WAITING_FOR_WEBHOOK`.
  Wakeups via `LISTEN/NOTIFY` on new-job and on semaphore-release; a slow poll is only a backstop.
- **Generation-API rate limiting (429) — graceful (decision #18):** the generation API is the
  *authoritative* concurrency cap and returns **429** on excess (1 concurrent base / 5 upgraded). A 429
  is a clean **rejection — no charge, no handle, fully retriable** — and can occur even with a correct
  semaphore (timing gap between our completion signal and the API's internal slot release), so it is
  handled as normal operation, not an edge case. On 429 the dispatcher **releases the optimistic slot,
  reconciles the semaphore from Postgres truth (count of active-state rows), requeues the job to
  `QUEUED` with backoff, and does NOT mark it failed or refund** (nothing was lost). The retry-on-429
  loop behaves as a fallback queue that waits for a genuinely free slot — so even a drifted/wiped
  semaphore can never *overspend*, it can only bounce excess requests off as 429s (this reframes the
  Redis semaphore as a best-effort optimization + per-user cap, not the sole enforcer). **Client-facing
  (graceful):** the 429 is absorbed into a friendly per-user SSE status (`queued` — "high demand — your
  song will generate shortly"); the raw 429 is **never** surfaced. Only persistent, unrecoverable
  saturation over an extended window escalates to a friendly failure + refund. A 429 in the happy path
  is a **monitored metric/alert** (signals semaphore drift or a misconfigured `n`). Distinguish from
  terminal 4xx (bad prompt, credits exhausted → `FAILED` + refund) and transient 5xx/timeout (watchdog
  retry).
- **Completion:** the generation API's create call returns `task_id` + both `conversion_id`s + `eta` +
  `credit_estimate` **synchronously** (handles known at submit time — this is what makes by-handle
  polling possible). The **webhook** arrives minutes later with the finished audio. The webhook handler
  is **thin**: record the audio URL(s) + metadata, set `INGEST_PENDING`, `NOTIFY`, return 200 fast.
- **Ingest (async, event-driven):** an ingest worker wakes on the NOTIFY, downloads the audio, uploads
  to R2, sets `READY` with the object key, enqueues the song onto the playback queue. The webhook URL
  is a *hint*; if expired/failed, refresh it via the API's **by-handle lookup** and retry.
- **Watchdog (sole recovery owner):** periodic sweep. `WAITING_FOR_WEBHOOK` + overdue → poll by handle
  (no re-charge). `SUBMITTING` + no handle + age > lease → re-call (accepted, *delayed* double-charge).
  `INGEST_PENDING` + overdue → refresh URL + retry; escalate to `FAILED` after a ceiling.
  `QUEUED`/stuck rows from a crashed worker are re-claimed. **All side-effecting watchdog actions are
  row-claimed (`FOR UPDATE SKIP LOCKED` + state transition) before acting**, so multiple workers never
  double-act (critical for the money-spending re-call).
- **Failure → refund + retry:** on terminal failure, decrement the user's Redis daily-count (refund the
  *quota slot*, not money), notify the user via their per-user SSE channel, and allow user-initiated
  resubmit. **Never auto-regenerate a terminal failure** (it re-charges the API). Transient/our-side
  failures are auto-retried by the watchdog only.

### Concurrency, identity, rate limits (#4, #5, #10, #14)

- **Global generation concurrency** is capped **authoritatively by the generation API itself**, which
  returns **429** on excess (`n` = 1 base / 5 upgraded tier). The **Redis semaphore is a best-effort
  guard** — it avoids wasting round-trips on 429s and enforces the **per-user in-flight cap** the API
  doesn't know about — **not** the sole enforcer. Both counters are atomic Redis ops. Full 429 handling
  (release slot, reconcile, requeue, graceful client status) is in the generation-pipeline section
  (#18).
- **Identity is optional.** Anonymous users can listen and create; anonymous identity = signed
  long-lived cookie UUID. Accounts (verified email or OAuth) unlock history/favorites/personalization
  and a higher per-account daily limit.
- **Limits (all config-driven, single source, tunable via env; enforcement toggled by one
  `ENFORCE_RATE_LIMITS` boolean, default true):** anonymous ~2 songs/day per cookie, ~6/day per IP
  (anonymous only — authenticated users are IP-exempt and run on their account limit); ~2 accounts per
  IP; per-user ~2 concurrent jobs but never exceeding global `n`. A bot check gates the create action.
  Rate-limit *enforcement* is an atomic `INCR` (+ `EXPIRE` for daily reset); the displayed "songs left"
  is a `GET`.
- **Queue fairness = plain FIFO for v1.** Round-robin/WFQ is **dropped**. Deferred escalation at
  thousands of users with large asymmetric caps = a **per-user in-queue cap**, not reordering.

### Real-time playback & synchronization (#3, #9, #12)

- **Playback pointer** (in `radio_state`, cached in Redis): `song_id`, `playback_id` (per-instance
  identity token), `source` (`static | generated`), `started_at`, `ends_at`, `version`.
- **Global sync:** client computes `offset = server_now − started_at` and seeks; clock-skew corrected
  via a `server_time` field (`skew = client_clock_on_receipt − server_time`; `server_now =
  client_clock_now − skew`). Pressing play is a pure client-side calc.
- **Drift:** loose tolerance (±0.5–1s); **no RTT/2 correction.** Client runs a free local check on
  `timeupdate` (skew refreshed by the ~30s heartbeat): **deadband < 1s, hard-seek to expected at ≥ 1s**
  (chosen because deadbanding a large drift truncates the song's ending, worse than a mid-song skip).
  The song boundary re-anchors everyone via a fresh-from-0 file start, so drift never crosses songs.
- **Consistency of the advance (Postgres write + Redis publish):** the fragile link is *notification
  delivery* (Two Generals). Mitigations: pub/sub payload carries the full new pointer; every SSE
  reconnect re-reads truth; a **~30s heartbeat re-broadcast** is the single standing reconcile (covers
  the leader-publish-failure case where nobody disconnected). **Safeguard:** a client whose audio ends
  with no next-song anchor immediately re-fetches state.
- **App-local cache** of the pointer per instance, invalidated by the pub/sub song-change event; a
  cold instance warms from the **Redis pointer key** (fallback to Postgres only if Redis is down).

### Radio coordinator & leader election (#11, #15, #16)

- **The coordinator lives in the worker.** It owns a clock-driven timer loop (`sleep(ends_at − now)`,
  woken early by a generation-completion `NOTIFY` for interrupts).
- **Leader election = Postgres advisory lock.** Whichever worker holds it runs the timer; others block
  on `pg_advisory_lock` and take over automatically when the holder's session (connection) dies.
  No TTL, no renewal heartbeat, pause-tolerant. **etcd/ZooKeeper/Raft explicitly rejected.** Failover
  gap = connection-death detection time; **tune TCP keepalive/`tcp_user_timeout` to ~10–15s.**
- **The advance is an idempotent version-CAS** (the safety net for a rare split-brain instant):

  ```
  UPDATE radio_state
     SET song_id=:next, playback_id=:new_id, source=:src,
         started_at=now(), ends_at=now()+:dur, version=version+1
   WHERE id=1 AND version=:version_i_read;   -- 0 rows updated ⇒ lost the race, back off
  ```

  `version_i_read` is the coordinator's own read, never client-supplied. No `ends_at` guard (it would
  break interrupts).
- **Advance rule at each boundary:** `next = pop_user_queue() if nonempty else pick_static(avoid_recent)`;
  then the version-CAS; then publish to Redis pub/sub. **Interrupts:** a freshly-completed user song may
  preempt a currently-playing **static** song (crossfade on the client) but **never** a user song;
  auto-limited to ≤1 interrupt per static song. **Never-empty fallback:** `pick_static` must never
  return empty; if it does, replay the current song.
- **First boot:** leader acquires the lock, reads `radio_state`; pointer exists → reconstruct & resume;
  no pointer → initialize (`pick_static`, idempotent insert-if-absent, `version` 0); no pointer and no
  songs → idle gracefully until content exists. Radio starts **eagerly** (plays to an empty room).

### Worker role gating (#15)

- **Organizing rule:** *row-claimable work → `SKIP LOCKED` parallelism; time-triggered singleton work →
  leader election.*
- **Dispatch, ingest, watchdog** run on **every** worker instance, serialized by `FOR UPDATE SKIP
  LOCKED`. **The radio coordinator** is **single-leader** (advisory lock). The watchdog is leaderless
  but every side-effecting action is row-claimed first.
- Dev: N=1 worker (does all four, trivially leader). Prod: N=1 works; N=2–3 for HA.

### Retention (#8, #13)

- **`jobs`** = active only (evict terminal rows after a short grace window for late-duplicate
  idempotency). **`songs`** = permanent catalog; **audio in R2 = permanent** (no lifecycle aging —
  storage is negligible vs. sunk generation cost). **`playback_history`** = the only retained entity
  (~60 days, **time-partitioned `DROP`**, not mass `DELETE`); it is the *cold* append-only analytics
  log (~1 write per advance), **not** the hot path — the hot anti-repeat/recently-played reads come
  from the Redis capped list.
- Queue claim uses a **partial index** `(seq) WHERE state='QUEUED'` → O(log n) index descent, no sort.
- **PgBouncer** for connection fan-in on the web tier; the worker's advisory-lock and `LISTEN/NOTIFY`
  connections bypass the pooler (session-level, direct).

### Observability & environments (#17)

- **Instrumentation always on** (every env, incl. prod): a `/metrics` endpoint (Prometheus format),
  structured JSON logs, and a **correlation ID** stamped on each song from submit through play.
- **Stack:** Prometheus + Grafana + Postgres/Redis exporters; **k6** as the load generator; a small
  custom async client only for the idle-connection ceiling test.
- **Environments:** *local* (docker-compose: full stack + fake API) for dev and cheap tests; *staging*
  (on-demand, prod-tier instance **size**, fewer instances, extrapolate count) runs the full
  self-hosted stack to produce the headline graphs on credible infra; *prod* exposes `/metrics` and is
  scraped continuously by a **managed free tier** (Grafana Cloud) plus a few alerts — no self-hosted
  24/7 stack.
- **Prove the model by shape, not scale:** (a) per-instance connection ceiling, (b) flat
  datastore-ops-vs-connections curve (the thesis), (c) write path at target create-rate via the fake
  API; then instances = target ÷ ceiling.

## Testing Decisions

**What makes a good test here:** it asserts on **externally observable behavior** — SSE events a
client receives, HTTP responses, rows/states in Postgres, objects in R2, counters in Redis — never on
internal function calls or private structure. Tests drive the system the way a listener, a creator, or
the generation API would, and read back what a listener/creator/operator could observe.

**Seams (please confirm — the skill wants these agreed):**

1. **Primary, single high seam — the system edge.** Drive real **HTTP + SSE** into the FastAPI app;
   use the **fault-injectable MusicGPT simulator** as the external-API boundary (it *is* the seam for
   the third party); run against **ephemeral, real Postgres/Redis/MinIO** (containers/testcontainers,
   fresh per run). Assert on SSE frames, DB job/pointer state, R2 objects, and Redis counters. This is
   the highest, fewest-seams option and tests real behavior. It exercises: create → dispatch →
   (simulated) generation → webhook → ingest → enqueue → advance → SSE broadcast, plus rate-limit
   enforcement, refund-on-failure, and idempotent duplicate webhooks.
2. **Fault injection through the same seam.** The simulator's switches (webhook-never-arrives,
   URL-expires-before-ingest, `ERROR`/`FAILED`, **429 rate-limit rejection**, delayed/duplicate webhook,
   crash-in-the-gap) drive the watchdog/recovery/refund and 429-requeue/graceful-status paths
   deterministically — so recovery is tested at the edge, not by mocking internals.
3. **Lower, process-level seam — leader failover only.** Testing "leader crashes → survivor takes over,
   station advances exactly once, no double-advance" requires killing a worker **process** and
   observing another acquire the advisory lock. This is the one place the edge seam can't reach; keep it
   isolated to a small set of process-lifecycle tests.
4. **Client-side sync math** (offset, skew, deadband/hard-seek thresholds, drift decision) is pure and
   should be unit-tested directly on the frontend as pure functions, since it's deterministic logic
   with no I/O.

**Modules exercised behaviorally:** create/rate-limit path; dispatch + semaphore; webhook + ingest;
watchdog recovery matrix; radio advance (boundary, interrupt-static-only, never-empty fallback,
version-CAS single-advance); SSE fan-out + reconnect-reread + heartbeat; cold-start/herd warming.

**Prior art:** the current Flask app already ships a **sim-webhook harness** — the new fault-injectable
simulator is its faithful successor and the anchor of the edge seam. The existing `radio/watchdog.py`
and job-lifecycle logic are the reference behaviors to preserve (states, recovery decisions) while
re-homing them behind the new seams.

## Out of Scope

- Kafka / any message broker (dropped, #7) — do not introduce it.
- Round-robin / weighted-fair queue dispatch (v1 is FIFO, #14). The deferred per-user in-queue cap is
  noted but **not** built now.
- Smarter static-rotation curation (weighting, categories, listener-aware, whole-catalog vs.
  recent-weighted) — deferred product choice.
- Ingesting/serving the second generated version, and any "variation" feature.
- RTT/2 sync precision and sub-100ms sync — deliberately not pursued (loose tolerance chosen).
- Pausing the rotation when listeners = 0 — deferred optimization; radio is always-on.
- Audio-file lifecycle/aging, promote-on-favorite, favorited-song retention exemptions — moot because
  audio is retained permanently (#13).
- Self-hosted 24/7 production monitoring, alerting policies/on-call — prod uses a managed free tier;
  the heavy stack is staging/on-demand.
- Exact tunable **values** (`T_lease`, TCP keepalive timeout, measured per-instance ceiling, semaphore
  `n` beyond 1) — to be set/measured during implementation and load test, not fixed here.
- Surfacing raw HTTP `429` / rate-limit errors to the user — deliberately handled as a graceful
  "high demand" queue status instead, never a raw error (#18).

## Further Notes

- **Portfolio framing is the spec.** Each pattern must be *correct and internally consistent*;
  over-engineering is not a valid critique, but a pattern implemented incorrectly is. The
  "System Design Concepts Demonstrated" list from the plan is the acceptance surface — minus Kafka.
- **The unifying insight** to keep visible in the implementation: *row-claimable work scales via
  `SKIP LOCKED`; only the time-triggered radio advance needs leader election.* This is also why Kafka
  was unnecessary.
- **Development phases** (from the plan, updated): (1) Postgres catalog + static playback; (2) Redis
  pointer + global sync; (3) SSE + preloaded transitions; (4) prompt submission + Postgres job queue
  (no Kafka); (5) generation API + webhook + async ingest to R2; (6) user-song queue with static
  fallback + interrupts; (7) Redis rate limiting; (8) retries/timeouts/idempotency/failure recovery +
  refund; (9) leader election + failover; (10) observability + load test; (11) multi-instance + deploy.
- **Doc reconciliation:** `plans/system_redesig_plan.md` still shows Kafka in its diagram and concepts
  list. It should be updated (or annotated as superseded by this spec) so it doesn't mislead
  implementation.
- **Full decision provenance** lives in the memory files `redesign-decisions.md` (#1–#17 index) and the
  eight supporting `redesign-*.md` files; consult them for the reasoning behind any decision above.
