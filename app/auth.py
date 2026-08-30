"""鉴权：注册（凭激活码）/ 登录 / JWT。不开放公开注册。"""
from datetime import datetime, timedelta
from jose import jwt
from fastapi import Depends, HTTPException, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.config import SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES
from app import db

bearer_scheme = HTTPBearer(auto_error=False)


def create_token(user_id: int, username: str) -> str:
    payload = {
        "sub": str(user_id),
        "username": username,
        "exp": datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(
    cred: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> dict:
    if cred is None or not cred.credentials:
        raise HTTPException(status_code=401, detail="未登录或 token 缺失")
    try:
        payload = jwt.decode(cred.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        uid = int(payload["sub"])
    except Exception:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=401, detail="用户不存在")
    return dict(row)


def register(username: str, password: str, code: str) -> dict:
    conn = db.get_conn()
    cur = conn.cursor()
    # 校验激活码
    row = cur.execute("SELECT * FROM activation_codes WHERE code=?", (code,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=400, detail="激活码不存在")
    row = dict(row)
    if row["used"]:
        conn.close()
        raise HTTPException(status_code=400, detail="激活码已被使用")
    # 用户名唯一
    if cur.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="用户名已存在")
    # 按激活码携带的套餐/额度开通（缺省 buyout/500）
    plan = row.get("plan") or "buyout"
    quota = row.get("quota") or 500
    cur.execute(
        "INSERT INTO users(username,password_hash,activation_code,plan,monthly_quota,created_at) VALUES(?,?,?,?,?,?)",
        (username, db._hash_pw(password), code, plan, quota, db._now()))
    uid = cur.lastrowid
    cur.execute("UPDATE activation_codes SET used=1, used_by=? WHERE code=?", (uid, code))
    conn.commit()
    conn.close()
    return {"user_id": uid, "token": create_token(uid, username)}


def login(username: str, password: str) -> dict:
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    if not row or not db.verify_pw(password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {"user_id": row["id"], "token": create_token(row["id"], username),
            "username": row["username"], "monthly_quota": row["monthly_quota"],
            "used_quota": row["used_quota"]}


def consume_quota(user_id: int, n: int = 1) -> bool:
    """扣减月额度；返回是否成功。"""
    conn = db.get_conn()
    cur = conn.cursor()
    row = cur.execute("SELECT monthly_quota,used_quota FROM users WHERE id=?", (user_id,)).fetchone()
    if not row or row["used_quota"] + n > row["monthly_quota"]:
        conn.close()
        return False
    cur.execute("UPDATE users SET used_quota=used_quota+? WHERE id=?", (n, user_id))
    conn.commit()
    conn.close()
    return True
