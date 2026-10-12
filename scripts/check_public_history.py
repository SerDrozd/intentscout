"""Scan every reachable commit/blob in this sanitized public repository.

This is a targeted leak check, not a substitute for a dedicated secrets scanner
or manual review. It never prints suspect credential values.
"""
from __future__ import annotations

import re
import subprocess
import sys

BAD_PARTS = {"data", "data-v1.1-noise", ".venv", "venv", "__pycache__", "reports", "backups", "exports"}
BAD_NAMES = {".env", "feeds.local.yaml", "credentials.json", "secrets.json", "id_rsa", "id_ed25519"}
BAD_EXT = (".db", ".sqlite", ".sqlite3", ".log", ".rar", ".zip", ".jsonl", ".pem", ".key", ".p12", ".db-wal", ".db-shm")
PATTERNS = {
    "local Windows user path": re.compile(rb"(?i)\b[a-z]:\\Users\\"),
    "OpenAI-style key": re.compile(rb"(?i)\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b"),
    "GitHub token": re.compile(rb"(?i)\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "Telegram bot token": re.compile(rb"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    "AWS key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "Google API key": re.compile(rb"\bAIza[A-Za-z0-9_-]{30,}\b"),
    "private key block": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "authorization bearer": re.compile(rb"(?i)\bBearer\s+[A-Za-z0-9._-]{24,}\b"),
}
SUSPICIOUS_ASSIGNMENT = re.compile(
    rb"(?m)^[ \t]*(?:GITHUB_TOKEN|TELEGRAM_CHAT_ID|TELEGRAM_BOT_TOKEN|OPENAI_API_KEY|GOOGLE_API_KEY|AWS_SECRET_ACCESS_KEY)[ \t]*=[ \t]*[^\s#\r\n]+"
)
EMAIL = re.compile(rb"(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
FEED = re.compile(rb"(?i)https?://(?:www\.)?google\.com/alerts/feeds/[^\s\"'<>`]+")
ALLOWED_EMAILS = (b"@example.com", b"@example.org", b"@example.net", b"@users.noreply.github.com")


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL)


def check() -> tuple[int, int, list[str]]:
    findings: list[str] = []
    commits = git("rev-list", "--all").decode().splitlines()
    objects = git("rev-list", "--objects", "--all").splitlines()
    # Deduplicate objects even when they occur under several paths.
    visited: set[bytes] = set()
    blobs = 0
    for row in objects:
        sha, _, path = row.partition(b" ")
        if path:
            name = path.decode("utf-8", errors="replace")
            parts = name.split("/")
            if any(p in BAD_PARTS for p in parts) or parts[-1] in BAD_NAMES or name.lower().endswith(BAD_EXT):
                findings.append(f"Forbidden historical path: {name}")
        if sha in visited:
            continue
        visited.add(sha)
        kind = git("cat-file", "-t", sha.decode()).decode().strip()
        if kind not in {"blob", "commit"}:
            continue
        data = git("cat-file", kind, sha.decode())
        if kind == "blob":
            blobs += 1
        if b"\0" in data:
            findings.append(f"Unexpected binary Git object: {sha.decode()[:12]}")
            continue
        for label, rx in PATTERNS.items():
            if rx.search(data):
                findings.append(f"{label}: {sha.decode()[:12]} ({kind})")
        if SUSPICIOUS_ASSIGNMENT.search(data):
            findings.append(f"Nonempty credential assignment: {sha.decode()[:12]} ({kind})")
        for email in EMAIL.findall(data):
            if not email.lower().endswith(ALLOWED_EMAILS):
                findings.append(f"Non-example email: {sha.decode()[:12]} ({kind})")
                break
        for url in FEED.findall(data):
            segment = url.split(b"/feeds/", 1)[-1].upper()
            synthetic = (
                any(mark in segment for mark in (b"EXAMPLE", b"YOUR_", b"PLACEHOLDER", b"SAMPLE", b"TEST_"))
                or segment == b"123/456"
            )
            if not synthetic:
                findings.append(f"Account-specific Alerts URL: {sha.decode()[:12]} ({kind})")
                break
        if kind == "commit":
            for line in data.splitlines():
                if line.startswith((b"author ", b"committer ")) and b"@users.noreply.github.com>" not in line:
                    findings.append(f"Non-no-reply commit attribution: {sha.decode()[:12]}")
    return len(commits), blobs, sorted(set(findings))


if __name__ == "__main__":
    try:
        count, blobs, issues = check()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Unable to complete historical Git scan: {type(exc).__name__}", file=sys.stderr)
        sys.exit(2)
    if issues:
        print("Public Git history check FAILED:\n- " + "\n- ".join(issues))
        sys.exit(1)
    print(f"Public Git history check passed: {count} commits, {blobs} blobs across all refs.")
    print("Pattern-based checks do not replace human review.")
