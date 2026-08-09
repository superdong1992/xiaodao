import hashlib
import json
from pathlib import Path


package_root = Path("/opt/claude-code")
metadata = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
if metadata.get("name") != "@anthropic-ai/claude-code":
    raise SystemExit("unexpected Claude npm package name")
if metadata.get("version") != "2.1.89":
    raise SystemExit("unexpected Claude npm package version")
actual = hashlib.sha256((package_root / "cli.js").read_bytes()).hexdigest()
expected = "a9950ef6407fdc750bddb673852485500387e524a99d42385cb81e7d17128e01"
if actual != expected:
    raise SystemExit("Claude npm CLI does not match the frozen hash")
print("package=@anthropic-ai/claude-code")
print("version=2.1.89")
print(f"cli_sha256={actual}")
