"""Scan tracked/staged source without ever printing credential values.

Local baseline checks also compare against configured .env secrets. This is a
defense-in-depth check, not a replacement for provider rotation/host secret scanning.
"""
import argparse
from pathlib import Path
import re
import subprocess
import sys

PATTERNS = [
    re.compile(rb"sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}"),
    re.compile(rb"pplx-[A-Za-z0-9]{20,}"),
    re.compile(rb"AKIA[A-Z0-9]{16}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--working-tree", action="store_true")
    args = parser.parse_args()
    command = ["git", "ls-files", "-z"]
    if args.working_tree:
        command += ["--cached", "--others", "--exclude-standard"]
    paths = set(subprocess.check_output(command).decode().split("\0")) - {""}
    local_secrets = []
    if Path(".env").exists():
        for line in Path(".env").read_text().splitlines():
            name, separator, value = line.partition("=")
            value = value.strip().strip("\"'")
            if separator and re.search(r"KEY|SECRET|PASSWORD|TOKEN", name) and len(value) >= 16:
                local_secrets.append(value.encode())
    failures = []
    for path in sorted(paths):
        if Path(path).name.startswith(".env") and path != ".env.example":
            failures.append(path)
            continue
        data = Path(path).read_bytes() if args.working_tree else subprocess.check_output(["git", "show", f":{path}"])
        if any(pattern.search(data) for pattern in PATTERNS) or any(secret in data for secret in local_secrets):
            failures.append(path)
    if failures:
        print("Possible credentials detected (values withheld):", *failures, sep="\n")
        return 1
    print(f"Credential check passed for {len(paths)} files; no values displayed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
