"""
Merge two versions of state/combined.json (the remote's current version
vs. this run's freshly-computed local version) at the JSON level,
instead of relying on git's line-based text merge/rebase.

Why: multiple runs can each append DIFFERENT new job IDs to the same
recent_ids array. Git's text-based merge/rebase has no idea these are
semantically just "two sets of additions" — it sees conflicting line
changes and gives up, leaving the working tree in a stuck mid-rebase
state that a simple retry loop can't recover from. Since the correct
merge here is obvious (take the newer timestamp, union the ID lists),
we do that ourselves rather than asking git to guess.

Usage: python3 merge_state.py <remote_file> <local_file> <output_file>
"""

import json
import sys

# Keep in sync with RECENT_ID_CAP in job_alert.py.
RECENT_ID_CAP = 2000


def merge(remote: dict, local: dict) -> dict:
    last_seen_iso = max(
        remote.get("last_seen_iso", ""), local.get("last_seen_iso", "")
    )

    remote_ids = remote.get("recent_ids", [])
    local_ids = local.get("recent_ids", [])

    seen = set()
    merged_ids = []
    for id_ in remote_ids + [i for i in local_ids if i not in remote_ids]:
        if id_ not in seen:
            seen.add(id_)
            merged_ids.append(id_)

    return {
        "last_seen_iso": last_seen_iso,
        "recent_ids": merged_ids[-RECENT_ID_CAP:],
    }


def main() -> None:
    if len(sys.argv) != 4:
        print("Usage: python3 merge_state.py <remote_file> <local_file> <output_file>",
              file=sys.stderr)
        sys.exit(1)

    remote_path, local_path, output_path = sys.argv[1], sys.argv[2], sys.argv[3]

    with open(remote_path, encoding="utf-8") as f:
        remote = json.load(f)
    with open(local_path, encoding="utf-8") as f:
        local = json.load(f)

    merged = merge(remote, local)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)

    print(f"Merged state: last_seen_iso={merged['last_seen_iso']}, "
          f"{len(merged['recent_ids'])} recent_ids")


if __name__ == "__main__":
    main()
