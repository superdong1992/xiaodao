"""Browser onboarding samples must remain valid public API responses."""
import json
from pathlib import Path
import subprocess

from problem_locator.agent.models import ConversationReportView


def test_offline_report_preview_samples_follow_the_actual_response_contract():
    root = Path(__file__).resolve().parents[4]
    result = subprocess.run(
        ["node", "--input-type=module", "-e",
         "import {previewSamples} from './examples/website-agent/preview.mjs';"
         "process.stdout.write(JSON.stringify(previewSamples()));"],
        cwd=root, capture_output=True, check=True, text=True, encoding="utf-8", timeout=15,
    )
    samples = json.loads(result.stdout)
    assert len(samples) == 8
    for sample in samples:
        envelope = sample["response"]
        assert envelope["ok"] is True and envelope["error"] is None
        ConversationReportView.model_validate_json(json.dumps(envelope["data"]))
