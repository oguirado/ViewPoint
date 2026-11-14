import sys
import json

def _sig(node):
    return (
        node.get("tag", ""),
        node.get("id", ""),
        node.get("role", ""),
        node.get("name", "")
    )

def diff_states(nodes1: list, nodes2: list) -> dict:
    M, N = len(nodes1), len(nodes2)
    dp = [[0] * (N + 1) for _ in range(M + 1)]

    for i in range(1, M + 1):
        for j in range(1, N + 1):
            if _sig(nodes1[i - 1]) == _sig(nodes2[j - 1]):
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])

    i, j = M, N
    matched_pairs = []
    unmatched_a = set(range(M))
    unmatched_b = set(range(N))

    while i > 0 and j > 0:
        if _sig(nodes1[i - 1]) == _sig(nodes2[j - 1]):
            matched_pairs.append((i - 1, j - 1))
            unmatched_a.remove(i - 1)
            unmatched_b.remove(j - 1)
            i -= 1
            j -= 1
        elif dp[i - 1][j] >= dp[i][j - 1]:
            i -= 1
        else:
            j -= 1

    matched_pairs.reverse()
    removed = sorted(list(unmatched_a))
    added = sorted(list(unmatched_b))

    changed = []
    for i1, i2 in matched_pairs:
        n1 = nodes1[i1]
        n2 = nodes2[i2]
        change = {}

        box1, box2 = n1.get("box"), n2.get("box")
        if box1 != box2:
            change["box"] = box2
            if box1 is not None and box2 is not None:
                change["dx"] = box2[0] - box1[0]
                change["dy"] = box2[1] - box1[1]

        if n1.get("value") != n2.get("value"):
            change["value"] = n2.get("value")

        if n1.get("checked") != n2.get("checked"):
            change["checked"] = n2.get("checked")

        if n1.get("act") != n2.get("act"):
            change["act"] = n2.get("act")

        if change:
            change["i1"] = i1
            change["i2"] = i2
            changed.append(change)

    return {
        "added": added,
        "changed": changed,
        "removed": removed,
    }

def actions_report(doc: dict) -> dict:
    states = doc.get("states", [])
    steps_report = []

    for idx in range(1, len(states)):
        prev_st = states[idx - 1]
        curr_st = states[idx]
        delta = diff_states(prev_st.get("nodes", []), curr_st.get("nodes", []))
        step_entry = {
            "delta": delta,
            "error": curr_st.get("error"),
            "ok": curr_st.get("ok"),
            "op": curr_st.get("op"),
            "step": curr_st.get("step"),
        }
        steps_report.append(step_entry)

    return {
        "page": doc.get("page", ""),
        "schema": doc.get("schema", "vqa.x2-state/v1"),
        "steps": steps_report,
    }

def main():
    if len(sys.argv) < 2:
        sys.stderr.write("Usage: python3 state_diff.py STATE_FILE\n")
        sys.exit(1)

    filepath = sys.argv[1]
    with open(filepath, 'r', encoding='utf-8') as f:
        doc = json.load(f)

    report = actions_report(doc)
    canonical = json.dumps(report, ensure_ascii=True, sort_keys=True, separators=(',', ':'))
    sys.stdout.write(canonical + '\n')

if __name__ == '__main__':
    main()
