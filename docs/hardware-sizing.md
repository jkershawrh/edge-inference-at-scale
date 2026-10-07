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
CPU and memory, peak CPU and memory, base and per-release storage, p95 latency,
and error rate. Keep unsuccessful trials. The calculator selects the smallest
passing observation for each mode and adds the declared CPU, memory, storage,
rollback, and energy reserves.

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
