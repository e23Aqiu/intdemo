"""Command-line interface for the local train-and-infer CAPTCHA engine."""

from __future__ import annotations

import argparse
import json
import sys

from .dataset import DatasetError
from .engine import EngineError, serve
from .model_bundle import ENGINE_API_VERSION
from .protocol import PROTOCOL_VERSION, TrainerProtocolError
from .training import TrainingError, self_test, train


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="intdemo-trainer")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("self-test")
    check.add_argument("--protocol-version", required=True, type=int)

    training = subparsers.add_parser("train")
    training.add_argument("--protocol-version", required=True, type=int)
    training.add_argument("--dataset", required=True)
    training.add_argument(
        "--captcha-type",
        required=True,
        choices=("numeric", "click"),
    )
    training.add_argument("--output", required=True)

    engine = subparsers.add_parser("serve")
    engine.add_argument("--engine-api-version", required=True, type=int)
    return parser


def main(arguments=None) -> int:
    parser = build_parser()
    options = parser.parse_args(arguments)
    if options.command in {"self-test", "train"} and (
        options.protocol_version != PROTOCOL_VERSION
    ):
        parser.error(f"仅支持训练协议版本 {PROTOCOL_VERSION}")
    if options.command == "serve" and options.engine_api_version != ENGINE_API_VERSION:
        parser.error(f"仅支持推理引擎 API {ENGINE_API_VERSION}")
    try:
        if options.command == "self-test":
            print(
                json.dumps(
                    self_test(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                flush=True,
            )
        elif options.command == "train":
            train(options.dataset, options.captcha_type, options.output)
        else:
            return serve()
        return 0
    except (TrainingError, TrainerProtocolError, DatasetError, EngineError) as exc:
        print(
            json.dumps(
                {
                    "event": "error",
                    "protocol_version": PROTOCOL_VERSION,
                    "message": str(exc),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            file=sys.stderr,
            flush=True,
        )
        return 2
    except KeyboardInterrupt:
        print(
            json.dumps(
                {
                    "event": "cancelled",
                    "protocol_version": PROTOCOL_VERSION,
                    "message": "强化训练已取消",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            file=sys.stderr,
            flush=True,
        )
        return 130
    except Exception as exc:  # noqa: BLE001 - executable boundary must emit protocol JSON
        message = " ".join(str(exc).replace("\x00", "").split())[:1_000]
        print(
            json.dumps(
                {
                    "event": "error",
                    "protocol_version": PROTOCOL_VERSION,
                    "message": "强化训练失败" + (f"：{message}" if message else ""),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            file=sys.stderr,
            flush=True,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
