// ===== 全局状态（跨页面传递：文案/音频/视频/剪辑/封面 id） =====
const STATE = { script_id: null, audio_id: null, video_id: null, edit_id: null, cover_id: null };

// ===== 工具 =====
function qp() { // 解析 hash 查询参数
  const h = location.hash.split("?")[1] || "";
  return Object.fromEntries(new URLSearchParams(h));
}
function go(hash) { location.hash = hash; }
function esc(s) { return (s || "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

function toast(msg) {
  const t = document.createElement("div");
  t.className = "toast"; t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2600);
}
function showMask(text) {
  document.getElementById("maskText").textContent = text || "正在生成...";
  document.getElementById("maskBar").style.width = "0%";
  document.getElementById("maskNum").textContent = "0%";
  document.getElementById("mask").classList.remove("hidden");
}
function updateMask(p) {
  document.getElementById("maskBar").style.width = p + "%";
  document.getElementById("maskNum").textContent = p + "%";
}
function hideMask() { document.getElementById("mask").classList.add("hidden"); }

function modal(title, bodyHtml, footHtml) {
  document.getElementById("modalTitle").textContent = title;
  document.getElementById("modalBody").innerHTML = bodyHtml;
  document.getElementById("modalFoot").innerHTML = footHtml || "";
  document.getElementById("modal").classList.remove("hidden");
}
function closeModal() { document.getElementById("modal").classList.add("hidden"); }
document.getElementById("modalClose").onclick = closeModal;

// 带进度的生成：提交 -> 轮询 -> 返回 result
async function genWithProgress(path, formObj, maskText) {
  showMask(maskText);
  try {
    const r = await API.postForm(path, formObj);
    const result = await API.pollTask(r.task_id, (p) => updateMask(p));
    hideMask();
    return result;
  } catch (e) {
    hideMask();
    toast("生成失败：" + e.message);
    throw e;
  }
}

function topbar(extra) {
  const u = API.token ? "已登录" : "";
  return `<div class="topbar"><div class="brand">数字人短视频智能体</div>
    <div class="user">${extra || ""}<button class="btn ghost sm" id="logout">退出</button></div></div>`;
}
function bindLogout() {
  const b = document.getElementById("logout");
  if (b) b.onclick = () => { API.token = null; go("#/login"); };
}

const STEP_TITLES = ["文案", "配音", "数字人", "剪辑", "封面", "发布"];
function stepsBar(cur) {
  return `<div class="steps">` + STEP_TITLES.map((s, i) =>
    `<span class="step ${i === cur ? "on" : (i < cur ? "done" : "")}">${i + 1}.${s}</span>`).join("") + `</div>`;
}

// ===== 路由 =====
const routes = {
  "#/login": pageLogin,
  "#/dashboard": pageDashboard,
  "#/industry": pageIndustry,
  "#/rewrite": pageRewrite,
  "#/extract": pageExtract,
  "#/dubbing": pageDubbing,
  "#/digital": pageDigital,
  "#/editing": pageEditing,
  "#/cover": pageCover,
  "#/publish": pagePublish,
};

async function render() {
  const hash = location.hash || "#/login";
  if ((hash === "#/login") === false && !API.token && hash !== "#/login") {
    // 未登录跳登录（除登录页外）
    if (hash !== "#/login") { go("#/login"); return; }
  }
  const page = routes[hash.split("?")[0]] || pageLogin;
  const app = document.getElementById("app");
  try {
    await page(app);
  } catch (e) {
    app.innerHTML = `<div class="wrap"><div class="card"><h2>出错了</h2><div class="muted">${esc(e.message)}</div></div></div>`;
  }
}
window.addEventListener("hashchange", render);

// ===== 登录 / 注册 =====
function pageLogin(app) {
  STATE.script_id = STATE.audio_id = STATE.video_id = STATE.edit_id = STATE.cover_id = null;
  app.innerHTML = `
  <div class="auth">
    <h1>数字人短视频智能体</h1>
    <div class="tip">买断版 · 凭激活码开通账号</div>
    <div class="tabs">
      <div class="tab on" id="tabLogin">登录</div>
      <div class="tab" id="tabReg">注册(激活码)</div>
    </div>
    <div id="formLogin">
      <label>用户名</label><input id="lUser" />
      <label>密码</label><input id="lPw" type="password" />
      <button class="btn" style="width:100%;margin-top:18px" id="btnLogin">登录</button>
    </div>
    <div id="formReg" class="hidden">
      <label>用户名</label><input id="rUser" />
      <label>密码</label><input id="rPw" type="password" />
      <label>激活码</label><input id="rCode" placeholder="如 LAOPAN2026" />
      <button class="btn" style="width:100%;margin-top:18px" id="btnReg">注册并开通</button>
    </div>
  </div>`;
  document.getElementById("tabLogin").onclick = () => {
    document.getElementById("tabLogin").classList.add("on");
    document.getElementById("tabReg").classList.remove("on");
    document.getElementById("formLogin").classList.remove("hidden");
    document.getElementById("formReg").classList.add("hidden");
  };
  document.getElementById("tabReg").onclick = () => {
    document.getElementById("tabReg").classList.add("on");
    document.getElementById("tabLogin").classList.remove("on");
    document.getElementById("formReg").classList.remove("hidden");
    document.getElementById("formLogin").classList.add("hidden");
  };
  document.getElementById("btnLogin").onclick = async () => {
    try {
      const r = await API.postForm("/auth/login", { username: lUser.value, password: lPw.value });
      API.token = r.token; go("#/dashboard");
    } catch (e) { toast(e.message); }
  };
  document.getElementById("btnReg").onclick = async () => {
    try {
      const r = await API.postForm("/auth/register", { username: rUser.value, password: rPw.value, code: rCode.value });
      API.token = r.token; toast("注册成功"); go("#/dashboard");
    } catch (e) { toast(e.message); }
  };
}

// ===== 仪表盘（双入口）=====
function pageDashboard(app) {
  app.innerHTML = topbar() + `<div class="wrap">
    <div class="card"><h2>工作台</h2><div class="sub">选择一种方式开始制作你的数字人短视频</div>
      <div class="entry-grid">
        <div class="entry" id="eIndustry"><div><div class="ico">🔥</div><h3>行业爆款改写</h3><p>输入你的行业，自动筛选该行业高热度口播文案，挑一篇一键改写。</p></div><div class="muted">入口一 →</div></div>
        <div class="entry" id="eLink"><div><div class="ico">🔗</div><h3>链接提取改写</h3><p>粘贴一条爆款视频链接，自动提取文案，直接进入改写流程。</p></div><div class="muted">入口二 →</div></div>
      </div>
    </div>
  </div>`;
  bindLogout();
  document.getElementById("eIndustry").onclick = () => go("#/industry");
  document.getElementById("eLink").onclick = () => go("#/extract");
}

// ===== 入口一：行业爆款 =====
async function pageIndustry(app) {
  app.innerHTML = topbar() + `<div class="wrap">
    ${stepsBar(0)}
    <div class="card"><h2>行业爆款文案筛选</h2><div class="sub">输入你的行业，系统匹配该赛道高热度口播文案</div>
      <label>你的行业</label>
      <input id="indInput" placeholder="如：餐饮、房产、教育、美妆、穿搭、健身、数码、本地生活" />
      <button class="btn" style="margin-top:14px" id="btnFilter">筛选爆款</button>
      <div id="indHint" class="muted" style="margin-top:10px"></div>
      <div id="indList" style="margin-top:16px"></div>
    </div>
  </div>`;
  bindLogout();
  const avail = ["餐饮美食", "房产中介", "教育培训", "美妆护肤", "服装穿搭", "健身减肥", "数码家电", "本地生活"];
  document.getElementById("indHint").textContent = "支持行业：" + avail.join("、");
  document.getElementById("btnFilter").onclick = async () => {
    const v = indInput.value.trim();
    if (!v) { toast("请输入行业"); return; }
    try {
      const r = await API.postForm("/scripts/industry", { industry: v });
      const list = document.getElementById("indList");
      if (!r.matched) {
        list.innerHTML = `<div class="muted">未命中专属库，可选行业：${r.available.join("、")}</div>` +
          r.items.map((it, i) => `<div class="item" data-i="${i}"><div class="t">${esc(it.title)}</div><div class="c">${esc(it.content)}</div></div>`).join("");
      } else {
        list.innerHTML = `<div class="muted" style="margin-bottom:10px">已匹配行业：<b style="color:var(--brand)">${esc(r.industry)}</b></div>` +
          r.items.map((it, i) => `<div class="item" data-i="${i}"><div class="t">${esc(it.title)}</div><div class="c">${esc(it.content)}</div></div>`).join("");
      }
      list.querySelectorAll(".item").forEach(el => el.onclick = async () => {
        const it = r.items[+el.dataset.i];
        const sv = await API.postForm("/scripts/save", { source: "industry", industry: r.industry || v, original_text: it.content });
        STATE.script_id = sv.script_id;
        go("#/rewrite?sid=" + sv.script_id);
      });
    } catch (e) { toast(e.message); }
  };
}

// ===== 入口二：链接提取 =====
async function pageExtract(app) {
  app.innerHTML = topbar() + `<div class="wrap">
    ${stepsBar(0)}
    <div class="card"><h2>链接提取文案</h2><div class="sub">粘贴爆款视频链接，自动提取口播文案（百炼 Paraformer 转写）</div>
      <label>视频链接</label><input id="linkInput" placeholder="https://..." />
      <button class="btn" style="margin-top:14px" id="btnExtract">提取文案</button>
      <div class="muted" style="margin-top:12px">链接下载失败时，可直接上传视频/音频文件提取：</div>
      <input type="file" id="fileInput" accept="video/*,audio/*" />
      <button class="btn" style="margin-top:10px" id="btnExtractFile">上传文件提取</button>
      <div class="muted" style="margin-top:12px">若无链接/文件，可直接在下方粘贴文案</div>
      <label>文案内容（可编辑）</label><textarea id="extText" placeholder="在此粘贴或编辑视频口播文案..."></textarea>
      <button class="btn" style="margin-top:14px" id="btnNext">保存并去改写</button>
    </div>
  </div>`;
  bindLogout();
  const runExtract = async (payload, isFile) => {
    const btn = isFile ? document.getElementById("btnExtractFile") : document.getElementById("btnExtract");
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = "提取中...";
    try {
      const r = isFile
        ? await API.postForm("/extract/file", payload)
        : await API.postForm("/extract", payload);
      extText.value = r.original_text || "";
      if (r.note) toast(r.note);
      else if (!r.original_text) toast("未提取到文案，可换文件或直接粘贴");
    } catch (e) { toast(e.message); }
    finally { btn.disabled = false; btn.textContent = old; }
  };
  document.getElementById("btnExtract").onclick = async () => {
    const url = linkInput.value.trim();
    if (!url) { toast("请输入链接"); return; }
    await runExtract({ url }, false);
  };
  document.getElementById("btnExtractFile").onclick = async () => {
    const f = document.getElementById("fileInput").files[0];
    if (!f) { toast("请选择视频/音频文件"); return; }
    await runExtract({ file: f }, true);
  };
  document.getElementById("btnNext").onclick = async () => {
    const txt = extText.value.trim();
    if (!txt) { toast("请先提取或粘贴文案"); return; }
    const sv = await API.postForm("/scripts/save", { source: "link", original_text: txt });
    STATE.script_id = sv.script_id;
    go("#/rewrite?sid=" + sv.script_id);
  };
}

// ===== 改写页 =====
const TYPES = ["解题型", "推荐型", "揭秘型", "案例型", "疑问型"];
const PERSONAS = ["老板", "专家", "邻家大哥", "毒舌朋友"];
let _rewriteState = { type: "解题型", persona: "老板", generated: "" };

async function pageRewrite(app) {
  const { sid } = qp();
  STATE.script_id = sid;
  let script = {};
  try { script = await API.get("/scripts/" + sid); } catch (e) {}
  app.innerHTML = topbar() + `<div class="wrap">
    ${stepsBar(0)}
    <div class="card"><h2>文案改写</h2><div class="sub">选择写法类型与人设，一键生成专属口播文案</div>
      <label>原始文案</label>
      <div class="preview-box">${esc(script.original_text || "")}</div>
      <label>写法类型</label>
      <div class="chips" id="typeChips">${TYPES.map(t => `<div class="chip ${t === _rewriteState.type ? "on" : ""}" data-t="${t}">${t}</div>`).join("")}</div>
      <label>人设</label>
      <div class="chips" id="personaChips">${PERSONAS.map(p => `<div class="chip ${p === _rewriteState.persona ? "on" : ""}" data-p="${p}">${p}</div>`).join("")}</div>
      <div class="row" style="margin-top:18px">
        <button class="btn" id="btnGen">开始生成</button>
        <button class="btn ghost" id="btnBack">返回</button>
      </div>
    </div>
  </div>`;
  bindLogout();
  document.getElementById("typeChips").querySelectorAll(".chip").forEach(c => c.onclick = () => {
    _rewriteState.type = c.dataset.t;
    document.getElementById("typeChips").querySelectorAll(".chip").forEach(x => x.classList.remove("on"));
    c.classList.add("on");
  });
  document.getElementById("personaChips").querySelectorAll(".chip").forEach(c => c.onclick = () => {
    _rewriteState.persona = c.dataset.p;
    document.getElementById("personaChips").querySelectorAll(".chip").forEach(x => x.classList.remove("on"));
    c.classList.add("on");
  });
  document.getElementById("btnBack").onclick = () => go("#/dashboard");
  document.getElementById("btnGen").onclick = () => doRewrite(sid);
}

async function doRewrite(sid) {
  try {
    const r = await API.postForm("/scripts/rewrite", { script_id: sid, type_: _rewriteState.type, persona: _rewriteState.persona });
    _rewriteState.generated = r.generated_text;
    showPreview(r.generated_text, sid);
  } catch (e) { toast(e.message); }
}

function highlightHits(text, hits) {
  let html = esc(text);
  (hits || []).forEach(h => { html = html.split(esc(h.word)).join(`<span class="hit">${esc(h.word)}</span>`); });
  return html;
}

function showPreview(text, sid) {
  const body = `<div class="preview-box" id="prevBox">${esc(text)}</div>`;
  const foot = `
    <button class="btn ghost" id="mCheck">违禁词检查</button>
    <button class="btn ghost" id="mRegen">不满意 重新生成</button>
    <button class="btn" id="mOk">满意 前往配音</button>`;
  modal("改写预览", body, foot);
  document.getElementById("mRegen").onclick = () => { closeModal(); doRewrite(sid); };
  document.getElementById("mOk").onclick = () => { closeModal(); go("#/dubbing?sid=" + sid); };
  document.getElementById("mCheck").onclick = async () => {
    try {
      const r = await API.postForm("/scripts/check", { text });
      const box = document.getElementById("prevBox");
      box.innerHTML = highlightHits(text, r.hits.map(h => h.word));
      if (r.safe) toast("✅ 未检出违禁词");
      else toast(`检出 ${r.count} 个风险词，已高亮`);
    } catch (e) { toast(e.message); }
  };
}

// ===== 配音页 =====
let _dubState = { timbre_id: 0, emotion: "自然", speed: 1.0 };
const EMOTIONS = ["自然", "嫌弃", "高兴", "伤心", "说教", "激动", "生气"];

async function pageDubbing(app) {
  const { sid } = qp();
  STATE.script_id = sid;
  app.innerHTML = topbar() + `<div class="wrap">
    ${stepsBar(1)}
    <div class="card"><h2>配音</h2><div class="sub">上传你的音色，选择情绪与语速，生成专属配音</div>
      <label>上传音色（音频文件）</label>
      <input type="file" id="timbreFile" accept=".wav,.mp3,.opus,.aac,.flac,.pcm" />
      <button class="btn sm" style="margin-top:8px" id="btnUpTimbre">上传音色</button>
      <div id="timbreList" class="chips" style="margin-top:12px"></div>

      <label>情绪调节</label>
      <div class="chips" id="emoChips">${EMOTIONS.map(e => `<div class="chip ${e === _dubState.emotion ? "on" : ""}" data-e="${e}">${e}</div>`).join("")}</div>

      <label>语速调节：<span id="spVal">1.0</span>x</label>
      <div class="range-row"><input type="range" id="speed" min="0.5" max="2" step="0.1" value="1.0" /><span class="muted">慢 ←→ 快</span></div>

      <button class="btn" style="margin-top:18px" id="btnGenDub">生成配音</button>
      <div id="dubResult" style="margin-top:16px"></div>
    </div>
  </div>`;
  bindLogout();
  document.getElementById("emoChips").querySelectorAll(".chip").forEach(c => c.onclick = () => {
    _dubState.emotion = c.dataset.e;
    document.getElementById("emoChips").querySelectorAll(".chip").forEach(x => x.classList.remove("on"));
    c.classList.add("on");
  });
  const sp = document.getElementById("speed");
  sp.oninput = () => { _dubState.speed = parseFloat(sp.value); spVal.textContent = sp.value; };
  document.getElementById("btnUpTimbre").onclick = async () => {
    const f = timbreFile.files[0];
    if (!f) { toast("请选择音频文件"); return; }
    const _allowed = ["wav", "mp3", "opus", "aac", "flac", "pcm"];
    const _fe = (f.name.split(".").pop() || "").toLowerCase();
    if (!_allowed.includes(_fe)) { toast("格式不支持，只能选 wav / mp3 / opus / aac / flac / pcm"); return; }
    const r = await API.postForm("/timbres/upload", { name: f.name, file: f });
    _dubState.timbre_id = r.timbre_id;
    toast("音色已上传"); loadTimbres();
  };
  document.getElementById("btnGenDub").onclick = async () => {
    try {
      const r = await genWithProgress("/dubbing/generate",
        { script_id: sid, timbre_id: _dubState.timbre_id, emotion: _dubState.emotion, speed: _dubState.speed },
        "正在生成配音...");
      STATE.audio_id = r.audio_id;
      renderDubResult(r);
    } catch (e) {}
  };
  loadTimbres();

  async function loadTimbres() {
    try {
      const list = await API.get("/timbres");
      const box = document.getElementById("timbreList");
      if (!list.length) { box.innerHTML = `<span class="muted">暂无音色，请先上传</span>`; return; }
      box.innerHTML = list.map(t => `<div class="chip ${t.id === _dubState.timbre_id ? "on" : ""}" data-id="${t.id}">${esc(t.name)}</div>`).join("");
      box.querySelectorAll(".chip").forEach(c => c.onclick = () => {
        _dubState.timbre_id = +c.dataset.id;
        box.querySelectorAll(".chip").forEach(x => x.classList.remove("on"));
        c.classList.add("on");
      });
    } catch (e) {}
  }
  function renderDubResult(r) {
    document.getElementById("dubResult").innerHTML = `
      <div class="card" style="margin:0"><h2 style="font-size:16px">配音完成</h2>
        <audio class="media" controls src="${r.url}"></audio>
        <div class="row">
          <button class="btn ghost sm" id="btnRedub">重新配音</button>
          <a class="btn ghost sm" href="${r.url}" download>下载配音</a>
          <button class="btn sm" id="btnToDH">满意 前往数字人</button>
        </div>
      </div>`;
    document.getElementById("btnRedub").onclick = document.getElementById("btnGenDub").onclick;
    document.getElementById("btnToDH").onclick = () => go(`#/digital?sid=${sid}&audio=${r.audio_id}`);
  }
}

// ===== 数字人页 =====
let _dhState = { avatar_id: 0 };
async function pageDigital(app) {
  const { sid, audio } = qp();
  STATE.script_id = sid; STATE.audio_id = audio;
  app.innerHTML = topbar() + `<div class="wrap">
    ${stepsBar(2)}
    <div class="card"><h2>数字人</h2><div class="sub">上传你的形象，生成口播视频</div>
      <label>上传人物形象（驱动视频，正脸效果最佳）</label>
      <input type="file" id="avatarFile" accept="video/*" />
      <button class="btn sm" style="margin-top:8px" id="btnUpAvatar">上传形象</button>
      <div id="avatarList" class="row" style="margin-top:12px"></div>
      <button class="btn" style="margin-top:18px" id="btnGenDH" disabled>生成口播视频</button>
      <div id="dhResult" style="margin-top:16px"></div>
    </div>
  </div>`;
  bindLogout();
  document.getElementById("btnUpAvatar").onclick = async () => {
    const f = avatarFile.files[0];
    if (!f) { toast("请选择驱动视频"); return; }
    const r = await API.postForm("/avatars/upload", { name: f.name, file: f });
    _dhState.avatar_id = r.avatar_id;
    toast("形象已上传"); loadAvatars();
  };
  document.getElementById("btnGenDH").onclick = async () => {
    if (!_dhState.avatar_id) { toast("请先上传并选择形象"); return; }
    try {
      const r = await genWithProgress("/digital_human/generate",
        { audio_id: audio, avatar_id: _dhState.avatar_id }, "正在生成数字人视频...");
      STATE.video_id = r.video_id;
      renderDH(r);
    } catch (e) {}
  };
  loadAvatars();

  async function loadAvatars() {
    try {
      const list = await API.get("/avatars");
      const box = document.getElementById("avatarList");
      if (!list.length) { box.innerHTML = `<span class="muted">暂无形象，请先上传</span>`; return; }
      box.innerHTML = list.map(a => `<div style="width:120px" class="${a.id === _dhState.avatar_id ? "" : ""}">
        <video class="media" style="max-width:120px" src="${a.url}" data-id="${a.id}" muted></video></div>`).join("");
      box.querySelectorAll("img").forEach(im => im.onclick = () => {
        _dhState.avatar_id = +im.dataset.id;
        box.querySelectorAll("img").forEach(x => x.style.border = "none");
        im.style.border = "3px solid var(--brand)";
        document.getElementById("btnGenDH").disabled = false;
      });
    } catch (e) {}
  }
  function renderDH(r) {
    const media = r.video_url
      ? `<video class="media" controls src="${r.video_url}"></video>`
      : `<img class="media" src="${r.poster_url}" /><div class="muted">（本环境无 ffmpeg，以静态形象+配音预览；上云可生成真实视频）</div>`;
    document.getElementById("dhResult").innerHTML = `
      <div class="card" style="margin:0"><h2 style="font-size:16px">口播视频生成完成</h2>
        ${media}
        <button class="btn sm" id="btnToEdit">满意 去剪辑</button>
      </div>`;
    document.getElementById("btnToEdit").onclick = () => go(`#/editing?vid=${r.video_id}`);
  }
}

// ===== 剪辑页 =====
async function pageEditing(app) {
  const { vid } = qp();
  STATE.video_id = vid;
  const opts = { color: true, bigtext: true, mg: true, bgm: true };
  app.innerHTML = topbar() + `<div class="wrap">
    ${stepsBar(3)}
    <div class="card"><h2>自动剪辑</h2><div class="sub">一键应用以下智能剪辑效果</div>
      <div id="optList">
        ${[["color", "自动调色"], ["bigtext", "网感大字"], ["mg", "MG动画"], ["bgm", "背景音乐"]].map(([k, label]) =>
          `<label style="display:flex;align-items:center;gap:10px;color:var(--text);font-size:15px">
            <input type="checkbox" id="op_${k}" ${opts[k] ? "checked" : ""} style="width:auto" /> ${label}</label>`).join("")}
      </div>
      <button class="btn" style="margin-top:18px" id="btnEdit">开始剪辑</button>
      <div id="editResult" style="margin-top:16px"></div>
    </div>
  </div>`;
  bindLogout();
  document.getElementById("btnEdit").onclick = async () => {
    const o = {
      color: op_color.checked, bigtext: op_bigtext.checked,
      mg: op_mg.checked, bgm: op_bgm.checked
    };
    try {
      const r = await genWithProgress("/editing/generate",
        { video_id: vid, color: o.color, bigtext: o.bigtext, mg: o.mg, bgm: o.bgm }, "正在智能剪辑...");
      STATE.edit_id = r.edit_id;
      const media = r.video_url
        ? `<video class="media" controls src="${r.video_url}"></video>`
        : `<img class="media" src="${r.poster_url}" /><div class="muted">（无 ffmpeg，以静态预览；上云生成真实剪辑视频）</div>`;
      document.getElementById("editResult").innerHTML = `
        <div class="card" style="margin:0"><h2 style="font-size:16px">剪辑完成</h2>${media}
          <button class="btn sm" id="btnToCover">前往生成标题封面</button></div>`;
      document.getElementById("btnToCover").onclick = () => go(`#/cover?eid=${r.edit_id}`);
    } catch (e) {}
  };
}

// ===== 封面页 =====
async function pageCover(app) {
  const { eid } = qp();
  STATE.edit_id = eid;
  let styles = ["大字标题型", "对比型", "悬念型", "表情包型"];
  let title = "";
  try { styles = (await API.get("/covers/styles")).styles; } catch (e) {}
  // 尝试取文案标题
  if (STATE.script_id) { try { const s = await API.get("/scripts/" + STATE.script_id); title = s.title || ""; } catch (e) {} }
  let sel = styles[0];
  app.innerHTML = topbar() + `<div class="wrap">
    ${stepsBar(4)}
    <div class="card"><h2>标题封面</h2><div class="sub">选择封面风格，一键生成</div>
      <label>封面风格</label>
      <div class="chips" id="styleChips">${styles.map(s => `<div class="chip ${s === sel ? "on" : ""}" data-s="${s}">${s}</div>`).join("")}</div>
      <label>封面标题</label><input id="coverTitle" value="${esc(title)}" />
      <label>副标题（可选）</label><input id="coverSub" placeholder="如：老板亲测" />
      <button class="btn" style="margin-top:18px" id="btnCover">生成封面</button>
      <div id="coverResult" style="margin-top:16px"></div>
    </div>
  </div>`;
  bindLogout();
  document.getElementById("styleChips").querySelectorAll(".chip").forEach(c => c.onclick = () => {
    sel = c.dataset.s;
    document.getElementById("styleChips").querySelectorAll(".chip").forEach(x => x.classList.remove("on"));
    c.classList.add("on");
  });
  document.getElementById("btnCover").onclick = async () => {
    try {
      const r = await API.postForm("/covers/generate",
        { edit_id: eid, style: sel, title: coverTitle.value, subtitle: coverSub.value });
      STATE.cover_id = r.cover_id;
      document.getElementById("coverResult").innerHTML = `
        <div class="card" style="margin:0"><h2 style="font-size:16px">封面生成完成</h2>
          <img class="media" style="max-width:240px" src="${r.url}" />
          <button class="btn sm" id="btnToPub">满意 前往发布</button></div>`;
      document.getElementById("btnToPub").onclick = () => go(`#/publish?cid=${r.cover_id}`);
    } catch (e) { toast(e.message); }
  };
}

// ===== 发布页 =====
async function pagePublish(app) {
  const { cid } = qp();
  STATE.cover_id = cid;
  let platforms = ["抖音", "视频号", "小红书", "快手", "B站"];
  try { platforms = (await API.get("/publish/platforms")).platforms; } catch (e) {}
  let sel = platforms[0];
  let coverUrl = "";
  try { const c = await API.get("/covers/" + cid).catch(() => null); } catch (e) {}
  // 取封面 url：通过 publish 前先拿不到，改为前端用已知 STATE？简单用 cover id 调接口拿不到 url，这里用占位
  app.innerHTML = topbar() + `<div class="wrap">
    ${stepsBar(5)}
    <div class="card"><h2>发布</h2><div class="sub">选择发布平台，一键发布</div>
      <label>发布平台</label>
      <div class="chips" id="pfChips">${platforms.map(p => `<div class="chip ${p === sel ? "on" : ""}" data-p="${p}">${p}</div>`).join("")}</div>
      <div id="coverPrev" class="muted" style="margin-top:12px">封面预览将显示在发布卡片中</div>
      <button class="btn" style="margin-top:18px" id="btnPub">发布</button>
      <div id="pubResult" style="margin-top:16px"></div>
    </div>
  </div>`;
  bindLogout();
  document.getElementById("pfChips").querySelectorAll(".chip").forEach(c => c.onclick = () => {
    sel = c.dataset.p;
    document.getElementById("pfChips").querySelectorAll(".chip").forEach(x => x.classList.remove("on"));
    c.classList.add("on");
  });
  document.getElementById("btnPub").onclick = async () => {
    try {
      const r = await API.postForm("/publish", { cover_id: cid, platform: sel });
      document.getElementById("pubResult").innerHTML = `
        <div class="card" style="margin:0"><h2 style="font-size:16px;color:var(--ok)">✅ 已发布到 ${esc(sel)}</h2>
          <div class="kv"><span>平台</span><span>${esc(r.platform)}</span></div>
          <div class="kv"><span>状态</span><span>${esc(r.status)}</span></div>
          <div class="muted" style="margin-top:10px">${esc(r.note || "")}</div>
          <button class="btn sm" style="margin-top:12px" id="btnHome">返回首页</button></div>`;
      document.getElementById("btnHome").onclick = () => go("#/dashboard");
    } catch (e) { toast(e.message); }
  };
}

// 启动
render();
