"""Render reports/needs_review.json into a GitHub Issue. See ARCHITECTURE.md."""

import json
import os
import sys
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import review_state
from spotify_match import MATCH_THRESHOLD

PROJECT_DIR = Path(__file__).parent
REPORT_FILE = PROJECT_DIR / "reports" / "needs_review.json"
ISSUE_LABEL = "auto-sync-review"
# Re-runs within this window comment on the latest open review issue instead
# of opening a new one (e.g. manual re-triggers while debugging).
CLUSTER_WINDOW = timedelta(hours=12)


def _track_id(uri: str) -> str:
    return uri.rsplit(":", 1)[-1] if uri else ""


def _fmt_candidate(c: dict) -> str:
    bits = [
        f"**{c['name']}** — {c['artist']}",
        f"_{c['album']}_" if c.get("album") else "",
        f"({c['album_type']}, {c['release_date']})" if c.get("album_type") else "",
        f"score `{c['score']:.2f}`",
        f"`{_track_id(c['uri'])}`",
    ]
    return " · ".join(b for b in bits if b)


def _fmt_apple_neighborhood(a: dict) -> list[str]:
    pos = a.get("position", "?")
    total = a.get("total", "?")
    lines = [f"  > _Apple #{pos}/{total}_"]
    base = pos - len(a.get("before", [])) if isinstance(pos, int) else pos
    for i, n in enumerate(a.get("before", [])):
        lines.append(f"  > #{base + i} _{n['name']}_ — {n['artist']}")
    lines.append(f"  > **#{pos} {a.get('name', '')} — {a.get('artist', '')}** ←")
    if isinstance(pos, int):
        for i, n in enumerate(a.get("after", []), start=1):
            lines.append(f"  > #{pos + i} _{n['name']}_ — {n['artist']}")
    return lines


def _this_run_ids(report: dict) -> set[str]:
    ids: set[str] = set()
    for p in report.get("playlists", []):
        ids.update(t.get("apple_id", "") for t in p.get("unmatched", []))
        ids.update(u["apple"].get("apple_id", "") for u in p.get("uncertain", []))
    ids.discard("")
    return ids


def prior_pending(report: dict, pending: dict) -> dict:
    """Pending entries not already shown as this run's uncertain items."""
    shown = _this_run_ids(report)
    return {aid: e for aid, e in pending.items() if aid not in shown}


def _render_changes(changes: dict | None) -> list[str]:
    """Song-level added/removed lists (empty when nothing changed)."""
    if not changes:
        return []
    lines: list[str] = []
    for label, key in (("Added", "added"), ("Removed", "removed")):
        tracks = changes.get(key, [])
        if tracks:
            lines.append(f"- **{label} ({len(tracks)}):**")
            lines.extend(f"  - {t['name']} — {t['artist']}" for t in tracks)
    if changes.get("reordered"):
        lines.append(f"- **Reordered:** {changes['reordered']} moves")
    return lines


def _render_errors(report: dict) -> list[str]:
    errored = [p for p in report.get("playlists", []) if p.get("error")]
    if not errored:
        return []
    lines = ["## ⚠️ Sync failed"]
    for p in errored:
        lines.append(f"### {p['name']}")
        lines.append(p["error"])
        planned = _render_changes(p.get("planned_changes"))
        if planned:
            lines.append("")
            lines.append("Changes the run was making when it failed (some may already be applied):")
            lines.extend(planned)
        lines.append("")
    return lines


def _render_run_changes(report: dict) -> list[str]:
    """What each successful playlist sync changed on Spotify this run."""
    lines: list[str] = []
    for p in report.get("playlists", []):
        if p.get("error") or "resolved" not in p:
            continue
        lines.append(
            f"**{p['name']}** — ✅ synced, {p['resolved']}/{p.get('apple_count', 0)} resolved"
        )
        changed = _render_changes(p.get("changes"))
        lines.extend(changed or ["- No changes to Spotify."])
        lines.append("")
    return lines


def _fmt_version(v: dict) -> str:
    run = f"run #{v['run']}" if v.get("run") else "local run"
    return f"first synced {v.get('first_synced_at', '?')}, last synced {v.get('last_synced_at', '?')} ({run})"


def _render_alerts(report: dict) -> list[str]:
    """Revert / outside-change alerts. Informational: the sync still ran."""
    flagged = [(p, a) for p in report.get("playlists", []) for a in p.get("alerts", [])]
    if not flagged:
        return []
    lines = [
        "## 🔎 Playlist changed outside the sync",
        "_The sync still ran. Recorded for proof; full version history is in "
        "`cache/playlist_history.json`._",
        "",
    ]
    for p, a in flagged:
        side = "Apple Music" if a["kind"] == "apple_revert" else "Spotify"
        lines.append(f"### {p['name']} — {side}")
        lines.append(a["summary"])
        last = a.get("last_version") or {}
        lines.append(f"- Last synced version: {_fmt_version(last)}")
        matched = a.get("matched_version")
        if matched:
            lines.append(f"- Matches older version: {_fmt_version(matched)}")
        for label, key in (("Missing vs last sync", "removed"), ("New vs last sync", "added")):
            tracks = a.get(key, [])
            if tracks:
                lines.append(f"- **{label} ({len(tracks)}):**")
                lines.extend(f"  - {t['name']} — {t['artist']}" for t in tracks)
        lines.append("")
    return lines


def _render_playlist(p: dict) -> list[str]:
    unmatched = p.get("unmatched", [])
    uncertain = p.get("uncertain", [])
    if not unmatched and not uncertain:
        return []

    lines = [f"## {p['name']}", f"_{p.get('resolved', 0)}/{p.get('apple_count', 0)} resolved_", ""]

    if unmatched:
        lines.append(f"### Unmatched ({len(unmatched)})")
        for t in unmatched:
            album = f" · _{t['album']}_" if t.get("album") else ""
            isrc = f" · ISRC `{t['isrc']}`" if t.get("isrc") else ""
            lines.append(f"- **{t['name']}** — {t['artist']}{album}{isrc}")
            if t.get("apple_id"):
                lines.append(
                    f"  - Apple ID: `{t['apple_id']}` — fix: "
                    f"`/override {t['apple_id']} spotify:track:<TRACK_ID>`"
                )
            lines.extend(_fmt_apple_neighborhood(t))
        lines.append("")

    if uncertain:
        lines.append(f"### Uncertain ({len(uncertain)})")
        for u in uncertain:
            a = u["apple"]
            picked = u["picked"]
            alternates = u.get("alternates", [])
            lines.append(
                f"<details><summary>"
                f"<b>{a['name']}</b> — {a['artist']} "
                f"(Apple #{a.get('position', '?')}/{a.get('total', '?')}) "
                f"→ picked score <code>{picked['score']:.2f}</code>"
                f"</summary>"
            )
            lines.append("")
            lines.append(f"- Apple album: _{a.get('album', '—')}_  ·  ISRC `{a.get('isrc', '—')}`")
            if a.get("apple_id"):
                lines.append(
                    f"- Apple ID: `{a['apple_id']}` — keep: `/accept {a['apple_id']}` · "
                    f"fix: `/override {a['apple_id']} spotify:track:<TRACK_ID>`"
                )
            lines.append("- Apple context:")
            for line in _fmt_apple_neighborhood(a):
                lines.append("  " + line.lstrip())
            lines.append(f"- **Picked:** {_fmt_candidate(picked)}")
            if alternates:
                lines.append("- **Alternates:**")
                for alt in alternates:
                    lines.append(f"  - {_fmt_candidate(alt)}")
            lines.append("")
            lines.append("</details>")
            lines.append("")
    return lines


def _render_pending(pending: dict) -> list[str]:
    if not pending:
        return []
    lines = [
        f"## Pending from prior runs ({len(pending)})",
        "_Picks below the match threshold that are still in use on Spotify. "
        "They keep appearing in every review issue until resolved._",
        "",
    ]
    ordered = sorted(pending.items(), key=lambda kv: kv[1].get("picked_at", ""))
    for aid, e in ordered:
        track_id = _track_id(e.get("picked_uri", ""))
        picked_on = e.get("picked_at", "?")[:10]
        lines.append(
            f"- **{e.get('name', '?')}** — {e.get('artist', '?')} "
            f"({e.get('playlist', '?')}) · Apple ID `{aid}`"
        )
        lines.append(
            f"  - Current pick: [`{track_id}`](https://open.spotify.com/track/{track_id}) · "
            f"score `{e.get('picked_score', 0):.2f}` · picked {picked_on}"
        )
        lines.append(
            f"  - Keep: `/accept {aid}` · Fix: `/override {aid} spotify:track:<TRACK_ID>`"
        )
    lines.append("")
    return lines


def render_issue_body(report: dict, pending: dict) -> str:
    playlists = report.get("playlists", [])
    total_unmatched = sum(len(p.get("unmatched", [])) for p in playlists)
    total_uncertain = sum(len(p.get("uncertain", [])) for p in playlists)
    prior = prior_pending(report, pending)

    lines: list[str] = []
    lines.append(f"_Generated {report.get('generated_at', '?')}_")
    lines.append("")
    lines.extend(_render_errors(report))
    lines.extend(_render_alerts(report))
    lines.append(f"- **Unmatched:** {total_unmatched}")
    lines.append(f"- **Uncertain (score < {MATCH_THRESHOLD}):** {total_uncertain}")
    lines.append(f"- **Pending from prior runs:** {len(prior)}")
    lines.append("")
    lines.append(
        "Resolve entries by commenting on any open review issue:\n"
        "- `/override <apple_id> spotify:track:<track_id>` — the pick is wrong; use this track instead.\n"
        "- `/accept <apple_id>` — the pick is right; stop asking about it.\n\n"
        "Only the repo owner's comments are applied. The next sync run picks them up. "
        "Close this issue when you're done with it."
    )
    lines.append("")

    for p in playlists:
        lines.extend(_render_playlist(p))
    lines.extend(_render_pending(prior))
    run_changes = _render_run_changes(report)
    if run_changes:
        lines.append("## Spotify changes this run")
        lines.extend(run_changes)

    return "\n".join(lines).rstrip() + "\n"


def render_rerun_comment(report: dict, pending: dict, run_number: str) -> str:
    playlists = report.get("playlists", [])
    prior = prior_pending(report, pending)
    run = f" #{run_number}" if run_number else ""

    lines = [f"## Re-run{run} — {_display_time(report)}", ""]
    lines.extend(_render_run_changes(report))
    lines.extend(_render_errors(report))
    lines.extend(_render_alerts(report))

    new_items: list[str] = []
    for p in playlists:
        new_items.extend(_render_playlist(p))
    if new_items:
        lines.append("New items:")
        lines.append("")
        lines.extend(new_items)
    else:
        lines.append("New items:")
        lines.append("- (none)")
        lines.append("")

    lines.append(f"Pending from prior runs: {len(prior)}")
    for aid, e in sorted(prior.items(), key=lambda kv: kv[1].get("picked_at", "")):
        lines.append(f"- **{e.get('name', '?')}** — {e.get('artist', '?')} · `{aid}`")
    return "\n".join(lines).rstrip() + "\n"


def has_review_items(report: dict, pending: dict) -> bool:
    if pending:
        return True
    for p in report.get("playlists", []):
        if p.get("unmatched") or p.get("uncertain") or p.get("error") or p.get("alerts"):
            return True
    return False


def _gh(*args: str) -> str:
    result = subprocess.run(
        ["gh", *args],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        # Surface what `gh` actually said. Without this, capture_output swallows
        # stderr and the workflow log only shows a Python traceback with the
        # command's arg list — enough to see what was called, not why it failed.
        print(f"gh {' '.join(args)} failed (exit {result.returncode}):", file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
    return result.stdout.strip()


def find_cluster_parent(repo: str, now: datetime) -> str | None:
    """Newest open review issue created within CLUSTER_WINDOW, if any."""
    out = _gh(
        "issue", "list",
        "--repo", repo,
        "--label", ISSUE_LABEL,
        "--state", "open",
        "--json", "number,createdAt",
        "--limit", "200",
    )
    issues = json.loads(out) if out else []
    recent = [
        i for i in issues
        if now - review_state.parse_ts(i["createdAt"]) <= CLUSTER_WINDOW
    ]
    if not recent:
        return None
    newest = max(recent, key=lambda i: i["createdAt"])
    return str(newest["number"])


def _display_time(report: dict) -> str:
    return report.get("generated_at", "?").replace("T", " ")


def main():
    if not REPORT_FILE.exists():
        print("No report file — nothing to do.")
        return 0

    report = json.loads(REPORT_FILE.read_text())
    pending = review_state.load_pending()
    needs_review = has_review_items(report, pending)
    run_number = os.environ.get("GITHUB_RUN_NUMBER", "")
    repo = os.environ.get("GITHUB_REPOSITORY")

    if not needs_review:
        # A clean run normally posts nothing. But if it's a re-run within the
        # cluster window of an open review issue, record the outcome there so
        # the thread shows how that issue resolved.
        parent = find_cluster_parent(repo, datetime.now(timezone.utc)) if repo else None
        if not parent:
            print("Report is clean (no errors, alerts, unmatched, uncertain or pending). Nothing to post.")
            return 0
        body = render_rerun_comment(report, pending, run_number)
        try:
            _gh("issue", "comment", parent, "--repo", repo, "--body", body)
        except subprocess.CalledProcessError:
            print(body)
            return 1
        print(f"Clean re-run: commented outcome on issue #{parent}")
        return 0

    if not repo:
        print("GITHUB_REPOSITORY not set; printing the body to stdout instead.")
        print(render_issue_body(report, pending))
        return 0

    title = f"Sync review needed — {_display_time(report)}"
    if run_number:
        title += f" — run #{run_number}"

    body = ""
    try:
        parent = find_cluster_parent(repo, datetime.now(timezone.utc))
        if parent:
            body = render_rerun_comment(report, pending, run_number)
            _gh("issue", "comment", parent, "--repo", repo, "--body", body)
            print(f"Commented on issue #{parent} (created within the last {CLUSTER_WINDOW})")
        else:
            body = render_issue_body(report, pending)
            _gh(
                "issue", "create",
                "--repo", repo,
                "--label", ISSUE_LABEL,
                "--title", title,
                "--body", body,
            )
            print("Created new review issue")
    except subprocess.CalledProcessError:
        # Posting failed — but the review items are the whole point of running
        # this script, so dump the rendered body to the workflow log where it
        # stays visible, then exit non-zero so the failure email fires.
        print("\n=== Posting the review failed. Review body follows: ===\n", file=sys.stderr)
        print(body or render_issue_body(report, pending))
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
