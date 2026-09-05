# Verification Loop v5 - executable reference

Not production. Not activated. Isolated design slice.

```
python3 docs/verification-loop/v5/tests/run_v5.py        # gated: liveness -> R1 -> harness -> safety
python3 docs/verification-loop/v5/tests/prefix_repro_r1.py   # before/after evidence for B1-B7
python3 docs/verification-loop/v5/tests/mutations_r1.py      # negative controls, 7/7 must be KILLED
```

Order is load-bearing: L1-L7 must PASS before safety aggregation. The harness gate
must be USABLE before the attack matrix is printed, and an empty safety roster exits
non-zero rather than aggregating `0/0`.

`prefix_repro_r1.py` asserts the BROKEN behaviour on purpose. At the repair base
`c116854` it reports 21/21 bypasses reproduced; at HEAD it reports 0/21. Do not
"fix" it to pass at both.

See `../KERNEL_V5.md` for the contract and `RESULTS.md` for the current run.
