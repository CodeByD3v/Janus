"""language.py — detect programming language from file extension."""

_EXTENSION_MAP = {
    ".py": "Python",
    ".js": "JavaScript",
    ".ts": "TypeScript",
    ".jsx": "JavaScript (JSX)",
    ".tsx": "TypeScript (TSX)",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".rb": "Ruby",
    ".cs": "C#",
    ".cpp": "C++",
    ".c": "C",
    ".swift": "Swift",
    ".php": "PHP",
}

def detect_language(filename: str) -> str:
    """Detect language from file extension. Returns 'Unknown' if unrecognized."""
    from pathlib import Path
    ext = Path(filename).suffix.lower()
    return _EXTENSION_MAP.get(ext, "Unknown")
