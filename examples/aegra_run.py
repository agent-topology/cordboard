"""Submit the minimal graph through Aegra; print only identities and counts."""

import argparse
import json

from cord_runtime.aegra_client import execute


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aegra", default="http://127.0.0.1:2026")
    parser.add_argument("--subject", default="urn:cordboard:slice:0")
    args = parser.parse_args()
    try:
        result = execute(args.aegra, "minimal-graph", args.subject,
                         {"subject": args.subject, "source": "hello"})
        draft = result["values"]["steps"][1]
        print(json.dumps({"run_id": result["run_id"], "thread_id": result["thread_id"],
                          "outcome": draft["outcome"], "attempts": len(draft["attempts"])}))
    except Exception:
        parser.exit(1, "Aegra graph failed; check service readiness, input contract and archive\n")


if __name__ == "__main__":
    main()
