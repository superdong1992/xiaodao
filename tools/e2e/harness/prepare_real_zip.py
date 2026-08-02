import base64
import hashlib
import zipfile
from pathlib import Path

source = Path(
    "/opt/src/xiaodao/tests/fixtures/components/logparse/real/"
    "synthetic-rpc-service-takeover.zip.b64"
)
target = Path("/evidence/synthetic-rpc-service-takeover.zip")
raw = source.read_bytes()
source_sha256 = hashlib.sha256(raw).hexdigest()
if source_sha256 != "9161aa8ede7d6bc7cedefd9588b94dc5370112c44394911092dc6cd7da7c1f9e":
    raise SystemExit("unexpected base64 fixture SHA-256")
ascii_whitespace = frozenset(b" \t\r\n\v\f")
normalized = bytes(octet for octet in raw if octet not in ascii_whitespace)
payload = base64.b64decode(normalized, validate=True)
payload_sha256 = hashlib.sha256(payload).hexdigest()
if len(payload) != 2367:
    raise SystemExit("unexpected decoded ZIP size")
if payload_sha256 != "194f69fecd8dc8d40d1aedeb6fc25d2b7b4922b176be2b15be73ffe386cc5064":
    raise SystemExit("unexpected decoded ZIP SHA-256")
target.write_bytes(payload)
with zipfile.ZipFile(target) as archive:
    corrupt_member = archive.testzip()
    members = archive.namelist()
if corrupt_member is not None:
    raise SystemExit("corrupt ZIP member: " + corrupt_member)
print("source_sha256=" + source_sha256)
print("strict_base64_validation=pass")
print("zip_validation=pass")
print("zip_size=" + str(len(payload)))
print("zip_sha256=" + payload_sha256)
print("zip_member_count=" + str(len(members)))
