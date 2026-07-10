"""Registry of import parsers, keyed by source_id."""

from dtos.imports import DetectMatch, ImportSourceInfo
from services.imports.base import ImportParser

_PARSERS: dict[str, ImportParser] = {}


def register(cls: type[ImportParser]) -> type[ImportParser]:
    """Class decorator: instantiate and register a parser."""
    instance = cls()
    _PARSERS[instance.source_id] = instance
    return cls


def get_parser(source_id: str) -> ImportParser | None:
    return _PARSERS.get(source_id)


def list_parsers() -> list[ImportSourceInfo]:
    return [
        ImportSourceInfo(
            source_id=p.source_id,
            label=p.label,
            category=p.category.value,
            file_hint=p.file_hint,
            supports_mapping=p.supports_mapping,
        )
        for p in sorted(_PARSERS.values(), key=lambda p: (p.category.value, p.source_id))
    ]


def detect_source(csv_content: str) -> list[DetectMatch]:
    """Score every parser against the CSV headers, best matches first."""
    matches = [
        DetectMatch(source_id=p.source_id, score=score)
        for p in _PARSERS.values()
        if (score := p.detect(csv_content)) > 0
    ]
    matches.sort(key=lambda m: m.score, reverse=True)
    return matches
