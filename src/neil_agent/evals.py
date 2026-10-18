"""CLI facade for offline checks, real protocol acceptance and coding tasks."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from rich.console import Console

from .config import get_settings
from .errors import NeilAgentError
from .host_runtime import RuntimeProfile
from .evaluation.catalog import DEFAULT_TASKS_PATH as DEFAULT_TASKS_PATH
from .evaluation.common import EvalResult as EvalResult
from .evaluation.offline import run_offline_evals as run_offline_evals
from .evaluation.coding import run_coding_evals
from .evaluation.acceptance import (
    REAL_SYSTEM_PROMPT as REAL_SYSTEM_PROMPT,
    REAL_V1_TOOLS as REAL_V1_TOOLS,
    REAL_V2_PROMPT as REAL_V2_PROMPT,
    run_real_deepseek_acceptance as run_real_deepseek_acceptance,
)


def _show_results(console: Console, title: str, results: Sequence[EvalResult]) -> int:
    console.print(title, style="bold", markup=False, highlight=False)
    for result in results:
        marker = "[ok]" if result.passed else "[!]"
        style = "green" if result.passed else "red"
        console.print(
            f"{marker} {result.task_id} ({result.duration_ms} ms): {result.detail}",
            style=style,
            markup=False,
            highlight=False,
        )
    passed = sum(result.passed for result in results)
    console.print(f"结果：{passed}/{len(results)} 通过", markup=False, highlight=False)
    return 0 if passed == len(results) else 1


def _show_json_results(
    title: str,
    results: Sequence[EvalResult],
    *,
    runtime_profile: RuntimeProfile,
) -> int:
    passed = sum(result.passed for result in results)
    print(
        json.dumps(
            {
                "title": title,
                "runtime_profile": runtime_profile.value,
                "passed": passed,
                "total": len(results),
                "success": passed == len(results),
                "results": [
                    {
                        "task_id": result.task_id,
                        "passed": result.passed,
                        "detail": result.detail,
                        "duration_ms": result.duration_ms,
                    }
                    for result in results
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if passed == len(results) else 1


def main() -> None:
    """Run evaluations; real API calls require two explicit command flags."""

    parser = argparse.ArgumentParser(description="Run Neil Agent evaluations.")
    parser.add_argument("--tasks", type=Path, default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--real-deepseek", action="store_true")
    mode.add_argument(
        "--coding-tasks",
        action="store_true",
        help="Evaluate fixed coding tasks; graders execute Python on the host without OS sandboxing.",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Use reference patches to verify the coding harness offline; produces no real model score.",
    )
    parser.add_argument("--trials", type=int, choices=range(1, 4), default=1)
    parser.add_argument("--confirm-api-cost", action="store_true")
    parser.add_argument("--task", action="append", dest="task_ids")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument(
        "--runtime-profile",
        choices=tuple(profile.value for profile in RuntimeProfile),
        default=RuntimeProfile.BENCHMARK_MINIMAL.value,
        help="Harness preset used for this evaluation run.",
    )
    arguments = parser.parse_args()
    runtime_profile = RuntimeProfile(arguments.runtime_profile)
    console = Console()

    if arguments.simulate and not arguments.coding_tasks:
        parser.error("--simulate 只能与 --coding-tasks 一起使用")
    if arguments.trials != 1 and not arguments.coding_tasks:
        parser.error("--trials 只能与 --coding-tasks 一起使用")
    if arguments.coding_tasks:
        if arguments.tasks is not None:
            parser.error("代码任务只使用随包分发的固定语料，不能传入 --tasks")
        if runtime_profile is not RuntimeProfile.BENCHMARK_MINIMAL:
            parser.error("代码任务固定使用 benchmark-minimal")
        if not arguments.simulate and not arguments.confirm_api_cost:
            parser.error("真实代码任务会消耗 API 额度；请同时提供 --confirm-api-cost")
        try:
            if arguments.simulate:
                from .config import Settings
                from pydantic import SecretStr

                settings = Settings.model_construct(
                    deepseek_api_key=SecretStr("offline-fixture")
                )
            else:
                settings = get_settings()
            report = run_coding_evals(
                settings,
                task_ids=arguments.task_ids,
                trials=arguments.trials,
                simulated=arguments.simulate,
            )
        except (NeilAgentError, ValueError):
            console.print(
                "代码任务评测初始化失败；请检查配置与任务 ID。",
                style="red",
                markup=False,
            )
            raise SystemExit(2) from None
        if arguments.format == "json":
            print(json.dumps(report.summary(), ensure_ascii=False, sort_keys=True))
        else:
            console.print(f"代码任务评测 · {report.mode}", markup=False)
            for result in report.results:
                tokens = (
                    "unavailable"
                    if result.usage is None
                    else str(result.usage.total_tokens)
                )
                console.print(
                    f"{result.task_id} #{result.trial}: {result.status}; {result.duration_ms} ms; tokens={tokens}",
                    markup=False,
                )
        raise SystemExit(0 if report.success else 1)

    if arguments.real_deepseek:
        if arguments.task_ids:
            parser.error("--task 仅适用于离线评测")
        if not arguments.confirm_api_cost:
            console.print(
                "真实 DeepSeek 验收会消耗 API 额度；请同时提供 --confirm-api-cost。",
                style="red",
                markup=False,
            )
            raise SystemExit(2)
        try:
            results = run_real_deepseek_acceptance(
                get_settings(),
                runtime_profile=runtime_profile,
            )
        except (NeilAgentError, ValueError) as error:
            console.print(f"真实验收失败：{error}", style="red", markup=False)
            raise SystemExit(1) from None
        if arguments.format == "json":
            raise SystemExit(
                _show_json_results(
                    "真实 DeepSeek 验收",
                    results,
                    runtime_profile=runtime_profile,
                )
            )
        raise SystemExit(_show_results(console, "真实 DeepSeek 验收", results))

    try:
        results = run_offline_evals(
            arguments.tasks,
            task_ids=arguments.task_ids,
            runtime_profile=runtime_profile,
        )
    except ValueError as error:
        console.print(str(error), style="red", markup=False)
        raise SystemExit(2) from None
    if arguments.format == "json":
        raise SystemExit(
            _show_json_results(
                "Neil Agent 离线评测",
                results,
                runtime_profile=runtime_profile,
            )
        )
    raise SystemExit(_show_results(console, "Neil Agent 离线评测", results))


if __name__ == "__main__":
    main()
