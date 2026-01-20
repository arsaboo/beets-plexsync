"""Configuration management for Harmony."""

from typing import Optional, Dict, Any
from pathlib import Path
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
import yaml
import logging
import os

# Suppress python-dotenv warnings for YAML configuration files
os.environ.setdefault("DOTENV_PROPAGATE_WARNINGS", "false")

logger = logging.getLogger(__name__)


class PlexConfig(BaseModel):
    """Plex server configuration."""

    host: str = "localhost"
    port: int = 32400
    token: str
    library_name: str = "Music"
    verify_ssl: bool = True


class BeetsConfig(BaseModel):
    """Beets library configuration."""

    library_db: Optional[str] = None  # Path to musiclibrary.blb


class AudioMuseConfig(BaseModel):
    """AudioMuse backend configuration for acoustic enrichment."""

    base_url: str = "http://localhost:8001"
    enabled: bool = False
    timeout: int = 30
    acoustic_enrichment: bool = True
    cache_ttl_days: int = 7


class LLMSearchConfig(BaseModel):
    """LLM search provider configuration."""

    searxng_host: Optional[str] = None
    exa_api_key: Optional[str] = None
    brave_api_key: Optional[str] = None
    tavily_api_key: Optional[str] = None


class SpotifyConfig(BaseModel):
    """Spotify provider configuration."""

    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    redirect_uri: Optional[str] = None
    scopes: Optional[str] = None
    cache_path: Optional[str] = None


class ListenBrainzConfig(BaseModel):
    """ListenBrainz provider configuration."""

    token: Optional[str] = None
    username: Optional[str] = None


class ProvidersConfig(BaseModel):
    """External service providers configuration."""

    spotify: SpotifyConfig = Field(default_factory=SpotifyConfig)
    listenbrainz: ListenBrainzConfig = Field(default_factory=ListenBrainzConfig)
    audiomuse: AudioMuseConfig = Field(default_factory=AudioMuseConfig)
    m3u8_dir: Optional[str] = None


class LLMConfig(BaseModel):
    """LLM configuration."""

    enabled: bool = False
    provider: str = "ollama"  # ollama, openai, etc
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    ollama_host: str = "http://localhost:11434"
    model: str = "qwen3:latest"
    use_llm_search: bool = False  # Enable LLM metadata cleanup/enhancement in search
    search: LLMSearchConfig = Field(default_factory=LLMSearchConfig)


class SmartPlaylistConfig(BaseModel):
    """Smart playlist configuration."""

    name: str
    type: str  # daily_discovery, forgotten_gems, etc.
    enabled: bool = True
    num_tracks: int = 50


class PlaylistDefaults(BaseModel):
    """Playlist configuration defaults."""

    max_tracks: int = 20
    manual_search: bool = False


class PlaylistItemConfig(BaseModel):
    """Individual playlist configuration - supports both smart and imported playlists."""

    id: str
    name: str
    type: Optional[str] = None  # Type of playlist: smart type (daily_discovery, forgotten_gems, etc.) or "imported"
    enabled: bool = True
    
    # Smart playlist fields
    num_tracks: Optional[int] = None
    max_tracks: Optional[int] = None  # Alias for num_tracks
    filters: Optional[Dict[str, Any]] = None
    history_days: Optional[int] = None
    exclusion_days: Optional[int] = None
    discovery_ratio: Optional[int] = None
    
    # Imported playlist fields
    sources: Optional[list[str]] = None  # URLs for imported playlists
    manual_search: Optional[bool] = None  # Override default manual_search for this playlist
    
    def model_post_init(self, __context) -> None:
        """Auto-detect playlist type if not specified."""
        if self.type is None:
            # If sources are provided, it's an imported playlist
            if self.sources:
                self.type = "imported"
            # Otherwise, assume it's a smart playlist and use the id as the type
            else:
                self.type = self.id


class PlaylistsConfig(BaseModel):
    """Playlist configuration."""

    defaults: PlaylistDefaults = Field(default_factory=PlaylistDefaults)
    items: list[PlaylistItemConfig] = Field(default_factory=list)
    smart: list[SmartPlaylistConfig] = Field(default_factory=list)  # Keep for backward compatibility


class CacheConfig(BaseModel):
    """Cache configuration options."""

    enabled: bool = True
    db_path: str = "harmony_cache.db"
    negative_cache_ttl: int = 30  # Days until negative cache entries expire
    playlist_cache_ttl: int = 168  # Hours until playlist cache entries expire
    auto_cleanup: bool = True
    clear_old_format_on_startup: bool = False


class SearchConfig(BaseModel):
    """Search configuration options."""


    use_llm_cleanup: bool = False  # Use LLM to clean metadata and retry
    use_manual_confirmation: bool = False  # Ask user to confirm ambiguous matches
    similarity_threshold: float = 0.7  # Threshold for auto-accepting matches (0.0-1.0)


class HarmonyConfig(BaseSettings):
    """Main Harmony configuration."""

    plex: PlexConfig
    beets: BeetsConfig = Field(default_factory=BeetsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    playlists: PlaylistsConfig = Field(default_factory=PlaylistsConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)

    class Config:
        """Pydantic settings config."""

        # Don't use env_file - we load YAML manually via from_file()
        env_ignore_empty = True

    @classmethod
    def from_file(cls, config_path: str | Path = "harmony.yaml") -> "HarmonyConfig":
        """Load configuration from YAML file."""
        config_path = Path(config_path)

        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with open(config_path, "r") as f:
            config_dict = yaml.safe_load(f) or {}

        # Extract top-level keys
        plex_config = config_dict.get("plex", {})
        beets_config = config_dict.get("beets", {})
        providers_config = config_dict.get("providers", {})
        llm_config = config_dict.get("llm", {})
        playlists_config = config_dict.get("playlists", {})
        cache_config = config_dict.get("cache", {})
        search_config = config_dict.get("search", {})

        return cls(
            plex=PlexConfig(**plex_config),
            beets=BeetsConfig(**beets_config) if beets_config else BeetsConfig(),
            providers=ProvidersConfig(**providers_config) if providers_config else ProvidersConfig(),
            llm=LLMConfig(**llm_config) if llm_config else LLMConfig(),
            playlists=PlaylistsConfig(**playlists_config) if playlists_config else PlaylistsConfig(),
            cache=CacheConfig(**cache_config) if cache_config else CacheConfig(),
            search=SearchConfig(**search_config) if search_config else SearchConfig(),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert configuration to dictionary."""
        return {
            "plex": self.plex.model_dump(),
            "beets": self.beets.model_dump(),
            "providers": self.providers.model_dump(),
            "llm": self.llm.model_dump(),
            "playlists": self.playlists.model_dump(),
            "search": self.search.model_dump(),
            "cache": self.cache.model_dump(),
        }

    def to_yaml(self) -> str:
        """Convert configuration to YAML string."""
        return yaml.dump(self.to_dict(), default_flow_style=False)

    def save(self, path: str | Path = "harmony.yaml") -> None:
        """Save configuration to YAML file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, "w") as f:
            f.write(self.to_yaml())

        logger.info(f"Configuration saved to {path}")


def get_config_value(config: HarmonyConfig, path: str, default: Any = None) -> Any:
    """Get a nested config value using dot notation."""
    keys = path.split(".")
    current = config.model_dump()

    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return default

    return current
