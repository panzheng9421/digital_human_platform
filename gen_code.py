#!/usr/bin/env python
"""数字人平台 —— 激活码管理脚本（运维工具）。

仅操作 activation_codes 表，不生成演示账号。生产环境靠它批量出码、查库存。

用法：
  python gen_code.py init                                   # 建表 + 迁移（不 seed）
  python gen_code.py gen --count 10 --plan buyout --quota 500 --batch launch --prefix LAO [--days 365]
  python gen_code.py list [--unused]                        # 列出激活码
  python gen_code.py stats                                  # 统计使用率

说明：
  - 激活码是运营数据，家在数据库，不在源码里。本脚本就是出码入口。
  - 导入 app.db 会加载 config 并触发安全启动检查；本脚本只管库、不发 token，
    因此自动放行 ALLOW_INSECURE_KEY=1（临时随机密钥，仅本进程有效）。
"""
import os
import sys
import argparse
from datetime import datetime, timedelta

# 必须在 import app 之前放行：本脚本只操作数据库，不需要真实 JWT 密钥。
os.environ.setdefault("ALLOW_INSECURE_KEY", "1")

# 允许从项目根目录直接 `python gen_code.py` 运行
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import db


def cmd_init(args):
    db.init_db()  # 建表 + 迁移；SEED_DEMO 未设置则不会注入演示数据
    print("数据库已初始化（建表 + 迁移），未注入任何演示数据。")


def cmd_gen(args):
    db.init_db()  # 确保表存在 + 跑到最新 schema
    conn = db.get_conn()
    cur = conn.cursor()
    created_at = db._now()
    expired_at = None
    if args.days:
        expired_at = (datetime.now() + timedelta(days=args.days)).strftime("%Y-%m-%d %H:%M:%S")
    ok = 0
    for _ in range(args.count):
        code = db.gen_code(args.prefix)
        cur.execute(
            "INSERT OR IGNORE INTO activation_codes(code,batch,plan,quota,expired_at,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (code, args.batch, args.plan, args.quota, expired_at, created_at),
        )
        if cur.rowcount == 1:
            ok += 1
            print("  + " + code)
    conn.commit()
    conn.close()
    exp_desc = f"有效期 {args.days} 天" if args.days else "永久有效"
    print(f"已生成 {ok} 个激活码（批次={args.batch or '-'}，plan={args.plan}，"
          f"quota={args.quota}，{exp_desc}）")
    if ok < args.count:
        print(f"注意：{args.count - ok} 个因码值重复被跳过（碰撞概率极低，可忽略）。")


def cmd_list(args):
    conn = db.get_conn()
    cur = conn.cursor()
    if args.unused:
        rows = cur.execute(
            "SELECT code,batch,plan,quota,expired_at,created_at "
            "FROM activation_codes WHERE used=0 ORDER BY created_at DESC"
        ).fetchall()
    else:
        rows = cur.execute(
            "SELECT code,batch,plan,quota,used,expired_at,created_at "
            "FROM activation_codes ORDER BY created_at DESC"
        ).fetchall()
    conn.close()
    if not rows:
        print("（无激活码）")
        return
    for r in rows:
        exp = r["expired_at"] or "永久"
        if args.unused:
            print(f"  {r['code']:<18} batch={r['batch'] or '-'} plan={r['plan']} "
                  f"quota={r['quota']} exp={exp}")
        else:
            used = "已用" if r["used"] else "未用"
            print(f"  {r['code']:<18} {used} batch={r['batch'] or '-'} plan={r['plan']} "
                  f"quota={r['quota']} exp={exp}")


def cmd_stats(args):
    conn = db.get_conn()
    cur = conn.cursor()
    total = cur.execute("SELECT COUNT(*) FROM activation_codes").fetchone()[0]
    used = cur.execute("SELECT COUNT(*) FROM activation_codes WHERE used=1").fetchone()[0]
    conn.close()
    print(f"激活码总计 {total}，已使用 {used}，未使用 {total - used}")


def cmd_wipe_demo(args):
    """清理历史演示激活码（batch='seed'）。默认 dry-run，--yes 才真删。"""
    conn = db.get_conn()
    cur = conn.cursor()
    n = cur.execute(
        "SELECT COUNT(*) FROM activation_codes WHERE batch='seed'"
    ).fetchone()[0]
    if args.yes:
        cur.execute("DELETE FROM activation_codes WHERE batch='seed'")
        conn.commit()
        print(f"已删除 {n} 个演示激活码（batch='seed'）。")
    else:
        print(f"将删除 {n} 个演示激活码（batch='seed'）。确认执行请加 --yes。")
    conn.close()


def main():
    p = argparse.ArgumentParser(description="数字人平台激活码管理工具")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="初始化数据库（建表+迁移，不 seed）")

    g = sub.add_parser("gen", help="生成激活码")
    g.add_argument("--count", type=int, default=1, help="生成数量（默认 1）")
    g.add_argument("--plan", default="buyout", help="套餐名（默认 buyout）")
    g.add_argument("--quota", type=int, default=500, help="月额度（条，默认 500）")
    g.add_argument("--batch", default="manual", help="批次标识（默认 manual）")
    g.add_argument("--prefix", default="CODE", help="码前缀（默认 CODE）")
    g.add_argument("--days", type=int, default=None, help="有效期天数，默认永久")

    l = sub.add_parser("list", help="列出激活码")
    l.add_argument("--unused", action="store_true", help="仅列未使用")

    sub.add_parser("stats", help="统计激活码使用情况")

    w = sub.add_parser("wipe-demo", help="清理历史演示激活码（batch='seed'）")
    w.add_argument("--yes", action="store_true", help="确认执行删除（默认仅预览）")

    args = p.parse_args()
    {
        "init": cmd_init,
        "gen": cmd_gen,
        "list": cmd_list,
        "stats": cmd_stats,
        "wipe-demo": cmd_wipe_demo,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
