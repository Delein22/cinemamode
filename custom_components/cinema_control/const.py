"""Shared constants for the cinema control integration."""

DOMAIN = "cinema_control"
VERSION = "0.2.33"
PANEL_PATH = "kinosteuerung"
STORAGE_VERSION = 1
PROVIDERS = {
    "netflix": {"name": "Netflix", "package": "com.netflix.ninja"},
    "disney": {"name": "Disney+", "package": "com.disney.disneyplus"},
    "prime": {"name": "Prime Video", "package": "com.amazon.avod.thirdpartyclient"},
    "plex": {"name": "Plex", "package": "com.plexapp.android"},
    "apple": {"name": "Apple TV+", "package": "com.apple.atve.amazon.appletv"},
    "youtube": {"name": "YouTube", "package": "com.amazon.firetv.youtube"},
}
SAMSUNG_PLEX_APP_ID = "3201512006963"
READY_STATES = {"on", "idle", "playing", "paused", "buffering"}
OFF_STATES = {"off", "standby"}
WAKE_TIMEOUT = 30
SOURCE_TIMEOUT = 12
FEEDBACK_TIMEOUT = 10
PROFILE_TIMEOUT = 120
PRIME_PROFILE_DELAY = 12
PRIME_CONTINUE_DELAY = 20
PRIME_DELAY_MAX = 90
SERVICE_TIMEOUT = 10

