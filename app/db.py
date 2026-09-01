"""SQLite 数据访问层（轻量封装，无 ORM 依赖）。"""
import sqlite3
import os
import hashlib
import secrets
from datetime import datetime, timedelta
from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    activation_code TEXT,
    plan TEXT DEFAULT 'buyout',
    monthly_quota INTEGER DEFAULT 500,
    used_quota INTEGER DEFAULT 0,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS activation_codes (
    code TEXT PRIMARY KEY,
    used INTEGER DEFAULT 0,
    used_by INTEGER,
    batch TEXT,
    plan TEXT,
    quota INTEGER,
    expired_at TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS scripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    source TEXT,            -- industry | link
    industry TEXT,
    original_text TEXT,
    type TEXT,              -- 解题/推荐/揭秘/案例/疑问
    persona TEXT,           -- 老板/专家...
    generated_text TEXT,
    title TEXT,
    status TEXT DEFAULT 'done',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS timbres (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT,
    file_path TEXT,
    reference_audio_id TEXT,   -- 百炼 CosyVoice voice_id（缓存，失效时由后端重新注册复用）
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS audios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    script_id INTEGER,
    timbre_id INTEGER,
    emotion TEXT,
    speed REAL,
    pitch REAL,      -- 音调 pitch_rate，0.5~2.0，默认 1.0
    volume INTEGER,  -- 音量 volume，0~100，默认 50
    seed INTEGER,    -- 随机种子 seed，0~65535，默认 0（同参数可复现结果）
    file_path TEXT,
    duration REAL,
    status TEXT DEFAULT 'done',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS avatars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT,
    file_path TEXT,
    status TEXT DEFAULT 'done',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    audio_id INTEGER,
    avatar_id INTEGER,
    file_path TEXT,
    poster_path TEXT,
    status TEXT DEFAULT 'done',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    video_id INTEGER,
    options TEXT,           -- json
    file_path TEXT,
    status TEXT DEFAULT 'done',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS covers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    edit_id INTEGER,
    style TEXT,
    title TEXT,
    file_path TEXT,
    status TEXT DEFAULT 'done',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS publishes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    cover_id INTEGER,
    platform TEXT,
    status TEXT DEFAULT 'done',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    user_id INTEGER,
    kind TEXT,
    progress INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',   -- pending|running|done|error
    result TEXT,                      -- json
    error TEXT,
    created_at TEXT
);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate(conn)
    _seed(conn)
    conn.close()


def _migrate(conn):
    # 兼容旧库：已有 activation_codes 表缺新列时补上
    cur = conn.cursor()
    cols = [r[1] for r in cur.execute("PRAGMA table_info(activation_codes)")]
    for col, ctype in [("plan", "TEXT"), ("quota", "INTEGER"), ("expired_at", "TEXT")]:
        if col not in cols:
            cur.execute(f"ALTER TABLE activation_codes ADD COLUMN {col} {ctype}")
    # timbres 表缺 reference_audio_id 时补上（CosyVoice2 音色缓存）
    tcols = [r[1] for r in cur.execute("PRAGMA table_info(timbres)")]
    if "reference_audio_id" not in tcols:
        cur.execute("ALTER TABLE timbres ADD COLUMN reference_audio_id TEXT")
    # scripts 表补真实文案元数据列（链接提取带入的点赞/评论/转发等）
    scols = [r[1] for r in cur.execute("PRAGMA table_info(scripts)")]
    _script_cols = [
        ("source_url", "TEXT"), ("video_title", "TEXT"), ("uploader", "TEXT"),
        ("like_count", "INTEGER"), ("comment_count", "INTEGER"),
        ("share_count", "INTEGER"), ("collect_count", "INTEGER"), ("duration", "REAL"),
    ]
    for col, ctype in _script_cols:
        if col not in scols:
            cur.execute(f"ALTER TABLE scripts ADD COLUMN {col} {ctype}")
    # edits 表补 note 列（记录本次剪辑真实状态：已生成字幕/未读取文案/已降级等）
    ecols = [r[1] for r in cur.execute("PRAGMA table_info(edits)")]
    if "note" not in ecols:
        cur.execute("ALTER TABLE edits ADD COLUMN note TEXT")
    # scripts 表补 updated_at（重复保存时记录更新时间）
    if "updated_at" not in scols:
        cur.execute("ALTER TABLE scripts ADD COLUMN updated_at TEXT")
    # audios 表补音调/音量/随机种子列（百炼 CosyVoice 合成可调项，便于复现与回显）
    acols = [r[1] for r in cur.execute("PRAGMA table_info(audios)")]
    for col, ctype in [("pitch", "REAL"), ("volume", "INTEGER"), ("seed", "INTEGER")]:
        if col not in acols:
            cur.execute(f"ALTER TABLE audios ADD COLUMN {col} {ctype}")
    # scripts 表补 is_public（提取链接文案默认公共，跨账号可见爆款库）
    scols2 = [r[1] for r in cur.execute("PRAGMA table_info(scripts)")]
    if "is_public" not in scols2:
        cur.execute("ALTER TABLE scripts ADD COLUMN is_public INTEGER DEFAULT 0")
    # 历史 source='link' 文案批量置公共（幂等）
    cur.execute("UPDATE scripts SET is_public=1 WHERE source='link' AND (is_public IS NULL OR is_public=0)")
    conn.commit()


def _seed(conn):
    # 仅开发/演示环境（SEED_DEMO=1）才注入演示账号与演示激活码；生产绝不 seed
    if os.environ.get("SEED_DEMO") != "1":
        return
    cur = conn.cursor()
    demo_codes = [
        ("LAOPAN2026", "buyout", 500),
        ("BUYOUT2999", "buyout", 500),
        ("BUYOUT4399", "buyout", 2000),
        ("TESTFREE", "buyout", 50),
    ]
    for c, plan, quota in demo_codes:
        cur.execute(
            "INSERT OR IGNORE INTO activation_codes(code,batch,plan,quota,created_at) VALUES(?,?,?,?,?)",
            (c, "seed", plan, quota, _now()))
    cur.execute("SELECT id FROM users WHERE username=?", ("laopan",))
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO users(username,password_hash,activation_code,plan,monthly_quota,created_at) VALUES(?,?,?,?,?,?)",
            ("laopan", _hash_pw("laopan123"), "LAOPAN2026", "buyout", 500, _now()))
    conn.commit()


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


import hmac as _hmac

_PBKDF2_ROUNDS = 100_000


def _hash_pw(pw: str) -> str:
    # 新格式：pbkdf2$<salt_hex>$<dk_hex>，随机 salt 防彩虹表/相同密码同哈希
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return "pbkdf2$" + salt.hex() + "$" + dk.hex()


def verify_pw(pw: str, h: str) -> bool:
    # 兼容历史 sha256（64 位 hex）账户；新账户一律 pbkdf2
    if h.startswith("pbkdf2$"):
        try:
            _, salt_hex, dk_hex = h.split("$")
            salt = bytes.fromhex(salt_hex)
            dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, _PBKDF2_ROUNDS)
        except Exception:
            return False
        return _hmac.compare_digest(dk.hex(), dk_hex)
    return hashlib.sha256(pw.encode("utf-8")).hexdigest() == h


def gen_code(prefix="CODE"):
    return prefix + secrets.token_hex(4).upper()
