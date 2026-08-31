#!/usr/bin/env python3
"""oc-sessions: view & delete opencode sessions across all projects.

Standalone tool, standard library only. Safe for PyInstaller packaging.
"""

import argparse
import datetime
import os
import re
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
    except Exception as e:
        print(f"[!] could not verify opencode process state ({e}); continuing anyway")


def open_writable(db_path):
    """Open a locked writable connection for deletion (with running-process check)."""
    check_not_running()
    con = connect(db_path, read_only=False)
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=5000")
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
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            print(f"[!] skipping table with suspicious name: {name!r}")
            continue
        try:
            fks = con.execute(f'PRAGMA foreign_key_list("{name}")').fetchall()
        except sqlite3.Error:
            continue
        for fk in fks:
            if fk["table"] == "session":
                cols.setdefault(name, fk["from"])
    for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        name = row["name"]
        if name in cols or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        try:
            cols_i = con.execute(f'PRAGMA table_info("{name}")').fetchall()
        except sqlite3.Error:
            continue
        if any(c["name"] == "session_id" for c in cols_i):
            cols[name] = "session_id"
            print(f"[!] heuristic: '{name}' has a session_id column but no FK to session; will delete by it")
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
            shutil.rmtree(dest)
        except OSError:
            pass
        raise
    dst.close()
    src.close()
    print(f"[i] backup saved to: {dest}")
    return dest


def cmd_vacuum(con, db_path):
    before = os.path.getsize(db_path)
    print(f"[i] db size before: {before / 1024 / 1024:.2f} MB")
    try:
        con.execute("VACUUM")
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.commit()
    except sqlite3.Error as e:
        print(f"[!] vacuum failed: {e}")
        print("    database left unchanged")
        sys.exit(1)
    after = os.path.getsize(db_path)
    freed = (before - after) / 1024 / 1024
    print(f"[OK] vacuum done: {after / 1024 / 1024:.2f} MB (freed {freed:.2f} MB)")


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
            print(f"[i] removed snapshot dir: {SNAPSHOT_DIR}{os.sep}{project_id[:12]}...")
        except OSError as e:
            print(f"[!] failed to remove snapshot dir (db deletion already done): {snap}")
            print(f"    reason: {e}")


def cmd_list(con, args):
    show_list(list_sessions(con, args.keyword))


def expand_delete_set(con, session_ids):
    """Validate all ids, then expand to descendant sets with sub-session dedupe."""
    missing = []
    rows = {}
    for sid in session_ids:
        r = find_session(con, sid)
        if not r:
            missing.append(sid)
        else:
            rows[sid] = r
    if missing:
        print("[!] sessions not found:")
        for sid in missing:
            print(f"    - {sid}")
        print("    nothing was deleted")
        sys.exit(1)
    seen = set()
    targets = []
    for sid in session_ids:
        if sid in seen:
            continue
        ids = collect_descendants(con, sid)
        fresh = [i for i in ids if i not in seen]
        seen.update(ids)
        is_sub = bool(rows[sid]["parent_id"])
        targets.append((sid, is_sub, fresh[1:]))
    return targets


def cmd_delete(con, db_path, data_dir, backup_dir, args):
    targets = expand_delete_set(con, args.sessions)
    total = sum(1 + len(subs) for _, _, subs in targets)
    tag = "[dry-run] " if args.dry_run else ""
    print(f"{tag}[i] will delete {total} session(s) from {len(targets)} selection(s):")
    for root, is_sub, subs in targets:
        info = find_session(con, root)
        mark = "[sub]" if is_sub else "[sel]"
        print(f"    {mark} {root}  {(info['title'] or '')[:50]}")
        for sid in subs:
            info2 = find_session(con, sid)
            print(f"          |_ {sid}  {(info2['title'] or '')[:50]}")
    if args.dry_run:
        print("[dry-run] nothing was deleted")
        return
    if not args.yes and input("confirm delete? [y/N] ").lower() != "y":
        print("cancelled")
        return
    backup_db(db_path, backup_dir)
    projects = set()
    try:
        for root, _, _ in targets:
            info = find_session(con, root)
            projects.add(info["project_id"])
            delete_session_rows(con, root)
    except sqlite3.Error as e:
        con.rollback()
        print(f"[!] delete failed: {e}")
        print("    transaction rolled back - nothing was committed")
        sys.exit(1)
    con.commit()
    print(f"[OK] deleted {total} session(s)")
    for pid in projects:
        cleanup_snapshots(con, data_dir, pid)


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
    if args.dry_run:
        print("[dry-run] nothing was deleted")
        return
    if not args.yes and input("confirm delete all above? [y/N] ").lower() != "y":
        print("cancelled")
        return
    backup_db(db_path, backup_dir)
    deleted_all = []
    try:
        for p, sess in targets:
            for row in sess:
                deleted_all.extend(delete_session_rows(con, row["id"]))
    except sqlite3.Error as e:
        con.rollback()
        print(f"[!] delete failed: {e}")
        print("    transaction rolled back - nothing was committed")
        sys.exit(1)
    con.commit()
    print(f"[OK] deleted {len(set(deleted_all))} session(s)")
    for p, sess in targets:
        cleanup_snapshots(con, data_dir, p["id"])


def cmd_interactive(con, db_path, data_dir, backup_dir):
    rows = list_sessions(con, None)
    show_list(rows)
    print()
    try:
        raw = input("enter numbers to delete, space-separated, or 'vacuum' to reclaim space (enter to cancel): ")
    except EOFError:
        print("cancelled")
        return
    if raw.strip().lower() == "vacuum":
        try:
            con.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        cmd_vacuum(con, db_path)
        return
    tokens = raw.split()
    if not tokens:
        print("cancelled")
        return
    nums = []
    for t in tokens:
        try:
            n = int(t)
        except ValueError:
            print(f"[!] invalid number: {t}")
            print("    nothing was deleted")
            return
        if not 1 <= n <= len(rows):
            print(f"[!] number out of range: {n}")
            print("    nothing was deleted")
            return
        nums.append(n)
    ids = [rows[n - 1]["id"] for n in dict.fromkeys(nums)]
    print()
    cmd_delete(con, db_path, data_dir, backup_dir, argparse.Namespace(sessions=ids, yes=False, dry_run=False))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="oc-sessions",
        description="View and delete opencode sessions across all projects.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  oc-sessions                      interactive: list all, pick numbers to delete\n"
            "  oc-sessions list                 list all sessions (all projects)\n"
            "  oc-sessions list <keyword>       filter by keyword (title / project / id)\n"
            "  oc-sessions delete <id> [<id>...] delete one or more sessions (each incl. its sub-sessions)\n"
            "  oc-sessions delete <id> --dry-run  preview deletion without changing anything\n"
            "  oc-sessions delete <id> --yes    skip confirmation\n"
            "  oc-sessions delete-project <dir> delete all sessions of matching project\n"
            "  oc-sessions vacuum             reclaim free space after deletions\n"
        ),
    )
    parser.add_argument("--data-dir", metavar="PATH", help="opencode data dir (auto-detected by default)")
    parser.add_argument("--backup-dir", metavar="PATH", help="backup location (default: <data-dir>/../oc-sessions-backups)")
    sub = parser.add_subparsers(dest="cmd")
    p_list = sub.add_parser("list", help="list all sessions (optional keyword filter)")
    p_list.add_argument("keyword", nargs="?", default=None)
    p_del = sub.add_parser("delete", help="delete one or more sessions including their sub-sessions")
    p_del.add_argument("sessions", nargs="+", metavar="id")
    p_del.add_argument("--yes", action="store_true")
    p_del.add_argument("--dry-run", action="store_true", help="show what would be deleted without deleting")
    p_dp = sub.add_parser("delete-project", help="delete all sessions of a matching project")
    p_dp.add_argument("dir")
    p_dp.add_argument("--yes", action="store_true")
    p_dp.add_argument("--dry-run", action="store_true", help="show what would be deleted without deleting")
    sub.add_parser("vacuum", help="reclaim free space: DELETE frees pages but never shrinks the file")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    data_dir = detect_data_dir(args.data_dir)
    if args.data_dir and not os.path.isdir(args.data_dir):
        print(f"[!] data dir not found: {args.data_dir}")
        sys.exit(1)
    db_path = os.path.join(data_dir, DB_NAME)
    if not os.path.exists(db_path):
        print(f"[!] database not found: {db_path}")
        print("    pass --data-dir <path> if your opencode data lives elsewhere")
        sys.exit(1)

    if args.cmd in ("delete", "delete-project") or args.cmd is None:
        con = open_writable(db_path)
    elif args.cmd == "vacuum":
        check_not_running()
        con = connect(db_path, read_only=False)
    else:
        con = connect(db_path, read_only=True)

    backup_dir = args.backup_dir or os.path.join(os.path.dirname(os.path.abspath(data_dir)), "oc-sessions-backups")

    if args.cmd == "list":
        cmd_list(con, args)
    elif args.cmd == "delete":
        cmd_delete(con, db_path, data_dir, backup_dir, args)
    elif args.cmd == "delete-project":
        cmd_delete_project(con, db_path, data_dir, backup_dir, args)
    elif args.cmd == "vacuum":
        cmd_vacuum(con, db_path)
    else:
        cmd_interactive(con, db_path, data_dir, backup_dir)
    con.close()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
