# Verification Loop v5 — executable reference

Not production. Not activated. Isolated design slice.

```
python3 docs/verification-loop/v5/tests/run_v5.py
```

Order is load-bearing: L1–L7 must PASS before safety aggregation. Harness gate
must be USABLE before the attack matrix is printed.

See `../KERNEL_V5.md` for the contract.
