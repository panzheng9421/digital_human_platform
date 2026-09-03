"""全部 API 路由。串联：文案(入口1/入口2) → 配音 → 数字人 → 剪辑 → 封面 → 发布。"""
import os
import time
import threading
from datetime import datetime

from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse

from app import db, auth
from app.config import STORAGE_DIR, AVATAR_PROVIDER, COSYVOICE_FORMAT, ALLOWED_AVATAR_FORMATS, MAX_AVATAR_SIZE_MB
from app.task_manager import (create_task, update, get_task, set_result,
                              start_progress_ticker)
from app.data import viral_scripts as vs
from app.data import sensitive_words as sw
from app.services import script_service as ss
from app.services import media_utils as mu
from app.services import heygem_client as hg
from app.services import oss_client as oss
from app.services import cosyvoice_client as cv


def _register_timbre(ref_path: str) -> str:
    """注册音色：百炼 CosyVoice 声音复刻只接受公网音频 URL，官方入参**没有文本字段**
    （text 仅 Qwen-TTS 复刻 qwen-voice-enrollment 支持），因此无需先 ASR 转写逐字稿。
    复刻效果由官方可选参数 language_hints 辅助（见 cosyvoice_client.upload_reference_audio）。"""
    return cv.upload_reference_audio(ref_path)

api = APIRouter(prefix="/api")
get_user = auth.get_current_user

# 配音合成的**端到端吞吐**（字/秒），仅用于估算耗时、驱动进度条，不影响真实音频。
# 实测（2026-08-31，cosyvoice-v3.5-flash 复刻音色，语速 1.0）：
#   90 字 -> 9.7s（音频 18.7s）；315 字 -> 34.9s（音频 67.4s），RTF 稳定在 0.52。
# 即：音频语速约 4.7 字/秒，乘以 RTF 0.52 ≈ 9 字/秒的墙钟耗时。
TTS_CHARS_PER_SEC = 9.0


def _rel(path: str) -> str:
    """绝对路径 -> 相对 storage 的 url 路径。"""
    return os.path.relpath(path, STORAGE_DIR).replace("\\", "/")


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _looks_like_audio(head: bytes, ext: str) -> bool:
    """按扩展名对应的文件头 magic 校验是否为真实音频；命中任一已知音频签名也放行。"""
    if ext == ".pcm":
        return True  # raw PCM 无文件头 magic，信任扩展名
    sigs = {
        ".wav": [b"RIFF", b"WAVE"],
        ".wma": [b"\x30\x26\xb2\x75"],
        ".mp3": [b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"],
        ".m4a": [b"ftyp"],
        ".aac": [b"\xff\xf1", b"\xff\xf9", b"ID3"],
        ".flac": [b"fLaC"],
        ".ogg": [b"OggS"],
        ".opus": [b"OggS"],
        ".webm": [b"\x1a\x45\xdf\xa3"],
    }
    for sig in sigs.get(ext, []):
        if sig in head:
            return True
    for others in sigs.values():
        for sig in others:
            if sig in head:
                return True
    return False


def _looks_like_avatar(head: bytes, ext: str) -> bool:
    """按扩展名对应的文件头 magic 校验形象是否为真实图片或视频；命中任一已知签名也放行。

    真实生成(heygem/duix)用视频形象(对口型驱动源)，mock 降级预览用静态图，故图片视频都认。
    """
    sigs = {
        ".jpg":  [b"\xff\xd8\xff"],
        ".jpeg": [b"\xff\xd8\xff"],
        ".png":  [b"\x89PNG"],
        ".webp": [b"WEBP"],
        ".bmp":  [b"BM"],
        ".gif":  [b"GIF87a", b"GIF89a"],
        ".mp4":  [b"ftyp"],
        ".mov":  [b"ftyp"],
        ".m4v":  [b"ftyp"],
        ".webm": [b"\x1a\x45\xdf\xa3"],
        ".mkv":  [b"\x1a\x45\xdf\xa3"],
        ".avi":  [b"AVI"],
    }
    for sig in sigs.get(ext, []):
        if sig in head[:16]:
            return True
    for others in sigs.values():
        for sig in others:
            if sig in head[:16]:
                return True
    return False


def _audio_duration(path: str, ext: str) -> float:
    """尽量从音频文件读真实时长（秒）；读不到返回 0.0。

    注意：百炼 DashScope 返回的 WAV 文件头 nframes 可能为占位大数(0x7FFFFFFF)，
    遇到这种异常值改按文件实际字节数估算时长。
    """
    try:
        if ext == "wav":
            import wave, os
            with wave.open(path, "rb") as wf:
                nframes = wf.getnframes()
                fr = wf.getframerate()
                ch = wf.getnchannels()
                sw = wf.getsampwidth()
                if fr and ch and sw:
                    # 正常 nframes
                    if 0 < nframes < 1_000_000_000:
                        return round(nframes / fr, 2)
                    # 异常：按文件大小估算（减去标准 44 字节 WAV 头）
                    size = os.path.getsize(path)
                    samples = max(0, size - 44) / (ch * sw)
                    return round(samples / fr, 2)
    except Exception:
        pass
    return 0.0


def _run(task_id, fn):
    """后台执行并捕获异常。"""
    try:
        fn()
    except Exception as e:
        update(task_id, status="error", error=str(e))


# ============ 鉴权 ============
@api.post("/auth/register")
def register(username: str = Form(...), password: str = Form(...), code: str = Form(...)):
    return auth.register(username, password, code)


@api.post("/auth/login")
def login(username: str = Form(...), password: str = Form(...)):
    return auth.login(username, password)


# ============ 行业爆款（入口1）============
@api.post("/scripts/industry")
def industry_scripts(industry: str = Form(...), user=Depends(get_user)):
    from app.services import classify as cl
    name, items = vs.match_industry(industry)
    norm = cl.normalize_industry(industry)
    # 合并用户真实库里、同行业的提取文案（让搜索能用真实数据）
    # 公共爆款库：is_public=1 的提取链接文案对所有账号可见
    conn = db.get_conn()
    if norm and norm != "其他":
        rows = conn.execute(
            "SELECT id,user_id,original_text,video_title,uploader,like_count,comment_count,share_count,collect_count,source_url,industry,is_public "
            "FROM scripts WHERE (user_id=? OR is_public=1) AND industry=? ORDER BY created_at DESC", (user["id"], norm)).fetchall()
    else:
        rows = conn.execute(
            "SELECT id,user_id,original_text,video_title,uploader,like_count,comment_count,share_count,collect_count,source_url,industry,is_public "
            "FROM scripts WHERE (user_id=? OR is_public=1) AND source='link' ORDER BY created_at DESC", (user["id"],)).fetchall()
    conn.close()
    mine = [{
        "title": (r["video_title"] or "我的提取文案"),
        "content": r["original_text"] or "",
        "isMine": r["user_id"] == user["id"],
        "isPublic": bool(r["is_public"]),
        "sid": r["id"],
        "industry": r["industry"],
        "like_count": r["like_count"], "comment_count": r["comment_count"],
        "share_count": r["share_count"], "collect_count": r["collect_count"], "source_url": r["source_url"],
        "uploader": r["uploader"],
    } for r in rows]
    sample_items = [dict(it, isMine=False) for it in items]
    merged = mine + sample_items
    if not items and not mine:
        return {"matched": False, "industry": None,
                "items": [{"title": "未找到该行业专属文案，先看看通用爆款",
                           "content": "你可以先输入更具体的行业，如：餐饮、房产、教育、美妆、穿搭、健身、数码、本地生活。下方为通用示例。",
                           "isMine": False}],
                "available": list(vs.VIRAL_SCRIPTS.keys())}
    return {"matched": bool(name or mine), "industry": name or norm, "items": merged}


@api.post("/scripts/save")
def save_script(source: str = Form(...), industry: str = Form(""),
                original_text: str = Form(...), type_: str = Form("解题型"),
                persona: str = Form("老板"),
                source_url: str = Form(""), video_title: str = Form(""),
                uploader: str = Form(""), like_count: int = Form(0),
                comment_count: int = Form(0), share_count: int = Form(0),
                collect_count: int = Form(0), duration: float = Form(0.0), user=Depends(get_user)):
    conn = db.get_conn()
    cur = conn.cursor()
    # —— 去重：有 source_url 按链接判重，否则按原文内容判重（同用户）——
    existing = None
    su = (source_url or "").strip()
    if su:
        existing = cur.execute(
            "SELECT id FROM scripts WHERE user_id=? AND source_url=?",
            (user["id"], su)).fetchone()
    if existing is None:
        existing = cur.execute(
            "SELECT id FROM scripts WHERE user_id=? AND original_text=? AND (source_url IS NULL OR source_url='')",
            (user["id"], original_text)).fetchone()

    if existing:
        # 重复：更新已有记录（保留 generated_text/title/status/created_at），返回 duplicated 标记
        sid = existing["id"]
        cur.execute("""UPDATE scripts SET source=?,industry=?,original_text=?,type=?,persona=?,
                        source_url=?,video_title=?,uploader=?,like_count=?,comment_count=?,
                        share_count=?,collect_count=?,duration=?,updated_at=?
                        WHERE id=?""",
                    (source, industry, original_text, type_, persona,
                     source_url, video_title, uploader, like_count, comment_count,
                     share_count, collect_count, duration, _now(), sid))
        conn.commit(); conn.close()
        return {"script_id": sid, "duplicated": True}

    cur.execute("""INSERT INTO scripts(user_id,source,industry,original_text,type,persona,status,
                    source_url,video_title,uploader,like_count,comment_count,share_count,collect_count,duration,created_at,is_public)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (user["id"], source, industry, original_text, type_, persona, "created",
                 source_url, video_title, uploader, like_count, comment_count, share_count, collect_count, duration, _now(),
                 1 if source == "link" else 0))
    sid = cur.lastrowid
    conn.commit(); conn.close()
    return {"script_id": sid, "duplicated": False}


@api.post("/scripts/rewrite")
def rewrite(script_id: int = Form(...), type_: str = Form("解题型"),
            persona: str = Form("老板"), user=Depends(get_user)):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM scripts WHERE id=? AND user_id=?", (script_id, user["id"])).fetchone()
    conn.close()
    if not row:
        raise HTTPException(400, "文案不存在")
    # 若已是改写结果，基于 original 重改
    base = row["original_text"]
    res = ss.rewrite(base, type_, persona)
    conn = db.get_conn()
    conn.execute("UPDATE scripts SET type=?,persona=?,generated_text=?,title=?,status='done' WHERE id=?",
                 (type_, persona, res["generated_text"], res["title"], script_id))
    conn.commit(); conn.close()
    return {"generated_text": res["generated_text"], "title": res["title"],
            "cover_title": res.get("cover_title", ""), "cover_subtitle": res.get("cover_subtitle", ""),
            "source": res.get("source", "template"), "note": res.get("note", "")}


@api.post("/scripts/check")
def check_words(text: str = Form(...)):
    hits = sw.check(text)
    return {"hits": hits, "count": len(hits), "safe": len(hits) == 0}


@api.post("/scripts/update-generated")
def update_generated(script_id: int = Form(...), generated_text: str = Form(...), user=Depends(get_user)):
    conn = db.get_conn()
    row = conn.execute("SELECT id FROM scripts WHERE id=? AND user_id=?", (script_id, user["id"])).fetchone()
    if not row:
        conn.close()
        raise HTTPException(400, "文案不存在")
    conn.execute("UPDATE scripts SET generated_text=?, updated_at=? WHERE id=?",
                 (generated_text, _now(), script_id))
    conn.commit(); conn.close()
    return {"script_id": script_id}


@api.get("/scripts/list")
def list_scripts(user=Depends(get_user)):
    """文案库列表：返回当前用户全部文案（含智能分类与真实元数据）。注意须定义在 /scripts/{sid} 之前。"""
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT id,source,industry,type,original_text,video_title,uploader,"
        "like_count,comment_count,share_count,collect_count,source_url,duration,status,created_at "
        "FROM scripts WHERE user_id=? ORDER BY id DESC", (user["id"],)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@api.get("/scripts/{sid}")
def get_script(sid: int, user=Depends(get_user)):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM scripts WHERE id=? AND user_id=?", (sid, user["id"])).fetchone()
    conn.close()
    return dict(row) if row else {}


@api.get("/scripts/{sid}/audios")
def list_script_audios(sid: int, user=Depends(get_user)):
    """返回某条文案的全部历史配音，按 id 倒序（最新在前）。"""
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT id, script_id, timbre_id, emotion, speed, pitch, volume, seed, "
        "file_path, duration, status, created_at "
        "FROM audios WHERE script_id=? AND user_id=? ORDER BY id DESC",
        (sid, user["id"])
    ).fetchall()
    conn.close()
    return [{
        "audio_id": r["id"],
        "script_id": r["script_id"],
        "timbre_id": r["timbre_id"],
        "emotion": r["emotion"],
        "speed": r["speed"],
        "pitch": r["pitch"],
        "volume": r["volume"],
        "seed": r["seed"],
        "url": "/files/" + r["file_path"],
        "duration": r["duration"],
        "status": r["status"],
        "created_at": r["created_at"],
    } for r in rows]


def _rows_to_videos(rows, conn=None):
    """把 videos 查询行转成前端需要的数据结构（含 script_id / 文案标题）。"""
    out = []
    if not rows:
        return out
    audio_ids = [r["audio_id"] for r in rows if r["audio_id"]]
    script_map = {}
    if conn and audio_ids:
        placeholders = ",".join("?" * len(audio_ids))
        for ar in conn.execute(
            f"SELECT a.id AS audio_id, a.script_id, s.generated_text "
            f"FROM audios a LEFT JOIN scripts s ON a.script_id=s.id "
            f"WHERE a.id IN ({placeholders})",
            audio_ids
        ).fetchall():
            title = ""
            if ar["generated_text"]:
                title = ar["generated_text"].replace("\n", " ")[:36]
            script_map[ar["audio_id"]] = {
                "script_id": ar["script_id"],
                "script_title": title,
            }
    for r in rows:
        item = {
            "video_id": r["id"],
            "audio_id": r["audio_id"],
            "avatar_id": r["avatar_id"],
            "status": r["status"],
            "created_at": r["created_at"],
        }
        if r["file_path"]:
            item["video_url"] = "/files/" + r["file_path"]
        if r["poster_path"]:
            item["poster_url"] = "/files/" + r["poster_path"]
        info = script_map.get(r["audio_id"])
        if info:
            item["script_id"] = info["script_id"]
            item["script_title"] = info["script_title"]
        out.append(item)
    return out


@api.get("/scripts/{sid}/videos")
def list_script_videos(sid: int, user=Depends(get_user)):
    """返回某条文案的全部历史口播视频，按 id 倒序（最新在前）。"""
    conn = db.get_conn()
    audio_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM audios WHERE script_id=? AND user_id=?",
        (sid, user["id"])
    ).fetchall()]
    if not audio_ids:
        conn.close()
        return []
    placeholders = ",".join("?" * len(audio_ids))
    rows = conn.execute(
        f"SELECT id, audio_id, avatar_id, file_path, poster_path, status, created_at "
        f"FROM videos WHERE user_id=? AND audio_id IN ({placeholders}) "
        f"ORDER BY id DESC",
        [user["id"], *audio_ids]
    ).fetchall()
    out = _rows_to_videos(rows, conn)
    conn.close()
    return out


@api.get("/videos")
def list_user_videos(user=Depends(get_user)):
    """返回当前用户全部历史口播视频（数字人页侧边栏直接点进来也能看到记录）。"""
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT id, audio_id, avatar_id, file_path, poster_path, status, created_at "
        "FROM videos WHERE user_id=? ORDER BY id DESC",
        (user["id"],)
    ).fetchall()
    out = _rows_to_videos(rows, conn)
    conn.close()
    return out


@api.delete("/videos/{video_id}")
def delete_video(video_id: int, user=Depends(get_user)):
    """删除数字人视频（级联删除其名下所有剪辑成品）。
    清理：本地视频文件 + 关联 edits 文件/行 + videos 行。
    注意：poster_path 指向用户数字人形象（storage/avatars/），属可复用资产，不删。"""
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM videos WHERE id=? AND user_id=?",
                       (video_id, user["id"])).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "视频不存在")
    # 1) 级联删剪辑成品（文件 + 行）
    edits = conn.execute("SELECT file_path FROM edits WHERE video_id=? AND user_id=?",
                         (video_id, user["id"])).fetchall()
    for e in edits:
        fp = os.path.join(STORAGE_DIR, (e["file_path"] or "").replace("/", os.sep))
        if fp and os.path.exists(fp):
            try:
                os.remove(fp)
            except Exception:
                pass
    conn.execute("DELETE FROM edits WHERE video_id=? AND user_id=?", (video_id, user["id"]))
    # 2) 删主视频文件
    vf = os.path.join(STORAGE_DIR, (row["file_path"] or "").replace("/", os.sep))
    if vf and os.path.exists(vf):
        try:
            os.remove(vf)
        except Exception:
            pass
    # 3) 删视频行
    conn.execute("DELETE FROM videos WHERE id=?", (video_id,))
    conn.commit(); conn.close()
    return {"ok": True}


# ============ 链接提取（入口2）============
@api.post("/extract")
def extract(url: str = Form(...), user=Depends(get_user)):
    try:
        res = ss.extract_from_link(url)
        print(f"[/extract] user={user.get('id')} url={url[:80]} original_text_type={type(res.get('original_text')).__name__} len={len(str(res.get('original_text','')))} meta={res.get('meta')}")
        return res
    except Exception as e:
        # 把具体错误返回给前端，方便定位抖音/快手/B站等链接下载失败原因
        print(f"[/extract] ERROR user={user.get('id')} url={url[:80]} err={e}")
        return JSONResponse(status_code=400, content={
            "original_text": "",
            "source_url": url,
            "industry": "", "type": "", "meta": {},
            "note": f"提取失败：{e}",
        })


@api.post("/extract/file")
def extract_file(file: UploadFile = File(...), user=Depends(get_user)):
    """上传视频/音频文件直接提取文案（最稳，不依赖第三方下载）。"""
    from app.services import asr_client as ac
    if not ac.available():
        return {"original_text": "", "industry": "", "type": "", "meta": {},
                "note": "未配置百炼 DASHSCOPE_API_KEY，无法转写"}
    ext = (os.path.splitext(file.filename)[1] or ".mp4").lower()
    path = os.path.join(STORAGE_DIR, "temp", f"up_{user['id']}_{int(time.time() * 1000)}{ext}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(file.file.read())
    try:
        return ss.extract_from_file(path)
    except Exception as e:
        return {"original_text": "", "industry": "", "type": "", "meta": {},
                "note": "提取失败：" + str(e)}


# ============ 配音 ============
@api.post("/timbres/upload")
def upload_timbre(name: str = Form("我的音色"), file: UploadFile = File(...), user=Depends(get_user)):
    """上传参考音频并注册为可复刻音色。

    校验规则与百炼 CosyVoice 声音复刻官方要求严格对齐：
      格式 WAV(16bit)/MP3/M4A、大小 ≤10MB、时长 ≤60s（推荐 10~20s）。
    """
    # 1) 扩展名：官方仅支持 wav / mp3 / m4a
    ext = (os.path.splitext(file.filename)[1] or "").lower()
    if ext not in cv.ALLOWED_TIMBRE_FORMATS:
        raise HTTPException(400,
            f"仅支持 {' / '.join(sorted(f.lstrip('.') for f in cv.ALLOWED_TIMBRE_FORMATS))} 格式"
            f"（百炼 CosyVoice 声音复刻官方要求），当前上传：{ext or '无扩展名'}")
    # 2) 二次校验：读文件头 magic bytes，拦截改后缀的非音频文件
    head = file.file.read(12)
    file.file.seek(0)
    if not _looks_like_audio(head, ext):
        raise HTTPException(400, "文件内容不是有效音频，请重新选择")
    # 3) 体积校验：官方 ≤10MB
    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)
    if size > cv.MAX_TIMBRE_SIZE_MB * 1024 * 1024:
        raise HTTPException(400,
            f"音频文件不能超过 {cv.MAX_TIMBRE_SIZE_MB}MB（当前 {size / 1024 / 1024:.1f}MB），"
            f"建议裁剪到 10~20 秒后重新上传")
    path = os.path.join(STORAGE_DIR, "timbre", f"t{user['id']}_{int(time.time())}{ext}")
    with open(path, "wb") as f:
        f.write(file.file.read())
    # 4) 时长校验：官方最长 60s（推荐 10~20s）。
    #    目前仅 WAV 能可靠读到时长；mp3/m4a 读不到（返回 0）则跳过，避免误杀。
    duration = _audio_duration(path, ext.lstrip("."))
    if duration > 0 and duration > cv.MAX_TIMBRE_DURATION_SEC:
        try:
            os.remove(path)
        except Exception:
            pass
        raise HTTPException(400,
            f"音频时长 {duration:.1f}s 超过官方上限 {cv.MAX_TIMBRE_DURATION_SEC:.0f}s，请裁剪后重新上传")
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO timbres(user_id,name,file_path,created_at) VALUES(?,?,?,?)",
                (user["id"], name, _rel(path), _now()))
    tid = cur.lastrowid
    conn.commit(); conn.close()

    # 后台预注册：转写真实逐字稿 + 注册 CosyVoice 音色并缓存，失败不影响上传（首次配音时会重试）
    def _pre_register():
        try:
            rid = _register_timbre(path)
            conn2 = db.get_conn()
            conn2.execute("UPDATE timbres SET reference_audio_id=? WHERE id=?", (rid, tid))
            conn2.commit(); conn2.close()
            print(f"[timbre] 预注册完成 timbre={tid} raid={rid}")
        except Exception as e:
            print(f"[timbre] 预注册失败（首次配音时会重试注册）: {e}")
    threading.Thread(target=_pre_register, daemon=True).start()

    return {"timbre_id": tid, "name": name, "url": "/files/" + _rel(path)}


@api.get("/timbres")
def list_timbres(user=Depends(get_user)):
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM timbres WHERE user_id=?", (user["id"],)).fetchall()
    conn.close()
    return [{"id": r["id"], "name": r["name"], "url": "/files/" + r["file_path"],
             "reference_audio_id": r["reference_audio_id"] or "",
             "created_at": r["created_at"]} for r in rows]


@api.delete("/timbres/{tid}")
def delete_timbre(tid: int, user=Depends(get_user)):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM timbres WHERE id=? AND user_id=?",
                       (tid, user["id"])).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "音色不存在")
    raid = (row["reference_audio_id"] or "").strip()
    conn.execute("DELETE FROM timbres WHERE id=?", (tid,))
    conn.commit(); conn.close()
    # 同步删除百炼云端音色，释放配额。
    # 官方规则：每账号 CosyVoice 上限 1000 个音色，超限后新建直接失败且不会自动淘汰；
    # 另有「1 年未被合成调用则自动清理」兜底。云端删除失败不回滚本地删除。
    cloud_deleted, cloud_msg = False, ""
    if raid:
        try:
            cv.delete_voice(raid)
            cloud_deleted = True
            print(f"[timbre] 已同步删除云端音色 voice_id={raid}")
        except Exception as e:
            cloud_msg = str(e)
            print(f"[timbre] 删除云端音色失败（本地已删除）voice_id={raid}: {e}")
    # 顺带删除落盘音频文件（路径相对 storage）
    try:
        fp = os.path.join(STORAGE_DIR, row["file_path"].replace("/", os.sep))
        if os.path.exists(fp):
            os.remove(fp)
    except Exception:
        pass
    return {"ok": True, "cloud_deleted": cloud_deleted, "cloud_msg": cloud_msg}


@api.post("/dubbing/generate")
def dubbing_generate(script_id: int = Form(...), timbre_id: int = Form(0),
                     emotion: str = Form("自然"), speed: float = Form(1.0),
                     pitch: float = Form(1.0), volume: int = Form(50),
                     seed: int = Form(0), user=Depends(get_user)):
    """生成配音。pitch=音调(0.5~2.0)、volume=音量(0~100)、seed=随机种子(0~65535，可复现)。"""
    conn = db.get_conn()
    s = conn.execute("SELECT * FROM scripts WHERE id=? AND user_id=?", (script_id, user["id"])).fetchone()
    conn.close()
    if not s:
        raise HTTPException(400, "文案不存在")
    # 必须改写后才能配音：未改写（generated_text 为空）禁止用原始文案生成
    if not (s["generated_text"] and str(s["generated_text"]).strip()):
        raise HTTPException(400, "该文案尚未改写，请先到改写页处理后再配音")
    text = s["generated_text"]
    tid = create_task("dubbing", user["id"])

    def work():
        update(tid, progress=10, status="running")
        time.sleep(0.3)
        fmt = (COSYVOICE_FORMAT or "wav").lower()
        ext = "mp3" if fmt == "mp3" else "wav"
        out = os.path.join(STORAGE_DIR, "audios", f"a{user['id']}_{int(time.time()*1000)}.{ext}")
        duration = 3.0
        provider = "placeholder"
        note = ""
        # 百炼 CosyVoice 的 call() 会一直阻塞到整段音频返回，期间**拿不到真实中间进度**
        # （on_progress 只在最后触发一次）。所以按「文本字数 / (语速 × 经验字速)」估算耗时，
        # 用推进器把 15%→72% 平滑推着走；预估用尽仍未完成则停在 70% 等真实完成，绝不提前到 100%。
        est_sec = max(2.0, len(text or "") / max(0.5, TTS_CHARS_PER_SEC * (speed or 1.0)))
        ticker = start_progress_ticker(tid, 15, 72, est_sec)
        try:
            if cv.available() and timbre_id:
                # —— 真实声音克隆分支：CosyVoice2（替代 fish+asr）——
                try:
                    conn = db.get_conn()
                    t = conn.execute("SELECT * FROM timbres WHERE id=? AND user_id=?",
                                     (timbre_id, user["id"])).fetchone()
                    conn.close()
                    ref_path = os.path.join(STORAGE_DIR, t["file_path"]) if t and t["file_path"] else None
                    if not ref_path or not os.path.exists(ref_path):
                        raise RuntimeError("音色文件缺失")
                    raid = (t["reference_audio_id"] or "") if t else ""
                    # 模型升级后，DB 里旧音色是在旧模型下注册的（跨模型不可用）；
                    # 主动检测前缀不匹配就清空 raid，触发下方用本地原文件重新注册到当前模型。
                    if raid and not raid.startswith(cv.TARGET_MODEL + "-"):
                        print(f"[bailian] 音色模型不匹配（raid={raid[:24]}… 当前={cv.TARGET_MODEL}），主动重新注册")
                        raid = ""
                    audio_bytes = None
                    last_err = None
                    # 情绪 -> CosyVoice instruct 指令。
                    # 现在不再写死 "用XX的语气说"，而是从 cosyvoice_client.EMOTION_INSTRUCTIONS 取多维度描述
                    # （情感 + 语气 + 场景），并且不写语速/音调（由 speech_rate / pitch_rate 参数控制）。
                    instruct = cv.build_instruction(emotion)
                    for _attempt in range(2):
                        if not raid:
                            # 首次 / raid 失效：重新注册音色，id 缓存到 timbre 行
                            raid = _register_timbre(ref_path)
                            conn = db.get_conn()
                            conn.execute("UPDATE timbres SET reference_audio_id=? WHERE id=?",
                                         (raid, timbre_id))
                            conn.commit(); conn.close()
                        try:
                            audio_bytes = cv.synthesize(text, raid, speed=speed, instruct=instruct,
                                                        pitch=pitch, volume=volume, seed=seed)
                            break
                        except Exception as e2:
                            last_err = e2
                            # raid 可能失效（服务重启/缓存清理），清空后用本地音频重传重取一次
                            raid = ""
                            continue
                    if audio_bytes is None:
                        raise RuntimeError(f"CosyVoice 合成失败（已尝试重传音色）: {last_err}")
                    with open(out, "wb") as f:
                        f.write(audio_bytes)
                    duration = _audio_duration(out, ext)
                    provider = "cosyvoice"
                except Exception as e:
                    # 任意失败 -> 回退占位 wav，保证流程不中断
                    msg = str(e)
                    if "格式不支持" in msg or "suffix" in msg or "InvalidFormData" in msg:
                        note = "音色格式不被 CosyVoice 支持，请重新上传 WAV / MP3 / M4A 格式的音色（≤10MB，10~20 秒最佳）"
                    else:
                        note = f"CosyVoice 失败已回退占位音频: {e}"
                    out = out.rsplit(".", 1)[0] + ".wav"
                    mu.gen_wav(text, emotion, speed, out)
            else:
                # —— 占位分支：本地合成可播放 WAV（未启用 CosyVoice 或无音色）——
                out = out.rsplit(".", 1)[0] + ".wav"
                mu.gen_wav(text, emotion, speed, out)
        finally:
            ticker.stop()
        update(tid, progress=80)
        conn = db.get_conn()
        cur = conn.cursor()
        cur.execute("INSERT INTO audios(user_id,script_id,timbre_id,emotion,speed,pitch,volume,seed,file_path,duration,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (user["id"], script_id, timbre_id, emotion, speed, pitch, volume, seed,
                     _rel(out), duration, "done", _now()))
        aid = cur.lastrowid
        # 按文案去重：同一 script_id 只保留本次最新配音，删除之前的旧配音（文件 + DB 行）。
        # 旧配音的 wav 不被任何数字人视频依赖（数字人视频是自包含 mp4，只走剪辑不重新配音），可放心删。
        try:
            old_rows = conn.execute(
                "SELECT id, file_path FROM audios WHERE script_id=? AND user_id=? AND id != ?",
                (script_id, user["id"], aid)
            ).fetchall()
            for o in old_rows:
                fp = os.path.join(STORAGE_DIR, (o["file_path"] or "").replace("/", os.sep))
                if fp and os.path.exists(fp):
                    try:
                        os.remove(fp)
                        print(f"[dubbing] 清理冗余旧配音: {fp}")
                    except Exception as e:
                        print(f"[dubbing] 清理冗余旧配音失败(可忽略): {fp} -> {e}")
            if old_rows:
                conn.execute("DELETE FROM audios WHERE script_id=? AND user_id=? AND id != ?",
                             (script_id, user["id"], aid))
                print(f"[dubbing] 已清理 {len(old_rows)} 条冗余旧配音记录(script={script_id})")
        except Exception as e:
            print(f"[dubbing] 旧配音去重异常(不影响本次结果): {e}")
        conn.commit(); conn.close()
        r = {"audio_id": aid, "url": "/files/" + _rel(out), "duration": duration,
             "emotion": emotion, "speed": speed, "pitch": pitch,
             "volume": volume, "seed": seed, "tts": provider}
        if note:
            r["note"] = note
        set_result(tid, r)
    threading.Thread(target=_run, args=(tid, work)).start()
    return {"task_id": tid}


# ============ 数字人 ============
@api.post("/avatars/upload")
def upload_avatar(name: str = Form("我的形象"), file: UploadFile = File(...), user=Depends(get_user)):
    # 1) 扩展名：真实生成吃视频形象，mock 降级吃图片，故图片/视频都放行
    ext = (os.path.splitext(file.filename)[1] or "").lower()
    if ext not in ALLOWED_AVATAR_FORMATS:
        raise HTTPException(400,
            f"形象仅支持 {' / '.join(sorted(f.lstrip('.') for f in ALLOWED_AVATAR_FORMATS))} 图片/视频格式"
            f"（当前上传：{ext or '无扩展名'}）")
    # 2) 二次校验：读文件头 magic bytes，拦截改后缀的非图片/视频文件
    head = file.file.read(16)
    file.file.seek(0)
    if not _looks_like_avatar(head, ext):
        raise HTTPException(400, "文件内容不是有效的图片或视频，请重新选择")
    # 3) 体积校验：≤ MAX_AVATAR_SIZE_MB
    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)
    if size > MAX_AVATAR_SIZE_MB * 1024 * 1024:
        raise HTTPException(400,
            f"形象文件不能超过 {MAX_AVATAR_SIZE_MB}MB（当前 {size / 1024 / 1024:.1f}MB）")
    path = os.path.join(STORAGE_DIR, "avatars", f"av{user['id']}_{int(time.time())}{ext}")
    with open(path, "wb") as f:
        f.write(file.file.read())
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO avatars(user_id,name,file_path,status,created_at) VALUES(?,?,?,?,?)",
                (user["id"], name, _rel(path), "done", _now()))
    aid = cur.lastrowid
    conn.commit(); conn.close()
    return {"avatar_id": aid, "name": name, "url": "/files/" + _rel(path)}


@api.get("/avatars")
def list_avatars(user=Depends(get_user)):
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM avatars WHERE user_id=?", (user["id"],)).fetchall()
    conn.close()
    return [{"id": r["id"], "name": r["name"], "url": "/files/" + r["file_path"]} for r in rows]


@api.delete("/avatars/{avatar_id}")
def delete_avatar(avatar_id: int, user=Depends(get_user)):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM avatars WHERE id=? AND user_id=?",
                       (avatar_id, user["id"])).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "形象不存在")
    fp = os.path.join(STORAGE_DIR, row["file_path"])
    try:
        if os.path.exists(fp):
            os.remove(fp)
    except Exception as e:
        conn.close()
        raise HTTPException(500, f"删除文件失败: {e}")
    conn.execute("DELETE FROM avatars WHERE id=?", (avatar_id,))
    conn.commit(); conn.close()
    return {"ok": True}


@api.post("/digital_human/generate")
def dh_generate(audio_id: int = Form(...), avatar_id: int = Form(...), user=Depends(get_user)):
    conn = db.get_conn()
    a = conn.execute("SELECT * FROM audios WHERE id=? AND user_id=?", (audio_id, user["id"])).fetchone()
    av = conn.execute("SELECT * FROM avatars WHERE id=? AND user_id=?", (avatar_id, user["id"])).fetchone()
    conn.close()
    if not a or not av:
        raise HTTPException(400, "音频或形象不存在")
    audio_path = os.path.join(STORAGE_DIR, a["file_path"])
    avatar_path = os.path.join(STORAGE_DIR, av["file_path"])
    tid = create_task("digital_human", user["id"])

    def work():
        update(tid, progress=20, status="running")
        time.sleep(0.3)
        out = os.path.join(STORAGE_DIR, "videos", f"v{user['id']}_{int(time.time()*1000)}.mp4")
        if hg.available():
            # —— 真实分支：调用 PAI-EAS 上的 HeyGem / duix.avatar 出片 ——
            if not oss.available():
                update(tid, status="error",
                       error="未配置 OSS：云端 EAS 无法拉取本地上传的音频/形象文件。"
                             "请在 start.bat / 环境变量设置 OSS_BUCKET / OSS_ENDPOINT / OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET。")
                return
            # 输入先传 OSS，拿到 EAS 可拉取的 URL
            try:
                audio_url = oss.upload_file(audio_path)
                video_url = oss.upload_file(avatar_path)
            except Exception as e:
                update(tid, status="error", error=f"OSS 上传失败: {e}")
                return
            # 收集输入副本的 object_key，生成完成后清理（只删 OSS，不删本地 storage/avatars、audios）
            in_keys = [oss.object_key_from_url(audio_url), oss.object_key_from_url(video_url)]
            est_sec = max(15.0, (a["duration"] or 5.0) * 3 + 15)

            def _on_prog(p):
                if p is None:
                    return
                # EAS 的 progress 只有 20/80 两档锚点（特征提取完成=20、视频处理完成=80），
                # 中间逐帧推理不报进度。用 ticker 当前值补帧，仅当 EAS 锚点更高时拉升；
                # 关键点：绝不 ticker.stop()，否则被 20% 冻住后进度条永久卡死。
                cur = ticker.current() if ticker else 0
                update(tid, progress=int(max(20, min(80, max(cur, p)))))

            ticker = start_progress_ticker(tid, 20, 80, est_sec)
            out_key = None
            try:
                try:
                    res = hg.generate_talking_video(audio_url, video_url, on_progress=_on_prog)
                except Exception as e:
                    update(tid, status="error", error=f"HeyGem 推理失败: {e}")
                    return
                finally:
                    ticker.stop()
                remote = res.get("video_url")
                if not remote:
                    update(tid, status="error",
                           error="HeyGem 未返回视频地址。请确认 EAS 容器入口 serve_oss.py 已部署（结果回传 OSS）。")
                    return
                try:
                    # remote 为 OSS 签名 URL 或公网直链，拉回本地 storage
                    oss.download_url(remote, out)
                    out_key = oss.object_key_from_url(remote)
                except Exception as e:
                    update(tid, status="error",
                           error=f"视频回传失败: {e}。请确认 EAS 返回的 result 是 OSS 可下载 URL。")
                    return
            finally:
                # 清理 OSS 中转副本：输入音频/形象 + 已下载回本地的输出视频；本地文件不动
                for k in in_keys:
                    oss.delete_object(k)
                if out_key:
                    oss.delete_object(out_key)
            poster_path = avatar_path  # 用上传形象作封面
            conn = db.get_conn()
            cur = conn.cursor()
            cur.execute("INSERT INTO videos(user_id,audio_id,avatar_id,file_path,poster_path,status,created_at) VALUES(?,?,?,?,?,?,?)",
                        (user["id"], audio_id, avatar_id, _rel(out), _rel(poster_path), "done", _now()))
            vid = cur.lastrowid
            conn.commit(); conn.close()
            set_result(tid, {"video_id": vid,
                             "poster_url": "/files/" + _rel(poster_path),
                             "video_url": "/files/" + _rel(out),
                             "provider": "heygem"})
        else:
            # —— 占位分支：本地合成静态形象+配音（无需 GPU，开发/演示用）——
            # 本地 ffmpeg 秒级完成，用短 ticker 平滑推到 80%，避免"卡 20% 直接跳满"。
            ticker = start_progress_ticker(tid, 20, 80, 3.0)
            try:
                res = mu.make_talking_video(avatar_path, audio_path, out)
            finally:
                ticker.stop()
            conn = db.get_conn()
            cur = conn.cursor()
            cur.execute("INSERT INTO videos(user_id,audio_id,avatar_id,file_path,poster_path,status,created_at) VALUES(?,?,?,?,?,?,?)",
                        (user["id"], audio_id, avatar_id, _rel(res.get("video_path")) if res.get("video_path") else "",
                         _rel(res.get("poster_path")), "done", _now()))
            vid = cur.lastrowid
            conn.commit(); conn.close()
            r = {"video_id": vid, "poster_url": "/files/" + _rel(res["poster_path"]), "provider": "mock"}
            if res.get("video_path"):
                r["video_url"] = "/files/" + _rel(res["video_path"])
            if res.get("note"):
                r["note"] = res["note"]
            set_result(tid, r)
    threading.Thread(target=_run, args=(tid, work)).start()
    return {"task_id": tid}


# ============ 剪辑 ============
@api.post("/editing/generate")
def editing_generate(video_id: int = Form(...),
                     color: bool = Form(False), bigtext: bool = Form(False),
                     mg: bool = Form(False), bgm: bool = Form(False), user=Depends(get_user)):
    conn = db.get_conn()
    v = conn.execute("SELECT * FROM videos WHERE id=? AND user_id=?", (video_id, user["id"])).fetchone()
    conn.close()
    if not v:
        raise HTTPException(400, "视频不存在")
    in_path = os.path.join(STORAGE_DIR, v["file_path"]) if v["file_path"] else None
    poster_path = os.path.join(STORAGE_DIR, v["poster_path"]) if v["poster_path"] else None
    # 找配音音频用于无视频降级；顺带取文案文本（用于网感大字幕）
    audio_path = None
    script_text = None
    if v["audio_id"]:
        conn = db.get_conn()
        a = conn.execute("SELECT file_path, script_id FROM audios WHERE id=?", (v["audio_id"],)).fetchone()
        if a:
            audio_path = os.path.join(STORAGE_DIR, a["file_path"])
            if a["script_id"]:
                s = conn.execute("SELECT generated_text FROM scripts WHERE id=?", (a["script_id"],)).fetchone()
                if s and s["generated_text"]:
                    script_text = s["generated_text"]
        conn.close()
    options = {"color": color, "bigtext": bigtext, "mg": mg, "bgm": bgm}
    tid = create_task("editing", user["id"])

    def work():
        update(tid, progress=5, status="running")
        out = os.path.join(STORAGE_DIR, "edits", f"e{user['id']}_{int(time.time()*1000)}.mp4")

        def on_progress(p):
            # edit_video 内部进度 0-100 映射到任务进度 10-85
            update(tid, progress=10 + int(max(0, min(100, p)) * 0.75))

        res = mu.edit_video(in_path, poster_path, options, audio_path, out, script_text,
                            on_progress=on_progress)
        update(tid, progress=90)
        conn = db.get_conn()
        cur = conn.cursor()
        # —— 剪辑去重：重剪同一条视频即覆盖旧版（重剪=对上一版不满意）。
        # 新片生成成功后，清掉该 video_id 下所有旧剪辑成品（成片 + 中途产物），再入库最新一条。
        # 中途产物含 _bgm.wav / _subs.ass / _base.mp4；_frame0.jpg 为封面底图，保留不删。 ——
        if res.get("video_path"):
            _old = conn.execute(
                "SELECT file_path FROM edits WHERE video_id=? AND user_id=? AND status='done'",
                (video_id, user["id"])).fetchall()
            for _o in _old:
                _ofp = os.path.join(STORAGE_DIR, (_o["file_path"] or "").replace("/", os.sep)) \
                    if _o["file_path"] else None
                if not _ofp:
                    continue
                for _suf in ("", "_bgm.wav", "_subs.ass", "_base.mp4"):
                    _p = (_ofp[:-4] + _suf) if _suf else _ofp
                    try:
                        if os.path.exists(_p):
                            os.remove(_p)
                    except Exception:
                        pass
            cur.execute("DELETE FROM edits WHERE video_id=? AND user_id=?", (video_id, user["id"]))
        cur.execute("INSERT INTO edits(user_id,video_id,options,file_path,note,status,created_at) VALUES(?,?,?,?,?,?,?)",
                    (user["id"], video_id, str(options), _rel(res.get("video_path")) if res.get("video_path") else "",
                     res.get("note", ""), "done", _now()))
        eid = cur.lastrowid
        conn.commit(); conn.close()
        r = {"edit_id": eid, "poster_url": "/files/" + _rel(res["poster_path"]), "options": options}
        if in_path:
            r["source_url"] = "/files/" + _rel(in_path)   # 原片，供前端对比
        if res.get("video_path"):
            r["video_url"] = "/files/" + _rel(res["video_path"])
        if res.get("note"):
            r["note"] = res["note"]
        set_result(tid, r)
    threading.Thread(target=_run, args=(tid, work)).start()
    return {"task_id": tid}


@api.get("/videos/{video_id}/edits")
def list_edits(video_id: int, user=Depends(get_user)):
    """返回该视频的剪辑历史（id 倒序），供剪辑页 enter 时回显上次产物。"""
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT e.id,e.file_path,e.options,e.note,e.created_at,v.file_path AS src "
        "FROM edits e LEFT JOIN videos v ON v.id=e.video_id "
        "WHERE e.video_id=? AND e.user_id=? ORDER BY e.id DESC LIMIT 20",
        (video_id, user["id"])).fetchall()
    conn.close()
    out = []
    for r in rows:
        item = {"edit_id": r["id"], "options": r["options"], "note": r["note"], "created_at": r["created_at"]}
        # edits.file_path 入库时已经是 _rel() 后的相对路径，不能再包一层 _rel()，
        # 否则会把 "edits/xxx.mp4" 当成 cwd 相对路径再算一次，得到错误的 "../edits/xxx.mp4"。
        if r["file_path"]:
            item["video_url"] = "/files/" + r["file_path"].replace("\\", "/")
        if r["src"]:
            item["source_url"] = "/files/" + r["src"].replace("\\", "/")
        out.append(item)
    return {"edits": out}


# ============ 封面 ============
COVER_STYLES = ["大字标题型", "对比型", "悬念型", "表情包型"]


@api.get("/covers/styles")
def cover_styles():
    return {"styles": COVER_STYLES}


@api.post("/covers/generate")
def cover_generate(edit_id: int = Form(...), style: str = Form("大字标题型"),
                   title: str = Form(""), subtitle: str = Form(""), user=Depends(get_user)):
    conn = db.get_conn()
    e = conn.execute("SELECT * FROM edits WHERE id=? AND user_id=?", (edit_id, user["id"])).fetchone()
    conn.close()
    if not e:
        raise HTTPException(400, "剪辑结果不存在")
    if not title:
        title = "我的短视频"
    # 推导剪辑成片首帧（命名约定：<视频名>_frame0.jpg），供封面当人物/场景底图。
    # 剪辑时已存好则直接命中；存量视频首次生成时实时截一帧并缓存，之后直接命中。
    frame_path = None
    vp = e["file_path"]
    if vp:
        abs_vp = os.path.join(STORAGE_DIR, vp)
        cand = os.path.splitext(abs_vp)[0] + "_frame0.jpg"
        if os.path.exists(cand):
            frame_path = cand
        elif os.path.exists(abs_vp):
            frame_path = mu._save_frame0(abs_vp)
    out = os.path.join(STORAGE_DIR, "covers", f"c{user['id']}_{int(time.time()*1000)}.jpg")
    # 推导用户海报装饰层：固定目录 storage/covers/templates/，优先 poster.png/jpg，否则取目录首个图片
    poster_path = None
    tdir = os.path.join(STORAGE_DIR, "covers", "templates")
    if os.path.isdir(tdir):
        for fn in ("poster.png", "poster.jpg", "poster.jpeg"):
            c = os.path.join(tdir, fn)
            if os.path.exists(c):
                poster_path = c
                break
        if not poster_path:
            for f in sorted(os.listdir(tdir)):
                if f.lower().endswith((".png", ".jpg", ".jpeg")):
                    poster_path = os.path.join(tdir, f)
                    break
    mu.make_cover(style, title, subtitle, out, frame_path, poster_path)
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO covers(user_id,edit_id,style,title,file_path,status,created_at) VALUES(?,?,?,?,?,?,?)",
                (user["id"], edit_id, style, title, _rel(out), "done", _now()))
    cid = cur.lastrowid
    conn.commit(); conn.close()
    return {"cover_id": cid, "url": "/files/" + _rel(out), "style": style, "title": title}


@api.get("/covers/{cid}")
def cover_get(cid: int, user=Depends(get_user)):
    conn = db.get_conn()
    c = conn.execute("SELECT * FROM covers WHERE id=? AND user_id=?", (cid, user["id"])).fetchone()
    conn.close()
    if not c:
        raise HTTPException(404, "封面不存在")
    return {"cover_id": cid, "url": "/files/" + c["file_path"], "style": c["style"], "title": c["title"]}


# ============ 发布 ============
PLATFORMS = ["抖音", "视频号", "小红书", "快手", "B站"]


@api.get("/publish/platforms")
def platforms():
    return {"platforms": PLATFORMS}


@api.post("/publish")
def publish(cover_id: int = Form(...), platform: str = Form("抖音"), user=Depends(get_user)):
    conn = db.get_conn()
    c = conn.execute("SELECT * FROM covers WHERE id=? AND user_id=?", (cover_id, user["id"])).fetchone()
    conn.close()
    if not c:
        raise HTTPException(400, "封面不存在")
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO publishes(user_id,cover_id,platform,status,created_at) VALUES(?,?,?,?,?)",
                (user["id"], cover_id, platform, "published", _now()))
    pid = cur.lastrowid
    conn.commit(); conn.close()
    return {"publish_id": pid, "platform": platform, "status": "published",
            "note": "示例发布成功（占位）。生产接入各平台开放平台发布 API 后即为真实发布。"}


# ============ 任务轮询 ============
@api.get("/task/{tid}")
def task_status(tid: str):
    t = get_task(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    return t
