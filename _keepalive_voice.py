import sys, os, json, time, sqlite3, re
sys.path.insert(0, os.getcwd())
# key 注入（不回显）：优先环境变量；fallback 扫描 start.bat(start.bat 在本地) / start.sh(ECS 上)
if not os.environ.get("MINIMAX_API_KEY"):
    for fn in ("start.bat", "start.sh"):
        p = os.path.join(os.getcwd(), fn)
        if os.path.exists(p):
            txt = open(p, encoding="utf-8", errors="ignore").read()
            m = re.search(r"MINIMAX_API_KEY\s*[=\s]\s*(\S+)", txt)
            if m:
                os.environ["MINIMAX_API_KEY"] = m.group(1)
                break
os.environ.setdefault("ALLOW_INSECURE_KEY", "1")
from app.services.minimax_client import synthesize, clone_voice, VOICE_FILE

VOICE_ID = "laopan1788700865"
SAMPLE = os.path.join("storage", "timbre", "t2_1788267992.m4a")
DB = "app.db"


def persist(vid):
    with open(VOICE_FILE, "w", encoding="utf-8") as f:
        json.dump({"voice_id": vid, "sample": SAMPLE, "cloned_at": int(time.time())},
                  f, ensure_ascii=False, indent=2)
    c = sqlite3.connect(DB)
    c.execute("UPDATE timbres SET mm_voice_id=? WHERE id=31", (vid,))
    c.commit()
    c.close()
    print(f"[recover] 已落盘 json + 写回 timbres.id=31.mm_voice_id={vid}")


# 1) 先验证云端 voice_id 是否还活（合成极短文本，几乎不烧额度）
try:
    data = synthesize("测试。", voice_id=VOICE_ID, speed=1.0, pitch=0, vol=1.0, emotion=None)
    print(f"[recover] 云端 voice_id={VOICE_ID} 仍有效，合成 {len(data)} 字节 -> 直接复用，不重新克隆")
    persist(VOICE_ID)
except Exception as e:
    print(f"[recover] 云端 voice_id 验证失败（可能已被 7 天规则删除）: {e}")
    # 2) 云端已删 -> 重新克隆（烧克隆费，首次合成时收）
    print(f"[recover] 重新克隆从样本 {SAMPLE}")
    vid = clone_voice(SAMPLE, VOICE_ID)
    persist(vid)
    print(f"[recover] 重新克隆完成 voice_id={vid}")
