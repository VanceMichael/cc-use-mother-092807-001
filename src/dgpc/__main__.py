"""命令行入口：python -m dgpc [--host H] [--port P] [--db PATH] [--seed] [--print-tokens PATH]"""

from __future__ import annotations

import argparse
import json

from .bootstrap import seed, write_token_file
from .server import make_server


def main() -> None:
    parser = argparse.ArgumentParser(description="深层气井产能承诺与复产决策后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default="data/dgpc.sqlite3")
    parser.add_argument("--seed", action="store_true", help="空库时写入合成引导数据")
    parser.add_argument("--print-tokens", default=None,
                        help="把引导账号令牌写入指定 JSON 文件")
    args = parser.parse_args()

    server = make_server(args.host, args.port, args.db)
    result = None
    if args.seed:
        result = seed(server.hub)
        if result.get("seeded"):
            print("已写入合成引导数据")
        if args.print_tokens and result and result.get("seeded"):
            write_token_file(args.print_tokens, result)
            print(f"引导账号令牌已写入 {args.print_tokens}")
    if result and result.get("tokens"):
        print(json.dumps(result["tokens"], ensure_ascii=False, indent=2))

    recovered = server.hub.recover()
    print(
        "启动恢复：待复核测试 "
        f"{len(recovered['pending_test_reviews'])} 条，"
        f"待解锁检修 {len(recovered['await_unlock_windows'])} 条，"
        f"开放缺口 {len(recovered['open_gap_reminders'])} 条，"
        f"隔离材料 {len(recovered['quarantined_materials'])} 条，"
        f"待审批例外 {len(recovered['pending_exceptions'])} 条"
    )
    print(f"监听 http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
