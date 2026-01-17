"""Command-line interface for Harmony."""

import logging
import sys
from pathlib import Path
from typing import Optional
import io

# Fix Windows console encoding for Unicode support
if sys.platform == 'win32':
    # Wrap stdout/stderr with UTF-8 encoding
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# Configure logging BEFORE any imports - default to WARNING for normal runs
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("harmony")

# Suppress root logger INFO messages
logging.getLogger().setLevel(logging.WARNING)

# Suppress noisy third-party loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("phi").setLevel(logging.WARNING)

# Suppress agno/LLM toolkit logs BEFORE importing harmony (which may use agno)
# Must call for each logger type: agent (default), team, workflow
try:
    import agno.utils.log
    agno.utils.log.set_log_level_to_warning()  # agno (agent) logger
    agno.utils.log.set_log_level_to_warning("team")  # agno-team logger
    agno.utils.log.set_log_level_to_warning("workflow")  # agno-workflow logger
    
    # Additional suppression: monkey-patch log_info to prevent ANY info logs
    # This is needed because some agno tools bypass the logger level check
    original_log_info = agno.utils.log.log_info
    def suppressed_log_info(msg, *args, **kwargs):
        pass  # Do nothing
    agno.utils.log.log_info = suppressed_log_info
except ImportError:
    pass

# Now import harmony modules
import typer
from rich.console import Console

from harmony import Harmony, __version__

app = typer.Typer(help="Harmony - Universal Playlist Manager")
console = Console()


def debug_callback(value: bool):
    """Enable debug mode."""
    if value:
        # Set all harmony loggers to DEBUG
        logging.getLogger("harmony").setLevel(logging.DEBUG)
        logging.getLogger("harmony.ai").setLevel(logging.DEBUG)
        logging.getLogger("harmony.playlist_import").setLevel(logging.DEBUG)
        logging.getLogger("harmony.search").setLevel(logging.DEBUG)
        logging.getLogger("harmony.providers").setLevel(logging.DEBUG)
        
        # Enable agno/LLM logs for debugging using agno's utility
        try:
            from agno.utils.log import set_log_level_to_debug
            set_log_level_to_debug()  # agent logger
            set_log_level_to_debug("team")  # team logger  
            set_log_level_to_debug("workflow")  # workflow logger
        except ImportError:
            pass
        
        # Keep httpx at INFO for debugging
        logging.getLogger("httpx").setLevel(logging.INFO)
        console.print("[dim]Debug mode enabled[/dim]")


@app.callback()
def common_options(
    debug: bool = typer.Option(
        False,
        "--debug",
        "-d",
        help="Enable debug logging",
        callback=debug_callback,
        is_eager=True,
    ),
):
    """Harmony - Universal Playlist Manager."""
    pass


@app.command()
def version() -> None:
    """Show version information."""
    console.print(f"[bold]Harmony[/bold] v{__version__}")
    console.print("Universal Playlist Manager")


@app.command()
def init(
    config_path: str = typer.Option(
        "harmony.yaml",
        "--config",
        "-c",
        help="Path to config file",
    ),
) -> None:
    """Initialize Harmony and test connections."""
    try:
        console.print("[bold]Initializing Harmony...[/bold]")
        harmony = Harmony(config_path)
        harmony.initialize()
        console.print("[green]✓[/green] Harmony initialized successfully")
        console.print(f"[green]✓[/green] Connected to Plex server")
        if harmony.beets and harmony.beets.connected:
            console.print(f"[green]✓[/green] Connected to beets library")
        console.print(f"[green]✓[/green] Vector index built with {len(harmony.vector_index)} tracks")
        harmony.shutdown()
    except Exception as e:
        console.print(f"[red]✗[/red] Initialization failed: {e}")
        sys.exit(1)


@app.command()
def test_plex(
    config_path: str = typer.Option(
        "harmony.yaml",
        "--config",
        "-c",
        help="Path to config file",
    ),
) -> None:
    """Test Plex connection."""
    try:
        console.print("[bold]Testing Plex connection...[/bold]")
        harmony = Harmony(config_path)
        harmony.plex.connect()
        console.print("[green]✓[/green] Connected to Plex server")

        # Try to get a few tracks
        tracks = harmony.plex.search_tracks(limit=5)
        console.print(f"[green]✓[/green] Retrieved {len(tracks)} sample tracks")
        if tracks:
            for track in tracks[:3]:
                console.print(f"  - {track.artist} - {track.title}")

        harmony.plex.disconnect()
    except Exception as e:
        console.print(f"[red]✗[/red] Plex test failed: {e}")
        sys.exit(1)


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query"),
    config_path: str = typer.Option(
        "harmony.yaml",
        "--config",
        "-c",
        help="Path to config file",
    ),
) -> None:
    """Search for tracks."""
    try:
        console.print(f"[bold]Searching for: {query}[/bold]")
        harmony = Harmony(config_path)
        harmony.initialize()

        results = harmony.search_plex(title=query, limit=10)
        if results:
            console.print(f"[green]Found {len(results)} tracks:[/green]")
            for track in results:
                console.print(f"  - {track.artist} - {track.title} ({track.album})")
        else:
            console.print("[yellow]No tracks found[/yellow]")

        harmony.shutdown()
    except Exception as e:
        console.print(f"[red]✗[/red] Search failed: {e}")
        sys.exit(1)


@app.command()
def import_playlist(
    playlist_name: str = typer.Argument(..., help="Name for the new Plex playlist"),
    url: str = typer.Option(..., "--url", "-u", help="URL of playlist (Spotify, YouTube, Apple Music, etc)"),
    config_path: str = typer.Option(
        "harmony.yaml",
        "--config",
        "-c",
        help="Path to config file",
    ),
    manual_search: bool = typer.Option(
        False,
        "--manual",
        "-m",
        help="Enable manual search for unmatched tracks",
    ),
    use_llm_cleanup: bool = typer.Option(
        False,
        "--llm-cleanup",
        help="Use LLM to clean metadata and retry failed searches",
    ),
    use_manual_confirmation: bool = typer.Option(
        False,
        "--manual-confirm",
        help="Ask user to confirm ambiguous matches",
    ),
    no_refresh: bool = typer.Option(
        False,
        "--no-refresh",
        help="Skip refreshing vector index after import",
    ),
) -> None:
    """Import a playlist from Spotify, YouTube, Apple Music, etc.

    Examples:
        harmony import-playlist "My Spotify Mix" --url https://open.spotify.com/playlist/PLAYLIST_ID
        harmony import-playlist "YouTube Hits" --url https://www.youtube.com/playlist?list=PLxxxx
        harmony import-playlist "Better Matches" --url https://open.spotify.com/playlist/ID --llm-cleanup
    """
    try:
        console.print(f"[bold]Importing playlist: {playlist_name}[/bold]")
        harmony = Harmony(config_path)

        # Override search config if CLI options provided
        if use_llm_cleanup:
            harmony.config.search.use_llm_cleanup = use_llm_cleanup
        if use_manual_confirmation:
            harmony.config.search.use_manual_confirmation = use_manual_confirmation

        harmony.initialize()

        count = harmony.import_playlist_from_url(
            playlist_name,
            url,
            manual_search=manual_search,
            auto_refresh_index=not no_refresh,
        )

        if count > 0:
            console.print(f"[green]✓[/green] Imported {count} tracks to '{playlist_name}'")
            if not no_refresh:
                console.print(f"[green]✓[/green] Vector index refreshed")
        else:
            console.print("[yellow]⚠[/yellow] No tracks were imported")

        harmony.shutdown()
    except Exception as e:
        console.print(f"[red]✗[/red] Import failed: {e}")
        sys.exit(1)


@app.command()
def transfer_playlist(
    playlist_name: str = typer.Argument(..., help="Source playlist name"),
    source: str = typer.Option(
        "plex",
        "--source",
        "-s",
        help="Source service (currently supports: plex)",
    ),
    destination: str = typer.Option(
        "spotify",
        "--destination",
        "-d",
        help="Destination service (currently supports: spotify)",
    ),
    destination_playlist: str = typer.Option(
        None,
        "--dest-name",
        help="Destination playlist name (defaults to source name)",
    ),
    limit: Optional[int] = typer.Option(
        None,
        "--limit",
        help="Limit number of tracks to transfer",
    ),
    config_path: str = typer.Option(
        "harmony.yaml",
        "--config",
        "-c",
        help="Path to config file",
    ),
) -> None:
    """Transfer a playlist between services."""
    try:
        console.print(f"[bold]Transferring playlist: {playlist_name}[/bold]")
        harmony = Harmony(config_path)
        harmony.initialize()

        result = harmony.transfer_playlist(
            source=source,
            destination=destination,
            playlist_name=playlist_name,
            destination_playlist=destination_playlist,
            limit=limit,
        )

        console.print(
            f"[green]û[/green] Matched {result['matched']} of {result['source_count']} tracks"
        )
        console.print(
            f"[green]û[/green] Added {result['added']} tracks to '{result['destination_playlist']}'"
        )

        missing = result.get("missing", [])
        if missing:
            console.print(f"[yellow]?[/yellow] Missing {len(missing)} tracks")
            for track in missing[:10]:
                console.print(f"  - {track}")
            if len(missing) > 10:
                console.print(f"  ... and {len(missing) - 10} more")

        harmony.shutdown()
    except Exception as e:
        console.print(f"[red]?[/red] Transfer failed: {e}")
        sys.exit(1)


@app.command()
def retry_failed(
    playlist_name: str = typer.Option(
        None,
        "--playlist",
        "-p",
        help="Specific playlist to retry (otherwise process all logs)",
    ),
    log_file: str = typer.Option(
        None,
        "--log-file",
        "-l",
        help="Specific log file to process",
    ),
    config_path: str = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to config file",
    ),
) -> None:
    """Retry failed imports from playlist import logs.

    Parses import log files and retries failed tracks with manual search enabled.
    Updates the log files with remaining failures after retry.

    Examples:
        harmony retry-failed                           # Process all import logs
        harmony retry-failed --playlist "My Mix"       # Retry specific playlist
        harmony retry-failed --log-file /path/to/log   # Process specific log file
    """
    try:
        console.print("[bold]Processing import logs...[/bold]")
        harmony = Harmony(config_path)
        harmony.initialize()

        log_files = [log_file] if log_file else None

        from harmony.workflows.playlist_import import process_import_logs
        stats = process_import_logs(harmony, log_files=log_files, playlist_name=playlist_name)

        console.print(f"[green]✓[/green] Processed {stats['processed']} failed tracks")
        console.print(f"[green]✓[/green] Matched {stats['matched']} tracks")
        if stats['failed'] > 0:
            console.print(f"[yellow]⚠[/yellow] {stats['failed']} tracks still failed")
        else:
            console.print(f"[green]✓[/green] All tracks matched successfully!")

        harmony.shutdown()
    except Exception as e:
        console.print(f"[red]✗[/red] Retry failed: {e}")
        sys.exit(1)


@app.command()
def smart_playlist(
    playlist_name: str = typer.Argument(..., help="Name for the new playlist"),
    playlist_type: str = typer.Option(
        "daily_discovery",
        "--type",
        "-t",
        help="Type of playlist (daily_discovery, forgotten_gems, recent_hits, fresh_favorites, 70s80s_flashback, highly_rated, most_played)",
    ),
    num_tracks: int = typer.Option(
        50,
        "--count",
        "-n",
        help="Number of tracks",
    ),
    config_path: str = typer.Option(
        "harmony.yaml",
        "--config",
        "-c",
        help="Path to config file",
    ),
) -> None:
    """Generate a smart playlist.

    Available types:
        - daily_discovery: Mix of familiar + new tracks
        - forgotten_gems: Highly-rated but unplayed tracks
        - recent_hits: New popular releases
        - fresh_favorites: New releases you rated highly
        - 70s80s_flashback: Nostalgic era-specific playlist
        - highly_rated: Your top-rated tracks
        - most_played: Your most frequently played tracks

    Examples:
        harmony smart-playlist "My Daily Mix" --type daily_discovery --count 50
        harmony smart-playlist "Forgotten Gems" -t forgotten_gems -n 40
    """
    try:
        console.print(f"[bold]Generating {playlist_type} playlist: {playlist_name}[/bold]")
        harmony = Harmony(config_path)
        harmony.initialize()

        result = harmony.generate_smart_playlist(playlist_name, playlist_type, num_tracks)

        if result:
            console.print(f"[green]✓[/green] Created playlist '{playlist_name}'")
            console.print(f"[green]✓[/green] Added {result.get('selected_count', num_tracks)} tracks")
        else:
            console.print("[red]✗[/red] Failed to generate playlist")
            sys.exit(1)

        harmony.shutdown()
    except Exception as e:
        console.print(f"[red]✗[/red] Playlist generation failed: {e}")
        sys.exit(1)


@app.command("smart-playlists")
def smart_playlists(
    playlist_ids: list[str] = typer.Argument(
        None,
        help="Specific playlist IDs to generate (if omitted, generates all enabled playlists)",
    ),
    config_path: str = typer.Option(
        "harmony.yaml",
        "--config",
        "-c",
        help="Path to config file",
    ),
    all_playlists: bool = typer.Option(
        False,
        "--all",
        help="Generate all enabled playlists from config (default behavior if no IDs specified)",
    ),
    manual_search: bool = typer.Option(
        None,
        "--manual",
        "-m",
        help="Enable manual search for imported playlists (overrides config)",
    ),
    no_refresh: bool = typer.Option(
        False,
        "--no-refresh",
        help="Skip refreshing vector index after imports",
    ),
) -> None:
    """Generate multiple playlists in one run (efficient batch processing).

    This command processes playlists defined in your harmony.yaml config
    in a single run, which is much more efficient since it only builds 
    the vector index cache once. Supports both smart playlists and imported playlists.

    To use this, add a 'playlists.items' section to your harmony.yaml:

    \b
    playlists:
      items:
        - id: daily_discovery
          name: "Daily Discovery"
          type: daily_discovery
          num_tracks: 50
        - id: retro_essentials
          name: "Retro Essentials"
          type: imported
          sources:
            - https://music.youtube.com/playlist?list=PLAYLIST_ID
        - id: recent_hits
          name: "Recent Hits"
          type: recent_hits
          num_tracks: 30
          enabled: false  # Skip this one

    Examples:
      # Generate all enabled playlists
      harmony smart-playlists
      harmony smart-playlists --all
      
      # Generate specific playlists by ID
      harmony smart-playlists daily_discovery retro_essentials

    Available smart playlist types:
      - daily_discovery: Mix of familiar + new tracks
      - forgotten_gems: Highly-rated but unplayed tracks
      - recent_hits: New popular releases
      - fresh_favorites: New releases you rated highly
      - 70s80s_flashback: Nostalgic era-specific playlist
      - highly_rated: Your top-rated tracks
      - most_played: Your most frequently played tracks
    
    Imported playlist type:
      - imported: Import from external sources (requires 'sources' list with URLs)
    """
    try:
        from harmony.config import HarmonyConfig

        # Load config to get playlist definitions
        config = HarmonyConfig.from_file(config_path)
        all_playlists_config = config.playlists.items
        
        # Also check old-style smart playlists for backward compatibility
        legacy_smart = config.playlists.smart

        if not all_playlists_config and not legacy_smart:
            console.print(
                "[yellow]No playlists configured in harmony.yaml[/yellow]\n"
                "Add a 'playlists.items' section to your config file. Example:\n\n"
                "[cyan]playlists:\n"
                "  items:\n"
                "    - id: daily_discovery\n"
                "      name: \"Daily Discovery\"\n"
                "      type: daily_discovery\n"
                "      num_tracks: 50\n"
                "    - id: retro_essentials\n"
                "      name: \"Retro Essentials\"\n"
                "      type: imported\n"
                "      sources:\n"
                "        - https://music.youtube.com/playlist?list=PLAYLIST_ID[/cyan]"
            )
            sys.exit(1)

        # Determine which playlists to generate
        if playlist_ids:
            # Generate specific playlists by ID
            selected_playlists = []
            for pid in playlist_ids:
                matches = [p for p in all_playlists_config if p.id == pid]
                if not matches:
                    console.print(f"[yellow]Warning: Playlist ID '{pid}' not found in config, skipping[/yellow]")
                else:
                    selected_playlists.extend(matches)
            
            if not selected_playlists:
                console.print("[red]✗[/red] No matching playlists found")
                sys.exit(1)
        else:
            # Generate all enabled playlists (default behavior)
            selected_playlists = [p for p in all_playlists_config if p.enabled]
            
            # Add legacy smart playlists if they exist
            for legacy in legacy_smart:
                if legacy.enabled:
                    # Convert to new format
                    from harmony.config import PlaylistItemConfig
                    converted = PlaylistItemConfig(
                        id=legacy.name.lower().replace(' ', '_'),
                        name=legacy.name,
                        type=legacy.type,
                        num_tracks=legacy.num_tracks,
                        enabled=legacy.enabled
                    )
                    selected_playlists.append(converted)
            
            if not selected_playlists:
                console.print("[yellow]No enabled playlists found in config[/yellow]")
                sys.exit(0)

        console.print(
            f"[bold]Processing {len(selected_playlists)} playlist(s)...[/bold]"
        )

        # Initialize Harmony once (builds cache)
        harmony = Harmony(config_path)
        harmony.initialize()

        results = []
        imported_count = 0
        
        # Separate imported and smart playlists
        imported_playlists = [p for p in selected_playlists if p.type == "imported"]
        smart_playlists = [p for p in selected_playlists if p.type != "imported"]
        
        # Process imported playlists in batch for efficiency
        if imported_playlists:
            console.print(f"\n[bold cyan]→ Batch importing {len(imported_playlists)} playlists...[/bold cyan]")
            
            # Determine manual search setting (use first playlist's setting as default)
            use_manual = manual_search if manual_search is not None else (
                imported_playlists[0].manual_search if imported_playlists[0].manual_search is not None 
                else config.playlists.defaults.manual_search
            )
            
            try:
                from harmony.workflows.playlist_import import batch_import_playlists
                
                # Prepare configs for batch import
                batch_configs = []
                for p in imported_playlists:
                    if not p.sources:
                        console.print(f"  [yellow]⚠[/yellow] Skipping {p.name}: No sources configured")
                        continue
                    batch_configs.append({
                        'name': p.name,
                        'sources': p.sources
                    })
                
                if batch_configs:
                    batch_results = batch_import_playlists(harmony, batch_configs, manual_search=use_manual)
                    
                    # Display results
                    console.print(f"\n[bold]Batch Import Summary:[/bold]")
                    console.print(f"  • Fetched: {batch_results['total_fetched']} songs")
                    console.print(f"  • Unique: {batch_results['total_unique']} songs")
                    console.print(f"  • Matched: {batch_results['total_matched']} songs")
                    console.print()
                    
                    for playlist_result in batch_results['playlists']:
                        name = playlist_result['name']
                        tracks = playlist_result['tracks']
                        
                        if tracks > 0:
                            console.print(f"  [green]✓[/green] {name}: {tracks} tracks")
                            results.append({
                                "name": name,
                                "tracks": tracks,
                                "type": "imported"
                            })
                            imported_count += tracks
                        else:
                            console.print(f"  [yellow]⚠[/yellow] {name}: No tracks matched")
                else:
                    console.print(f"  [yellow]⚠[/yellow] No valid playlists to import")
            except Exception as e:
                console.print(f"  [red]✗[/red] Batch import failed: {e}")
                logger.error(f"Batch import error: {e}", exc_info=True)
        
        # Process smart playlists
        for playlist_config in smart_playlists:
            playlist_type = playlist_config.type
            console.print(
                f"\n[cyan]→[/cyan] Generating {playlist_type}: {playlist_config.name}"
            )
            
            # Determine track count
            track_count = playlist_config.num_tracks or playlist_config.max_tracks or 50
            
            # Build filters from config
            filters = dict(playlist_config.filters or {})
            if playlist_config.history_days is not None:
                filters['history_days'] = playlist_config.history_days
            if playlist_config.exclusion_days is not None:
                filters['exclusion_days'] = playlist_config.exclusion_days
            if playlist_config.discovery_ratio is not None:
                filters['discovery_ratio'] = playlist_config.discovery_ratio

            result = harmony.generate_smart_playlist(
                playlist_config.name,
                playlist_type,
                track_count,
                filters=filters or None,
            )

            if result:
                results.append({
                    "name": playlist_config.name,
                    "tracks": result.get("selected_count", track_count),
                    "type": "smart"
                })
                console.print(
                    f"  [green]✓[/green] Added {result.get('selected_count', track_count)} tracks"
                )
            else:
                console.print(f"  [red]✗[/red] Failed to generate playlist")

        # Refresh vector index if we imported anything
        if imported_count > 0 and not no_refresh:
            console.print(f"\n[cyan]→[/cyan] Refreshing vector index after imports...")
            harmony.refresh_vector_index()
            console.print(f"  [green]✓[/green] Vector index refreshed")

        # Summary
        if results:
            console.print(f"\n[bold green]✓ Processed {len(results)} playlist(s):[/bold green]")
            for r in results:
                type_label = "[imported]" if r.get("type") == "imported" else "[smart]"
                console.print(f"  • {r['name']}: {r['tracks']} tracks {type_label}")
        else:
            console.print(f"\n[yellow]⚠[/yellow] No playlists were successfully processed")

        harmony.shutdown()

    except FileNotFoundError as e:
        console.print(f"[red]✗[/red] Config file not found: {e}")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]✗[/red] Batch processing failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


@app.command()
def ai_playlist(
    playlist_name: str = typer.Argument(..., help="Name for the new playlist"),
    config_path: str = typer.Option(
        "harmony.yaml",
        "--config",
        "-c",
        help="Path to config file",
    ),
    mood: str = typer.Option(
        None,
        "--mood",
        "-m",
        help="Playlist mood (relaxing, energetic, sad, happy, etc)",
    ),
    genre: str = typer.Option(
        None,
        "--genre",
        "-g",
        help="Music genre",
    ),
    era: str = typer.Option(
        None,
        "--era",
        "-e",
        help="Time period (80s, 90s, 2000s, etc)",
    ),
    num_songs: int = typer.Option(
        50,
        "--count",
        "-n",
        help="Number of songs",
    ),
    provider: str = typer.Option(
        "ollama",
        "--provider",
        "-p",
        help="LLM provider (ollama, openai)",
    ),
    model: str = typer.Option(
        None,
        "--model",
        help="LLM model name",
    ),
) -> None:
    """Generate a playlist using AI.

    Examples:
        harmony ai-playlist "Workout Mix" --mood energetic --genre rock --count 50
        harmony ai-playlist "Chill Vibes" -m relaxing -g jazz -n 40
    """
    try:
        console.print(f"[bold]Generating AI playlist: {playlist_name}[/bold]")
        harmony = Harmony(config_path)
        harmony.initialize()

        # Initialize LLM if model provided
        if model:
            console.print(f"[bold]Initializing {provider}...[/bold]")
            harmony.init_llm(provider=provider, model=model)

        count = harmony.generate_ai_playlist(
            playlist_name,
            mood=mood,
            genre=genre,
            era=era,
            num_songs=num_songs,
        )

        if count:
            console.print(f"[green]✓[/green] Created AI playlist '{playlist_name}'")
            console.print(f"[green]✓[/green] Added {count} tracks")
        else:
            console.print("[red]✗[/red] Failed to generate AI playlist")
            sys.exit(1)

        harmony.shutdown()
    except Exception as e:
        console.print(f"[red]✗[/red] AI playlist generation failed: {e}")
        sys.exit(1)


@app.command()
def refresh_index(
    config_path: str = typer.Option(
        "harmony.yaml",
        "--config",
        "-c",
        help="Path to config file",
    ),
) -> None:
    """Refresh the vector index (useful after adding tracks to Plex)."""
    try:
        console.print("[bold]Refreshing vector index...[/bold]")
        harmony = Harmony(config_path)
        harmony.initialize()
        harmony.refresh_vector_index()
        console.print(f"[green]✓[/green] Vector index refreshed with {len(harmony.vector_index)} tracks")
        harmony.shutdown()
    except Exception as e:
        console.print(f"[red]✗[/red] Refresh failed: {e}")
        sys.exit(1)


def main() -> None:
    """Main entry point."""
    app()


if __name__ == "__main__":
    main()
