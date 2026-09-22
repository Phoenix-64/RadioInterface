"""Configuration constants for the CAT bridge."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Immutable settings container."""

    # Rigctld connection
    RIG_HOST: str = "192.168.1.18"
    RIG_PORT: int = 4532

    # SDRConnect WebSocket URL
    SDR_WS_URL: str = "ws://127.0.0.1:5454/"

    # WaveLog API v2 (token needs scope radio:write)
    WAVELOG_URL: str = "https://log.hb9hih.com"
    WAVELOG_TOKEN: str = ""
    WAVELOG_RADIO_NAME: str = "SDRConnect"
    WAVELOG_MIN_INTERVAL: float = 0.5  # seconds between pushes

    # Polling intervals
    POLL_INTERVAL: float = 0.1  # seconds
    TUNE_DURATION: int = 5       # seconds

    # SDR property names
    FREQ_PROP: str = "device_vfo_frequency"
    MODE_PROP: str = "demodulator"
    AUDIO_MUTE_PROP: str = "audio_mute"
    SIGNAL_POWER_PROP: str = "signal_power"


# Global settings instance
settings = Settings()