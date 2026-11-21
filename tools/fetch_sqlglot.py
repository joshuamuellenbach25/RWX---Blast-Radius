"""Download sqlglot at the pinned commit and unpack it into a folder.

Usage: python fetch_sqlglot.py <target_dir>
"""

import io
import sys
import tarfile
import urllib.request
from pathlib import Path

PINNED_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
ARCHIVE_URL = f"https://github.com/tobymao/sqlglot/archive/{PINNED_COMMIT}.tar.gz"


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: fetch_sqlglot.py <target_dir>")
    target = Path(sys.argv[1])
    target.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(ARCHIVE_URL, timeout=120) as response:
        archive_bytes = response.read()
    prefix = f"sqlglot-{PINNED_COMMIT}/"
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        members = []
        for member in archive.getmembers():
            if not member.name.startswith(prefix):
                continue
            member.name = member.name[len(prefix):]
            if member.name:
                members.append(member)
        archive.extractall(target, members=members, filter="data")
    if not (target / "sqlglot" / "__init__.py").is_file():
        sys.exit(f"sqlglot package missing under {target} after unpacking")


if __name__ == "__main__":
    main()
