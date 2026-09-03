# Shared-Reader Lane Arbitration

How the ACE identifies a lane's spool when two lanes share one RFID reader, without
ever binding a lane to the wrong spool.

## The problem

Slots 0+1 share one reader/antenna; slots 2+3 share the other. The ACE firmware
**cannot reliably attribute a read to a specific slot of a pair** — shared coil, shared
page buffer, single-shot anticollision. A clean read of the *wrong* bay's tag is a normal
outcome. The hard cases:

- a lane reads its neighbour's tag while loading;
- two lanes of a pair inserted near-simultaneously → both tags collide in one coil;
- a parked sibling's tag sits in the coil and drowns out the lane being read;
- a spare is inserted while the paired lane is **actively printing** — that lane's tag
  transits the coil constantly as the print pays out filament.

Without arbitration the outcomes are: a wrong bind (a lane adopts a sibling's spool),
no bind, or the state machine chasing inconsistent reads (observed: "hit load and it
G28'd toward the hot zone").

## Core principle — lock-in identification

Treat the shared coil like a **lock-in amplifier**. A lock-in recovers a signal buried in
noise by *modulating* what it measures and keeping only the response that tracks the
modulation. Apply that to identity:

> **To identify a lane, jog that lane's spool and accept only the tag whose transit
> correlates with the jog you commanded. A read belongs to lane X iff its
> appearance/disappearance tracks motion you commanded on X.**

Everything uncorrelated is noise and is filtered:

- a **static** parked tag — doesn't move when you jog → filtered;
- an **independently-moving** lane (e.g. printing) — moves on its own schedule, not your
  increments; also bound, so a known tag → filtered twice;
- a **collision** frame — garbage → discarded;
- any **already-bound** tag — a known interferer → never adopted.

We stop *guessing* which tag is whose and *prove* it by moving one lane at a time.

## Invariants (must always hold)

1. **Grab first.** On insertion, immediately grab each lane to a known captured position
   (past the entry sensor, firmly gear-engaged) **before any arbitration**. Hand-off
   contract: once grabbed, the user may release and walk away, guaranteed the lane finishes
   loading with zero further physical interaction. A lane is only ever paused *after* it is
   captured.
2. **One reader, one identification.** A per-reader mutex (0+1, 2+3). Only one lane of a
   pair may be in identification at a time.
3. **ID ≠ park.** Identification needs only spool rotation (tag transit); parking needs the
   hub free. A blocked hub defers the precise park, never the identification.
4. **Never push a block.** Never jog filament into a blocked hub or hard stop (buckle
   guard — see the eject-buckle history).
5. **Never adopt a known tag.** Any already-bound tag (parked sibling, actively-printing
   lane) is a known interferer: reject reads matching it, never adopt.
6. **Never a wrong bind.** Escalation is bounded; on exhaustion the lane stays captured and
   **unbound**, surfaced for `MMU_GATE_MAP`. Unbound is always preferable to mis-bound.

## The identification loop (per lane, holding the reader mutex)

1. **Grab** — Phase 0, done at insertion; a precondition of everything below.
2. **Own the coil** — acquire the pair's reader mutex.
3. **Baseline** — read the coil a few ticks with nothing jogging. Record any *constant* tag
   (static interferer) and any already-bound lane's tag as **filters**.
4. **Jog-and-correlate** — jog the target lane in small increments (a few mm of feed → a
   small spool rotation). After each increment, read the coil. A tag whose presence
   rises/falls **in step with the jog** (and is not a filtered tag) is the target's. Confirm
   over K correlated transits → **bind** the target to that tag.
5. **Escalate** — only if a static interferer drowns the coil and no correlated read emerges
   within the jog budget: **nudge the interferer out of the coil** (direction by its state,
   see table), then return to step 4.
6. **Bounded fallback** — if the jog budget (≥ one full spool rotation, capped short of a
   blocked hub) is exhausted with no correlated read → leave the lane **captured + unbound**;
   surface `MMU_GATE_MAP`.
7. **Park** — once identified *and* the hub is free, feed the remainder to park precisely
   from the tracked position. Release the mutex.

## Nudge-direction state table

The nudge direction is decided by the state of the interferer we must displace:

| Interferer state | Nudge | After |
|---|---|---|
| Captured at entry (unidentified, near gate) | **forward** (short) | keeps it captured; is progress toward its own load; track the delta |
| Parked at hub rest (identified, tip at hub) | **backward** (short) | rolls its tag off the coil without pushing the hub; **re-park** it once the target is identified |
| — | never into a blocked hub / hard stop | buckle guard |

Nudging is iterative and bounded: nudge → re-read → nudge again, up to a small cap; then
escalate to "bind the interferer as a known neighbour" (making it filterable) before the
final unbound fallback.

## Case walkthroughs (all collapse into the loop)

- **Concurrent insertion (both unidentified):** grab both → mutex → jog-correlate lane A
  (its tag tracks the jog; B's is static/uncorrelated → filtered) → bind A → release →
  jog-correlate B → bind B. Serialized proof-by-motion.
- **Parked sibling blocks a new lane:** the sibling is static (or bound → known) → filtered
  at baseline. Jog the new lane → correlated read → bind. If the sibling's tag drowns the
  coil → nudge it clear (forward if captured-at-entry, backward + re-park if parked-at-hub)
  → re-jog.
- **Identify while printing (spare inserted, paired lane printing):** the printing lane is
  bound → known interferer (filter) *and* moves on its own schedule → uncorrelated with our
  jog (double-filtered). Jog the spare on its independent motor (concurrent with the print;
  only the coil is shared); accept only a jog-correlated read of a *different* tag. The
  blocked hub defers only the park. **Safety crux:** never let the spare adopt the printing
  lane's tag → the spare can never be bound to the running spool.

## Host / firmware split

- **Host (driver)** owns: insertion detection, the per-reader mutex, the grab, the jog
  increments + correlation, the state-aware nudge, the known-tag filter, the bounded
  fallback, and the deferred park. It builds on the existing guards (Fix 2 cross-lane
  re-read; the shim bind guard) and the V1.1.3Z firmware `sm_id` injection, which yields a
  clean sku on a good read.
- **Firmware (optional assist, firmware-first):** expose per-scan-tick tag PRESENT/ABSENT,
  or emit a decode only on a rising-then-falling presence edge. That turns "seen and
  left"/correlation into a hardware fact instead of a host inference over intermittent
  reads, tightening the lock-in.

## Parameters to calibrate

- Grab distance (firm capture at a determinable position).
- Jog increment (mm/step) and correlation confirmation count K.
- Full-spool-rotation feed budget for ID; ID-feed max = min(that, hub-entry − buckle margin).
- Nudge amount and iteration cap.
- Concurrent-insertion detection window; identification timeout.

## Relationship to existing mechanisms

- **Fix 2 (cross-lane re-read):** a read matching another lane's inventory is rejected. The
  lock-in generalises this via correlation; the known-tag filter subsumes it for bound lanes.
- **Shim bind guard:** a lane cannot bind a spool another gate already holds (persisted) —
  the final backstop against a wrong bind.
- **Firmware `sm_id` injection (V1.1.3Z):** provides the clean sku on a good read;
  arbitration decides *which* read to trust.

## Build order

1. Host: grab → mutex → jog-correlate on lanes 0+1; prove it.
2. Add the nudge escalation (state-aware) + the parked-at-hub backward + re-park.
3. Add the print-time known-tag filter (identify-while-printing).
4. Optional: the firmware present/absent tick for hardware-grade correlation.

---

# What actually happened (2026-09-02/03)

Step 1 of the build order was implemented and run on hardware. This section records the
outcome, including the parts that did not work, because most of the cost of this exercise was
in discovering them.

## The firmware defect that made the whole thing look impossible

`identify_by_jog` kept reading a tag and getting `sku=None`, while the SAME tag read moments
later by the background worker resolved perfectly. The cause was not the arbitration logic and
not the ACE: it was **our own patch**.

`rawtag_stub`'s "not Anycubic" branch committed the `0x0202` raw-image sentinel and branched
straight to `epilogue`, so it never reached `resume` and therefore never reached the `sm_id`
inject at `0x0800E8A2` — which sits on the Anycubic-SUCCESS path, after the native sku/version
stores. **Every foreign/OpenSpool tag bypassed the inject by construction on the live-read
path.** Fixed in firmware V1.1.41 by searching `0x20000704` for `sm_id` before committing the
sentinel and answering like a native decode (`version 101` + `"SM<n>"`). See
`ace2-pro-firmware-research/docs/01-firmware-patches.md`.

Lesson worth carrying: when one code path resolves a tag and another does not, suspect the
patch before the hardware.

## Three motion facts, each of which hid the next

1. **The ACE cannot service a `cmd-68` while it is moving** — the read times out. A single
   continuous feed while polling therefore never works. It has to be discrete: move, stop,
   read.
2. **A read needs roughly a full spool revolution** for the tag to sweep the coil. Forward room
   ends at the hub, so a lane parked near the hub could only travel 236 mm — under one turn,
   and the tag never reached the coil at all. The jog now picks whichever direction has more
   room (backward toward park for a near-hub lane).
3. **`_retract_async` is fire-and-forget and silently drops refusals.** 12 of 25 chunks were
   lost to the ACE's post-STOP refuse window with no error anywhere, so the spool never
   completed a rotation. Only the retrying variant actually rotates.

## What it cost, and why that matters more than the duration

First working run: **642 s to move 384 mm** — 0.60 mm/s effective against 12 mm/s commanded,
with only 32 s of that being motion. Causes, in order of size:

- a 3.0 s post-STOP backoff paid on 31 of 32 chunks, for a STOP `identify_by_jog` never issues
  (each jog ends on its own commanded length);
- a 12 mm chunk, which turns a full spool about 7.2°, against a tag window measured near 45°
  (4 of ~32 reads landed) — so most stops sampled nowhere near the coil;
- `_identify_read_sync` abandoning at 1.5 s while the transport holds the request for its full
  5.0 s timeout (`serial_manager` has no per-request timeout), leaving a bus slot held for a
  reply nobody is waiting for.

The first two are fixed (40 mm chunk, `refused_pause=0.3`). The real problem was never the
clock though — it was the **variance**. Another run confirmed on the first chunk. "Sometimes
instant, sometimes eleven minutes" is the design smell.

## The reframing: identify is a fallback, not the main path

A normalize already feeds the lane ~434 mm to the hub and backs off, the firmware worker reads
the tag during that rotation, and `_fetch_tag` collects it 2.5 s later via `ACE_SCAN_TAG`.
**That is the path that binds lanes in normal operation.** So `identify_by_jog` is largely
redundant for any lane that gets normalized, and its real niche is narrow: resolving *which*
lane a read belongs to when the shared reader is ambiguous, using motion correlation.

Sequence future orchestration accordingly — normalize-then-fetch first, `identify_by_jog` only
when that produced nothing or the pairing is ambiguous.

## Normalizing is a speed optimization, not a precondition

A lane that has not been normalized is still perfectly usable. Normalizing shortens the next
reload to `park_offset + hub->entry` instead of the whole bowden; it is not a safety gate. A
load is **self-locating**: it feeds toward instrumented points (hub switch, then toolhead entry
switch) and discovers position on the way. Crossing the hub re-anchors the counter, so a lane
called on to load while still queued for calibration simply complies and calibrates in passing.

The one operation that genuinely needs a datum first is **sweep-drying**, because it rolls back
and forth on the ACE side of park without crossing any sensor — a known datum is the only thing
bounding its motion. That gate lives in `ace_dryroll.cfg` (`ace_dryroll_datum`), not here.

## Deferred park is now a queue

`identify_by_jog` does not call `ACE_LANE_NORMALIZE` directly — that fails, subtly: lane status
comes from a 1 Hz heartbeat, so immediately after the last retract chunk the lane still reads
`unwinding` and normalize refuses on its readiness gate. It hands the lane to the path guard's
**ordered normalize queue** instead, which drains on its own poll once the lane really is ready
and the shared path is clear. Ordering is the schedule and is persisted; an operator or the
printer can promote, append or drop entries; a refused normalize is re-queued and retried, with
a cap so a lane that can never park is retired rather than spun on forever.

## Still open

- The `identify_by_jog` read deadline / transport timeout mismatch above.
- Steps 2-4 of the build order (nudge escalation, print-time known-tag filter, the firmware
  present/absent tick) are **not** implemented.
- `_hub_expected_busy()` is simply "is a print running", with no gcode lookahead — so the queue
  cannot drain during a print, and `_ACE_DRYROLL_PREPARE_PARK` must still schedule the hub
  itself because it runs inside `PRINT_START`.

