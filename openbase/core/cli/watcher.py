import time
from pathlib import Path

import click
import requests
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


class DirectoryEventHandler(FileSystemEventHandler):
    """Handles file system events and sends notifications to the server."""

    def __init__(self, base_path, server_url="http://localhost:8001"):
        self.base_path = Path(base_path)
        self.server_url = server_url
        self.endpoint = f"{server_url}/api/openbase/file-change/"

    def _get_relative_path(self, path):
        """Get relative path from base directory."""
        try:
            return str(Path(path).relative_to(self.base_path))
        except ValueError:
            return str(Path(path))

    def _send_notification(self, change_type, file_path, dest_path=None):
        """Send file change notification to the server."""
        data = {"change_type": change_type, "file_path": file_path}
        if dest_path:
            data["dest_path"] = dest_path

        try:
            response = requests.post(self.endpoint, json=data, timeout=1)
            if response.status_code != 200:
                print(f"Failed to send notification: {response.status_code}")
        except requests.exceptions.RequestException as e:
            print(f"Error sending notification: {e}")

    def on_created(self, event):
        if not event.is_directory:
            file_path = self._get_relative_path(event.src_path)
            self._send_notification("created", file_path)

    def on_modified(self, event):
        if not event.is_directory:
            file_path = self._get_relative_path(event.src_path)
            self._send_notification("modified", file_path)

    def on_deleted(self, event):
        if not event.is_directory:
            file_path = self._get_relative_path(event.src_path)
            self._send_notification("deleted", file_path)

    def on_moved(self, event):
        if not event.is_directory:
            src_path = self._get_relative_path(event.src_path)
            dest_path = self._get_relative_path(event.dest_path)
            self._send_notification("moved", src_path, dest_path)


class DirectoryWatcher:
    """Watches the current directory for file changes using watchdog."""

    def __init__(
        self, directory: Path = None, server_url: str = "http://localhost:8001"
    ):
        self.directory = directory or Path.cwd()
        self.server_url = server_url
        self.observer = None

    def start(self):
        """Start watching the directory."""
        if self.observer and self.observer.is_alive():
            return

        print(f"Watching {self.directory} for changes...")

        event_handler = DirectoryEventHandler(self.directory, self.server_url)
        self.observer = Observer()
        self.observer.schedule(event_handler, str(self.directory), recursive=True)
        self.observer.start()

    def stop(self):
        """Stop watching the directory."""
        if self.observer and self.observer.is_alive():
            self.observer.stop()
            self.observer.join()


@click.command()
@click.option("--host", default="localhost", help="Host to bind to")
@click.option("--port", default="8001", help="Port to bind to")
def watcher(host, port):
    """Run only the directory watcher."""
    click.echo("Starting directory watcher...")

    # Create watcher with server URL
    server_url = f"http://{host}:{port}"
    watcher = DirectoryWatcher(server_url=server_url)
    watcher.start()

    try:
        # Keep the watcher running
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        click.echo("\nStopping watcher...")
        watcher.stop()
        click.echo("Watcher stopped.")
