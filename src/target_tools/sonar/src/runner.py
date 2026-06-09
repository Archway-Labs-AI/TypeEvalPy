"""Thin wrapper: shell out to the Sonar Java harness once over the whole tree.

The Java JAR walks the corpus on its own and drops main_result.json next to
each main_gt.json, which is exactly what TypeEvalPy's result_analyzer reads.
"""
import argparse
import logging
import subprocess
import sys
from pathlib import Path

import utils

logger = logging.getLogger("runner")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
logger.addHandler(handler)

JAR = "/app/sonar-typeeval-runner.jar"


def main_runner(benchmark_path: str) -> int:
    benchmark_path = str(Path(benchmark_path).resolve())
    logger.info(f"sonar-typeeval-runner over {benchmark_path}")
    proc = subprocess.run(
        ["java", "-jar", JAR, benchmark_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.stdout:
        logger.info(proc.stdout.strip())
    if proc.stderr:
        logger.warning(proc.stderr.strip())
    return proc.returncode


if __name__ == "__main__":
    if not utils.is_running_in_docker():
        print("not running in docker — refusing to run on host")
        sys.exit(1)
    parser = argparse.ArgumentParser()
    parser.add_argument("--bechmark_path", default="/tmp/micro-benchmark")
    args = parser.parse_args()
    sys.exit(main_runner(args.bechmark_path))
