import json

def _has_float(obj):
    if isinstance(obj, float):
        return True
    if isinstance(obj, dict):
        return any(_has_float(k) or _has_float(v) for k, v in obj.items())
    if isinstance(obj, list):
        return any(_has_float(v) for v in obj)
    return False

def check_bytes(raw_bytes: bytes) -> list:
    problems = []

    if not isinstance(raw_bytes, bytes):
        return ["not valid UTF-8 or not pure ASCII"]

    try:
        text = raw_bytes.decode('ascii')
    except (UnicodeDecodeError, AttributeError):
        return ["not valid UTF-8 or not pure ASCII"]

    try:
        doc = json.loads(text)
    except Exception:
        return ["invalid JSON syntax"]

    try:
        canonical = json.dumps(doc, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii') + b'\n'
        if raw_bytes != canonical:
            problems.append("bytes not equal to canonical re-serialization")
    except Exception:
        problems.append("bytes not equal to canonical re-serialization")

    if _has_float(doc):
        problems.append("contains float")

    if not isinstance(doc, dict):
        problems.append("wrong types")
        return problems

    top_keys = set(doc.keys())
    expected_top_keys = {"schema", "page", "viewport", "states"}
    if top_keys != expected_top_keys:
        problems.append("missing or extra top-level keys")

    if "schema" in doc and not isinstance(doc["schema"], str):
        problems.append("wrong types")
    if "page" in doc and not isinstance(doc["page"], str):
        problems.append("wrong types")

    if "viewport" in doc:
        vp = doc["viewport"]
        if not isinstance(vp, list) or len(vp) != 2 or not all(isinstance(x, int) and not isinstance(x, bool) for x in vp):
            problems.append("wrong types")

    if "states" in doc:
        states = doc["states"]
        if not isinstance(states, list):
            problems.append("wrong types")
        else:
            if len(states) == 0:
                problems.append("states not starting with step -1 op load")
            else:
                first_state = states[0]
                if not isinstance(first_state, dict) or first_state.get("step") != -1 or first_state.get("op") != "load":
                    problems.append("states not starting with step -1 op load")

            for s_idx, st in enumerate(states):
                if not isinstance(st, dict):
                    if "wrong types" not in problems:
                        problems.append("wrong types")
                    continue

                st_keys = set(st.keys())
                expected_st_keys = {"step", "op", "ok", "error", "nodes"}
                if st_keys != expected_st_keys and "missing or extra keys" not in problems:
                    problems.append("missing or extra keys")

                if "step" in st and not (isinstance(st["step"], int) and not isinstance(st["step"], bool)):
                    if "wrong types" not in problems:
                        problems.append("wrong types")
                if "op" in st and not isinstance(st["op"], str):
                    if "wrong types" not in problems:
                        problems.append("wrong types")
                if "ok" in st and not isinstance(st["ok"], bool):
                    if "wrong types" not in problems:
                        problems.append("wrong types")
                if "error" in st and st["error"] is not None and not isinstance(st["error"], str):
                    if "wrong types" not in problems:
                        problems.append("wrong types")

                if "nodes" in st:
                    nodes = st["nodes"]
                    if not isinstance(nodes, list):
                        if "wrong types" not in problems:
                            problems.append("wrong types")
                    else:
                        for idx, node in enumerate(nodes):
                            if not isinstance(node, dict):
                                if "wrong types" not in problems:
                                    problems.append("wrong types")
                                continue

                            node_keys = set(node.keys())
                            expected_node_keys = {"i", "tag", "id", "role", "name", "value", "checked", "box", "act"}
                            if node_keys != expected_node_keys and "missing or extra keys" not in problems:
                                problems.append("missing or extra keys")

                            if "i" in node:
                                if not isinstance(node["i"], int) or isinstance(node["i"], bool):
                                    if "wrong types" not in problems:
                                        problems.append("wrong types")
                                elif node["i"] != idx and "i not equal to list position" not in problems:
                                    problems.append("i not equal to list position")

                            for str_field in ("tag", "id", "role", "name", "value"):
                                if str_field in node and not isinstance(node[str_field], str):
                                    if "wrong types" not in problems:
                                        problems.append("wrong types")

                            for bool_field in ("checked", "act"):
                                if bool_field in node and not isinstance(node[bool_field], bool):
                                    if "wrong types" not in problems:
                                        problems.append("wrong types")

                            if "box" in node:
                                box = node["box"]
                                if box is not None:
                                    if not isinstance(box, list) or len(box) != 4 or not all(isinstance(c, int) and not isinstance(c, bool) for c in box):
                                        if "wrong types" not in problems:
                                            problems.append("wrong types")
                                    else:
                                        x0, y0, x1, y1 = box
                                        if (x1 < x0 or y1 < y0) and "box with x1 less than x0 or y1 less than y0" not in problems:
                                            problems.append("box with x1 less than x0 or y1 less than y0")

    return problems
