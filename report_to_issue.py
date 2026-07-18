"""Render reports/needs_review.json into a GitHub Issue. See ARCHITECTURE.md."""

import json
import os
import sys
import subprocess
from pathlib import Path

PROJECT_DIR = Path(__file__).parent
REPORT_FILE = PROJECT_DIR / "reports" / "needs_review.json"
ISSUE_LABEL = "auto-sync-review"


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


def render_issue_body(report: dict) -> str:
    total_unmatched = sum(len(p.get("unmatched", [])) for p in report["playlists"])
    total_uncertain = sum(len(p.get("uncertain", [])) for p in report["playlists"])

    lines: list[str] = []
    lines.append(f"_Generated {report.get('generated_at', '?')}_")
    lines.append("")
    lines.append(f"- **Unmatched:** {total_unmatched}")
    lines.append(f"- **Uncertain (score < 0.82):** {total_uncertain}")
    lines.append("")
    lines.append(
        "To override a pick: paste the desired track ID into "
        "`cache/track_mapping.json` next to the right Apple ID, then re-run sync."
    )
    lines.append("")

    for p in report["playlists"]:
        unmatched = p.get("unmatched", [])
        uncertain = p.get("uncertain", [])
        if not unmatched and not uncertain:
            continue

        lines.append(f"## {p['name']}")
        lines.append(
            f"_{p.get('resolved', 0)}/{p.get('apple_count', 0)} resolved_"
        )
        lines.append("")

        if unmatched:
            lines.append(f"### Unmatched ({len(unmatched)})")
            for t in unmatched:
                album = f" · _{t['album']}_" if t.get("album") else ""
                isrc = f" · ISRC `{t['isrc']}`" if t.get("isrc") else ""
                lines.append(f"- **{t['name']}** — {t['artist']}{album}{isrc}")
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

    return "\n".join(lines).rstrip() + "\n"


def has_review_items(report: dict) -> bool:
    for p in report.get("playlists", []):
        if p.get("unmatched") or p.get("uncertain"):
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


def find_existing_issue(repo: str) -> str | None:
    out = _gh(
        "issue", "list",
        "--repo", repo,
        "--label", ISSUE_LABEL,
        "--state", "open",
        "--json", "number",
        "--limit", "1",
    )
    issues = json.loads(out) if out else []
    return str(issues[0]["number"]) if issues else None


def main():
    if not REPORT_FILE.exists():
        print("No report file — nothing to do.")
        return 0

    report = json.loads(REPORT_FILE.read_text())
    if not has_review_items(report):
        print("Report is clean (no unmatched / uncertain). Nothing to post.")
        return 0

    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        print("GITHUB_REPOSITORY not set; printing the body to stdout instead.")
        print(render_issue_body(report))
        return 0

    body = render_issue_body(report)
    title = f"Sync review needed — {report.get('generated_at', '?')}"

    existing = find_existing_issue(repo)
    try:
        if existing:
            _gh(
                "issue", "edit", existing,
                "--repo", repo,
                "--body", body,
                "--title", title,
            )
            print(f"Updated issue #{existing}")
        else:
            _gh(
                "issue", "create",
                "--repo", repo,
                "--label", ISSUE_LABEL,
                "--title", title,
                "--body", body,
            )
            print("Created new review issue")
    except subprocess.CalledProcessError:
        # Issue create/edit failed — but the review items are the whole point
        # of running this script, so dump the rendered body to the workflow log
        # where it stays visible, then exit non-zero so the failure email fires.
        print("\n=== Issue create/edit failed. Review body follows: ===\n", file=sys.stderr)
        print(body)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
