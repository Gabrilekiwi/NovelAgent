from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts.real_storyproject_e2e import RealStoryProjectE2EError, _require_opt_in, run_real_storyproject_e2e


class RealStoryProjectE2ETest(unittest.TestCase):
    def test_real_provider_execution_requires_explicit_opt_in(self) -> None:
        with patch.dict(os.environ, {"NOVELAGENT_REAL_STORYPROJECT_E2E": ""}, clear=False):
            with self.assertRaisesRegex(RealStoryProjectE2EError, "real_provider_opt_in_required"):
                _require_opt_in(False)

    @unittest.skipUnless(
        os.getenv("NOVELAGENT_REAL_STORYPROJECT_E2E", "").strip().lower() in {"1", "true", "yes", "on"},
        "set NOVELAGENT_REAL_STORYPROJECT_E2E=1 for the billable real OpenAI StoryProject E2E",
    )
    def test_two_chapter_real_openai_storyproject_e2e(self) -> None:
        sample = os.environ.get("NOVELAGENT_REAL_STORYPROJECT_SAMPLE", "").strip()
        calibration = os.environ.get("NOVELAGENT_REAL_STORYPROJECT_CALIBRATION_REPORT", "").strip()
        self.assertTrue(sample, "NOVELAGENT_REAL_STORYPROJECT_SAMPLE is required")
        self.assertTrue(calibration, "NOVELAGENT_REAL_STORYPROJECT_CALIBRATION_REPORT is required")

        report = run_real_storyproject_e2e(
            sample_path=Path(sample),
            calibration_report_path=Path(calibration),
        )

        self.assertTrue(report["ok"])
        self.assertTrue(report["redacted"])
        self.assertEqual(2, len(report["chapters"]))
        serialized = str(report).lower()
        for forbidden in ("api_key", "prompt", "chapter_text", "provider_response", "story_project_root"):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
