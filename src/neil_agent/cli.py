"""Interactive terminal interface for Neil Agent."""

from __future__ import annotations

from pydantic import ValidationError
from rich.console import Console

from .agent import Agent
from .config import get_settings
from .llm import LLMClient, LLMError

EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit"}
CLEAR_COMMANDS = {"clear", "/clear"}
HELP_COMMANDS = {"help", "/help"}

console = Console()


def main() -> None:
    """Run the interactive multi-turn chat loop."""

    try:
        settings = get_settings()
    except ValidationError as error:
        _show_config_error(error)
        raise SystemExit(1) from None

    llm = LLMClient(settings)
    agent = Agent(llm, max_rounds=settings.max_rounds)
    _show_welcome(settings.deepseek_model)

    while True:
        try:
            user_input = console.input("\n[bold cyan]你[/bold cyan] > ").strip()
        except (EOFError, KeyboardInterrupt):
            _show_goodbye()
            return

        command = user_input.lower()
        if command in EXIT_COMMANDS:
            _show_goodbye()
            return
        if command in CLEAR_COMMANDS:
            agent.clear()
            console.print("[dim]对话历史已清空。[/dim]")
            continue
        if command in HELP_COMMANDS:
            _show_help()
            continue
        if not user_input:
            continue

        console.print("[bold green]Neil Agent[/bold green] > ", end="")
        response_stream = agent.stream_chat(user_input)
        try:
            for chunk in response_stream:
                console.print(
                    chunk,
                    end="",
                    markup=False,
                    highlight=False,
                    soft_wrap=True,
                )
        except KeyboardInterrupt:
            response_stream.close()
            console.print("\n[yellow]已取消本次回答。[/yellow]")
        except LLMError as error:
            console.print(f"\n[bold red]请求失败：[/bold red]{error}")
        else:
            console.print()


def _show_welcome(model: str) -> None:
    console.print("[bold green]Neil Agent[/bold green] 已启动")
    console.print(f"[dim]模型：{model}[/dim]")
    console.print("[dim]输入 /help 查看命令。[/dim]")


def _show_help() -> None:
    console.print("[bold]可用命令[/bold]")
    console.print("  /clear  清空对话历史")
    console.print("  /exit   退出程序")
    console.print("  /help   显示帮助")


def _show_goodbye() -> None:
    console.print("\n[dim]Neil Agent 已退出。[/dim]")


def _show_config_error(error: ValidationError) -> None:
    missing_api_key = any(
        item["type"] == "missing" and item["loc"] == ("deepseek_api_key",)
        for item in error.errors()
    )
    if missing_api_key:
        console.print("[bold red]配置错误：[/bold red]未找到 DEEPSEEK_API_KEY。")
        console.print("请复制 .env.example 为 .env，并填写你的 DeepSeek API Key。")
        return
    console.print(f"[bold red]配置错误：[/bold red]{error}")
