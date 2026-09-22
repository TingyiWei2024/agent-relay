"""Read the config digest from a single-image `docker image save` stream.

Docker's containerd store exposes an index digest through image inspect, whereas
Kubernetes CRI reports the config digest. The Docker archive identifies the
config for the exact locally built tag without contacting a registry.
"""

import json
import re
import sys
import tarfile

manifest = None
with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as archive:
    for member in archive:
        if member.name == "manifest.json":
            manifest = json.load(archive.extractfile(member))
# Drain the pipe so docker save never receives a premature broken pipe.
while sys.stdin.buffer.read(1024 * 1024):
    pass
if not manifest or len(manifest) != 1:
    raise SystemExit("Expected exactly one image in the Docker archive")
match = re.fullmatch(r"(?:blobs/sha256/)?([0-9a-f]{64})(?:\.json)?", manifest[0]["Config"])
if match is None:
    raise SystemExit("Docker archive has an unrecognized config digest")
print("sha256:" + match.group(1))

