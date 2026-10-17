# AI-Generated Global Radio

## Goal

Build a globally synchronized internet radio where users submit prompts
to generate songs. User-generated songs are prioritized for playback,
while a static song library fills downtime.

## Core Architecture

``` text
React / Next.js Client
        │
     HTTP + SSE
        │
        ▼
Backend Monolith
   │     │      │
   ▼     ▼      ▼
Postgres Redis  Kafka
   │             │
   ▼             ▼
Object       Generation
Storage        Worker
                  │
                  ▼
             Music API
                  │
               Webhook
                  │
                  ▼
               Backend
```

## Components

### PostgreSQL --- Source of Truth

-   Users
-   Songs and metadata
-   Generation jobs and their states
-   Durable playback queue
-   Playback history
-   External generation IDs for idempotency

### Redis --- Real-Time / Ephemeral State

-   Currently playing song with `playback_id`, `started_at`, and
    `ends_at`
-   Hot queue containing upcoming user songs
-   Static-song shuffle state
-   Rate limiting
-   Eventually, coordinator locking / leader election

### Kafka --- Generation Jobs

-   User submits prompt → job persisted → Kafka event produced.
-   Generation worker consumes jobs.
-   Only **one music-generation request may be active at a time**.
-   Job lifecycle:

``` text
QUEUED → SUBMITTING → WAITING_FOR_WEBHOOK → COMPLETED / FAILED
```

-   Webhook completion persists the generated song and places it into
    the playback queue.
-   Duplicate webhooks are handled idempotently.

### Object Storage --- Audio

-   S3, R2, or MinIO stores audio files.
-   PostgreSQL stores URLs/object keys and metadata rather than audio
    blobs.

## Radio Playback

The backend acts as the authoritative **radio coordinator**.

``` text
User songs waiting?
       │
   ┌───┴───┐
  Yes      No
   │        │
   ▼        ▼
FIFO      Static
queue    shuffle
   │        │
   └───┬────┘
       ▼
  Play next song
```

Every listener receives the same playback state:

``` text
song_id
playback_id
started_at
server_time
```

A newly connected client calculates:

``` text
offset = server_time - started_at
```

and begins the audio at that position.

SSE broadcasts song changes to connected listeners. Clients preload the
upcoming song to minimize gaps between tracks.

## Development Phases

1.  Build PostgreSQL song catalog and static radio playback.
2.  Add Redis current playback state and global synchronization.
3.  Add SSE and seamless/preloaded song transitions.
4.  Add user prompt submission and Kafka generation jobs.
5.  Integrate the music API and webhook handling.
6.  Add Redis user-song playback queue with static fallback.
7.  Add Redis rate limiting.
8.  Add retries, timeouts, idempotency, and failure recovery.
9.  Add observability and load testing.
10. Later: multiple backend instances, distributed radio-coordinator
    locking, and deployment/cloud infrastructure.

## System Design Concepts Demonstrated

-   Event-driven architecture
-   Asynchronous job processing
-   Kafka
-   Caching
-   Relational data modeling
-   Real-time synchronization
-   Distributed coordination
-   Idempotency
-   Rate limiting
-   Failure recovery
-   Object storage
-   Horizontal scaling
-   Observability
