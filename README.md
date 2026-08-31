# oc-sessions

View and delete opencode sessions across **all projects**, not just the current one.

`opencode session list` only shows sessions of the project in the current directory and
hides sub-agent sessions. This tool reads the opencode SQLite database directly and shows
everything, with safe, backed-up deletion.

## Requirements

- Python 3 (standard library only, no dependencies)
- To build the EXE: PyInstaller

## Usage

```
oc-sessions                      interactive: list all sessions, pick a number to delete
oc-sessions list                 list all sessions (all projects, incl. sub-sessions)
oc-sessions list <keyword>       filter by keyword (matches title / project path / id)
oc-sessions delete <id>          delete one session incl. all its sub-sessions
oc-sessions delete <id> --yes    same, skip confirmation
oc-sessions delete-project <dir> delete all sessions of a matching project
```

### Options

| Option | Description |
|---|---|
| `--data-dir <path>` | opencode data directory (auto-detected if omitted) |
| `--backup-dir <path>` | backup location (default: `<data-dir>/../oc-sessions-backups`) |

### Examples

```
oc-sessions list
oc-sessions list stock
oc-sessions delete ses_xxxxxxxx
oc-sessions delete-project market
oc-sessions --data-dir C:\opencode-data list
```

## Output

```
total: 29 sessions

  #  updated          type     project / title
  1. 2026-08-27 01:01 build    / | Some session title
      id: ses_fc1694...
```

- `type`: `build` / `plan` = main session, `sub` = sub-agent session (deleted automatically
  together with its parent)
- `project`: `/` = global project (not inside a git repo), otherwise the project path

## Safety

Deletion is safe by design:

1. **Running check** - refuses to run if `opencode` is running (database lock conflicts).
   Exit opencode first.
2. **Automatic backup** - the full database is backed up via SQLite's online backup API to
   `<backup-dir>/<timestamp>/opencode.db` before anything is deleted.
3. **Cascade cleanup** - removes messages, parts, todos, events and all related rows.
   Child tables are auto-discovered (foreign keys + `session_id` columns), so future
   opencode schema changes are handled automatically.
4. **Sub-sessions** - deleting a parent session recursively deletes its sub-sessions.
5. **Snapshot cleanup** - when a project's last session is deleted, its `snapshot/` folder is
   removed (failure is only a warning; it never blocks the database deletion).

## Restore from backup

```
1. Exit opencode.
2. Copy the latest backup over the live database:
   copy "<backup-dir>\<timestamp>\opencode.db" "%USERPROFILE%\.local\share\opencode\opencode.db"
3. Remove leftover journal files so the restored DB is used cleanly:
   del "%USERPROFILE%\.local\share\opencode\opencode.db-wal"
   del "%USERPROFILE%\.local\share\opencode\opencode.db-shm"
```

## Build the EXE

```
build.bat
```

The single-file executable is written to `dist\oc-sessions.exe`. No Python needed on the
target machine.

## Notes

- Always exit opencode before deleting sessions.
- Backups accumulate over time; clean the backup folder periodically.
- The database uses WAL mode; do not manually delete `opencode.db-wal` / `opencode.db-shm`
  during normal use (only when restoring a backup as shown above).
- For testing only: `OC_SESSIONS_SKIP_RUN_CHECK=1` disables the running-process check.
