"""GitHub → OpenList Sync Tool - Entry Point.

Usage:
    python main.py                  # Run scheduler + dashboard
    python main.py --once           # Run a single sync cycle and exit
    python main.py --config path    # Use a custom config file path
"""

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time

from sync_app.config import Config
from sync_app.failure_manager import FailureManager
from sync_app.github_client import GitHubClient
from sync_app.logger import setup_logging
from sync_app.models import SyncState
from sync_app.openlist_client import OpenListClient
from sync_app.sync_engine import SyncEngine
from sync_app.web_dashboard import DashboardRunner

logger: logging.Logger | None = None


def load_sync_state(state_file: str) -> SyncState:
    """Load persisted sync state from JSON, or return a fresh one."""
    state = SyncState()
    if os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            state.last_sync_time = data.get("last_sync_time")
        except (json.JSONDecodeError, KeyError):
            pass
    return state


def save_sync_state(state: SyncState, state_file: str):
    """Persist sync state to JSON."""
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump({"last_sync_time": state.last_sync_time}, f)


def run_sync_cycle(
    engine: SyncEngine,
    state: SyncState,
    state_file: str,
) -> dict:
    """Execute one sync cycle and persist state. Returns summary dict."""
    try:
        summary = engine.run_sync()
    except Exception:
        logger.exception("Unhandled error during sync cycle")
        summary = {"repos_synced": 0, "files_uploaded": 0, "files_deleted": 0, "files_failed": 0}

    save_sync_state(state, state_file)
    return summary


def main():
    global logger

    parser = argparse.ArgumentParser(description="GitHub → OpenList Sync Tool")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    parser.add_argument("--once", action="store_true", help="Run a single sync cycle and exit")
    args = parser.parse_args()

    # Setup
    logger = setup_logging()
    config = Config(args.config)

    state_file = os.path.join("data", "sync_state.json")
    sync_state = load_sync_state(state_file)

    logger.info("=" * 60)
    logger.info("GitHub → OpenList Sync Tool starting")
    logger.info("Config: %s | Interval: %dh | Threads: %d | Retries: %d",
                args.config, config.sync_interval_hours, config.max_threads, config.max_retries)

    # Initialize components
    github_client = GitHubClient(token=config.github_token)

    openlist_client = OpenListClient(
        base_url=config.openlist_base_url,
        username=config.openlist_username,
        password=config.openlist_password,
    )

    failure_manager = FailureManager(
        data_file=os.path.join("data", "failures.json"),
        max_retries=config.max_retries,
        max_failures_per_minute=config.max_failures_per_minute,
        cooldown_minutes=config.cooldown_minutes,
    )

    engine = SyncEngine(
        config=config,
        github_client=github_client,
        openlist_client=openlist_client,
        failure_manager=failure_manager,
        sync_state=sync_state,
    )

    # Manual trigger event for dashboard
    trigger_event = threading.Event()

    def sync_trigger():
        trigger_event.set()

    # --once mode: single run
    if args.once:
        logger.info("Running single sync cycle (--once mode)")
        summary = run_sync_cycle(engine, sync_state, state_file)
        logger.info("Summary: %s", summary)
        logger.info("Done.")
        return

    # Start dashboard in daemon thread
    dashboard = DashboardRunner(
        sync_state=sync_state,
        failure_manager=failure_manager,
        sync_trigger=sync_trigger,
        host=config.web_host,
        port=config.web_port,
    )
    dashboard.start()

    # Graceful shutdown handler
    shutdown_event = threading.Event()

    def handle_signal(sig, frame):
        logger.info("Received signal %s, shutting down...", sig)
        shutdown_event.set()
        sync_state.stop_requested = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Scheduler loop
    interval_seconds = config.sync_interval_hours * 3600
    logger.info("Scheduler running. Interval: %dh. Dashboard: http://%s:%d",
                config.sync_interval_hours, config.web_host, config.web_port)

    while not shutdown_event.is_set():
        # Wait for interval or manual trigger
        trigger_event.wait(timeout=interval_seconds)
        trigger_event.clear()

        if shutdown_event.is_set():
            break

        if sync_state.is_running:
            logger.info("Sync already in progress, skipping scheduled trigger.")
            continue

        logger.info("Sync cycle triggered.")
        summary = run_sync_cycle(engine, sync_state, state_file)
        logger.info("Cycle complete. Uploaded: %(files_uploaded)d, Failed: %(files_failed)d, Deleted: %(files_deleted)d",
                    summary)

    logger.info("Shutdown complete.")


if __name__ == "__main__":
    main()
