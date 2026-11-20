import argparse
import json

from app.services.crawler_engine import install_engine


def main() -> None:
    parser = argparse.ArgumentParser(description="社交平台需求雷达命令行")
    parser.add_argument("command", choices=["install-engine"])
    args = parser.parse_args()
    if args.command == "install-engine":
        print(json.dumps(install_engine(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
