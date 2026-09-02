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
