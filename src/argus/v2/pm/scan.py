"""Deterministic PM diff scan before opening PRs."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    severity: str
    rule: str
    detail: str


_RULES = [
    ("CRITICAL", "secret-key", "private key material added",
     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("CRITICAL", "secret-aws", "AWS access key id added",
     re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("CRITICAL", "secret-token", "sk- style API key added",
     re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("CRITICAL", "secret-literal", "hardcoded credential literal added",
     re.compile(r"(password|secret|api[_-]?key|token)\s*[:=]\s*[\"'][^\"' $<][^\"' ]{7,}",
                re.IGNORECASE)),
    ("WARN", "dangerous-eval", "eval( call added",
     re.compile(r"\beval\s*\(")),
    ("WARN", "dangerous-shell", "subprocess shell=True added",
     re.compile(r"shell\s*=\s*True")),
    ("WARN", "dangerous-exec", "child_process.exec added",
     re.compile(r"child_process\.(exec|execSync)\s*\(")),
    ("WARN", "dangerous-chmod", "chmod 777 added",
     re.compile(r"chmod\s+(-R\s+)?0?777")),
]


def scan_diff(diff_text: str) -> list[Finding]:
    added = "\n".join(
        line[1:] for line in (diff_text or "").splitlines()
        if line.startswith("+") and not line.startswith("+++ ")
    )
    if not added:
        return []
    findings: list[Finding] = []
    for severity, rule, detail, pattern in _RULES:
        if pattern.search(added):
            findings.append(Finding(severity, rule, detail))
    return findings


def has_critical(findings: list[Finding]) -> bool:
    return any(f.severity == "CRITICAL" for f in findings)


def format_findings(findings: list[Finding]) -> str:
    return "; ".join(f"{f.severity}\t{f.rule}\t{f.detail}" for f in findings)
