"""Launch the hand-written Slice 0 Aegra deployment on loopback."""

import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def environment(config=None):
    # Aegra's optional OpenInference/LangSmith exporters bypass our redactor.
    # Pass only this deployment's settings to a fresh interpreter.
    names = ("PATH", "HOME", "TMPDIR", "SYSTEMROOT", "POSTGRES_USER",
             "POSTGRES_PASSWORD", "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB")
    env = {key: os.environ[key] for key in names if key in os.environ}
    env.update(AEGRA_CONFIG=str(Path(config).resolve() if config else ROOT / "aegra/aegra.json"), AUTH_TYPE="noop",
               LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false",
               OTEL_TARGETS="", OTEL_CONSOLE_EXPORT="false",
               REDIS_BROKER_ENABLED="false", DB_ECHO_LOG="false")
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=2026)
    parser.add_argument("--config", help="Aegra deployment file owned by the graph operator")
    parser.add_argument("--proxy", help="Optional model gateway for the minimal-graph example")
    parser.add_argument("--collector", default="http://127.0.0.1:4318/v1/traces")
    args = parser.parse_args()
    env = environment(args.config)
    env.update(CORD_COLLECTOR=args.collector)
    if args.proxy:
        env["EXAMPLE_PROXY"] = args.proxy
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "aegra_api.main:app", "--host", "127.0.0.1",
         "--port", str(args.port), "--no-access-log"], cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        code = process.wait()
    except KeyboardInterrupt:
        process.terminate()
        try:
            code = process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            code = process.wait()
    if code:
        parser.exit(1, "Aegra stopped; check locked environment, Postgres, configuration and port\n")


if __name__ == "__main__":
    main()
