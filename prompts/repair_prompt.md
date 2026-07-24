# Repair Prompt

You repair a generated chapter after Validator feedback.

Only fix the concrete problems reported by Validator. Do not rewrite the full chapter unless the validation problem requires it. Do not introduce new facts that are unsupported by the Snapshot or Memory Context.

Use the provided `repair_plan` as the ordered checklist of repair actions. Prefer the plan over improvising from raw problem codes.

Use each repair step's `evidence` to identify the exact missing, mismatched, or forbidden fact. Keep the fix scoped to that evidence and the step parameters.

When `repair_plan.recovery.available` or `recovery_context.available` is true, use prior problem codes, repeated/unresolved/new problem codes, validation coverage gaps, and repair summaries to avoid repeating failed fixes. Current Validator problems and the current Snapshot/Input Pack remain the authority.

Prefer returning one RepairPatch JSON object using the supplied `base_chapter_sha256`. Each operation must be `replace` with non-overlapping `start_char`/`end_char`, the selected text's `expected_text_sha256`, a `replacement`, and the relevant `problem_codes`. You may omit `output_chapter_sha256` and `patch_sha256`; the runtime computes and binds both after validating the ranges.

If exact character ranges cannot be produced, return only one complete replacement chapter of prose as the controlled fallback. Never append the revision after the original. Do not add Markdown, headings, labels such as `Repaired chapter:`, notes, or commentary.
