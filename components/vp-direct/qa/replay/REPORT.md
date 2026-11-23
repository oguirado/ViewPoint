# Replay and recovery qualification

The replay suite exercises offline transition and receipt handling.
Run `components/vp-direct/qa/replay/tests/safe_run.sh` for the checked-in suite.

## Open finding `REP-P4-P7-001`

Transition publication and durable receipt recovery can disagree across a restart.
The owning surface is `components/vp-direct/perception/lifecycle/`.
The affected consumer is `components/vp-direct/perception/receipts/`.
The regression witness remains in [failing-seeds.json](failing-seeds.json).
The finding stays open until recovery produces the same retained publication receipt
and authority binding after interruption and restart.
