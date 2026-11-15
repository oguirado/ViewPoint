# `swarm/broker/` — ViewPoint Swarm Broker & Blender Bridge

Implements [`swarm/CONTRACT.md`](../CONTRACT.md) for the single-capsule `vpswarm` architecture.

## Files
- `bridge.py`: Blender 4.0.2 main-thread bridge (`bpy.app.timers`), plus pure-Python `discover_nonce()` and `process_inbox_once()` helpers for unit tests.
- `broker.py`: Single-writer turn-queue / actor broker (`SwarmBroker`), exclusive `vision` caller (`--vision-mode live|mock`), nonce + lease issuer, bounded FIFO ingestion, and `events.jsonl` author.
- `test_broker.py`: Offline deterministic unit & negative-control suite (`unittest`).

## Running Blender Bridge
```bash
blender --factory-startup --python ~/Documentos/viewpoint-hackathon/swarm/broker/bridge.py
```

## Running Broker
```bash
# Live mode against vision job `vpswarm`
python3 -m swarm.broker.broker --job vpswarm --vision-mode live

# Offline dry-run / drain mode (isolated in /tmp/swarm_dryrun; never touches live /dev/shm or swarm/run/)
python3 -m swarm.broker.broker --vision-mode mock --drain-and-exit \
  --inbox /tmp/swarm_dryrun/inbox \
  --outbox /tmp/swarm_dryrun/outbox \
  --rejected /tmp/swarm_dryrun/rejected \
  --frames-dir /tmp/swarm_dryrun/frames \
  --intents-dir /tmp/swarm_dryrun/intents \
  --events-log /tmp/swarm_dryrun/events.jsonl \
  --nonce-file /tmp/swarm_dryrun/nonce.txt
```

## Running Unit Tests
```bash
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 \
  python3 -m unittest discover -s swarm/broker -p 'test_*.py' -v
```
