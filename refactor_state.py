with open("buzz/core/state.py", "r") as f:
    lines = f.readlines()

output = []

# 1. Insert module docstring after line 23 (from ..models import DavConfig)
for i, line in enumerate(lines):
    output.append(line)
    if line.strip() == "from ..models import DavConfig":
        output.append("\n")
        output.append('"""Core state management for Buzz.\n')
        output.append("\n")
        output.append("Handles library building, torrent caching, snapshot generation,\n")
        output.append("Real-Debrid synchronization, and DAV filesystem state.\n")
        output.append('"""\n')
        break

# Continue from after the break point
for j in range(i + 1, len(lines)):
    line = lines[j]

    # 2. Class docstrings
    if line == "class LibraryBuilder:\n":
        output.append(line)
        output.append('    """Builds a DAV library snapshot from torrent metadata."""\n')
        continue

    if line == "class BuzzState:\n":
        output.append(line)
        output.append('    """Manages cached torrent state, snapshots, and Real-Debrid sync."""\n')
        continue

    if line == "class Poller(threading.Thread):\n":
        output.append(line)
        output.append('    """Background thread that polls Real-Debrid for library changes."""\n')
        continue

    if line == "class InitialSync(threading.Thread):\n":
        output.append(line)
        output.append('    """Performs an initial library sync on startup."""\n')
        continue

    # 3. Method docstrings and comprehension conversions

    # build method
    if line == "    def build(self, infos: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:\n":
        output.append(line)
        output.append('        """Build a snapshot and current root list from torrent infos."""\n')
        continue

    # sync method
    if line == "    def sync(self, *, trigger_hook: bool = True) -> dict[str, Any]:\n":
        output.append(line)
        output.append('        """Synchronize torrent state with Real-Debrid and rebuild snapshot."""\n')
        continue

    # mark_startup_sync_complete
    if line == "    def mark_startup_sync_complete(self) -> None:\n":
        output.append(line)
        output.append('        """Flag that the initial startup sync has finished."""\n')
        continue

    # is_ready
    if line == "    def is_ready(self) -> bool:\n":
        output.append(line)
        output.append('        """Return whether the library is ready to serve requests."""\n')
        continue

    # lookup
    if line == "    def lookup(self, path: str) -> dict[str, Any] | None:\n":
        output.append(line)
        output.append('        """Look up a path in the current snapshot."""\n')
        continue

    # list_children
    if line == "    def list_children(self, path: str) -> list[str]:\n":
        output.append(line)
        output.append('        """List child entries for a directory path."""\n')
        continue

    # status
    if line == "    def status(self) -> dict[str, Any]:\n":
        output.append(line)
        output.append('        """Return current sync and hook status."""\n')
        continue

    # torrents - convert to comprehension
    if line == "    def torrents(self) -> list[dict[str, Any]]:\n":
        output.append(line)
        output.append('        """Return a sorted list of cached torrent summaries."""\n')
        output.append("        with self.lock:\n")
        output.append("            results = [\n")
        output.append("                {\n")
        output.append('                    "id": str(info.get("id") or torrent_id),\n')
        output.append('                    "name": self.builder._torrent_name(info),\n')
        output.append('                    "status": info.get("status", "unknown"),\n')
        output.append('                    "progress": info.get("progress", 0),\n')
        output.append('                    "bytes": info.get("bytes", 0),\n')
        output.append('                    "selected_files": sum(\n')
        output.append('                        1 for f in info.get("files", []) if f.get("selected")\n')
        output.append("                    ),\n")
        output.append('                    "links": len(info.get("links") or []),\n')
        output.append('                    "ended": info.get("ended"),\n')
        output.append("                }\n")
        output.append("                for torrent_id, cached in self.cache.items()\n")
        output.append("                if isinstance(cached, dict)\n")
        output.append('                and isinstance(info := cached.get("info"), dict)\n')
        output.append("            ]\n")
        output.append('        return sorted(results, key=lambda x: x["name"])\n')
        # Skip the old torrents method body
        k = j + 1
        while k < len(lines):
            if lines[k].startswith("    def ") or lines[k].startswith("class "):
                break
            k += 1
        j = k - 1
        continue

    # add_magnet
    if line == "    def add_magnet(self, magnet: str) -> dict[str, Any]:\n":
        output.append(line)
        output.append('        """Add a magnet link and return the new torrent info."""\n')
        continue

    # delete_torrent
    if line == "    def delete_torrent(self, torrent_id: str) -> dict[str, Any]:\n":
        output.append(line)
        output.append('        """Delete a torrent and move it to trash if possible."""\n')
        continue

    # archive_torrents - convert to comprehension
    if line == "    def archive_torrents(self) -> list[dict[str, Any]]:\n":
        output.append(line)
        output.append('        """Return archived (trashed) torrents, newest first."""\n')
        output.append("        with self.lock:\n")
        output.append("            results = [\n")
        output.append("                {\n")
        output.append('                    "hash": thash,\n')
        output.append('                    "name": entry.get("name", "Unknown"),\n')
        output.append('                    "bytes": entry.get("bytes", 0),\n')
        output.append('                    "file_count": len(entry.get("files", [])),\n')
        output.append('                    "deleted_at": entry.get("deleted_at"),\n')
        output.append("                }\n")
        output.append("                for thash, entry in self.trashcan.items()\n")
        output.append("            ]\n")
        output.append('            return sorted(results, key=lambda x: x["deleted_at"] or "", reverse=True)\n')
        # Skip old body
        k = j + 1
        while k < len(lines):
            if lines[k].startswith("    def ") or lines[k].startswith("class "):
                break
            k += 1
        j = k - 1
        continue

    # restore_trash
    if line == "    def restore_trash(self, thash: str) -> dict[str, Any]:\n":
        output.append(line)
        output.append('        """Restore a trashed torrent from its hash."""\n')
        continue

    # delete_trash_permanently
    if line == "    def delete_trash_permanently(self, thash: str) -> dict[str, Any]:\n":
        output.append(line)
        output.append('        """Permanently remove a trashed torrent."""\n')
        continue

    # select_files
    if line == "    def select_files(self, torrent_id: str, file_ids: list[str]) -> dict[str, Any]:\n":
        output.append(line)
        output.append('        """Select specific files in a torrent."""\n')
        continue

    # resolve_download_url
    if line == "    def resolve_download_url(self, source_url: str, force_refresh: bool = False) -> str:\n":
        output.append(line)
        output.append('        """Resolve a source URL to a direct download URL."""\n')
        continue

    # invalidate_download_url
    if line == "    def invalidate_download_url(self, source_url: str) -> None:\n":
        output.append(line)
        output.append('        """Remove a cached download URL."""\n')
        continue

    # verbose_log
    if line == "    def verbose_log(self, message: str) -> None:\n":
        output.append(line)
        output.append('        """Log a debug message if verbose mode is enabled."""\n')
        continue

    # Poller.run
    poller_context = "".join(lines[max(0, j-5):j])
    if line == "    def run(self) -> None:\n" and "class Poller" in poller_context:
        output.append(line)
        output.append('        """Poll Real-Debrid until stopped."""\n')
        continue

    # InitialSync.run
    initialsync_context = "".join(lines[max(0, j-5):j])
    if line == "    def run(self) -> None:\n" and "class InitialSync" in initialsync_context:
        output.append(line)
        output.append('        """Run startup sync and mark completion."""\n')
        continue

    # read_range_header
    if line == "def read_range_header(value: str | None, size: int) -> tuple[int, int] | None:\n":
        output.append('"""Parse an HTTP Range header value into (start, end) bounds."""\n')
        output.append(line)
        continue

    # Line length fixes for worst offenders

    # Line with mime_type
    if '"mime_type": mimetypes.guess_type(rel)[0] or "application/octet-stream",' in line and len(line.rstrip()) > 79:
        indent = "                "
        output.append(indent + '"mime_type":\n')
        output.append(indent + '    mimetypes.guess_type(rel)[0]\n')
        output.append(indent + '    or "application/octet-stream",\n')
        continue

    # Line with normalize_posix_path in _add_unplayable_tree
    if '"files": [normalize_posix_path(item["path"]) for item in entries],' in line and len(line.rstrip()) > 79:
        indent = "                    "
        output.append(indent + '"files": [\n')
        output.append(indent + '    normalize_posix_path(item["path"])\n')
        output.append(indent + '    for item in entries\n')
        output.append(indent + '],\n')
        continue

    # Waiting for Real-Debrid update
    if 'f"Waiting {self.config.rd_update_delay_secs}s for Real-Debrid update..."' in line and len(line.rstrip()) > 79:
        indent = "                    "
        output.append(indent + 'f"Waiting '\n')
        output.append(indent + '    {self.config.rd_update_delay_secs}s '\n')
        output.append(indent + '    for Real-Debrid update..."\n')
        continue

    # VFS visibility
    if 'f"Waiting for VFS visibility of {len(to_check)} roots in {mount} (timeout {timeout}s)..."' in line and len(line.rstrip()) > 79:
        indent = "            "
        output.append(indent + 'f"Waiting for VFS visibility of '\n')
        output.append(indent + '    {len(to_check)} roots in {mount} '\n')
        output.append(indent + '    (timeout {timeout}s)..."\n')
        continue

    # VFS still syncing
    if 'f"VFS still syncing... (missing: {len(missing)}, stale: {len(stale)})"' in line and len(line.rstrip()) > 79:
        indent = "                    "
        output.append(indent + 'f"VFS still syncing... '\n')
        output.append(indent + '    (missing: {len(missing)}, '\n')
        output.append(indent + '    stale: {len(stale)})"\n')
        continue

    # VFS timeout
    if 'f"VFS visibility timeout reached after {timeout}s. Proceeding with sync."' in line and len(line.rstrip()) > 79:
        indent = "            "
        output.append(indent + 'f"VFS visibility timeout reached '\n')
        output.append(indent + '    after {timeout}s. '\n')
        output.append(indent + '    Proceeding with sync."\n')
        continue

    # Library update hook timed out
    if 'f"Library update hook timed out after {exc.timeout}s: {exc.cmd}"' in line and len(line.rstrip()) > 79:
        indent = "                "
        output.append(indent + 'f"Library update hook timed out '\n')
        output.append(indent + '    after {exc.timeout}s: {exc.cmd}"\n')
        continue

    # Library update hook failed
    if 'f"Library update hook failed with exit code {exc.returncode}: {exc.cmd}"' in line and len(line.rstrip()) > 79:
        indent = "                    "
        output.append(indent + 'f"Library update hook failed '\n')
        output.append(indent + '    with exit code {exc.returncode}: '\n')
        output.append(indent + '    {exc.cmd}"\n')
        continue

    # name in _add_to_archive
    if '"name": info.get("filename") or info.get("original_filename") or "Unknown",' in line and len(line.rstrip()) > 79:
        indent = "            "
        output.append(indent + '"name": '\n')
        output.append(indent + '    info.get("filename") '\n')
        output.append(indent + '    or info.get("original_filename") '\n')
        output.append(indent + '    or "Unknown",\n')
        continue

    # Failed to auto-select files during restore
    if 'record_event(f"Failed to auto-select files during restore: {exc}", level="error")' in line and len(line.rstrip()) > 79:
        indent = "                "
        output.append(indent + 'record_event(\n')
        output.append(indent + '    f"Failed to auto-select files '\n')
        output.append(indent + '    during restore: {exc}",\n')
        output.append(indent + '    level="error",\n')
        output.append(indent + ')\n')
        continue

    # filename = ... strip()
    if 'filename = (info.get("filename") or info.get("original_filename") or "").strip()' in line and len(line.rstrip()) > 79:
        indent = "        "
        output.append(indent + 'filename = (\n')
        output.append(indent + '    info.get("filename") '\n')
        output.append(indent + '    or info.get("original_filename") '\n')
        output.append(indent + '    or ""\n')
        output.append(indent + ').strip()\n')
        continue

    # resolve_download_url signature
    if 'def resolve_download_url(self, source_url: str, force_refresh: bool = False) -> str:' in line and len(line.rstrip()) > 79:
        indent = "    "
        output.append(indent + 'def resolve_download_url(\n')
        output.append(indent + '    self,\n')
        output.append(indent + '    source_url: str,\n')
        output.append(indent + '    force_refresh: bool = False,\n')
        output.append(indent + ') -> str:\n')
        continue

    # Triggering curator rebuild
    if 'self.verbose_log(f"Triggering curator rebuild at {self.config.curator_url}...")' in line and len(line.rstrip()) > 79:
        indent = "        "
        output.append(indent + 'self.verbose_log(\n')
        output.append(indent + '    f"Triggering curator rebuild '\n')
        output.append(indent + '    at {self.config.curator_url}..."\n')
        output.append(indent + ')\n')
        continue

    # Running library update hook
    if 'self.verbose_log(f"Running library update hook: {self.config.hook_command}...")' in line and len(line.rstrip()) > 79:
        indent = "        "
        output.append(indent + 'self.verbose_log(\n')
        output.append(indent + '    f"Running library update hook: '\n')
        output.append(indent + '    {self.config.hook_command}..."\n')
        output.append(indent + ')\n')
        continue

    # threading.Thread
    if 'threading.Thread(target=self._run_hook_task, daemon=True).start()' in line and len(line.rstrip()) > 79:
        indent = "                "
        output.append(indent + 'threading.Thread(\n')
        output.append(indent + '    target=self._run_hook_task, daemon=True\n')
        output.append(indent + ').start()\n')
        continue

    # Curator returned HTTP
    if 'raise ValueError(f"Curator returned HTTP {response.status}")' in line and len(line.rstrip()) > 79:
        indent = "                    "
        output.append(indent + 'raise ValueError(\n')
        output.append(indent + '    f"Curator returned HTTP {response.status}"\n')
        output.append(indent + ')\n')
        continue

    # Failed to unrestrict
    if 'raise ValueError(f"Failed to unrestrict {source_url}: {exc}") from exc' in line and len(line.rstrip()) > 79:
        indent = "            "
        output.append(indent + 'raise ValueError(\n')
        output.append(indent + '    f"Failed to unrestrict {source_url}: {exc}"\n')
        output.append(indent + ') from exc\n')
        continue

    # Failed to resolve download link
    if 'f"Failed to resolve download link for {source_url}: {error_msg}"' in line and len(line.rstrip()) > 79:
        # This appears in a raise ValueError block
        indent = "                "
        output.append(indent + 'raise ValueError(\n')
        output.append(indent + '    f"Failed to resolve download link '\n')
        output.append(indent + '    for {source_url}: {error_msg}"\n')
        output.append(indent + ')\n')
        continue

    # Startup sync complete
    if 'record_event("Startup sync complete", event="startup_sync", report=report)' in line and len(line.rstrip()) > 79:
        indent = "            "
        output.append(indent + 'record_event(\n')
        output.append(indent + '    "Startup sync complete",\n')
        output.append(indent + '    event="startup_sync",\n')
        output.append(indent + '    report=report,\n')
        output.append(indent + ')\n')
        continue

    output.append(line)

with open("buzz/core/state.py", "w") as f:
    f.writelines(output)

print("Done writing file")
