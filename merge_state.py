import json
import sys

# Used only when the caller doesn't pass a cap (job_alert.py's value).
_DEFAULT_RECENT_ID_CAP = 2000


def merge(remote: dict, local: dict, recent_id_cap: int = _DEFAULT_RECENT_ID_CAP) -> dict:
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
        "recent_ids": merged_ids[-recent_id_cap:],
    }


def main() -> None:
    if len(sys.argv) not in (4, 5):
        print("Usage: python3 merge_state.py <remote_file> <local_file> <output_file> [recent_id_cap]",
              file=sys.stderr)
        sys.exit(1)

    remote_path, local_path, output_path = sys.argv[1], sys.argv[2], sys.argv[3]
    recent_id_cap = int(sys.argv[4]) if len(sys.argv) == 5 else _DEFAULT_RECENT_ID_CAP

    with open(remote_path, encoding="utf-8") as f:
        remote = json.load(f)
    with open(local_path, encoding="utf-8") as f:
        local = json.load(f)

    merged = merge(remote, local, recent_id_cap)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)

    print(f"Merged state: last_seen_iso={merged['last_seen_iso']}, "
          f"{len(merged['recent_ids'])} recent_ids (cap={recent_id_cap})")


if __name__ == "__main__":
    main()
