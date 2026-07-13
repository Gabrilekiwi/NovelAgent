from __future__ import annotations

import random
import string
import unittest

from core.story_project.managed_block import (
    MANAGED_BLOCK_END,
    MANAGED_BLOCK_START,
    ManagedBlockError,
    build_managed_projection,
    compute_base_source_digest,
    parse_managed_block,
    parse_manual_tombstones,
    three_way_merge_managed,
    write_managed_block,
)


SHA_A = "a" * 64


def projection(
    *,
    revision: str = "rev-1",
    owned: tuple[str, ...] = ("story_state.location",),
    values: dict | None = None,
    tombstones: tuple[dict, ...] = (),
) -> dict:
    return build_managed_projection(
        scope="context",
        book_id="book-test",
        run_id=f"run-{revision}",
        chapter=3,
        parser_version="shadow-1.0",
        base_revision=revision,
        base_source_digest=SHA_A,
        owned_fields=owned,
        values=values if values is not None else {"story_state.location": "旧城站"},
        tombstones=tombstones,
    )


class StoryProjectManagedBlockTest(unittest.TestCase):
    def test_round_trip_is_byte_idempotent_for_lf_crlf_and_bom(self) -> None:
        for original in (
            b"# Manual\n\nHuman text.\n",
            "\ufeff# 人工\r\n\r\n保留内容。\r\n".encode("utf-8"),
        ):
            with self.subTest(original=original[:10]):
                first = write_managed_block(original, projection())
                parsed = parse_managed_block(first)
                self.assertIsNotNone(parsed)
                self.assertEqual(projection(), parsed.projection)
                second = write_managed_block(first, projection())
                self.assertEqual(first, second)
                self.assertEqual(1, second.count(MANAGED_BLOCK_START.encode("utf-8")))

    def test_replacement_preserves_all_bytes_outside_the_managed_range(self) -> None:
        before = write_managed_block(b"\xef\xbb\xbf# Manual\r\n\r\nHuman bytes.\r\n", projection())
        parsed_before = parse_managed_block(before)
        self.assertIsNotNone(parsed_before)
        before_text = before.decode("utf-8")
        prefix = before_text[: parsed_before.start_char]
        suffix = before_text[parsed_before.end_char :]

        updated_projection = projection(revision="rev-2", values={"story_state.location": "控制室"})
        after = write_managed_block(before, updated_projection)
        parsed_after = parse_managed_block(after)
        self.assertIsNotNone(parsed_after)
        after_text = after.decode("utf-8")

        self.assertEqual(prefix, after_text[: parsed_after.start_char])
        self.assertEqual(suffix, after_text[parsed_after.end_char :])
        self.assertTrue(after.startswith(b"\xef\xbb\xbf# Manual\r\n"))
        self.assertNotIn(b"\r\r\n", after)

    def test_base_digest_excludes_current_managed_payload(self) -> None:
        first = write_managed_block(b"# Manual\n", projection())
        first_digest = compute_base_source_digest(first, external_sources=(("outline", "b" * 64),))
        second = write_managed_block(
            first,
            projection(revision="rev-2", values={"story_state.location": "新地点"}),
        )
        second_digest = compute_base_source_digest(second, external_sources=(("outline", "b" * 64),))

        self.assertEqual(first_digest, second_digest)
        self.assertNotEqual(first_digest, compute_base_source_digest(second, external_sources=(("outline", "c" * 64),)))

    def test_duplicate_reversed_and_malformed_markers_block_parsing(self) -> None:
        cases = (
            f"{MANAGED_BLOCK_START}\n{MANAGED_BLOCK_START}\n{MANAGED_BLOCK_END}\n",
            f"{MANAGED_BLOCK_END}\n{MANAGED_BLOCK_START}\n",
            f"{MANAGED_BLOCK_START}\n",
            f"prefix {MANAGED_BLOCK_START}\nbody\n{MANAGED_BLOCK_END} suffix\n",
        )
        for text in cases:
            with self.subTest(text=text), self.assertRaises(ManagedBlockError):
                parse_managed_block(text)

    def test_manual_tombstone_is_read_only_outside_the_managed_block(self) -> None:
        embedded = projection(
            owned=("foreshadowing.fs-old",),
            values={},
            tombstones=(
                {"field_path": "foreshadowing.fs-old", "reason": "resolved", "superseded_by": None},
            ),
        )
        document = write_managed_block(
            b"<!-- NovelAgent:tombstone field=foreshadowing.fs-manual superseded_by=foreshadowing.fs-new -->\n",
            embedded,
        )

        self.assertEqual(
            [
                {
                    "field_path": "foreshadowing.fs-manual",
                    "reason": "manual_tombstone",
                    "superseded_by": "foreshadowing.fs-new",
                }
            ],
            parse_manual_tombstones(document),
        )

    def test_three_way_merge_preserves_unknown_fields_and_missing_is_not_deletion(self) -> None:
        base = projection(
            owned=("story_state.location",),
            values={"story_state.location": "A", "future.unknown": {"raw": True}},
        )
        current = projection(
            revision="rev-current",
            owned=("story_state.location",),
            values={"story_state.location": "A", "future.unknown": {"raw": True}},
        )
        proposed = projection(revision="rev-next", owned=("story_state.location",), values={})

        result = three_way_merge_managed(base=base, current=current, proposed=proposed)

        self.assertTrue(result.ok)
        self.assertEqual("A", result.projection["values"]["story_state.location"])
        self.assertEqual({"raw": True}, result.projection["values"]["future.unknown"])

    def test_manual_value_wins_and_concurrent_managed_edit_conflicts(self) -> None:
        base = projection(values={"story_state.location": "A"})
        current = projection(revision="current", values={"story_state.location": "B"})
        proposed = projection(revision="next", values={"story_state.location": "C"})

        conflicted = three_way_merge_managed(base=base, current=current, proposed=proposed)
        resolved = three_way_merge_managed(
            base=base,
            current=current,
            proposed=proposed,
            manual_values={"story_state.location": "人工值"},
        )

        self.assertFalse(conflicted.ok)
        self.assertEqual("concurrent_managed_edit", conflicted.conflicts[0]["code"])
        self.assertTrue(resolved.ok)
        self.assertEqual("人工值", resolved.projection["values"]["story_state.location"])

    def test_tombstone_prevents_old_or_proposed_value_from_resurrecting(self) -> None:
        field = "foreshadowing.fs-old"
        base = projection(owned=(field,), values={field: "open"})
        current = projection(
            revision="current",
            owned=(field,),
            values={},
            tombstones=({"field_path": field, "reason": "manual_delete", "superseded_by": "foreshadowing.fs-new"},),
        )
        proposed = projection(revision="next", owned=(field,), values={field: "developing"})

        result = three_way_merge_managed(base=base, current=current, proposed=proposed)

        self.assertTrue(result.ok)
        self.assertNotIn(field, result.projection["values"])
        self.assertEqual("foreshadowing.fs-new", result.projection["tombstones"][0]["superseded_by"])

    def test_proposed_writer_cannot_modify_unowned_field(self) -> None:
        proposed = projection(owned=("story_state.location",), values={"characters.alice.status": "changed"})

        result = three_way_merge_managed(base=None, current=None, proposed=proposed)

        self.assertFalse(result.ok)
        self.assertEqual("managed_field_not_owned", result.conflicts[0]["code"])

    def test_randomized_projection_round_trip(self) -> None:
        rng = random.Random(20260713)
        for index in range(100):
            field = "timeline." + "".join(rng.choice(string.ascii_lowercase) for _ in range(10))
            value = {"index": index, "text": "值-" + str(rng.randrange(10_000)), "active": bool(index % 2)}
            item = projection(revision=f"random-{index}", owned=(field,), values={field: value})
            parsed = parse_managed_block(write_managed_block(b"# Manual\n", item))
            self.assertEqual(item, parsed.projection)


if __name__ == "__main__":
    unittest.main()
