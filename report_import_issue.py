"""Render reports/import_review.json (from spotify_to_apple.py) into a GitHub issue.

Same rules as report_to_issue.py, under its own `auto-import-review` label:
a new issue per run that has something to review, a comment instead when an
open issue for the same playlist was opened within the last 12 hours, and
nothing for a clean run (except that comment, so the thread shows the outcome).
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import review_state
from report_to_issue import CLUSTER_WINDOW, _gh
from spotify_match import MATCH_THRESHOLD

PROJECT_DIR = Path(__file__).parent
REPORT_FILE = PROJECT_DIR / "reports" / "import_review.json"
ISSUE_LABEL = "auto-import-review"

ACTION_WORDS = {
    "created": "Created a new Apple Music playlist",
    "updated": "Appended to the existing Apple Music playlist",
    "would_create": "Dry run: would create a new Apple Music playlist",
    "would_update": "Dry run: would append to the existing Apple Music playlist",
    "not_checked": "Dry run without APPLE_MUSIC_USER_TOKEN: Apple library not checked",
}


def _override_hint(spotify_id: str) -> str:
    return f"`/override spotify:track:{spotify_id} apple:song:<SONG_ID>`"


def _fmt_pick(c: dict) -> str:
    bits = [
        f"**{c['name']}** — {c['artist']}",
        f"_{c['album']}_" if c.get("album") else "",
        f"({c['release_date']})" if c.get("release_date") else "",
        f"score `{c['score']:.2f}`",
        f"song `{c['id']}`",
    ]
    return " · ".join(b for b in bits if b)


def has_review_items(report: dict) -> bool:
    return any(report.get(k) for k in (
        "error", "unmatched", "uncertain", "featured", "pending", "remove_by_hand", "out_of_order",
    ))


def _header(report: dict) -> list[str]:
    sp = report.get("spotify_playlist", {})
    ap = report.get("apple_playlist", {})
    lines = [
        f"_Generated {report.get('generated_at', '?')}_",
        "",
        f"- **Spotify:** [{sp.get('name', '?')}]({sp.get('url', '')})",
    ]
    if ap:
        lines.append(f"- **Apple Music:** {ACTION_WORDS.get(ap.get('action'), ap.get('action', '?'))}"
                     f" \"{ap.get('name', '?')}\"")
    if "resolved" in report:
        lines.append(f"- **Matched:** {report['resolved']}/{report.get('total', 0)} songs")
    if sp.get("basic_info"):
        lines.append("- **Note:** Spotify only allowed the basic track list for this playlist "
                     "(no album or ISRC), so songs were matched by title, artist and length.")
    if report.get("backup"):
        lines.append(f"- **Backup:** {', '.join(f'`{p}`' for p in report['backup'])} "
                     "(in the run's import-reports artifact)")
    lines.append("")
    return lines


def _render_error(report: dict) -> list[str]:
    if not report.get("error"):
        return []
    heading = "## Import paused (Apple rate limit)" if report.get("paused") else "## Import failed"
    return [heading, report["error"], ""]


def _render_fix_by_hand(report: dict) -> list[str]:
    lines: list[str] = []
    remove = report.get("remove_by_hand", [])
    if remove:
        lines.append(f"## Remove by hand ({len(remove)})")
        lines.append("_On the Apple playlist but not on Spotify. Apple's API can't remove songs, "
                     "so delete these in Apple Music; they stay listed until you do._")
        lines.extend(f"- **{t['name']}** — {t['artist']} (Apple position {t['apple_position']})"
                     for t in remove)
        lines.append("")
    moves = report.get("out_of_order", [])
    if moves:
        lines.append(f"## Out of order ({len(moves)})")
        lines.append("_The fewest songs to drag so the Apple order matches Spotify. "
                     "Newly added songs land at the end, so they usually appear here._")
        lines.extend(
            f"- **{t['name']}** — {t['artist']}: #{t['spotify_position']} on Spotify, "
            f"#{t['apple_position']} on Apple"
            for t in moves
        )
        lines.append("")
    return lines


def _render_matches(report: dict) -> list[str]:
    lines: list[str] = []
    unmatched = report.get("unmatched", [])
    if unmatched:
        lines.append(f"## Not found on Apple Music ({len(unmatched)})")
        lines.append("_No match in the Apple Music catalog, so nothing was added for these. "
                     "The song may be delisted or not available on Apple Music. If an Apple "
                     "version exists, point the import at it with `/override`._")
        for t in unmatched:
            album = f" · _{t['album']}_" if t.get("album") else ""
            isrc = f" · ISRC `{t['isrc']}`" if t.get("isrc") else ""
            lines.append(f"- **{t['name']}** — {t['artist']}{album}{isrc} (Spotify #{t['position']})")
            if t.get("spotify_id"):
                lines.append(f"  - fix: {_override_hint(t['spotify_id'])}")
            else:
                lines.append("  - local or unavailable Spotify track; can't be matched")
        lines.append("")

    featured = report.get("featured", [])
    if featured:
        lines.append(f"## Matched using featured artists ({len(featured)})")
        lines.append("_Added, but only matched once the featured artists in Apple's title "
                     "were counted (Spotify lists them as artists). Please check each pick; "
                     "if one is wrong, use `/override`._")
        for f in featured:
            s, picked = f["spotify"], f["picked"]
            lines.append(f"- **{s['name']}** — {s['artist']} (Spotify #{s['position']})")
            lines.append(f"  - Picked: {_fmt_pick(picked)} · score without featured artists "
                         f"`{f['score_without_featured']:.2f}`")
            lines.append(f"  - Fix if wrong: {_override_hint(s['spotify_id'])}")
        lines.append("")

    uncertain = report.get("uncertain", [])
    if uncertain:
        lines.append(f"## Uncertain ({len(uncertain)})")
        for u in uncertain:
            s, picked = u["spotify"], u["picked"]
            lines.append(
                f"<details><summary><b>{s['name']}</b> — {s['artist']} "
                f"(Spotify #{s['position']}) → picked score <code>{picked['score']:.2f}</code></summary>"
            )
            lines.append("")
            lines.append(
                f"- Spotify track `{s['spotify_id']}` — keep: `/accept {s['spotify_id']}` · "
                f"fix: {_override_hint(s['spotify_id'])}"
            )
            lines.append(f"- **Picked:** {_fmt_pick(picked)}")
            if u.get("alternates"):
                lines.append("- **Alternates:**")
                lines.extend(f"  - {_fmt_pick(a)}" for a in u["alternates"])
            lines.append("")
            lines.append("</details>")
            lines.append("")

    pending = report.get("pending", {})
    if pending:
        lines.append(f"## Pending from prior runs ({len(pending)})")
        lines.append(f"_Picks below the {MATCH_THRESHOLD} match threshold still in use. "
                     "Listed every run until resolved._")
        for tid, e in sorted(pending.items(), key=lambda kv: kv[1].get("picked_at", "")):
            lines.append(
                f"- **{e.get('name', '?')}** — {e.get('artist', '?')} · picked song "
                f"`{e.get('picked_id', '?')}` · score `{e.get('picked_score', 0):.2f}` · "
                f"picked {e.get('picked_at', '?')[:10]}"
            )
            lines.append(f"  - Keep: `/accept {tid}` · Fix: {_override_hint(tid)}")
        lines.append("")
    return lines


def _render_added(report: dict) -> list[str]:
    added = report.get("added", [])
    if not added:
        return []
    verb = "To add" if report.get("dry_run") else "Added"
    lines = [f"## {verb} this run ({len(added)})"]
    lines.extend(f"- {t['name']} — {t['artist']}" for t in added)
    lines.append("")
    return lines


def render_issue_body(report: dict) -> str:
    lines = _header(report) + _render_error(report)
    lines.append(
        "Commands, as comments on any open import review issue (only the repo owner's count):\n"
        "- `/override spotify:track:<id> apple:song:<id>` — use this Apple song instead.\n"
        "- `/accept <spotify track id>` — the pick is right; stop asking about it.\n\n"
        "The next import run picks them up. Close this issue when you're done with it."
    )
    lines.append("")
    lines += _render_fix_by_hand(report) + _render_matches(report) + _render_added(report)
    return "\n".join(lines).rstrip() + "\n"


def render_rerun_comment(report: dict, run_number: str) -> str:
    run = f" #{run_number}" if run_number else ""
    lines = [f"## Re-run{run} — {report.get('generated_at', '?').replace('T', ' ')}", ""]
    lines += _header(report)[2:] + _render_error(report)
    body = _render_fix_by_hand(report) + _render_matches(report) + _render_added(report)
    lines += body or ["Nothing to review."]
    return "\n".join(lines).rstrip() + "\n"


def _title_prefix(report: dict) -> str:
    return f"Import review needed: {report.get('spotify_playlist', {}).get('name', '?')} —"


def find_cluster_parent(repo: str, report: dict, now: datetime) -> str | None:
    """Newest open import issue for this playlist opened within CLUSTER_WINDOW."""
    issues = json.loads(_gh(
        "issue", "list", "--repo", repo, "--label", ISSUE_LABEL, "--state", "open",
        "--json", "number,createdAt,title", "--limit", "200",
    ) or "[]")
    prefix = _title_prefix(report)
    recent = [
        i for i in issues
        if i["title"].startswith(prefix)
        and now - review_state.parse_ts(i["createdAt"]) <= CLUSTER_WINDOW
    ]
    return str(max(recent, key=lambda i: i["createdAt"])["number"]) if recent else None


def _ensure_label(repo: str):
    try:
        _gh("label", "create", ISSUE_LABEL, "--repo", repo, "--force",
            "--color", "1D76DB", "--description", "Spotify → Apple Music import needs review")
    except subprocess.CalledProcessError:
        pass  # Creating the issue below reports any real permission problem.


def main() -> int:
    if not REPORT_FILE.exists():
        print("No import report — nothing to do.")
        return 0
    report = json.loads(REPORT_FILE.read_text())
    run_number = os.environ.get("GITHUB_RUN_NUMBER", "")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        print(render_issue_body(report))
        return 0

    body = ""
    try:
        parent = find_cluster_parent(repo, report, datetime.now(timezone.utc))
        if not has_review_items(report):
            if not parent:
                print("Import is clean. Nothing to post.")
                return 0
            body = render_rerun_comment(report, run_number)
            _gh("issue", "comment", parent, "--repo", repo, "--body", body)
            print(f"Clean re-run: commented outcome on issue #{parent}")
            return 0
        if parent:
            body = render_rerun_comment(report, run_number)
            _gh("issue", "comment", parent, "--repo", repo, "--body", body)
            print(f"Commented on issue #{parent}")
        else:
            body = render_issue_body(report)
            title = f"{_title_prefix(report)} {report.get('generated_at', '?').replace('T', ' ')}"
            if report.get("dry_run"):
                title += " (dry run)"
            if run_number:
                title += f" — run #{run_number}"
            _ensure_label(repo)
            _gh("issue", "create", "--repo", repo, "--label", ISSUE_LABEL,
                "--title", title, "--body", body)
            print("Created new import review issue")
    except subprocess.CalledProcessError:
        print("\n=== Posting the import review failed. Review body follows: ===\n", file=sys.stderr)
        print(body or render_issue_body(report))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
