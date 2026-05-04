from __future__ import annotations

import subprocess


def run_dig(args: list[str]) -> str:
    result = subprocess.run(
        ["dig", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"dig failed for {' '.join(args)}: {stderr}")
    return result.stdout


def dig_short(args: list[str]) -> list[str]:
    output = run_dig([*args, "+short"])
    return [line.strip() for line in output.splitlines() if line.strip()]


def dig_full(args: list[str]) -> str:
    return run_dig(args)


def parse_authoritative_answer(output: str) -> bool:
    for line in output.splitlines():
        if line.startswith(";; flags:"):
            return " aa " in f" {line} " or " aa;" in line
    return False

