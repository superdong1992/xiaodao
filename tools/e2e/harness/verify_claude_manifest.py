import hashlib
import json
from pathlib import Path

manifest = json.loads(
    Path("/evidence/claude-2.1.150-manifest.json").read_text(encoding="utf-8")
)
if manifest.get("version") not in (None, "2.1.150"):
    raise SystemExit("unexpected manifest version")
expected = manifest["platforms"]["linux-x64"]["checksum"]
actual = hashlib.sha256(Path("/cache/claude-2.1.150").read_bytes()).hexdigest()
frozen = "6c086a0f5fbf684d4148bb69629268b4f5109498c1a7be757acf18c51fd04f4b"
if expected != frozen or actual != expected:
    raise SystemExit("Claude checksum mismatch")
print("manifest_checksum=" + expected)
print("cache_checksum=" + actual)
print("checksum_validation=pass")
