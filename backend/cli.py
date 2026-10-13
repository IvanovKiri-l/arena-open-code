"""CLI: забег и судейство без веб-UI.

Примеры (запускать из каталога backend):

    python cli.py "Напиши функцию fizzbuzz(n) на Python"
    python cli.py "..." --judges 3
    python cli.py "..." --fake          # офлайн-прогон на фейковых клиентах
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from app.config import settings
from app.core.arena import ArenaOptions, ArenaRun, run_arena
from app.core.judge import JudgeResult, judge_run
from app.core.verify import VerifyError, verify_responses
from app.db import init_db
from app.llm.fake import FakeJudgeClient, FakeLLMClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arena", description="Arena: N параллельных ответов + судейство"
    )
    parser.add_argument("prompt", help="промпт для забега")
    parser.add_argument(
        "--n", type=int, default=None, help="число инстансов (по умолчанию ARENA_N)"
    )
    parser.add_argument(
        "--judges", type=int, default=None, help="число судей, напр. 1 или 3 (по умолчанию JUDGES)"
    )
    parser.add_argument(
        "--criteria", default=None, help="критерии судьи через запятую (по умолчанию JUDGE_CRITERIA)"
    )
    parser.add_argument(
        "--concurrency", type=int, default=None, help="макс. одновременных запросов (MAX_CONCURRENCY)"
    )
    parser.add_argument(
        "--timeout", type=float, default=None, help="таймаут одного вызова, сек (LLM_TIMEOUT_S)"
    )
    parser.add_argument("--fake", action="store_true", help="офлайн-прогон на фейковых клиентах")
    parser.add_argument(
        "--verify",
        dest="verify",
        action="store_true",
        default=None,
        help="включить верификацию (по умолчанию — если задан VERIFY_CMD)",
    )
    parser.add_argument(
        "--no-verify", dest="verify", action="store_false", help="выключить верификацию"
    )
    return parser


def _print_run(run: ArenaRun) -> None:
    print(f"\nЗабег {run.id} · инстансов: {len(run.responses)}")
    print("-" * 62)
    for response in run.responses:
        head = (
            f"#{response['slot']:>2} [{response['profile']:<12}] "
            f"t={response['temperature']}"
        )
        if response["status"] != "ok":
            print(f"{head}  ОШИБКА: {response['error']}")
            continue
        text = (response["output"] or "").strip()
        if len(text) > 300:
            text = text[:300] + "…"
        print(f"{head}  {response['latency_ms']} мс")
        print("    " + text.replace("\n", "\n    "))


def _print_judgement(run: ArenaRun, result: JudgeResult) -> None:
    print("-" * 62)
    if result.winner_id is None:
        print("Вердикт: победитель не определён")
    else:
        winner = next(
            (r for r in run.responses if r["id"] == result.winner_id), None
        )
        if winner is not None:
            print(f"ПОБЕДИТЕЛЬ: слот #{winner['slot']} [{winner['profile']}]")
            text = (winner["output"] or "").strip()
            if len(text) > 400:
                text = text[:400] + "…"
            print("    " + text.replace("\n", "\n    "))
    for verdict in result.verdicts:
        rationale = (verdict["rationale"] or "").strip()
        print(f"  судья {verdict['judge_index'] + 1}: {rationale}")
    for error in result.errors:
        print(f"  ! {error}")


async def main_async(args: argparse.Namespace) -> int:
    init_db()
    criteria = (
        [c.strip() for c in args.criteria.split(",") if c.strip()]
        if args.criteria
        else None
    )
    options = ArenaOptions(
        n=args.n,
        timeout_s=args.timeout,
        max_concurrency=args.concurrency,
    )
    run_client = FakeLLMClient() if args.fake else None
    judge_client = FakeJudgeClient(seed=1) if args.fake else None

    try:
        run = await run_arena(args.prompt, options, client=run_client)
    except RuntimeError as exc:
        print(f"Ошибка запуска: {exc}", file=sys.stderr)
        return 2

    _print_run(run)

    if args.verify and not settings.verify_cmd:
        print("Верификация запрошена, но VERIFY_CMD не задан в .env", file=sys.stderr)
        return 2
    if args.verify is not False and settings.verify_cmd:
        try:
            outcome = await verify_responses(run.id, run.responses)
        except VerifyError as exc:
            print(f"Ошибка верификации: {exc}", file=sys.stderr)
            return 4
        print(
            f"Верификация — прошли: {outcome.passed}, "
            f"дисквалифицированы: {outcome.disqualified}"
        )
        for record in outcome.updated:
            lines = (record.get("error") or "").splitlines()
            print(f"  слот #{record['slot']}: {lines[0] if lines else 'disqualified'}")

    try:
        result = await judge_run(
            run.id,
            run.prompt,
            run.responses,
            client=judge_client,
            criteria=criteria,
            judges=args.judges,
        )
    except RuntimeError as exc:
        print(f"Ошибка судейства: {exc}", file=sys.stderr)
        return 3

    _print_judgement(run, result)
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
