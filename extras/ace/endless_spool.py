"""
EndlessSpool: Handles endless spool filament matching logic.

Responsibilities:
- Find exact material/color matches across all slots
- No pause/resume logic (manager owns that)
- No jam detection (removed)

Integration:
- Called from: AceManager._handle_runout_detected()
"""

import logging
import re
from .config import (
    ACE_INSTANCES,
    SLOTS_PER_ACE,
    get_instance_from_tool,
    get_local_slot,
)


# Polymer family from a vendor label: strip at the first non-alphanumeric character.
# PLA+ / PLA-HF / PLA Silk -> PLA; PETG stays PETG. Same rule as ace_mmu_shim._family and
# the purge-scaling logic, so all three agree on what "same material" means.
_FAMILY_TRIM = re.compile(r"[^A-Za-z0-9].*$")


def _material_family(label):
    return _FAMILY_TRIM.sub("", str(label or "").strip().upper())


class EndlessSpool:
    """Endless spool matching logic only."""

    def __init__(self, printer, gcode, manager):
        """
        Initialize endless spool handler.

        Args:
            printer: Klipper printer object
            gcode: Klipper gcode object
            manager: AceManager instance
        """
        self.printer = printer
        self.gcode = gcode
        self.manager = manager
        self.reactor = printer.get_reactor()

    def get_match_mode(self):
        """
        Get the current endless spool match mode from persistent storage.

        Returns:
            str: "exact" (material + color), "material" (material only),
                 or "next" (first ready spool regardless of material/color)
        """
        mode = self.manager.state.get("ace_endless_spool_match_mode", "exact")

        # Normalize/guardrail unexpected values
        if mode not in {"exact", "material", "next"}:
            mode = "exact"

        return mode

    def find_exact_match(self, current_tool):
        """
        Find next spool with matching material and optionally color.

        Match mode (configurable via ace_endless_spool_match_mode):
        - "exact": Match both material AND color (default)
        - "material": Match material only, ignore color

        Searches all slots in all instances, starting from the next tool
        and wrapping around.

        Args:
            current_tool: Tool index with runout (0-based)

        Returns:
            int: Tool index of match, or -1 if none found
        """
        inst_num = get_instance_from_tool(current_tool)
        local_slot = get_local_slot(current_tool, inst_num)

        if inst_num < 0 or local_slot < 0:
            return -1

        ace_inst = ACE_INSTANCES.get(inst_num)
        if not ace_inst:
            return -1

        current_inv = ace_inst.inventory[local_slot]
        current_material = current_inv.get("material", "").lower().strip()
        current_color = current_inv.get("color", [0, 0, 0])

        match_mode = self.get_match_mode()

        if match_mode == "material":
            logging.info(
                f"ACE: Looking for endless spool match (MATERIAL ONLY): {current_material}"
            )
        elif match_mode == "next":
            logging.info("ACE: Looking for endless spool match (NEXT READY SPOOL)")
        else:
            logging.info(
                f"ACE: Looking for endless spool match (EXACT): "
                f"{current_material} RGB({current_color[0]},{current_color[1]},{current_color[2]})"
            )

        total_tools = len(ACE_INSTANCES) * SLOTS_PER_ACE

        for offset in range(1, total_tools):
            candidate_tool = (current_tool + offset) % total_tools

            candidate_inst_num = get_instance_from_tool(candidate_tool)
            candidate_local_slot = get_local_slot(candidate_tool, candidate_inst_num)

            if candidate_inst_num < 0 or candidate_local_slot < 0:
                continue

            cand_ace = ACE_INSTANCES.get(candidate_inst_num)
            if not cand_ace:
                continue

            cand_inv = cand_ace.inventory[candidate_local_slot]

            cand_status = cand_inv.get("status")
            if cand_status != "ready":
                logging.info(
                    f"ACE: T{candidate_tool} skipped - status={cand_status} (need 'ready')"
                )
                continue

            # In "next" mode we ignore material/color and take the first ready slot
            if match_mode == "next":
                logging.info(f"ACE: Match found (next ready): T{candidate_tool}")
                return candidate_tool

            cand_material = cand_inv.get("material", "").lower().strip()

            # SAFETY: Never match "unknown" materials - we don't know if they're compatible!
            if current_material == "unknown" or cand_material == "unknown":
                logging.info(
                    f"ACE: T{candidate_tool} skipped - cannot match unknown materials "
                    f"(current='{current_material}', candidate='{cand_material}')"
                )
                continue

            # MODE=material means "same polymer", not "same vendor label". An exact string
            # compare here made PLA and PLA+ mutually unmatchable even though they are the
            # same polymer at the same temperature, which is precisely the swap the mode
            # exists to allow (2026-08-26). MODE=exact keeps the strict compare.
            if match_mode == "material":
                if _material_family(cand_material) != _material_family(current_material):
                    logging.info(
                        f"ACE: T{candidate_tool} skipped - material family mismatch "
                        f"(want '{_material_family(current_material)}' from "
                        f"'{current_material}', got '{_material_family(cand_material)}' "
                        f"from '{cand_material}')"
                    )
                    continue
            elif cand_material != current_material:
                logging.info(
                    f"ACE: T{candidate_tool} skipped - material mismatch "
                    f"(want '{current_material}', got '{cand_material}')"
                )
                continue

            if match_mode == "exact":
                cand_color = cand_inv.get("color", [0, 0, 0])
                if cand_color != current_color:
                    logging.info(
                        f"ACE: T{candidate_tool} skipped - color mismatch "
                        f"(want RGB({current_color[0]},{current_color[1]},{current_color[2]}), "
                        f"got RGB({cand_color[0]},{cand_color[1]},{cand_color[2]}))"
                    )
                    continue

            logging.info(
                f"ACE: Match found: T{current_tool} → T{candidate_tool}"
            )
            return candidate_tool

        if match_mode == "material":
            logging.info(
                f"ACE: No match for T{current_tool} (material: {current_material})"
            )
        elif match_mode == "next":
            logging.info(
                f"ACE: No ready spool available for T{current_tool}"
            )
        else:
            logging.info(
                f"ACE: No match for T{current_tool} "
                f"({current_material}, RGB({current_color[0]},{current_color[1]},{current_color[2]}))"
            )
        return -1

    def list_candidates(self, current_tool):
        """Ready lanes that could take over after a runout on current_tool.

        Returns [(tool, material, color, exact)] in wrap-around search order.
        exact = same material AND colour as the runout lane. Unknown-material
        lanes are excluded, same safety rule as find_exact_match.
        """
        inst_num = get_instance_from_tool(current_tool)
        local_slot = get_local_slot(current_tool, inst_num)
        if inst_num < 0 or local_slot < 0:
            return []
        ace_inst = ACE_INSTANCES.get(inst_num)
        if not ace_inst:
            return []
        cur_inv = ace_inst.inventory[local_slot]
        cur_mat = cur_inv.get("material", "").lower().strip()
        cur_color = cur_inv.get("color", [0, 0, 0])
        total_tools = len(ACE_INSTANCES) * SLOTS_PER_ACE
        out = []
        for offset in range(1, total_tools):
            t = (current_tool + offset) % total_tools
            i_num = get_instance_from_tool(t)
            i_slot = get_local_slot(t, i_num)
            if i_num < 0 or i_slot < 0:
                continue
            inst = ACE_INSTANCES.get(i_num)
            if not inst:
                continue
            inv = inst.inventory[i_slot]
            if inv.get("status") != "ready":
                continue
            mat = inv.get("material", "").lower().strip()
            if mat == "unknown":
                continue
            color = inv.get("color", [0, 0, 0])
            exact = (cur_mat != "unknown" and mat == cur_mat
                     and color == cur_color)
            out.append((t, inv.get("material", "?"), color, exact))
        return out

    def execute_swap(self, from_tool, to_tool):
        """
        Execute endless spool swap with intelligent fallback.

        On feed failure:
        1. Smart unload the failed tool (to_tool)
        2. Search for next matching spool
        3. Retry with new candidate
        """
        self.gcode.respond_info(f"ACE: Endless spool swap: T{from_tool} → T{to_tool}")

        tried_tools = {from_tool}
        current_target_tool = to_tool
        max_swap_attempts = 3

        try:
            for swap_attempt in range(max_swap_attempts):
                try:
                    self.gcode.respond_info(
                        f"ACE: Tool change attempt {swap_attempt + 1}/{max_swap_attempts}: "
                        f"T{from_tool} → T{current_target_tool}"
                    )

                    from_inst_num = get_instance_from_tool(from_tool)
                    from_slot = get_local_slot(from_tool, from_inst_num)
                    if from_inst_num >= 0 and from_slot >= 0:
                        ace_inst = self.manager.instances[from_inst_num]
                        if ace_inst:
                            ace_inst.inventory[from_slot]["status"] = "empty"
                            self.manager._sync_inventory_to_persistent(from_inst_num, flush=False)
                            self.gcode.respond_info(f"ACE: Marked T{from_tool} as empty")

                    status = self.manager.perform_tool_change(from_tool, current_target_tool, is_endless_spool=True)
                    self.gcode.respond_info(f"ACE: {status}")

                    # Consumed-spool housekeeping: hand the emptied gate to the
                    # config layer (unbind, backend notes). Optional hook.
                    try:
                        if self.printer.lookup_object(
                                "gcode_macro _ACE_RUNOUT_CONSUMED", None) is not None:
                            self.gcode.run_script_from_command(
                                f"_ACE_RUNOUT_CONSUMED GATE={from_tool}"
                            )
                    except Exception as hook_err:
                        self.gcode.respond_info(
                            f"ACE: Consumed-spool hook failed: {hook_err}"
                        )

                    self.gcode.respond_info("ACE: Resuming print")
                    self.manager.gcode.run_script_from_command("RESUME PURGE=0")

                    return

                except Exception as load_error:
                    self.gcode.respond_info(
                        f"ACE: Tool change attempt {swap_attempt + 1} failed: {load_error}"
                    )

                    # If this was the last attempt, don't try recovery
                    if swap_attempt >= max_swap_attempts - 1:
                        raise

                    tried_tools.add(current_target_tool)

                    self.gcode.respond_info(
                        f"ACE: Attempting recovery - parking T{current_target_tool}..."
                    )

                    try:
                        to_inst_num = get_instance_from_tool(current_target_tool)
                        to_slot = get_local_slot(current_target_tool, to_inst_num)

                        if to_inst_num >= 0 and to_slot >= 0:
                            # Park, never eject: the fixed-length unload backed a lane
                            # that had not left its feeder out of the slot (2026-08-26).
                            self.gcode.run_script_from_command(
                                f"_ACE_RECOVERY_PARK T={current_target_tool}"
                            )

                    except Exception as unload_error:
                        self.gcode.respond_info(
                            f"ACE: Warning - recovery unload failed: {unload_error}"
                        )

                    self.gcode.respond_info("ACE: Searching for next matching spool...")

                    # Temporarily mark tried tools as unavailable during search
                    saved_statuses = {}
                    for tried_tool in tried_tools:
                        tried_inst_num = get_instance_from_tool(tried_tool)
                        tried_slot = get_local_slot(tried_tool, tried_inst_num)
                        if tried_inst_num >= 0 and tried_slot >= 0:
                            tried_ace = self.manager.instances[tried_inst_num]
                            if tried_ace:
                                saved_statuses[tried_tool] = tried_ace.inventory[tried_slot]["status"]
                                tried_ace.inventory[tried_slot]["status"] = "searching"  # Temp status

                    try:
                        next_tool = self.find_exact_match(from_tool)
                    finally:
                        # Restore original statuses
                        for tried_tool, saved_status in saved_statuses.items():
                            tried_inst_num = get_instance_from_tool(tried_tool)
                            tried_slot = get_local_slot(tried_tool, tried_inst_num)
                            if tried_inst_num >= 0 and tried_slot >= 0:
                                tried_ace = self.manager.instances[tried_inst_num]
                                if tried_ace:
                                    tried_ace.inventory[tried_slot]["status"] = saved_status

                    if next_tool == -1 or next_tool in tried_tools:
                        raise Exception(
                            f"No more matching spools available (already tried: {sorted(tried_tools)})"
                        )

                    self.gcode.respond_info(f"ACE: Found next candidate: T{next_tool}")
                    current_target_tool = next_tool

        except Exception as e:
            # LIFT FIRST, before anything else and before the prompt. By this point the
            # purge's RESTORE_GCODE_STATE has usually put the nozzle back over the part at
            # layer height, so whatever failed, leaving it there bakes the print while the
            # user reads the message. Swallow errors: a failure to park must never mask the
            # real exception below.
            try:
                self.gcode.run_script_from_command("_TOOLHEAD_PARK_PAUSE_CANCEL")
            except Exception:
                logging.exception("ACE: could not park the toolhead after a failed swap")
            self.gcode.respond_info("ACE: *** ENDLESS SPOOL SWAP FAILED ***")
            self.gcode.respond_info(f"ACE: {e}")
            self.gcode.respond_info("ACE: Print is PAUSED - fix the issue and RESUME manually")

            # Show user prompt for failed swap
            from_inst_num = get_instance_from_tool(from_tool)
            from_slot = get_local_slot(from_tool, from_inst_num)

            material = "unknown"
            color = [0, 0, 0]

            if from_inst_num >= 0 and from_slot >= 0:
                ace_inst = self.manager.instances[from_inst_num]
                if ace_inst:
                    # Restore status for retry
                    ace_inst.inventory[from_slot]["status"] = "ready"
                    self.manager._sync_inventory_to_persistent(from_inst_num, flush=False)

                    # Get material info for prompt
                    inv = ace_inst.inventory[from_slot]
                    material = inv.get("material", "unknown")
                    color = inv.get("color", [0, 0, 0])

            # Show failure prompt
            self._show_swap_failed_prompt(from_tool, from_inst_num, from_slot, material, color, str(e))

    def _show_swap_failed_prompt(self, tool_index, instance_num, local_slot, material, color, error_msg):
        """
        Show user prompt when endless spool swap fails.

        Args:
            tool_index: Original tool that ran out
            instance_num: ACE instance number
            local_slot: Local slot number
            material: Material type
            color: RGB color array
            error_msg: Error message explaining failure
        """
        self.gcode.run_script_from_command(
            'RESPOND TYPE=command MSG="action:prompt_begin Endless Spool Swap Failed"'
        )

        color_str = f"RGB({color[0]},{color[1]},{color[2]})"
        # Truncate error message to avoid overly long prompts
        short_error = error_msg.split('\n')[0][:100]

        prompt_text = (
            f"Endless spool swap failed for T{tool_index} (ACE {instance_num}, Slot {local_slot}) - "
            f"Material: {material}, Color: {color_str}. Error: {short_error}. "
            f"Please refill spool or load matching material, then RESUME."
        )

        self.gcode.run_script_from_command(
            f'RESPOND TYPE=command MSG="action:prompt_text {prompt_text}"'
        )

        self.gcode.run_script_from_command(
            'RESPOND TYPE=command MSG="action:prompt_footer_button Resume|RESUME|primary"'
        )
        self.gcode.run_script_from_command(
            'RESPOND TYPE=command MSG="action:prompt_footer_button Cancel Print|CANCEL_PRINT|error"'
        )

        self.gcode.run_script_from_command(
            'RESPOND TYPE=command MSG="action:prompt_show"'
        )

    def get_status(self):
        """Return status dict for Klipper."""
        return {}
