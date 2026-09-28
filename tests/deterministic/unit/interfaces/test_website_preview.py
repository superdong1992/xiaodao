"""Browser onboarding samples must remain valid public API responses."""
import json
from pathlib import Path
import subprocess

from problem_locator.interfaces.agent_http import ConversationDetailResponse


def test_offline_report_preview_samples_follow_the_actual_response_contract():
    root = Path(__file__).resolve().parents[4]
    result = subprocess.run(
        ["node", "--input-type=module", "-e",
         "import {previewSamples} from './examples/website-agent/preview.mjs';"
         "process.stdout.write(JSON.stringify(previewSamples()));"],
        cwd=root, capture_output=True, check=True, text=True, encoding="utf-8", timeout=15,
    )
    samples = json.loads(result.stdout)
    assert len(samples) == 9
    assert [sample["label"] for sample in samples[:8]] == [
        "完整结果", "部分结果", "暂无法确定", "等待结果", "未生成报告", "归档状态未知", "通用 Markdown", "历史报告",
    ]
    assert samples[8]["label"] == "旧报告追问"
    assert samples[8]["followup_snapshot_status"] == "UNAVAILABLE"
    for sample in samples:
        envelope = sample["response"]
        assert envelope["ok"] is True and envelope["error"] is None
        ConversationDetailResponse.model_validate_json(json.dumps(envelope["data"]))
