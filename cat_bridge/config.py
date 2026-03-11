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

    # WaveLog WebSocket server port
    WAVELOG_WS_PORT: int = 54322

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