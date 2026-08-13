"""PyInstaller entry point; keep absolute imports when executed as __main__."""

from enhanced_trainer.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
