"""Match known streaming apps to the selected device's reported source names."""

from __future__ import annotations

import unicodedata

from .const import PROVIDERS
from .models import CinemaError

_ALIASES = {
    "netflix": ("Netflix",),
    "disney": ("Disney+", "Disney", "Disney Plus", "DisneyPlus"),
    "prime": ("Prime Video", "Amazon Prime Video", "Amazon Video"),
    "apple": ("Apple TV", "Apple TV+", "Apple TV Plus"),
    "youtube": ("YouTube",),
    "plex": ("Plex",),
}


def _name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).casefold()
    return "".join(char for char in value if not unicodedata.category(char).startswith("M")
                   and (char.isalnum() or char == "+"))


_NORMALIZED = {provider: frozenset(map(_name, aliases)) for provider, aliases in _ALIASES.items()}


def source_matches(provider: str, sources: object) -> list[str]:
    """Return distinct exact device strings; never match partial names or invented IDs."""
    aliases = _NORMALIZED.get(provider) if isinstance(provider, str) else None
    if not aliases or not isinstance(sources, (list, tuple)) or len(sources) > 512:
        return []
    matches = []
    for source in sources:
        if (isinstance(source, str) and 0 < len(source) <= 160 and source.isprintable()
                and _name(source) in aliases and source not in matches):
            matches.append(source)
    return matches


def resolve_auto_source(provider: str, sources: object, device_name: str = "dem gewählten Gerät") -> str:
    """Resolve only one reported source. Missing or ambiguous data cannot send a command."""
    label = PROVIDERS.get(provider, {}).get("name") if isinstance(provider, str) else None
    if label is None:
        raise CinemaError("Für diese App ist keine automatische Quellenzuordnung verfügbar.", "app_source_unavailable")
    matches = source_matches(provider, sources)
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise CinemaError(
            f"Für {label} meldet {device_name} mehrere passende Quellen. "
            "Wähle unter Raum bearbeiten → Streaming-Apps → Erweiterte Einstellungen einmal die gewünschte Quelle.",
            "app_source_ambiguous",
        )
    raise CinemaError(
        f"Für {label} meldet {device_name} noch keine passende App-Quelle. "
        "Schalte das Gerät ein und prüfe seine Quellen; alternativ kannst du die Quelle in den erweiterten App-Einstellungen auswählen.",
        "app_source_unavailable",
    )
