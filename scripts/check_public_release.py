"""Fail on common accidental leaks in a curated, public source export.

This is defense in depth, *not* a comprehensive secret scanner. Review staged
files and the Git history manually before publishing to a public repository.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRET_MARKERS = (
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("AWS key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("Google API key", re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b")),
    ("Telegram bot token", re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b")),
    ("Private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("Personal machine path", re.compile(r"[A-Za-z]:\\(?:Users|Documents and Settings)\\", re.I)),
    ("Account Google Alerts feed URL", re.compile(r"https?://www\.google\.com/alerts/feeds/\d{5,}/\d+", re.I)),
    ("Bearer credential", re.compile(r"\bBearer\s+[A-Za-z0-9._-]{24,}\b", re.I)),
)
FORBIDDEN_NAMES = {".env", "feeds.local.yaml", "credentials.json", "secrets.json", "id_rsa", "id_ed25519"}
FORBIDDEN_COMPONENTS = {"data", ".venv", "venv", "__pycache__", "backups", "reports", "exports"}
FORBIDDEN_EXTS = {".db", ".sqlite", ".sqlite3", ".log", ".rar", ".7z", ".zip", ".pem", ".p12", ".key", ".jsonl"}


def files_for_review() -> list[Path]:
    """Use index if available; otherwise check all files in the export directory."""
    if (ROOT / ".git").exists():
        proc = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
        )
        return [ROOT / Path(item.decode("utf-8")) for item in proc.stdout.split(b"\0") if item]
    return sorted(path for path in ROOT.rglob("*") if path.is_file())


def check() -> list[str]:
    findings: list[str] = []
    for absolute in files_for_review():
        rel = absolute.relative_to(ROOT)
        parts = set(rel.parts)
        if rel.name in FORBIDDEN_NAMES or parts.intersection(FORBIDDEN_COMPONENTS):
            findings.append(f"Forbidden path: {rel}")
            continue
        if rel.suffix.lower() in FORBIDDEN_EXTS or rel.name.startswith(".env.") and rel.name != ".env.example":
            findings.append(f"Sensitive-looking file type: {rel}")
            continue
        if rel.name.endswith((".db-wal", ".db-shm", ".sqlite-wal", ".sqlite-shm")):
            findings.append(f"Database state file: {rel}")
            continue
        if absolute.stat().st_size > 1024 * 1024:
            findings.append(f"Unexpected file larger than 1 MiB: {rel}")
            continue
        try:
            data = absolute.read_text("utf-8")
        except UnicodeError:
            findings.append(f"Unexpected binary/non-UTF8 file: {rel}")
            continue
        for label, regex in SECRET_MARKERS:
            if regex.search(data):
                findings.append(f"{label}: {rel} (value intentionally not printed)")
        # Known external examples may use *.example.com. Other address literals
        # deserve deliberate review before distributing a general-purpose export.
        for match in re.finditer(r"(?i)\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}\b", data):
            address = match.group().lower()
            if not address.endswith(("@example.com", "@example.org", "@example.net")):
                findings.append(f"Non-example email address: {rel} (value intentionally not printed)")
                break
    return findings


if __name__ == "__main__":
    findings = check()
    if findings:
        print("Public-release review FAILED:\n- " + "\n- ".join(findings))
        sys.exit(1)
    print(f"Public-release checks passed ({len(files_for_review())} files reviewed).")
    print("Manual review of staged files and Git history is still required.")
