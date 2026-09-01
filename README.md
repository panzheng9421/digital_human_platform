# 数字人短视频智能体平台（交付版）

一套完整的「文案 → 配音 → 数字人 → 剪辑 → 封面 → 发布」全自动短视频生产线，带登录鉴权、多用户隔离、任务进度。
面向**买断式本地/云端部署**（非 SaaS 开放注册）：付款发激活码，凭码登录即用。

> ⚠️ 当前为**可运行演示版**：配音 TTS 已接入真实服务（阿里云百炼 CosyVoice v3.5），数字人对口型默认走 HeyGem/duix.avatar（音频驱动，需部署）；
> 文案改写、平台发布按开关可切换占位/真实实现。本地 CPU 即可跑通全流程预览；接真实服务改 `app/config.py`（见「AI 推理替换位」）。

---

## 一、快速启动（Windows）

### 方式 A：直接打开（现在就能用）
平台后端服务**已在运行**，浏览器访问即可：

```
http://localhost:8000
```

### 方式 B：以后重启 / 换机器
项目根目录已提供 `start.bat`，双击即可启动：

```
D:\ai\workbuddy\opc\digital_human_platform\start.bat
```

或手动：

```bat
cd D:\ai\workbuddy\opc\digital_human_platform
venv\Scripts\activate
python main.py
```

启动后控制台打印 `Uvicorn running on http://0.0.0.0:8000` 即成功。浏览器打开 `http://localhost:8000`。
若 8000 端口被占用（例如旧进程未关），先结束占用进程，或改 `main.py` 末尾 `port=8000` 为其他值。

---

## 二、默认账号与激活码

**演示账号（醒来即可登录）：**

| 用户名 | 密码 | 套餐 |
|---|---|---|
| `laopan` | `laopan123` | 买断，月额度 500 条 |

**种子激活码（注册新用户时用，见 `app/db.py`）：**

```
LAOPAN2026   BUYOUT2999   BUYOUT4399   TESTFREE
```

注册入口在登录页「注册」：填用户名 + 密码 + 激活码，即创建新买断账号（月额度 500、并发 2 路）。
生产环境请删除 `TESTFREE` 等公开码，改为后台批量生成一次性激活码。

---

## 三、功能链路（对应你的需求）

登录后首页两个入口：

**入口一 · 行业爆款改写**
1. 输入行业 → 系统匹配该行业爆款文案库（见 `app/data/viral_scripts.py`，已内置餐饮/房产/教育/美妆/穿搭/健身/数码/本地生活）
2. 选一篇 → 进入改写页
3. 选**写法类型**（5 种）：解题型 / 推荐型 / 揭秘型 / 案例型 / 疑问型
4. 选**人设**：老板 / 专家（角色化改写模板）
5. 点「开始生成」→ 弹框预览改写文案
6. 「违禁词检查」按钮（真实词库，见 `app/data/sensitive_words.py`）→ 标红命中词
7. 满意 → 「前往配音」；不满意 → 「重新生成」

**入口二 · 链接提取改写**
- 粘贴视频链接 → 提取文案（当前为占位，可手动粘贴真实文案）→ 后续流程同入口一

**配音页**
- 上传音色（或默认合成音）→ 选「我的音色」
- 情绪（7 种）：自然 / 嫌弃 / 高兴 / 伤心 / 说教 / 激动 / 生气
- 语速滑块 → 点生成 → 全屏遮罩进度条
- 完成可试听、重新配音、下载配音

**数字人页**
- 上传形象（照片）→ 生成预览（静态形象 + 配音）
- 选形象 → 「生成口播视频」→ 进度条 → 展示视频（有 GPU/ffmpeg 时为真 MP4，否则降级为静态形象+配音预览）

**剪辑页（自动）**
- 4 个开关：① 自动调色 ② 网感大字 ③ MG 动画 ④ 背景音乐
- 点「开始剪辑」→ 智能剪辑进度条 → 出片

**封面页**
- 预制 4 种风格：大字标题型 / 对比型 / 悬念型 / 表情包型
- 选风格 → 满意 → 「前往发布」

**发布页**
- 平台：抖音 / 视频号 / 小红书 / 快手 / B站
- 当前为占位发布（写库标记成功）；接各平台开放平台 API 后为真实发布

---

## 四、目录结构

```
digital_human_platform/
├── main.py                 # 入口，组装 FastAPI，挂载静态资源与 /files
├── start.bat              # Windows 一键启动
├── app.db                 # SQLite 数据库（首次启动自动建表+种子）
├── app/
│   ├── config.py          # 全局配置 + AI 推理替换位（接真实服务的开关/密钥）
│   ├── db.py              # SQLite 数据访问 + 种子账号/激活码
│   ├── auth.py            # JWT 鉴权（注册需激活码，不开放注册）
│   ├── task_manager.py    # 任务进度管理（0-100，前端轮询）
│   ├── routers.py         # 全部 API 路由（全链路串联）
│   ├── data/
│   │   ├── viral_scripts.py   # 行业爆款文案库
│   │   └── sensitive_words.py  # 违禁词库
│   └── services/
│       ├── script_service.py   # 改写/链接提取
│       ├── cosyvoice_client.py # 阿里云百炼 CosyVoice v3.5 配音 / 声音复刻（真实 TTS）
│       ├── heygem_client.py    # HeyGem / duix.avatar 对口型客户端（PAI-EAS + OSS 中转）
│       ├── asr_client.py       # 抖音链接提取 + 文案 ASR（Paraformer-v2）
│       ├── oss_client.py       # OSS 文件中转（本地平台 <-> 云端 EAS）
│       ├── classify.py         # 行业分类（餐饮/房产/…/二手车）
│       └── media_utils.py      # 占位降级：静态图+音频预览 / ffmpeg 合成
├── static/
│   ├── index.html         # SPA 外壳
│   ├── css/styles.css
│   └── js/{api.js, app.js}  # 前端逻辑（路由、双入口、全流程页面）
└── storage/               # 生成的媒体文件（audios/avatars/videos/edits/covers/timbre/temp）
```

---

## 五、AI 推理替换位（生产化关键）

所有占位实现都集中在 `app/config.py` 用环境变量开关，接真实服务改这里即可，无需动前端：

| 环节 | 当前 | 生产替换 | 开关/变量 |
|---|---|---|---|
| 文案改写 | 模板拼接 | DeepSeek / GPT / 通义千问 | `LLM_API_KEY` `LLM_BASE_URL` `LLM_MODEL` |
| 配音 TTS | 占位 WAV | 阿里云百炼 CosyVoice v3.5（plus / flash，已接入真实服务） | `DASHSCOPE_API_KEY` + `DASHSCOPE_TTS_MODEL` |
| 数字人 | 静态图+音频 | HeyGem / duix.avatar（PAI-EAS，音频驱动对口型） | `AVATAR_PROVIDER=heygem` + `HEYGEM_ENDPOINT` |
| 发布 | 占位写库 | 抖音/视频号开放平台 | `PUBLISH_ENABLED=1` |

接真实服务时，修改 `app/services/script_service.py`（改写）、`app/services/media_utils.py`（配音/数字人/剪辑）、`app/routers.py`（发布）对应函数，把占位逻辑换成 HTTP 调用即可。

---

## 六、生产化 checklist（上线卖给客户前必须做）

1. **改密钥**：`DH_SECRET_KEY` 设环境变量，别用默认值；激活码换成后台生成的一次性码。
2. **HTTPS**：只开 443，用 Caddy/Nginx 反代 + 免费证书；别裸 http 暴露。
3. **Redis 队列**：当前用内存线程做任务。多用户并发时需上 Redis（你之前设计的「全局 3 路 + 每用户 2 路 + 月额度拒绝」），防单人薅爆 GPU。
4. **GPU 上云**：本地 CPU 只能预览。量产把服务部署到阿里云 T4（ecs.gn6i），装 nvidia-container-toolkit，用 HeyGem 的 `docker-compose.yml` 起推理容器，前端 `DH_API_URL` 指向它。
5. **多用户隔离**：数据库所有查询已带 `user_id`，租户数据天然隔离；确认无误即可。
6. **合规**：用户协议写明禁止上传他人肖像/声音；生成内容建议加 AI 标识；数字人声音参考《民法典》肖像/声音权（可识别性标准）。
7. **安全组**：云服务器只放行 443 + SSH（改非 22），其余全关；服务别用 root 跑。

---

## 七、已知限制

- 文案改写、配音、数字人、发布均为**占位演示**，效果需接真实 API 才达到商用水平。
- 无 ffmpeg 环境时，数字人/剪辑视频降级为「静态形象图 + 配音音频」预览（已装 `imageio-ffmpeg`，多数环境可直接出 MP4）。
- 移动端布局未专项优化。
- 任务状态存内存（`task_manager.py`），进程重启后进行中的任务进度丢失（已完成的结果已落库不受影响）。
- **数字人底座（HeyGem / duix.avatar）为音频驱动对口型**：仅驱动面部口型与头部微动，**不会根据文案语义自动生成肢体动作**。参考视频里的动作按原时序播放，与配音内容不一定对齐（即「动作和配音对不上」）。若需动作与口播同步，要么选「不出动作、只做口播头部」的干净形象，要么后续加「动作时间轴手动对齐」功能。
