# Hardware sizing before field hardware

The sizing pipeline turns OpenShift resource experiments into a procurement
envelope while preserving the evidence boundary. An `OPENSHIFT_QUOTA` trial can
produce only `ESTIMATED_ONLY`; it cannot qualify CPU architecture, an NPU,
physical power, radio behavior, thermal behavior, or solar autonomy.

Run the checked-in planning example:

```bash
make hardware-plan
```

The result is written to `artifacts/hardware-sizing-estimate.json`. Replace the
placeholder trials with observations from the live EDD capacity run: allocated
CPU and memory requests and limits, observed peak CPU and memory, base and
per-release storage, p95 latency, and error rate. Keep unsuccessful trials. The
calculator selects the smallest
passing observation for each mode and adds the declared CPU, memory, storage,
rollback, and energy reserves.

The declared resource requests are also sizing floors. A short metrics window
cannot turn one lightly loaded burst into an unrealistically small node.

The first retained cluster observation is
`hardware/observations/openshift-rag-only-2026-10-07.json`. Its burst-10 result
is 10/10 completed, 381.4 ms p95, and 26.12 requests/second for the small Summit
corpus. All responses were RAG-direct, so it narrows only the RAG-only envelope;
it is not evidence for BitNet generation capacity.

A separate direct runtime probe is retained in
`hardware/observations/openshift-bitnet-probe-2026-10-07.json`. On the shared
Xeon Platinum 8260 worker, the pinned 1.188 GB model produced 32 tokens in 3.327
seconds (about 9.62 tokens/second) with two runtime threads and one slot. This is
useful model evidence, but one short probe is not a passing full-generation
trial; the report therefore keeps that envelope at `NO_PASSING_TRIAL`.

The power scenarios are assumptions until a trial uses `PHYSICAL_HARDWARE` and
supplies an exact hardware identity plus average and peak watts. Battery and
solar values are planning minima:

- daily energy = average watts × 24;
- battery = energy × autonomy × reserve ÷ usable fraction ÷ conversion efficiency;
- solar = daily energy ÷ peak-sun-hours ÷ solar derating; and
- continuous DC supply = 125% of the largest measured or estimated load.

Even a physical measurement produces `MEASURED_CANDIDATE`, not a field-ready
claim. Final support still requires the complete CUT on the exact board, OS
image, storage, modem, LoRa radio, antennas, enclosure, and power system.
