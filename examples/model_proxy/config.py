"""Validate and launch the optional example graph's DB-free LiteLLM gateway."""

import argparse
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

import yaml

CALLBACK = "callback.handler"


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def keys(value, allowed, location):
    require(isinstance(value, dict) and set(value) <= set(allowed),
            f"{location}: remove unsupported settings; see docs/model-proxy.md")


def validate(config):
    keys(config, {"model_list", "router_settings", "litellm_settings"}, "proxy")
    router = config.get("router_settings", {})
    require(isinstance(router, dict), "router_settings must be a mapping")
    for key in ("fallbacks", "context_window_fallbacks", "content_policy_fallbacks", "default_fallbacks"):
        require(not router.get(key),
                "cross-alias fallback is prohibited: use deployments under the same model_name; graphs own escalation")
    keys(router, {"num_retries", "routing_strategy", "fallbacks", "context_window_fallbacks",
                  "content_policy_fallbacks", "default_fallbacks"}, "router_settings")
    require(type(router.get("num_retries", 0)) is int and 0 <= router.get("num_retries", 0) <= 2,
            "num_retries must be an integer from 0 to 2")
    require(router.get("routing_strategy", "simple-shuffle") == "simple-shuffle",
            "use simple-shuffle for this slice")
    settings = config.get("litellm_settings", {})
    require(isinstance(settings, dict), "litellm_settings must be a mapping")
    require(settings.get("callbacks") == [CALLBACK] and
            not any(settings.get(k) for k in ("success_callback", "failure_callback", "input_callback")),
            "direct Langfuse/other callbacks bypass the gate: use only callback.handler")
    keys(settings, {"callbacks", "turn_off_message_logging", "telemetry"}, "litellm_settings")
    require(settings.get("turn_off_message_logging") is True and settings.get("telemetry") is False,
            "set turn_off_message_logging: true and telemetry: false")
    models = config.get("model_list")
    require(isinstance(models, list) and bool(models), "model_list must contain fast and deep deployments")
    aliases = set()
    for model in models:
        keys(model, {"model_name", "litellm_params"}, "model_list entry")
        require(model.get("model_name") in ("fast", "deep"), "only fast and deep aliases are supported")
        aliases.add(model["model_name"])
        params = model.get("litellm_params")
        keys(params, {"model", "api_base", "api_key", "order"}, "litellm_params")
        for key in ("model", "api_base", "api_key"):
            require(isinstance(params.get(key), str) and bool(params[key]), f"litellm_params requires {key}")
        require(params["api_key"].startswith("os.environ/"), "api_key must reference an environment variable")
        if "order" in params:
            require(type(params["order"]) is int and params["order"] >= 0, "order must be a nonnegative integer")
    require(aliases == {"fast", "deep"}, "configure both fast and deep aliases")
    return config


def load(path):
    try:
        config = yaml.safe_load(Path(path).read_text())
    except Exception:
        raise ValueError("cannot read proxy YAML; check file and syntax") from None
    require((Path(path).parent / "callback.py").is_file(),
            "missing callback.py next to config; copy examples/model_proxy/callback.py alongside YAML")
    return validate(config)


def environment(config, collector):
    url = urlsplit(collector)
    require(url.scheme == "http" and url.hostname == "127.0.0.1" and
            url.path == "/v1/traces" and not url.username and not url.query and not url.fragment,
            "Collector must be http://127.0.0.1:<port>/v1/traces")
    # Pass only runtime essentials and the explicit provider references. Inherited
    # DB, Langfuse, OTel auto-export and debug flags cannot open another exit.
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT") if key in os.environ}
    for model in config["model_list"]:
        for value in model["litellm_params"].values():
            if isinstance(value, str) and value.startswith("os.environ/"):
                name = value.removeprefix("os.environ/")
                require(name.startswith("EXAMPLE_PROVIDER_") or name in {"EXAMPLE_FAST_MODEL", "EXAMPLE_DEEP_MODEL"},
                        "provider environment references must use EXAMPLE_PROVIDER_* or EXAMPLE_FAST_MODEL/EXAMPLE_DEEP_MODEL")
                require(bool(os.environ.get(name)), "missing provider environment reference; set variables named in config")
                env[name] = os.environ[name]
    env.update(PYTHONPATH=str(Path(__file__).resolve().parents[2]),
               CORD_COLLECTOR_URL=collector, LITELLM_LOCAL_MODEL_COST_MAP="True",
               LITELLM_MODE="PRODUCTION", LANGCHAIN_TRACING_V2="false",
               LANGSMITH_TRACING="false")
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="examples/model_proxy/config.yaml")
    parser.add_argument("--port", type=int, default=4000)
    parser.add_argument("--collector", default="http://127.0.0.1:4318/v1/traces")
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args()
    try:
        config = load(args.config)
        require(1 <= args.port <= 65535, "port must be from 1 to 65535")
        if args.validate:
            print("proxy configuration valid")
            return
        env = environment(config, args.collector)
    except ValueError as exc:
        parser.exit(2, str(exc) + "\n")
    print("Starting local LiteLLM; payload-capable upstream console logs suppressed.", flush=True)
    # LiteLLM can print provider exception bodies/configuration outside logging.
    # Never persist these diagnostics. Startup is checked via /health/liveliness.
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    executable = str(Path(sys.executable).parent / "litellm")
    os.execve(executable, [executable, "--config", str(Path(args.config).resolve()),
                           "--host", "127.0.0.1", "--port", str(args.port)], env)


if __name__ == "__main__":
    main()
