#!/usr/bin/env python3
"""oc-sessions: view & delete opencode sessions across all projects.

Standalone tool, standard library only. Safe for PyInstaller packaging.
"""

import argparse
import datetime
import os
import shutil
import sqlite3
import subprocess
import sys
from collections import deque

APP_NAME = "oc-sessions"
DEFAULT_DATA_SUBDIR = os.path.join(".local", "share", "opencode")
DB_NAME = "opencode.db"
SNAPSHOT_DIR = "snapshot"

CHILD_TABLES = [
    "session_input",
    "session_message",
    "session_context_epoch",
    "session_share",
    "todo",
    "part",
    "message",
]


def detect_data_dir(override):
    if override:
        return os.path.abspath(override)
    if os.environ.get("OPENCODE_DB_PATH"):
        return os.path.dirname(os.path.abspath(os.environ["OPENCODE_DB_PATH"]))
    home = os.path.expanduser("~")
    xdg = os.environ.get("XDG_DATA_HOME")
    if sys.platform.startswith("win"):
        return os.path.join(home, *DEFAULT_DATA_SUBDIR.replace("/", os.sep).split(os.sep))
    base = xdg or os.path.join(home, ".local", "share")
    return os.path.join(base, "opencode")


def connect(db_path, read_only=False):
    if read_only:
        uri = f"file:{db_path.replace(os.sep, '/')}?mode=ro"
        con = sqlite3.connect(uri, uri=True)
    else:
        con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def check_not_running():
    if os.environ.get("OC_SESSIONS_SKIP_RUN_CHECK"):
        return
    try:
        if sys.platform.startswith("win"):
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq opencode.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            running = "opencode.exe" in out
        else:
            out = subprocess.run(
                ["pgrep", "-x", "opencode"], capture_output=True, text=True, timeout=10
            ).stdout
            running = bool(out.strip())
        if running:
            print("[!] opencode is running - please exit opencode first and retry")
            sys.exit(1)
    except Exception:
        pass


def open_writable(db_path):
    """Open a locked writable connection for deletion (with running-process check)."""
    check_not_running()
    con = connect(db_path, read_only=False)
    con.execute("PRAGMA busy_timeout=500")
    try:
        con.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        print("[!] cannot lock database - is opencode running? exit it and retry")
        sys.exit(1)
    return con


def fmt_time(ms):
    if not ms:
        return "-"
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def list_sessions(con, keyword=None):
    rows = con.execute(
        """
        SELECT s.id, s.title, s.parent_id, s.agent, s.time_updated, p.worktree AS proj_dir
        FROM session s LEFT JOIN project p ON p.id = s.project_id
        ORDER BY s.time_updated DESC
        """
    ).fetchall()
    if keyword:
        kw = keyword.lower()
        rows = [
            r for r in rows
            if kw in ((r["title"] or "") + " " + (r["proj_dir"] or "") + " " + r["id"]).lower()
        ]
    return rows


def show_list(rows):
    if not rows:
        print("(no sessions match)")
        return
    print(f"total: {len(rows)} sessions\n")
    print(f"{'#':>3}  {'updated':<16} {'type':<8} project / title")
    print("-" * 100)
    for i, r in enumerate(rows, 1):
        kind = "sub" if r["parent_id"] else (r["agent"] or "main")
        title = (r["title"] or "").replace("\n", " ")[:45]
        proj = r["proj_dir"] or "global"
        mark = "  |_" if r["parent_id"] else ""
        print(f"{i:>3}. {fmt_time(r['time_updated']):<16} {kind:<8} {mark}{proj} | {title}")
        print(f"      id: {r['id']}")


def find_session(con, session_id):
    return con.execute("SELECT * FROM session WHERE id=?", (session_id,)).fetchone()


def discover_child_tables(con):
    """Find tables referencing sessions: known list + FK declarations + any session_id column."""
    cols = {t: "session_id" for t in CHILD_TABLES}
    for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        name = row["name"]
        try:
            fks = con.execute(f'PRAGMA foreign_key_list("{name}")').fetchall()
        except sqlite3.Error:
            continue
        for fk in fks:
            if fk["table"] == "session":
                cols.setdefault(name, fk["from"])
    for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        name = row["name"]
        if name in cols:
            continue
        try:
            cols_i = con.execute(f'PRAGMA table_info("{name}")').fetchall()
        except sqlite3.Error:
            continue
        if any(c["name"] == "session_id" for c in cols_i):
            cols[name] = "session_id"
    return cols


def collect_descendants(con, session_id):
    ids = [session_id]
    queue = deque([session_id])
    while queue:
        cur = queue.popleft()
        for c in con.execute("SELECT id FROM session WHERE parent_id=?", (cur,)):
            ids.append(c["id"])
            queue.append(c["id"])
    return ids


def delete_session_rows(con, session_id):
    ids = collect_descendants(con, session_id)
    con.execute("PRAGMA foreign_keys=ON")
    tables = discover_child_tables(con)
    for sid in reversed(ids):
        con.execute("DELETE FROM event WHERE aggregate_id=?", (sid,))
        con.execute("DELETE FROM event_sequence WHERE aggregate_id=?", (sid,))
        for t, col in tables.items():
            con.execute(f"DELETE FROM {t} WHERE {col}=?", (sid,))
        con.execute("DELETE FROM session WHERE id=?", (sid,))
    return ids


def backup_db(db_path, backup_dir):
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    dest = os.path.join(backup_dir, stamp)
    os.makedirs(dest, exist_ok=True)
    target = os.path.join(dest, DB_NAME)
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(target)
    try:
        with dst:
            src.backup(dst)
    except Exception:
        dst.close()
        src.close()
        try:
            os.remove(target)
        except OSError:
            pass
        raise
    dst.close()
    src.close()
    print(f"[i] backup saved to: {dest}")
    return dest


def cleanup_snapshots(con, data_dir, project_id):
    if not project_id:
        return
    remain = con.execute("SELECT count(*) FROM session WHERE project_id=?", (project_id,)).fetchone()[0]
    if remain > 0:
        return
    snap = os.path.join(data_dir, SNAPSHOT_DIR, project_id)
    if os.path.isdir(snap):
        try:
            shutil.rmtree(snap)
            print(f"[i] removed snapshot dir: snapshot\\{project_id[:12]}...")
        except OSError as e:
            print(f"[!] failed to remove snapshot dir (db deletion already done): {snap}")
            print(f"    reason: {e}")


def cmd_list(con, args):
    show_list(list_sessions(con, args.keyword))


def cmd_delete(con, db_path, data_dir, backup_dir, args):
    s = find_session(con, args.session)
    if not s:
        print(f"[!] session not found: {args.session}")
        sys.exit(1)
    ids = collect_descendants(con, args.session)
    label = "sub-session" if s["parent_id"] else "main session"
    extra = f" + {len(ids) - 1} sub-session(s)" if len(ids) > 1 else ""
    print(f"[i] will delete 1 {label}{extra}:")
    for sid in ids:
        info = find_session(con, sid)
        print(f"    - {sid}  {(info['title'] or '')[:50]}")
    if not args.yes and input("confirm delete? [y/N] ").lower() != "y":
        print("cancelled")
        return
    backup_db(db_path, backup_dir)
    deleted = delete_session_rows(con, args.session)
    con.commit()
    print(f"[OK] deleted {len(deleted)} session(s)")
    cleanup_snapshots(con, data_dir, s["project_id"])


def cmd_delete_project(con, db_path, data_dir, backup_dir, args):
    def esc(w):
        return w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    projects = con.execute(
        "SELECT id, worktree FROM project WHERE lower(worktree) LIKE ? ESCAPE '\\'",
        (f"%{esc(args.dir.lower())}%",),
    ).fetchall()
    if not projects:
        print(f"[!] no project matches '{args.dir}'")
        return
    targets = []
    for p in projects:
        sess = con.execute("SELECT id FROM session WHERE project_id=?", (p["id"],)).fetchall()
        targets.append((p, sess))
    for p, sess in targets:
        print(f"[i] project: {p['worktree']} ({p['id'][:12]}...)  -> {len(sess)} session(s)")
    if not args.yes and input("confirm delete all above? [y/N] ").lower() != "y":
        print("cancelled")
        return
    backup_db(db_path, backup_dir)
    deleted_all = []
    for p, sess in targets:
        for row in sess:
            deleted_all.extend(delete_session_rows(con, row["id"]))
    con.commit()
    print(f"[OK] deleted {len(set(deleted_all))} session(s)")
    for p, sess in targets:
        cleanup_snapshots(con, data_dir, p["id"])


def cmd_interactive(con, db_path, data_dir, backup_dir):
    rows = list_sessions(con, None)
    show_list(rows)
    print()
    try:
        n = int(input("enter number to delete (enter to cancel): "))
    except (ValueError, EOFError):
        print("cancelled")
        return
    if not 1 <= n <= len(rows):
        print("invalid number")
        return
    print()
    con = open_writable(db_path)
    try:
        cmd_delete(con, db_path, data_dir, backup_dir, argparse.Namespace(session=rows[n - 1]["id"], yes=False))
    finally:
        con.close()


def build_parser():
    parser = argparse.ArgumentParser(
        prog="oc-sessions",
        description="View and delete opencode sessions across all projects.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  oc-sessions                      interactive: list all, pick a number to delete\n"
            "  oc-sessions list                 list all sessions (all projects)\n"
            "  oc-sessions list <keyword>       filter by keyword (title / project / id)\n"
            "  oc-sessions delete <id>          delete a session incl. its sub-sessions\n"
            "  oc-sessions delete <id> --yes    skip confirmation\n"
            "  oc-sessions delete-project <dir> delete all sessions of matching project\n"
        ),
    )
    parser.add_argument("--data-dir", metavar="PATH", help="opencode data dir (auto-detected by default)")
    parser.add_argument("--backup-dir", metavar="PATH", help="backup location (default: <data-dir>/../oc-sessions-backups)")
    sub = parser.add_subparsers(dest="cmd")
    p_list = sub.add_parser("list", help="list all sessions (optional keyword filter)")
    p_list.add_argument("keyword", nargs="?", default=None)
    p_del = sub.add_parser("delete", help="delete a session including its sub-sessions")
    p_del.add_argument("session")
    p_del.add_argument("--yes", action="store_true")
    p_dp = sub.add_parser("delete-project", help="delete all sessions of a matching project")
    p_dp.add_argument("dir")
    p_dp.add_argument("--yes", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    data_dir = detect_data_dir(args.data_dir)
    db_path = os.path.join(data_dir, DB_NAME)
    if not os.path.exists(db_path):
        print(f"[!] database not found: {db_path}")
        print("    pass --data-dir <path> if your opencode data lives elsewhere")
        sys.exit(1)

    if args.cmd in ("delete", "delete-project"):
        con = open_writable(db_path)
    else:
        con = connect(db_path, read_only=True)

    backup_dir = args.backup_dir or os.path.join(os.path.dirname(os.path.abspath(data_dir)), "oc-sessions-backups")

    if args.cmd == "list":
        cmd_list(con, args)
    elif args.cmd == "delete":
        cmd_delete(con, db_path, data_dir, backup_dir, args)
    elif args.cmd == "delete-project":
        cmd_delete_project(con, db_path, data_dir, backup_dir, args)
    else:
        cmd_interactive(con, db_path, data_dir, backup_dir)
    con.close()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
