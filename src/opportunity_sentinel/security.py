import re
from dataclasses import dataclass, field

INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(all\s+)?(previous|prior|system)\s+instructions?", re.I),
    re.compile(r"reveal\s+(the\s+)?(system prompt|api keys?|secrets?)", re.I),
    re.compile(r"mark\s+(this\s+)?opportunit(?:y|ies)\s+as\s+verified", re.I),
    re.compile(r"(?:execute|run)\s+(?:this\s+)?(?:code|command|script)", re.I),
    re.compile(r"تجاهل\s+(كل\s+)?التعليمات", re.I),
    re.compile(r"اكشف\s+(مفاتيح|كلمة|التعليمات)", re.I),
)


@dataclass(frozen=True)
class SecurityScan:
    safe: bool
    matches: list[str] = field(default_factory=list)


def scan_untrusted_content(content: str) -> SecurityScan:
    matches = [pattern.pattern for pattern in INJECTION_PATTERNS if pattern.search(content)]
    return SecurityScan(safe=not matches, matches=matches)
