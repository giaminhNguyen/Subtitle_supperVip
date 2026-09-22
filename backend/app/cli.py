"""Small local-only commands for configuring the application."""
import argparse
import getpass

from .config import set_youtube_api_key


def main() -> None:
    parser = argparse.ArgumentParser(description="YouTube Subtitle Manager")
    subcommands = parser.add_subparsers(dest="command", required=True)
    set_key = subcommands.add_parser("set-youtube-api-key", help="Lưu YouTube Data API key vào .env")
    set_key.add_argument("--key", help="API key (không khuyến nghị vì có thể lưu vào lịch sử terminal)")
    arguments = parser.parse_args()

    if arguments.command == "set-youtube-api-key":
        key = arguments.key or getpass.getpass("YouTube Data API key: ")
        set_youtube_api_key(key)
        print("Đã lưu YouTube API key vào .env.")


if __name__ == "__main__":
    main()
