# vmem executable mutants

Run `PYTHONDONTWRITEBYTECODE=1 taskset -c 5-9 python3 tests/mutants/vmem/run.py`.
The unittest entry point is `tests.test_e3_router_mutants.TestMutantSuite`.

The JSON manifest binds each mutation to a unique exact source fragment and an
executable test. The runner first executes every selected test on an unmodified
copy. It then parses each changed module, loads the selected oracle, and requires
executed checks with a failing unittest result. Syntax failures, import failures,
missing tests and green results cannot count as kills. Every subprocess has a
timeout. Copies and per-process JSON receipts live under `build/room_V2/mutants/`;
separate executions never share a writable copy.

M-VM1 through M-VM12 cover the specification's named mutant families. Additional
mutants cover each runtime module. M-VM2/M-VM3 use actual file/directory fsync
observations before rename, CURRENT replacement and journal acknowledgement;
SIGKILL timing alone does not establish durable flush ordering.

M-VM11 deliberately models a wrapping unsigned sum. Plain Python integer addition
does not wrap and is equivalent to the subtraction expression; no claim is made
that an ordinary Python sum-form replacement can be killed.

The catalogued Godot mutant runner accepts C/GDScript books and compiler/Godot
oracles. This Python suite uses stdlib unittest oracles instead.
