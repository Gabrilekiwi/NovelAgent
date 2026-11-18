"""Offline page-script state regression; real browser verification is separate."""
import hashlib,json,shutil,subprocess,unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

@unittest.skipUnless(shutil.which('node'),'Node is needed for the offline page-script regression; browser acceptance remains separate')
class ReviewedBatchPageStateTest(unittest.TestCase):
    def test_accepted_resume_survives_busy_reads_until_completion_without_duplicate_post(self):
        result=subprocess.run([shutil.which('node'),str(ROOT/'tests/js/reviewed_batches_task_state.cjs')],
            cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
        evidence=json.loads(result.stdout)
        self.assertEqual(evidence['source_sha256'],hashlib.sha256((ROOT/'workbench/static/reviewed-batches.js').read_bytes()).hexdigest())
        self.assertTrue(evidence['production_asset'])
        self.assertTrue(evidence['readonly_busy_retry_and_completion_passed'])
        self.assertEqual(sum(r['method']=='POST' for r in evidence['simulated_requests']),1)
        self.assertEqual(evidence['final_scheduled_polls'],0)
        self.assertIn('已完成',evidence['final_visible_status'])
        self.assertEqual(evidence['real_http_requests'],0)
        self.assertEqual(evidence['real_provider_calls'],0)
        self.assertFalse(evidence['browser_verified'])
