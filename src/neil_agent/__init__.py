"""Neil Agent package."""

__version__ = "0.1.0"


def main() -> None:
    """Run the command-line application."""

    from .cli import main as cli_main

    cli_main()

