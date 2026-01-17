"""Main Harmony application class."""

import logging
from typing import Any, Dict, List, Optional

from harmony.config import HarmonyConfig
from harmony.backends.plex import PlexBackend
from harmony.backends.beets import BeetsBackend
from harmony.core.cache import Cache
from harmony.core.vector_index import VectorIndex
from harmony.models import Track
from harmony.workflows.search import search_backend_song

logger = logging.getLogger("harmony")


class Harmony:
    """Main Harmony application managing backends and features."""

    def __init__(self, config_path: str = "harmony.yaml"):
        """Initialize Harmony.

        Args:
            config_path: Path to harmony.yaml configuration file
        """
        # Load configuration
        self.config = HarmonyConfig.from_file(config_path)

        # Initialize Plex backend (required)
        self.plex = PlexBackend(self.config.plex.model_dump())
        self.backend = self.plex

        # Initialize beets backend (optional - will gracefully skip if beets not installed)
        beets_config = self.config.beets.model_dump()
        self.beets = BeetsBackend(beets_config) if beets_config.get("library_db") else None

        # Initialize cache
        cache_db_path = self.config.cache.db_path if hasattr(self.config, 'cache') else "harmony_cache.db"
        self.cache = Cache(cache_db_path)
        
        # Store cache TTL for use in search operations
        self.negative_cache_ttl = self.config.cache.negative_cache_ttl if hasattr(self.config, 'cache') else 30

        # Initialize vector index
        self.vector_index = VectorIndex()
        self.beets_vector_index = VectorIndex()
        self.beets_lookup: Dict[int, Any] = {}

        # Initialize LLM (optional)
        self.llm = None
        self._llm_search_enabled = False

        # Connection state
        self._initialized = False
        self._vector_index_info: Dict[str, Any] = {}  # Track cache metadata

        # Candidate confirmation queue for manual search
        self._candidate_queue: List[Dict] = []

    def initialize(self, force_refresh: bool = False) -> None:
        """Initialize all backends and build indices.

        Args:
            force_refresh: If True, rebuild vector index from scratch, ignoring cache
        """
        try:
            # Connect to Plex (required)
            self.plex.connect()

            # Connect to beets if available
            if self.beets:
                self.beets.connect()

            # Initialize LLM if configured
            llm_config = self.config.llm.model_dump() if hasattr(self.config, 'llm') else {}
            if llm_config and llm_config.get('enabled', False):
                try:
                    from harmony.ai.llm import create_llm_from_config
                    self.llm = create_llm_from_config(llm_config)
                    self._llm_search_enabled = llm_config.get('use_llm_search', False)
                    if self.llm:
                        logger.info(f"LLM initialized (search enabled: {self._llm_search_enabled})")
                except Exception as e:
                    logger.warning(f"Failed to initialize LLM: {e}")
                    self.llm = None
                    self._llm_search_enabled = False

            # Build vector index from primary backend (Plex)
            # Try to load from cache first (unless force_refresh)
            vector_cache_path = "harmony_vector_index.json"

            if force_refresh:
                logger.info("Force refresh requested - rebuilding vector index")
                self._build_vector_index()
                self.vector_index.save_to_file(vector_cache_path)
                self._save_vector_index_metadata(vector_cache_path)
            elif not self._load_and_validate_cached_vector_index(vector_cache_path):
                # Build new index if cache doesn't exist, failed to load, or is stale
                logger.info("Cache invalid or missing - building fresh vector index")
                self._build_vector_index()
                self.vector_index.save_to_file(vector_cache_path)
                self._save_vector_index_metadata(vector_cache_path)

            self._initialized = True
            logger.info("Harmony initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize Harmony: {e}")
            self._initialized = False
            raise

    def _load_and_validate_cached_vector_index(self, cache_path: str) -> bool:
        """Load cached vector index and validate it's still current.

        Checks if library (beets or Plex) has been modified since cache was created.

        Returns:
            True if cache was loaded and validated successfully, False otherwise
        """
        try:
            if not self.vector_index.load_from_file(cache_path):
                return False

            # Load metadata from companion file
            self._load_vector_index_metadata(cache_path)
            cached_size = self._vector_index_info.get("plex_size")
            cached_source = self._vector_index_info.get("source", "plex")

            # When beets is configured, validate against beets instead of Plex
            if self.beets and self.beets.connected:
                try:
                    # Get beets track count without loading all tracks
                    # This is much faster than loading all tracks
                    import sqlite3
                    conn = sqlite3.connect(self.beets.library_db)
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM items")
                    current_tracks = cursor.fetchone()[0]
                    conn.close()

                    if cached_size is not None and current_tracks > 0:
                        variance = abs(current_tracks - cached_size) / cached_size
                        if variance > 0.02:  # More than 2% change
                            logger.info(
                                f"Beets library changed (was {cached_size} tracks, now {current_tracks} tracks, {variance*100:.1f}% change) - rebuilding index"
                            )
                            return False
                    
                    logger.info(f"Loaded cached vector index from beets ({len(self.vector_index)} indexed tracks)")
                    return True
                except Exception as e:
                    logger.debug(f"Failed to validate beets cache: {e}, falling back to Plex validation")
                    # Fall through to Plex validation

            # Validate against Plex when beets is not configured
            plex_section = self.plex.music
            if plex_section:
                # Count tracks without forcing a full library scan
                current_tracks = self.plex.get_track_count()
                if current_tracks is None:
                    logger.debug(
                        "Plex track count unavailable; skipping cache validation"
                    )
                    return True

                # If track count differs significantly, cache is stale
                # Allow small variations (±2%) for transient changes
                if cached_size is not None and current_tracks > 0:
                    variance = abs(current_tracks - cached_size) / cached_size
                    if variance > 0.02:  # More than 2% change
                        logger.info(
                            f"Plex library changed (was {cached_size} tracks, now {current_tracks} tracks, {variance*100:.1f}% change) - rebuilding index"
                        )
                        return False

            logger.info(f"Loaded cached vector index ({len(self.vector_index)} indexed tracks)")
            return True
        except Exception as e:
            logger.debug(f"Failed to validate cached vector index: {e}")
        return False

    def _save_vector_index_metadata(self, cache_path: str) -> None:
        """Save vector index metadata to companion file."""
        import json
        from pathlib import Path

        try:
            metadata_path = Path(cache_path).with_suffix('.meta.json')
            with open(metadata_path, 'w') as f:
                json.dump(self._vector_index_info, f)
            logger.debug(f"Saved vector index metadata to {metadata_path}")
        except Exception as e:
            logger.debug(f"Failed to save vector index metadata: {e}")

    def _load_vector_index_metadata(self, cache_path: str) -> None:
        """Load vector index metadata from companion file."""
        import json
        from pathlib import Path

        try:
            metadata_path = Path(cache_path).with_suffix('.meta.json')
            if metadata_path.exists():
                with open(metadata_path, 'r') as f:
                    self._vector_index_info = json.load(f)
                logger.debug(f"Loaded vector index metadata from {metadata_path}")
        except Exception as e:
            logger.debug(f"Failed to load vector index metadata: {e}")
            self._vector_index_info = {}

    def _load_cached_vector_index(self, cache_path: str) -> bool:
        """Deprecated: use _load_and_validate_cached_vector_index instead."""
        return self._load_and_validate_cached_vector_index(cache_path)

    def shutdown(self) -> None:
        """Shutdown all backends."""
        try:
            if self.plex:
                self.plex.disconnect()
            if self.beets:
                self.beets.disconnect()
            self._initialized = False
            logger.info("Harmony shutdown complete")
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")

    def _build_vector_index(self) -> None:
        """Build vector index from Plex tracks, optionally enriched with beets."""
        logger.info("Building vector index...")

        # Build beets lookup + vector index if configured
        self.beets_vector_index = VectorIndex()
        self.beets_lookup = {}
        used_beets_index = False
        if self.beets and self.beets.connected:
            try:
                beets_tracks = self.beets.get_all_tracks()
                logger.debug(f"Loaded {len(beets_tracks)} tracks from beets")
                for track in beets_tracks:
                    metadata = {
                        "title": track.title,
                        "artist": track.artist,
                        "album": track.album,
                        "backend_id": track.plex_ratingkey,
                        "plex_ratingkey": track.plex_ratingkey,
                        "provider_ids": track.metadata.get("provider_ids", {}),
                    }
                    item_id = track.beets_id if track.beets_id is not None else hash(
                        track.title + track.artist
                    )
                    self.beets_vector_index.add_item(item_id, metadata)
                    if track.plex_ratingkey:
                        self.beets_lookup[int(track.plex_ratingkey)] = track

                # Use beets index as primary for search when available.
                self.vector_index = self.beets_vector_index
                used_beets_index = True
                logger.info(
                    f"Vector index built with {len(self.vector_index)} tracks from beets"
                )
            except Exception as e:
                logger.debug(f"Failed to build beets vector index: {e}")

        # Merge in Plex tracks only if beets is not used or incomplete
        # (ensures coverage for items missing in beets)
        actual_track_count = 0
        if not used_beets_index:
            # Only scan Plex if beets is not configured or failed
            plex_tracks = self.plex.get_all_tracks()
            logger.debug(f"Loaded {len(plex_tracks)} tracks from Plex")
            actual_track_count = len(plex_tracks)

            for track in plex_tracks:
                metadata = {
                    "title": track.title,
                    "artist": track.artist,
                    "album": track.album,
                    "backend_id": track.backend_id,
                    "plex_ratingkey": track.plex_ratingkey,
                }
                item_id = track.plex_ratingkey if track.plex_ratingkey else hash(track.title + track.artist)
                self.vector_index.add_item(item_id, metadata)
        else:
            # When using beets, trust beets as the source of truth
            logger.info("Using beets as primary source; skipping full Plex scan")
            actual_track_count = len(self.beets_vector_index)

        # Store metadata for cache validation
        self._vector_index_info = {
            "plex_size": actual_track_count,
            "track_count": len(self.vector_index),
        }

        # Note: metadata will be saved by the caller (refresh_vector_index) along with the index file
        logger.info(f"Vector index built with {len(self.vector_index)} unique tracks from {actual_track_count} Plex tracks")

    def get_local_beets_candidates(self, song: Dict[str, str]) -> List[Any]:
        """Get local beets candidates from vector index.

        Returns list of LocalCandidate-like objects with:
        - metadata: Dict with title, artist, album
        - score: Similarity score
        - overlap_tokens: List of matching tokens
        """
        if not self.beets or not self.beets.connected:
            return []

        try:
            # Build query vector
            query_counts, query_norm = self.vector_index.build_query_vector(song)

            # Get candidates from vector index
            candidates = self.vector_index.candidate_scores(
                query_counts, query_norm, limit=5, min_score=0.35
            )

            # Convert to local candidate format
            result = []
            for entry, score in candidates:
                # Create a simple candidate object
                candidate = type(
                    "LocalCandidate",
                    (),
                    {
                        "metadata": dict(entry.metadata),
                        "score": score,
                        "overlap_tokens": entry.overlap_tokens(query_counts),
                        "song_dict": lambda: {
                            "title": entry.metadata.get("title", ""),
                            "artist": entry.metadata.get("artist", ""),
                            "album": entry.metadata.get("album", ""),
                        },
                    },
                )()
                result.append(candidate)

            logger.debug(f"Found {len(result)} beets candidates for {song}")
            return result

        except Exception as e:
            logger.debug(f"Failed to get beets candidates: {e}")
            return []

    def search_plex(
        self,
        title: Optional[str] = None,
        artist: Optional[str] = None,
        album: Optional[str] = None,
        limit: int = 50,
    ) -> List[Track]:
        """Search for tracks in Plex library.

        Args:
            title: Track title
            artist: Artist name
            album: Album name
            limit: Maximum number of results

        Returns:
            List of Track objects
        """
        if not self._initialized or not self.plex.connected:
            logger.error("Harmony not initialized")
            return []

        return self.plex.search_tracks(
            title=title, artist=artist, album=album, limit=limit
        )

    def search_plex_song(
        self,
        song: Dict[str, str],
        manual_search: bool = False,
        use_local_candidates: bool = True,
    ) -> Optional[Dict]:
        """Search for a track using the advanced 5-stage pipeline.

        This uses the full search pipeline with:
        1. Cache lookup
        2. Local vector index candidates (with direct ratingKey matching)
        3. Multi-strategy backend search (6 strategies)
        4. LLM enhancement (if enabled)
        5. Manual search with candidate confirmation queue

        Args:
            song: Dict with 'title', 'artist', 'album' keys
            manual_search: Whether to prompt for manual confirmation
            use_local_candidates: Whether to use vector index for fast matching

        Returns:
            Dict with track metadata or None if not found
        """
        if not self._initialized or not self.plex.connected:
            logger.error("Harmony not initialized")
            return None

        # Clear candidate queue before search
        self._candidate_queue.clear()

        # Import matching module for manual search
        import harmony.core.matching as matching_module

        return search_backend_song(
            backend=self.backend,
            cache=self.cache,
            vector_index=self.vector_index,
            beets_vector_index=self.beets_vector_index if self.beets else None,
            beets_lookup=self.beets_lookup if self.beets else None,
            song=song,
            manual_search=manual_search,
            use_local_candidates=use_local_candidates,
            llm_agent=self.llm if self._llm_search_enabled else None,
            candidate_queue=self._candidate_queue,
            matching_module=matching_module,
            harmony_app=self,  # Pass self for incremental refresh support
        )

    def search_song(
        self,
        song: Dict[str, str],
        manual_search: bool = False,
        use_local_candidates: bool = True,
    ) -> Optional[Dict]:
        """Backend-agnostic search for a track using the advanced pipeline."""
        return self.search_plex_song(
            song,
            manual_search=manual_search,
            use_local_candidates=use_local_candidates,
        )

    def get_plex_track(self, rating_key: int) -> Optional[Track]:
        """Get a track from Plex by rating key."""
        if not self._initialized or not self.plex.connected:
            logger.error("Harmony not initialized")
            return None

        return self.plex.get_track(str(rating_key))

    def get_plex_object(self, rating_key: int) -> Optional[Any]:
        """Get raw Plex track object by rating key."""
        if not self._initialized or not self.plex.connected:
            logger.error("Harmony not initialized")
            return None

        return self.plex.get_plex_track(str(rating_key))

    def is_initialized(self) -> bool:
        """Check if Harmony is initialized."""
        return self._initialized

    def refresh_vector_index(self) -> None:
        """Manually refresh the vector index (useful after importing new tracks).

        This rebuilds the vector index from the current Plex library,
        useful when you've added tracks during an import session.
        """
        if not self._initialized:
            logger.error("Harmony not initialized")
            return

        logger.info("Refreshing vector index...")
        self.vector_index = VectorIndex()  # Clear existing
        self._build_vector_index()

        # Save updated cache
        vector_cache_path = "harmony_vector_index.json"
        self.vector_index.save_to_file(vector_cache_path)
        self._save_vector_index_metadata(vector_cache_path)
        logger.info("Vector index refresh complete")

    def incremental_refresh_vector_index(self, limit: int = 100) -> int:
        """Incrementally refresh the vector index with new tracks from Plex.

        This is much faster than a full refresh since it only adds new tracks
        that aren't already in the index. Perfect for mid-import refreshes.

        Args:
            limit: Maximum number of new tracks to fetch and index

        Returns:
            Number of new tracks added to the index
        """
        if not self._initialized:
            logger.error("Harmony not initialized")
            return 0

        logger.info(f"Incrementally refreshing vector index (checking up to {limit} recent tracks)...")
        
        try:
            # Get recently added tracks from Plex using the native API
            if not self.plex.music_library:
                logger.error("Plex music library not available")
                return 0
            
            # Use Plex's recentlyAdded() to get newest tracks
            recent_plex_tracks = self.plex.music_library.recentlyAdded(maxresults=limit)
            
            if not recent_plex_tracks:
                logger.debug("No recent tracks found")
                return 0
            
            # Track how many new items we add
            added_count = 0
            
            for plex_track in recent_plex_tracks:
                # Check if track is already in the index
                rating_key = getattr(plex_track, "ratingKey", None)
                if rating_key and rating_key in self.vector_index._entries:
                    continue  # Already indexed
                
                # Add to vector index
                artist_name = self.plex._get_artist_name(plex_track) if hasattr(self.plex, '_get_artist_name') else getattr(plex_track, "grandparentTitle", "Unknown Artist")
                metadata = {
                    "title": getattr(plex_track, "title", ""),
                    "artist": artist_name,
                    "album": getattr(plex_track, "parentTitle", ""),
                    "backend_id": rating_key,
                    "plex_ratingkey": rating_key,
                }
                item_id = rating_key if rating_key else hash(metadata["title"] + metadata["artist"])
                
                if self.vector_index.upsert_item(item_id, metadata):
                    added_count += 1
                    logger.debug(f"Added to index: {metadata['artist']} - {metadata['title']}")
            
            if added_count > 0:
                logger.info(f"Added {added_count} new tracks to vector index")
                
                # Update metadata
                self._vector_index_info["track_count"] = len(self.vector_index)
                
                # Save updated index
                vector_cache_path = "harmony_vector_index.json"
                self.vector_index.save_to_file(vector_cache_path)
                self._save_vector_index_metadata(vector_cache_path)
            else:
                logger.debug("No new tracks to add to index")
            
            return added_count
            
        except Exception as e:
            logger.error(f"Error during incremental refresh: {e}")
            import traceback
            traceback.print_exc()
            return 0

    def add_track_to_index(self, track: Track) -> bool:
        """Add a single track to the vector index.

        Useful for immediately making a newly added track searchable
        without doing a full or incremental refresh.

        Args:
            track: Track object to add to the index

        Returns:
            True if track was added, False otherwise
        """
        if not self._initialized:
            logger.error("Harmony not initialized")
            return False

        try:
            metadata = {
                "title": track.title,
                "artist": track.artist,
                "album": track.album,
                "backend_id": track.backend_id,
                "plex_ratingkey": track.plex_ratingkey,
            }
            item_id = track.plex_ratingkey if track.plex_ratingkey else hash(track.title + track.artist)
            
            if self.vector_index.upsert_item(item_id, metadata):
                logger.debug(f"Added track to index: {track.artist} - {track.title}")
                
                # Update metadata
                self._vector_index_info["track_count"] = len(self.vector_index)
                
                return True
            return False
            
        except Exception as e:
            logger.error(f"Error adding track to index: {e}")
            return False

    # ========== Playlist Methods ==========

    def create_playlist(self, playlist_name: str, tracks: List[Dict]) -> bool:
        """Create a playlist with the given tracks on the active backend.

        Args:
            playlist_name: Name for the playlist
            tracks: List of track dicts with 'plex_ratingkey'

        Returns:
            True if successful
        """
        if not self._initialized or not self.plex.connected:
            logger.error("Harmony not initialized")
            return False

        try:
            self.backend.add_tracks_to_playlist(playlist_name, tracks)
            logger.info(f"Created playlist {playlist_name} with {len(tracks)} tracks")
            return True
        except Exception as e:
            logger.error(f"Error creating playlist: {e}")
            return False

    def generate_smart_playlist(
        self,
        playlist_name: str,
        playlist_type: str = "daily_discovery",
        num_tracks: int = 50,
        filters: Dict = None,
    ) -> Optional[Dict]:
        """Generate a smart playlist.

        Args:
            playlist_name: Name for the playlist
            playlist_type: Type of playlist (daily_discovery, forgotten_gems, recent_hits, etc)
            num_tracks: Number of tracks to include
            filters: Optional filter config (min_rating, min_year, etc)

        Returns:
            Dict with playlist details or None if failed
        """
        if not self._initialized or not self.plex.connected:
            logger.error("Harmony not initialized")
            return None

        try:
            from harmony.plex.smartplaylists import generate_playlist

            def _normalize_genres(value):
                if not value:
                    return []
                if isinstance(value, str):
                    parts = [g.strip() for g in value.replace(";", ",").split(",")]
                    return [g for g in parts if g]
                if isinstance(value, list):
                    return [str(g).strip() for g in value if str(g).strip()]
                return []

            tracks_dicts = []

            if self.beets and self.beets.connected:
                beets_tracks = self.beets.get_all_tracks()
                for track in beets_tracks:
                    if track.plex_ratingkey is None:
                        continue
                    beets_item = track.metadata.get("beets_obj")
                    tracks_dicts.append(
                        {
                            "title": track.title,
                            "artist": track.artist,
                            "album": track.album,
                            "plex_ratingkey": track.plex_ratingkey,
                            "userRating": getattr(beets_item, "plex_userrating", None)
                            if beets_item
                            else None,
                            "viewCount": getattr(beets_item, "plex_viewcount", None)
                            if beets_item
                            else None,
                            "lastViewedAt": getattr(beets_item, "plex_lastviewedat", None)
                            if beets_item
                            else None,
                            "year": getattr(track, "year", None),
                            "genres": _normalize_genres(getattr(track, "genre", None)),
                            "popularity": getattr(track, "popularity", 0),
                        }
                    )

                if filters:
                    include = filters.get("include", {}) if isinstance(filters, dict) else {}
                    exclude = filters.get("exclude", {}) if isinstance(filters, dict) else {}
                    need_genres = bool((include or {}).get("genres") or (exclude or {}).get("genres"))
                    need_years = bool((include or {}).get("years") or (exclude or {}).get("years"))
                    need_years = need_years or ("min_year" in filters or "max_year" in filters)

                    if need_genres or need_years:
                        for track_dict in tracks_dicts:
                            if (need_genres and track_dict.get("genres")) and (need_years and track_dict.get("year")):
                                continue
                            plex_track = self.plex.get_track(str(track_dict.get("plex_ratingkey")))
                            if not plex_track:
                                continue
                            if need_genres and not track_dict.get("genres"):
                                track_dict["genres"] = _normalize_genres(
                                    [g.tag for g in getattr(plex_track, "genres", []) if getattr(g, "tag", None)]
                                )
                            if need_years and not track_dict.get("year"):
                                track_dict["year"] = getattr(plex_track, "year", None)
            else:
                all_tracks = self.plex.get_all_tracks()
                tracks_dicts = [
                    {
                        "title": t.title,
                        "artist": t.artist,
                        "album": t.album,
                        "plex_ratingkey": t.plex_ratingkey,
                        "userRating": getattr(t, "rating", None),
                        "viewCount": getattr(t, "play_count", 0),
                        "lastViewedAt": getattr(t, "last_viewed", None),
                        "year": getattr(t, "year", None),
                        "genres": _normalize_genres(
                            [g.tag for g in getattr(t, "genres", []) if getattr(g, "tag", None)]
                        ),
                        "popularity": getattr(t, "popularity", 0),
                    }
                    for t in all_tracks
                    if t.plex_ratingkey is not None
                ]

            filter_kwargs = dict(filters or {})
            history_days = filter_kwargs.pop("history_days", None)
            exclusion_days = filter_kwargs.pop("exclusion_days", None)
            discovery_ratio = filter_kwargs.pop("discovery_ratio", None)

            # Generate playlist
            result = generate_playlist(
                tracks=tracks_dicts,
                playlist_name=playlist_name,
                num_tracks=num_tracks,
                playlist_type=playlist_type,
                history_days=history_days,
                exclusion_days=exclusion_days,
                discovery_ratio=discovery_ratio,
                **filter_kwargs
            )

            # Add to Plex
            if result.get("tracks"):
                self.create_playlist(playlist_name, result["tracks"])

            return result
        except Exception as e:
            logger.error(f"Error generating smart playlist: {e}")
            return None

    def import_playlist_from_url(
        self,
        playlist_name: str,
        url: str,
        manual_search: bool = False,
        auto_refresh_index: bool = True,
    ) -> int:
        """Import a playlist from external source (Spotify, YouTube, etc).

        Args:
            playlist_name: Name for the Plex playlist
            url: URL of the playlist to import
            manual_search: Whether to use manual search for unmatched tracks
            auto_refresh_index: Whether to refresh vector index after import

        Returns:
            Number of tracks successfully imported
        """
        if not self._initialized or not self.plex.connected:
            logger.error("Harmony not initialized")
            return 0

        try:
            from harmony.workflows.playlist_import import import_from_url
            count = import_from_url(self, playlist_name, url, manual_search)

            # Refresh vector index if tracks were added
            if count > 0 and auto_refresh_index:
                logger.info(f"Imported {count} tracks - refreshing vector index")
                self.refresh_vector_index()

            return count
        except Exception as e:
            logger.error(f"Error importing playlist from {url}: {e}")
            return 0

    def retry_failed_imports(
        self,
        playlist_name: str = None,
        log_files: list = None,
    ) -> dict:
        """Retry failed imports from playlist import logs.

        Args:
            playlist_name: Specific playlist to retry (otherwise process all logs)
            log_files: List of log file paths to process (otherwise auto-discover)

        Returns:
            Dict with statistics: processed, matched, failed
        """
        if not self._initialized or not self.plex.connected:
            logger.error("Harmony not initialized")
            return {'processed': 0, 'matched': 0, 'failed': 0}

        try:
            from harmony.workflows.playlist_import import process_import_logs
            return process_import_logs(self, log_files=log_files, playlist_name=playlist_name)
        except Exception as e:
            logger.error(f"Error processing import logs: {e}")
            return {'processed': 0, 'matched': 0, 'failed': 0}

    def transfer_playlist(
        self,
        source: str,
        destination: str,
        playlist_name: str,
        destination_playlist: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Transfer a playlist between services."""
        if not self._initialized:
            raise RuntimeError("Harmony not initialized")

        from harmony.transfer import transfer_playlist

        return transfer_playlist(
            self,
            source=source,
            destination=destination,
            playlist_name=playlist_name,
            destination_playlist=destination_playlist,
            limit=limit,
        )

    # ========== AI Methods ==========

    def generate_ai_playlist(
        self,
        playlist_name: str,
        mood: str = None,
        genre: str = None,
        era: str = None,
        num_songs: int = 50,
    ) -> Optional[int]:
        """Generate a playlist using AI.

        Args:
            playlist_name: Name for the playlist
            mood: Playlist mood (relaxing, energetic, sad, etc)
            genre: Music genre
            era: Time period
            num_songs: Number of songs to generate

        Returns:
            Number of tracks added or None if failed
        """
        if not self._initialized or not self.plex.connected:
            logger.error("Harmony not initialized")
            return None

        if not hasattr(self, "llm") or not self.llm:
            logger.warning("LLM not initialized")
            return None

        try:
            from harmony.ai.llm import Song

            # Generate songs using LLM
            songs = self.llm.generate_playlist(mood, genre, era, num_songs)

            if not songs:
                logger.warning("LLM generated no songs")
                return 0

            # Convert to song dicts for search
            song_dicts = [
                {
                    "title": song.title,
                    "artist": song.artist,
                    "album": song.album,
                }
                for song in songs
            ]

            # Match with Plex
            from harmony.workflows.playlist_import import add_songs_to_playlist
            count = add_songs_to_playlist(self, playlist_name, song_dicts, manual_search=False)

            logger.info(f"Created AI playlist {playlist_name} with {count} matched tracks")
            return count
        except Exception as e:
            logger.error(f"Error generating AI playlist: {e}")
            return None

    def init_llm(self, provider: str = None, model: str = None, **kwargs) -> bool:
        """Initialize LLM integration.

        Args:
            provider: LLM provider (ollama, openai, etc)
            model: Model name
            **kwargs: Additional config

        Returns:
            True if initialized successfully
        """
        try:
            from harmony.ai.llm import MusicSearchTools

            config = {
                "provider": provider or self.config.llm.get("provider", "ollama"),
                "model": model or self.config.llm.get("model"),
                **kwargs
            }

            self.llm = MusicSearchTools(**config)
            logger.info(f"LLM initialized with provider={config['provider']}")
            return self.llm is not None
        except Exception as e:
            logger.error(f"Failed to initialize LLM: {e}")
            self.llm = None
            return False
