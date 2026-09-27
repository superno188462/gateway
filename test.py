"""PostgreSQL 连接测试：uv run test.py --user postgres --dbname postgres。"""

import argparse
import getpass
import os
import sys
import time

import psycopg


def main() -> int:
    parser = argparse.ArgumentParser(description="测试 PostgreSQL 连接并执行只读查询")
    parser.add_argument("--host", default=os.getenv("PGHOST", "119.45.48.180"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PGPORT", "5432")))
    parser.add_argument("--user", default=os.getenv("PGUSER", "postgres"))
    parser.add_argument("--dbname", default=os.getenv("PGDATABASE", "postgres"))
    parser.add_argument("--timeout", type=int, default=5, help="连接和查询超时秒数，默认 5")
    parser.add_argument(
        "--sslmode", choices=["disable", "prefer", "require", "verify-ca", "verify-full"],
        default=os.getenv("PGSSLMODE", "prefer"), help="SSL 模式，默认 prefer",
    )
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout 必须大于 0")

    password = os.getenv("PGPASSWORD")
    if password is None:
        password = getpass.getpass("请输入数据库密码（输入不会显示）：")

    print(f"正在连接 {args.host}:{args.port}，数据库={args.dbname}，用户={args.user}...")
    started = time.monotonic()
    try:
        with psycopg.connect(
            host=args.host, port=args.port, user=args.user, password=password,
            dbname=args.dbname, connect_timeout=args.timeout, sslmode=args.sslmode,
            application_name="postgres_connection_test", autocommit=True,
            options=f"-c statement_timeout={args.timeout * 1000} -c default_transaction_read_only=on",
        ) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT current_database(), current_user, version(), 1")
                database, user, version, result = cursor.fetchone()
            print(f"连接及查询成功！耗时 {time.monotonic() - started:.2f} 秒")
            print(f"数据库：{database}\n当前用户：{user}\n服务器版本：{version}")
            print(f"测试查询结果：{result}\nSSL 加密：{'是' if conn.pgconn.ssl_in_use else '否'}")
        return 0
    except psycopg.Error as exc:
        detail = str(exc).replace(password, "***") if password else str(exc)
        print(f"连接或查询失败：{detail}", file=sys.stderr)
        print("请检查地址、端口、安全组/防火墙、数据库名、账号密码及 pg_hba.conf 配置。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\n测试已取消；非交互环境请设置 PGPASSWORD。", file=sys.stderr)
        sys.exit(130)
