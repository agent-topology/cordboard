"""Declarative Signal -> Assistant routing rules (#16), including the
``run.finished`` cascade case (#18).

A Rule is a Signal -> Assistant transformation declaration (ADR-0002): which
Signal type it applies to, an equality filter on the Signal payload, the
already-registered connection/assistant it targets, and bounded dot-path
expressions that map payload fields into Assistant input and extract the
Subject. A Rule names a connection concern, never a graph descriptor
(ADR-0014): adding one is a configuration change, not a router code change.

For every other Signal type that filter is ``match`` and is optional (an
absent one matches everything). A ``run.finished`` Rule instead declares
``when``: non-empty and required, since an unconditional cascade Rule would
fire on every Run's completion. ``subject`` also becomes optional for
``run.finished`` Rules, defaulting to the dot-path ``"subject"`` -- the
completed Run's own Subject -- so a cascade Rule inherits it by default and
only needs to declare ``subject`` to explicitly override it.

A ``run.finished`` Rule whose declared ``when`` pins the same (connection,
assistant) pair it targets is rejected here as a self-loop: it would
immediately re-trigger the Run that produced it. A longer cascade cycle
(A -> B -> A -> ...) is not statically declared this way; ``router.py``
bounds it at runtime instead, via the planned maximum cascade depth.
"""

import json
from pathlib import Path

from cord_runtime.signals import SIGNAL_TYPES


class InvalidRule(ValueError):
    pass


def rules_path(board_dir: Path) -> Path:
    return Path(board_dir) / ".cordboard" / "rules.json"


def load_rules(board_dir: Path) -> list[dict]:
    path = rules_path(board_dir)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InvalidRule(f"rules file is not valid JSON: {path}") from exc
    if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
        raise InvalidRule(f"rules file must be a JSON array of rule objects: {path}")
    return data


def _validate_input_mapping(rule: dict) -> dict:
    input_mapping = rule.get("input", {})
    if not isinstance(input_mapping, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in input_mapping.items()
    ):
        raise InvalidRule("rule 'input' must be an object of destination key -> dot-path string")
    return input_mapping


def _validate_rule(rule: dict) -> dict:
    name = rule.get("name")
    if not isinstance(name, str) or not name.strip():
        raise InvalidRule("rule 'name' must be a non-empty string")
    signal_type = rule.get("signal_type")
    if signal_type not in SIGNAL_TYPES:
        raise InvalidRule(f"rule 'signal_type' must be one of {SIGNAL_TYPES}")
    connection = rule.get("connection")
    if not isinstance(connection, str) or not connection.strip():
        raise InvalidRule("rule 'connection' must name a registered connection alias")
    assistant = rule.get("assistant")
    if not isinstance(assistant, str) or not assistant.strip():
        raise InvalidRule("rule 'assistant' must be a non-empty string")

    if signal_type == "run.finished":
        if rule.get("match"):
            raise InvalidRule("run.finished rule declares 'when', not 'match'")
        when = rule.get("when", {})
        if not isinstance(when, dict) or not when or not all(isinstance(k, str) for k in when):
            raise InvalidRule("run.finished rule requires a non-empty 'when' condition")
        if when.get("connection", connection) == connection and when.get("assistant") == assistant:
            raise InvalidRule(
                f"rule '{name}' would create a self-loop: 'when' targets the same "
                "connection/assistant it routes to"
            )
        subject = rule.get("subject", "subject")
        if not isinstance(subject, str) or not subject.strip():
            raise InvalidRule("rule 'subject' must be a non-empty dot-path string")
        return {
            "name": name, "signal_type": signal_type, "when": when,
            "connection": connection, "assistant": assistant, "subject": subject,
            "input": _validate_input_mapping(rule),
        }

    if rule.get("when"):
        raise InvalidRule("'when' is only valid for a run.finished rule; use 'match'")
    subject = rule.get("subject")
    if not isinstance(subject, str) or not subject.strip():
        raise InvalidRule("rule 'subject' must be a non-empty dot-path string")
    match = rule.get("match", {})
    if not isinstance(match, dict) or not all(isinstance(k, str) for k in match):
        raise InvalidRule("rule 'match' must be an object of dot-path -> literal value")
    return {
        "name": name, "signal_type": signal_type, "match": match,
        "connection": connection, "assistant": assistant, "subject": subject,
        "input": _validate_input_mapping(rule),
    }


def _write_atomic(path: Path, rules: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(rules, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def add_rule(board_dir: Path, rule: dict, *, replace: bool = False) -> None:
    validated = _validate_rule(rule)
    rules = load_rules(board_dir)
    existing_index = next((i for i, r in enumerate(rules) if r.get("name") == validated["name"]), None)
    if existing_index is not None:
        if not replace:
            raise InvalidRule(f"rule '{validated['name']}' already exists")
        rules[existing_index] = validated
    else:
        rules.append(validated)
    _write_atomic(rules_path(board_dir), rules)
