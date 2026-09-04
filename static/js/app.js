// ===== 全局状态（跨页面传递：文案/音频/视频/剪辑/封面 id） =====
const STATE = { script_id: null, audio_id: null, video_id: null, edit_id: null, cover_id: null, cover_url: "", cover_title: "", cover_subtitle: "" };

// 刷新浏览器后内存 STATE 会清空，导致点导航跳回的页面（如文案页）丢失上下文、要重选素材。
// 持久化到 localStorage，进入页面即恢复，兜底 beforeunload 保存。
function saveState() {
  try { localStorage.setItem("dh_state", JSON.stringify(STATE)); } catch (e) {}
}
function loadState() {
  try {
    const s = JSON.parse(localStorage.getItem("dh_state") || "{}");
    if (s && typeof s === "object") Object.assign(STATE, s);
  } catch (e) {}
}
loadState();
initTheme();
window.addEventListener("beforeunload", saveState);


// ===== 工具 =====
function qp() { // 解析 hash 查询参数
  const h = location.hash.split("?")[1] || "";
  return Object.fromEntries(new URLSearchParams(h));
}
function go(hash) {
  if (location.hash !== hash) {
    location.hash = hash;
  }
  scrollContentToTop();
}

// 路由切换后把右侧内容区滚回顶部（左侧菜单点的是这个入口）
function scrollContentToTop() {
  const main = document.getElementById("main");
  if (main) main.scrollTop = 0;
  if (typeof window !== "undefined") window.scrollTo(0, 0);
}

// 兜底：浏览器前进后退 / 程序里其他改 hash 的路径也能捕到
window.addEventListener("hashchange", scrollContentToTop);
function esc(s) { return (s || "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

// 自动画中画：产物视频点击播放后直接进入 PiP（PiP 需用户手势，play 事件由点击触发，满足条件）
document.addEventListener("play", function (e) {
  const v = e.target;
  if (v.tagName !== "VIDEO" || !v.classList.contains("auto-pip") || !v.requestPictureInPicture) return;
  v.requestPictureInPicture().catch(() => {});
}, true);

// 根据视频真实宽高给容器打比例标签，便于 UI 自适应
function markVideoAspect(v) {
  if (!v || !v.videoWidth) return;
  const ratio = v.videoWidth / v.videoHeight;
  const asp = ratio < 1 ? "9:16" : (ratio > 1.3 ? "16:9" : "1:1");
  v.dataset.aspect = asp;
  if (ratio < 1) v.classList.add("portrait");
  else v.classList.add("landscape");
  // 同步给父 .dh-h-media 打同一组 aspect 标记，弹窗里用它按 16:9 / 9:16 区分缩略图尺寸
  const media = v.closest && v.closest(".dh-h-media");
  if (media) {
    media.dataset.aspect = asp;
    if (ratio < 1) media.classList.add("portrait");
    else if (ratio > 1.3) media.classList.add("landscape");
    else media.classList.add("square");
  }
}
document.addEventListener("loadedmetadata", function (e) {
  const v = e.target;
  if (v.tagName === "VIDEO") markVideoAspect(v);
}, true);

function toast(msg, type = "") {
  const t = document.createElement("div");
  t.className = "toast" + (type ? " " + type : "");
  t.textContent = msg;
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

const BRAND_LOGO_SVG = `<svg class="brand-logo" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><rect x="4" y="6" width="24" height="20" rx="5" stroke="currentColor" stroke-width="2.4"/><circle cx="13" cy="15" r="3.5" fill="currentColor"/><path d="M7 24c0-3.5 3-6 6-6s6 2.5 6 6" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" fill="none"/><path d="M21 14l6 3-6 3v-6z" fill="currentColor"/></svg>`;

const THEME_TOGGLE_SVG = `<svg class="theme-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>`;
const THEME_TOGGLE_MOON_SVG = `<svg class="theme-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>`;

function topbar(extra) {
  const theme = document.documentElement.getAttribute("data-theme") || "auto";
  const isLight = document.documentElement.classList.contains("theme-light");
  const icon = isLight ? THEME_TOGGLE_MOON_SVG : THEME_TOGGLE_SVG;
  return `<div class="topbar"><div class="brand">${BRAND_LOGO_SVG}数字人短视频智能体</div>
    <div class="user">${extra || ""}<button class="theme-toggle" id="themeToggle" title="切换主题">${icon}</button><button class="btn ghost sm" id="logout">退出</button></div></div>`;
}
function bindLogout() {
  const b = document.getElementById("logout");
  if (b) b.onclick = () => { API.token = null; go("#/login"); };
}

// ===== 主题切换 =====
function applyTheme(mode) {
  const m = mode || "auto";
  const html = document.documentElement;
  const sysLight = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches;
  const light = m === "light" || (m !== "dark" && sysLight);
  // 跟随系统(auto)：移除 data-theme 属性，让 CSS 的 html:not([data-theme]) + 媒体查询接管
  // 否则（light/dark）显式写属性，优先级高于系统媒体查询
  if (m === "auto") html.removeAttribute("data-theme");
  else html.setAttribute("data-theme", m);
  html.classList.toggle("theme-light", light);
  html.classList.toggle("theme-dark", !light);
  try { localStorage.setItem("dh-theme", m); } catch (e) {}
}
function updateThemeIcon() {
  const b = document.getElementById("themeToggle");
  if (!b) return;
  const isLight = document.documentElement.classList.contains("theme-light");
  b.innerHTML = isLight ? THEME_TOGGLE_MOON_SVG : THEME_TOGGLE_SVG;
  b.title = isLight ? "切换深色主题" : "切换浅色主题";
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute("data-theme") || "auto";
  const sysLight = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches;
  const isLight = cur === "light" || (cur !== "dark" && sysLight);
  applyTheme(isLight ? "dark" : "light");
  updateThemeIcon();
}
function bindThemeToggle() {
  const b = document.getElementById("themeToggle");
  if (b) b.onclick = toggleTheme;
}
function initTheme() {
  let saved = "auto";
  try { saved = localStorage.getItem("dh-theme") || "auto"; } catch (e) {}
  applyTheme(saved);
  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
      const cur = document.documentElement.getAttribute("data-theme") || "auto";
      if (cur === "auto") applyTheme("auto");
    });
  }
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
  "#/library": pageLibrary,
};

function currentStepIndex(hash) {
  const base = hash.split("?")[0];
  if (base === "#/dashboard") return -1;
  if (base === "#/library") return -2;
  if (["#/extract", "#/industry", "#/rewrite"].includes(base)) return 0;
  if (["#/dubbing"].includes(base)) return 1;
  if (["#/digital"].includes(base)) return 2;
  if (["#/editing"].includes(base)) return 3;
  if (["#/cover"].includes(base)) return 4;
  if (["#/publish"].includes(base)) return 5;
  return -1;
}

const STEP_MAP = [
  { title: "文案", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20h4L18 10l-4-4L4 16v4z"/><path d="M13.5 6.5l4 4"/></svg>', hash: "#/extract" },
  { title: "配音", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v3"/></svg>', hash: "#/dubbing" },
  { title: "数字人", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/></svg>', hash: "#/digital" },
  { title: "剪辑", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="2.4"/><circle cx="6" cy="18" r="2.4"/><path d="M8 7.5 20 18M8 16.5 20 6"/></svg>', hash: "#/editing" },
  { title: "封面", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9" r="1.5"/><path d="M21 16l-5-5L5 20"/></svg>', hash: "#/cover" },
  { title: "发布", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 3 10 14"/><path d="M21 3 14 21l-4-7-7-4 18-7z"/></svg>', hash: "#/publish" },
];

function sidebar(curIdx) {
  const done = STEP_MAP.filter((_, i) => i < curIdx).length;
  const onHome = curIdx === -1;
  const onLib = curIdx === -2;
  return `<div class="sidebar-head">
      <div class="sidebar-brand">创作工作流</div>
      <div class="sidebar-progress">已完成 ${done} / ${STEP_MAP.length}</div>
    </div>` +
    `<div class="home-item ${onHome ? "on" : ""}" id="navHome">
      <span class="step-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 11 12 4l8 7"/><path d="M6 10v9h12v-9"/></svg></span><span class="step-title">工作台</span>
    </div>` +
    `<div class="home-item ${onLib ? "on" : ""}" id="navLib">
      <span class="step-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 4h4v16H5z"/><path d="M10 4h4v16h-4z"/><path d="M15 5l4 1-3 14-4-1"/></svg></span><span class="step-title">文案库</span>
    </div>` +
    STEP_MAP.map((s, i) => {
      const cls = i === curIdx ? "on" : (i < curIdx ? "done" : "");
      const dot = i < curIdx ? `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" style="width:12px;height:12px;display:block"><polyline points="20 6 9 17 4 12"/></svg>` : (i + 1);
      return `<div class="step-item ${cls}" data-idx="${i}">
        <span class="step-num">${dot}</span>
        <span class="step-icon">${s.icon}</span>
        <span class="step-title">${s.title}</span>
      </div>`;
    }).join("");
}

function bindSidebar() {
  const home = document.getElementById("navHome");
  if (home) home.onclick = () => go("#/dashboard");
  const lib = document.getElementById("navLib");
  if (lib) lib.onclick = () => go("#/library");
  document.querySelectorAll("#sidebar .step-item").forEach(el => {
    el.onclick = () => {
      const idx = +el.dataset.idx;
      const step = STEP_MAP[idx];
      if (!step) return;
      // 点击时尽量带上传参，否则跳到该步骤默认入口
      let h = step.hash;
      if (idx === 0 && STATE.script_id) h = `#/rewrite?sid=${STATE.script_id}`;
      if (idx === 1 && STATE.script_id) h = `#/dubbing?sid=${STATE.script_id}`;
      if (idx === 2 && STATE.script_id) {
        h = `#/digital?sid=${STATE.script_id}`;
        if (STATE.audio_id) h += `&audio=${STATE.audio_id}`;
      }
      if (idx === 3 && STATE.video_id) h = `#/editing?vid=${STATE.video_id}`;
      if (idx === 4 && STATE.edit_id) h = `#/cover?eid=${STATE.edit_id}`;
      if (idx === 5 && STATE.cover_id) h = `#/publish?cid=${STATE.cover_id}`;
      go(h);
    };
  });
}

function bindSidebarToggle() {
  const t = document.getElementById("sidebarToggle");
  if (!t) return;
  t.onclick = () => document.getElementById("layout").classList.toggle("nav-collapsed");
}

async function render() {
  const hash = location.hash || "#/login";
  const app = document.getElementById("app");

  if (hash.split("?")[0] === "#/login" || !API.token) {
    if (hash.split("?")[0] !== "#/login") { go("#/login"); return; }
    app.innerHTML = "";
    try { await pageLogin(app); } catch (e) { app.innerHTML = `<div class="center-screen">出错了：${esc(e.message)}</div>`; }
    return;
  }

  // 已登录：左侧工作流 + 右侧内容区
  if (!document.getElementById("layout")) {
    app.innerHTML = topbar() +
      `<div id="layout"><aside id="sidebar"></aside><main id="main">
        <button class="sidebar-toggle" id="sidebarToggle" title="收起/展开导航"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:18px;height:18px;display:block"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg></button>
      </main></div>`;
    bindLogout();
    bindSidebarToggle();
    bindThemeToggle();
  }

  const sidebarEl = document.getElementById("sidebar");
  if (sidebarEl) {
    sidebarEl.innerHTML = sidebar(currentStepIndex(hash));
    bindSidebar();
  }

  const page = routes[hash.split("?")[0]] || pageDashboard;
  const main = document.getElementById("main");
  try {
    await page(main);
  } catch (e) {
    main.innerHTML = `<div class="wrap"><div class="card"><h2>出错了</h2><div class="muted">${esc(e.message)}</div></div></div>`;
  }
  saveState();   // 路由切完（page 内对 STATE 的赋值都已落定）持久化，刷新后可恢复上下文
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

// ===== 仪表盘 =====
function pageDashboard(app) {
  app.innerHTML = `<div class="wrap">
    <div class="card"><h2>工作台</h2><div class="sub">选择一种方式开始制作你的数字人短视频</div>
      <div class="entry-grid">
        <div class="entry" id="eIndustry"><div><div class="ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 16l5-5 4 4 7-7"/><path d="M20 8h-4M20 8v4"/></svg></div><h3>行业爆款改写</h3><p>输入你的行业，自动筛选该行业高热度口播文案，挑一篇一键改写。</p></div></div>
        <div class="entry" id="eLink"><div><div class="ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M9 12h6"/><path d="M10 8H7a4 4 0 0 0 0 8h3M14 8h3a4 4 0 0 1 0 8h-3"/></svg></div><h3>链接提取改写</h3><p>粘贴一条爆款视频链接，自动提取文案，直接进入改写流程。</p></div></div>
        <div class="entry" id="eLib"><div><div class="ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 4h4v16H5z"/><path d="M10 4h4v16h-4z"/><path d="M15 5l4 1-3 14-4-1"/></svg></div><h3>我的文案库</h3><p>查看所有提取/保存的文案（含智能分类与真实视频数据），点开可复用改写。</p></div></div>
      </div>
    </div>
  </div>`;
  bindLogout();
  document.getElementById("eIndustry").onclick = () => go("#/industry");
  document.getElementById("eLink").onclick = () => go("#/extract");
  document.getElementById("eLib").onclick = () => go("#/library");
}

// ===== 入口一：行业爆款 =====
async function pageIndustry(app) {
  app.innerHTML = `<div class="wrap">
    <div class="card"><h2>行业爆款文案筛选</h2><div class="sub">输入你的行业，系统匹配该赛道高热度口播文案</div>
      <label>你的行业</label>
      <input id="indInput" placeholder="如：餐饮、房产、教育、美妆、穿搭、健身、数码、本地生活、二手车" />
      <button class="btn" style="margin-top:14px" id="btnFilter">筛选爆款</button>
      <div id="indHint" class="muted" style="margin-top:10px"></div>
      <div id="indList" style="margin-top:16px"></div>
    </div>
  </div>`;
  bindLogout();
  const avail = ["本地生活", "餐饮美食", "房产中介", "教育培训", "美妆护肤", "服装穿搭", "健身减肥", "数码家电", "二手车"];
  document.getElementById("indHint").textContent = "支持行业：" + avail.join("、");
  document.getElementById("btnFilter").onclick = async () => {
    const v = indInput.value.trim();
    if (!v) { toast("请输入行业"); return; }
    try {
      const r = await API.postForm("/scripts/industry", { industry: v });
      const list = document.getElementById("indList");
      const metaLine = (it) => {
        const parts = [];
        if (it.isMine) parts.push(`<span class="tag mine">我的真实文案</span>`);
        else if (it.isPublic) parts.push(`<span class="tag pub"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 11l18-5v12L3 13v-2z"/><path d="M11.6 16.8a3 3 0 1 1-2.5-5.4"/></svg>公共爆款</span>`);
        if (it.like_count != null) parts.push(`<span class="stat stat-like"><svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/></svg>${it.like_count}</span>`);
        if (it.comment_count != null) parts.push(`<span class="stat stat-comment"><svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7A8.38 8.38 0 0 1 4 11.5a8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>${it.comment_count}</span>`);
        if (it.share_count != null) parts.push(`<span class="stat stat-share"><svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg>${it.share_count}</span>`);
        if (it.collect_count != null) parts.push(`<span class="stat stat-collect"><svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>${it.collect_count}</span>`);
        return parts.length ? `<div class="item-meta">${parts.join(" ")}</div>` : "";
      };
      const hint = r.matched
        ? `<div class="muted" style="margin-bottom:10px">已匹配行业：<b style="color:var(--brand)">${esc(r.industry)}</b>${r.items.some(i => i.isMine) ? " · 含你库中的真实文案" : ""}</div>`
        : `<div class="muted">未命中专属库，可选行业：${r.available.join("、")}</div>`;
      list.innerHTML = hint + r.items.map((it, i) =>
        `<div class="item" data-i="${i}"><div class="t">${esc(it.title)}</div>${metaLine(it)}<div class="c">${esc(it.content)}</div></div>`
      ).join("");
      list.querySelectorAll(".item").forEach(el => el.onclick = async () => {
        const it = r.items[+el.dataset.i];
        if (it.isMine && it.sid) { STATE.script_id = it.sid; go("#/rewrite?sid=" + it.sid); return; }
        const sv = await API.postForm("/scripts/save", { source: "industry", industry: r.industry || v, original_text: it.content });
        STATE.script_id = sv.script_id;
        if (sv.duplicated) toast("文案已存在，已更新该条");
        go("#/rewrite?sid=" + sv.script_id);
      });
    } catch (e) { toast(e.message); }
  };
}

// ===== 文案库列表 =====
async function pageLibrary(app) {
  app.innerHTML = `<div class="wrap">
    <div class="card"><h2 class="page-title"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>我的文案库</h2><div class="sub">所有提取/保存的文案（含智能分类与真实视频数据），点开可复用改写</div>
      <div id="libList" style="margin-top:12px"></div>
    </div>
  </div>`;
  bindLogout();
  try {
    const list = await API.get("/scripts/list");
    const box = document.getElementById("libList");
    if (!list.length) {
      box.innerHTML = `<div class="muted">还没有文案。去「链接提取」或「行业爆款」存几条吧。</div>`;
      return;
    }
    box.innerHTML = list.map((s, i) => {
      const ico = (d) => `<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
      const lc = s.like_count != null ? `<span class="stat stat-like">${ico('<path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/>')}${s.like_count}</span>` : "";
      const cc = s.comment_count != null ? `<span class="stat stat-comment">${ico('<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7A8.38 8.38 0 0 1 4 11.5a8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>')}${s.comment_count}</span>` : "";
      const sc = s.share_count != null ? `<span class="stat stat-share">${ico('<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/>')}${s.share_count}</span>` : "";
      const kc = s.collect_count != null ? `<span class="stat stat-collect">${ico('<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>')}${s.collect_count}</span>` : "";
      const meta = (lc + cc + sc + kc) ? `<div class="item-meta">${lc}${cc}${sc}${kc}</div>` : "";
      const src = s.source === "link" ? "提取" : (s.source === "industry" ? "行业" : (s.source || "其他"));
      const title = s.video_title || s.title || (s.original_text || "").slice(0, 24) || "未命名文案";
      const snippet = (s.original_text || "").slice(0, 80);
      return `<div class="item lib-item" data-i="${i}">
        <div class="row" style="justify-content:space-between;align-items:center;margin-bottom:6px">
          <span class="tag">${esc(s.industry || "其他")}</span>
          <span class="muted" style="font-size:12px">${esc(src)} · ${esc(s.created_at || "")}</span>
        </div>
        <div class="t">${esc(title)}</div>
        ${meta}
        <div class="c">${esc(snippet)}${snippet.length >= 80 ? "…" : ""}</div>
      </div>`;
    }).join("");
    box.querySelectorAll(".lib-item").forEach(el => el.onclick = () => {
      const s = list[+el.dataset.i];
      STATE.script_id = s.id;
      go("#/rewrite?sid=" + s.id);
    });
  } catch (e) { toast(e.message); }
}

// ===== 入口二：链接提取 =====
async function pageExtract(app) {
  let curMeta = {}, curSourceUrl = "";
  const IND_OPTS = ["本地生活", "餐饮美食", "房产中介", "教育培训", "美妆护肤", "服装穿搭", "健身减肥", "数码家电", "二手车", "其他"];
  const TYP_OPTS = ["解题型", "推荐型", "揭秘型", "案例型", "疑问型"];
  app.innerHTML = `<div class="wrap">
    <div class="card"><h2>链接提取文案</h2><div class="sub">粘贴爆款视频链接，自动提取口播文案 + 智能分类</div>
      <label>视频链接</label><input id="linkInput" placeholder="https://..." />
      <button class="btn" style="margin-top:14px" id="btnExtract">提取文案</button>
      <div id="metaBox" class="meta-box" style="display:none;margin-top:14px"></div>
      <div class="muted" style="margin-top:12px">若无链接，可直接在下方粘贴文案</div>
      <label>文案内容（可编辑）</label><textarea id="extText" placeholder="在此粘贴或编辑视频口播文案..."></textarea>
      <div class="sel-row">
        <div><label>智能分类-行业</label>
          <select id="indSel" class="sel">${IND_OPTS.map(o => `<option value="${o}">${o}</option>`).join("")}</select></div>
        <div><label>写法类型</label>
          <select id="typeSel" class="sel">${TYP_OPTS.map(o => `<option value="${o}">${o}</option>`).join("")}</select></div>
      </div>
      <div class="row" style="margin-top:16px">
        <button class="btn" id="btnSaveLib">仅存入文案库</button>
        <button class="btn primary" id="btnNext">保存并去改写</button>
      </div>
    </div>
  </div>`;
  bindLogout();
  const renderMeta = (r) => {
    const m = r.meta || {};
    const fmt = (n) => (n == null ? "—" : n);
    const dur = m.duration ? Math.round(m.duration) : 0;
    const mm = dur ? `${Math.floor(dur / 60)}:${String(dur % 60).padStart(2, "0")}` : "—";
    const box = document.getElementById("metaBox");
    box.style.display = "block";
    const ico2 = (d) => `<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:14px;height:14px;display:block">${d}</svg>`;
    box.innerHTML = `<div class="meta-title">${ico2('<line x1="12" y1="20" x2="12" y2="10"/><line x1="18" y1="20" x2="18" y2="4"/><line x1="6" y1="20" x2="6" y2="16"/>')}视频数据</div>
      <div class="meta-grid">
        <span class="stat stat-like"><svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/></svg>点赞 ${fmt(m.like_count)}</span>
        <span class="stat stat-comment"><svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7A8.38 8.38 0 0 1 4 11.5a8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>评论 ${fmt(m.comment_count)}</span>
        <span class="stat stat-share"><svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg>转发 ${fmt(m.share_count)}</span>
        <span class="stat stat-collect"><svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>收藏 ${fmt(m.collect_count)}</span>
        <span class="stat stat-time"><svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>时长 ${mm}</span>
      </div>
      ${m.title ? `<div class="meta-line">${ico2('<rect x="2" y="2" width="20" height="20" rx="2.18"/><line x1="7" y1="2" x2="7" y2="22"/><line x1="17" y1="2" x2="17" y2="22"/><line x1="2" y1="12" x2="22" y2="12"/>')} ${esc(m.title)}</div>` : ""}
      ${m.uploader ? `<div class="meta-line">${ico2('<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>')} 作者：${esc(m.uploader)}</div>` : ""}`;
    if (r.industry) document.getElementById("indSel").value = IND_OPTS.includes(r.industry) ? r.industry : "其他";
    if (r.type) document.getElementById("typeSel").value = TYP_OPTS.includes(r.type) ? r.type : "解题型";
    curMeta = m; curSourceUrl = r.source_url || "";
  };
  const runExtract = async (payload) => {
    const btn = document.getElementById("btnExtract");
    btn.disabled = true; const old = btn.textContent; btn.textContent = "提取中...";
    try {
      const r = await API.postForm("/extract", payload);
      // 防御：后端若误把对象传过来，强制转字符串，避免 textarea 显示 [object Object]
      const rawText = r && r.original_text;
      extText.value = (rawText == null ? "" : (typeof rawText === "object" ? JSON.stringify(rawText) : String(rawText)));
      renderMeta(r);
      if (r.note) toast(r.note);
      else if (!r.original_text) toast("未提取到文案，可直接粘贴");
    } catch (e) { toast(e.message); }
    finally { btn.disabled = false; btn.textContent = old; }
  };
  document.getElementById("btnExtract").onclick = async () => {
    const url = linkInput.value.trim();
    if (!url) { toast("请输入链接"); return; }
    await runExtract({ url });
  };
  const doSave = async (goRewrite) => {
    const txt = extText.value.trim();
    if (!txt) { toast("请先提取或粘贴文案"); return; }
    const m = curMeta || {};
    const payload = {
      source: "link",
      industry: document.getElementById("indSel").value,
      type_: document.getElementById("typeSel").value,
      original_text: txt,
      source_url: curSourceUrl || linkInput.value.trim(),
      video_title: m.title || "",
      uploader: m.uploader || "",
      like_count: Number(m.like_count) || 0,
      comment_count: Number(m.comment_count) || 0,
      share_count: Number(m.share_count) || 0,
      collect_count: Number(m.collect_count) || 0,
      duration: Number(m.duration) || 0,
    };
    const sv = await API.postForm("/scripts/save", payload);
    STATE.script_id = sv.script_id;
    if (goRewrite) go("#/rewrite?sid=" + sv.script_id);
    else toast(sv.duplicated ? "文案已存在，已更新该条" : "已存入文案库");
  };
  document.getElementById("btnSaveLib").onclick = () => doSave(false);
  document.getElementById("btnNext").onclick = () => doSave(true);
}

// ===== 改写页 =====
const TYPES = ["解题型", "推荐型", "揭秘型", "案例型", "疑问型"];
const PERSONAS = ["老板", "专家", "邻家大哥", "毒舌朋友"];
let _rewriteState = { type: "解题型", persona: "老板", generated: "" };
let _rewriteBusy = false;  // 改写互斥锁：LLM 返回前禁止重复点击

async function pageRewrite(app) {
  const { sid } = qp();
  STATE.script_id = sid;
  let script = {};
  try { script = await API.get("/scripts/" + sid); } catch (e) {}
  // 用脚本已保存的写法类型/人设初始化选择器，避免从提取页跳转后丢失「疑问型」等分类
  if (script.type && TYPES.includes(script.type)) _rewriteState.type = script.type;
  if (script.persona && PERSONAS.includes(script.persona)) _rewriteState.persona = script.persona;
  const hasGen = !!(script.generated_text && script.generated_text.trim());
  app.innerHTML = `<div class="wrap cols-rewrite">
    <div class="card"><h2>文案改写</h2><div class="sub">选择写法类型与人设，一键生成专属口播文案</div>
      <label>原始文案</label>
      <div class="preview-box">${esc(script.original_text || "")}</div>
      <div class="word-count">字数：${(script.original_text || "").length}</div>
      <label>写法类型</label>
      <div class="chips" id="typeChips">${TYPES.map(t => `<div class="chip ${t === _rewriteState.type ? "on" : ""}" data-t="${t}">${t}</div>`).join("")}</div>
      <label>人设</label>
      <div class="chips" id="personaChips">${PERSONAS.map(p => `<div class="chip ${p === _rewriteState.persona ? "on" : ""}" data-p="${p}">${p}</div>`).join("")}</div>
      <div class="row" style="margin-top:18px">
        <button class="btn" id="btnGen">开始生成</button>
        <button class="btn ghost" id="btnBack">返回</button>
      </div>
    </div>
    ${hasGen ? `<div class="card"><h2 style="font-size:16px;display:flex;align-items:center;gap:8px"><svg viewBox="0 0 24 24" fill="none" stroke="var(--ok)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:18px;height:18px"><polyline points="20 6 9 17 4 12"/></svg>改写稿<span class="muted" style="font-size:12px;font-weight:400;margin-left:8px">可继续编辑，配音默认用这份${script.updated_at ? " · 上次更新 " + String(script.updated_at).slice(5, 16) : ""}</span></h2>
      <div class="preview-box" style="max-height:320px">${esc(script.generated_text)}</div>
      <div class="word-count">字数：${wordCount(script.generated_text)}</div>
      <div class="row" style="margin-top:12px">
        <button class="btn ghost" id="btnViewGen">查看 / 编辑改写稿</button>
        <button class="btn" id="btnGenToDub">用这份改写稿去配音 →</button>
      </div>
    </div>` : ""}
  </div>`;
  bindLogout();
  if (hasGen) {
    document.getElementById("btnViewGen").onclick = () => showPreview(script.generated_text, sid);
    document.getElementById("btnGenToDub").onclick = () => go("#/dubbing?sid=" + sid);
  }
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
  if (_rewriteBusy) return;            // 防止连点重复烧 token
  _rewriteBusy = true;
  const genBtn = document.getElementById("btnGen");
  const oldLabel = genBtn ? genBtn.textContent : "开始生成";
  if (genBtn) { genBtn.disabled = true; genBtn.textContent = "生成中..."; }
  try {
    const r = await API.postForm("/scripts/rewrite", { script_id: sid, type_: _rewriteState.type, persona: _rewriteState.persona });
    _rewriteState.generated = r.generated_text;
    STATE.cover_title = r.cover_title || "";
    STATE.cover_subtitle = r.cover_subtitle || "";
    saveState();
    showPreview(r.generated_text, sid, r);
  } catch (e) { toast(e.message); }
  finally {
    _rewriteBusy = false;
    if (genBtn) { genBtn.disabled = false; genBtn.textContent = oldLabel; }
  }
}

function highlightHits(text, hits) {
  // 先转义，再保留换行，最后把命中词包成高亮 span
  let html = esc(text).split("\n").join("<br>");
  (hits || []).forEach(h => { html = html.split(esc(h.word)).join(`<span class="hit">${esc(h.word)}</span>`); });
  return html;
}
function scrollToFirstHit(box) {
  const first = box && box.querySelector ? box.querySelector(".hit") : null;
  if (first) {
    first.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

function wordCount(str) { return (str || "").replace(/\s/g, "").length; }

function showPreview(text, sid, meta) {
  meta = meta || {};
  const initial = text || "";
  const body = `<div class="preview-box" id="prevBox" contenteditable="true" spellcheck="false">${esc(initial)}</div>
    <div class="preview-meta-row">
      <div class="muted" style="font-size:12px;display:flex;align-items:center;gap:5px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:13px;height:13px;color:var(--faint)"><path d="M9.663 17h4.673M12 3v1M6.343 6.343l-.707-.707M18.364 18.364l-.707-.707M4.93 12H4M20 12h-.93M6.343 17.657l-.707.707M18.364 5.636l-.707.707"/><circle cx="12" cy="12" r="4"/></svg>可直接在上方框内修改文案</div>
      <div class="word-count" id="prevWordCount">字数：${wordCount(initial)}</div>
    </div>
    <div id="checkSummary"></div>`;
  const foot = `
    <button class="btn ghost" id="mCheck">违禁词检查</button>
    <button class="btn ghost" id="mRegen">不满意 重新生成</button>
    <button class="btn" id="mOk">满意 前往配音</button>`;
  modal("改写预览", body, foot);
  document.getElementById("mRegen").onclick = () => { closeModal(); doRewrite(sid); };
  document.getElementById("mOk").onclick = async () => {
    try {
      const edited = document.getElementById("prevBox").innerText;
      await API.postForm("/scripts/update-generated", { script_id: sid, generated_text: edited });
      closeModal(); go("#/dubbing?sid=" + sid);
    } catch (e) { toast(e.message); }
  };
  document.getElementById("mCheck").onclick = async () => {
    try {
      const cur = document.getElementById("prevBox").innerText;
      const r = await API.postForm("/scripts/check", { text: cur });
      const box = document.getElementById("prevBox");
      const summary = document.getElementById("checkSummary");
      box.innerHTML = highlightHits(cur, r.hits);
      if (r.safe) {
        summary.innerHTML = "";
        toast("未检出违禁词", "ok");
      } else {
        summary.innerHTML = `<div class="check-summary" style="display:flex;align-items:center;gap:7px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:15px;height:15px;flex-shrink:0"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>命中：${r.hits.map(h => `「${esc(h.word)}」${esc(h.category)}`).join("、")}</div>`;
        toast(`检出 ${r.count} 个风险词，已高亮并定位`);
        scrollToFirstHit(box);
      }
    } catch (e) { toast(e.message); }
  };
  const prevBox = document.getElementById("prevBox");
  const wcBox = document.getElementById("prevWordCount");
  const updateCount = () => { wcBox.innerText = "字数：" + wordCount(prevBox.innerText); };
  prevBox.addEventListener("input", updateCount);
  prevBox.addEventListener("keyup", updateCount);
}

// ===== 配音页 =====
let _dubState = { timbre_id: 0, emotion: "自然", speed: 1.0, pitch: 1.0, volume: 50, seed: +(localStorage.getItem("lastGoodSeed") || 0) };
function _saveGoodSeed(seed) {
  try { localStorage.setItem("lastGoodSeed", String(seed)); } catch (e) {}
}
const EMOTIONS = ["自然", "嫌弃", "高兴", "伤心", "说教", "激动", "生气"];

async function pageDubbing(app) {
  const { sid } = qp();
  STATE.script_id = sid;
  let dubText = "", usingGen = false, hasScript = false;
  if (sid) {
    try {
      const s = await API.get("/scripts/" + sid);
      hasScript = true;
      usingGen = !!(s.generated_text && s.generated_text.trim());
      dubText = usingGen ? s.generated_text : "";   // 未改写不得用原始文案配音
    } catch (e) {}
  }
  app.innerHTML = `<div class="wrap">
    <div class="card"><h2>配音</h2><div class="sub">上传你的音色，调节情绪 / 语速 / 音调 / 音量，生成专属配音</div>
      ${sid ? (usingGen ? `<div class="check-summary" style="background:var(--panel2);border-color:var(--line);color:var(--muted);display:flex;justify-content:space-between;align-items:center;gap:12px">
        <span style="display:flex;align-items:center;gap:6px"><svg viewBox="0 0 24 24" fill="none" stroke="var(--ok)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:14px;height:14px"><polyline points="20 6 9 17 4 12"/></svg>本次配音文案：改写稿 · ${wordCount(dubText)} 字</span>
        <button class="btn ghost sm" id="btnViewDubText">查看</button>
      </div>` : `<div class="check-summary" style="background:rgba(255,204,77,.1);border-color:rgba(255,204,77,.4);color:var(--warn);display:flex;justify-content:space-between;align-items:center;gap:12px">
        <span style="display:flex;align-items:center;gap:6px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:14px;height:14px"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>该文案尚未改写，不能直接配音</span>
        <button class="btn ghost sm" id="btnGoRewrite">去改写 →</button>
      </div>`) : `<div class="check-summary" style="margin-bottom:8px">未指定文案，请先从改写页点「满意 前往配音」或从文案库选择</div>`}
      <label style="margin-top:${sid ? "14" : "0"}px">我的音色（点击选用，无需重复上传）</label>
      <div id="timbreList" class="chips" style="margin-top:6px"></div>

      <label style="margin-top:16px">上传新音色</label>
      <div style="margin-top:8px">
        <button class="btn ghost sm" id="btnToggleUp">＋ 上传新音色</button>
      </div>
      <div id="upArea" style="display:none;margin-top:10px">
        <div class="file-drop" id="timbreDrop" style="padding:28px 16px;width:100%;box-sizing:border-box">
          <input type="file" id="timbreFile" accept=".wav,.mp3,.m4a" hidden />
          <span id="timbreFileName" style="font-size:14px;display:flex;align-items:center;justify-content:center;gap:7px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:16px;height:16px"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>点击选择音频文件，或拖拽到此处（WAV / MP3 / M4A）</span>
          <button class="btn" style="margin-top:14px" id="btnUpTimbre">确认上传</button>
        </div>
      </div>

      <label style="margin-top:16px">情绪调节</label>
      <div class="chips" id="emoChips">${EMOTIONS.map(e => `<div class="chip ${e === _dubState.emotion ? "on" : ""}" data-e="${e}">${e}</div>`).join("")}</div>
      <div class="muted" style="margin-top:4px;font-size:12px">情绪会生成「语气 + 音调走向 + 场景」指令；选「自然」则不传指令，让模型按你的本音发挥；下方滑块做全局语速 / 音高微调</div>

      <div style="margin-top:10px">
        <button class="btn ghost sm" id="btnToggleAdv">高级参数 ▼</button>
      </div>
      <div id="advParams" style="display:none;margin-top:10px">
        <label>语速调节：<span id="spVal">1.0</span>x</label>
        <div class="range-row"><input type="range" id="speed" min="0.5" max="2" step="0.1" value="1.0" /><span class="muted">慢 ←→ 快</span></div>

        <label>音调调节：<span id="ptVal">1.0</span>x</label>
        <div class="range-row"><input type="range" id="pitch" min="0.5" max="2" step="0.1" value="1.0" /><span class="muted">低沉 ←→ 尖亮</span></div>

        <label>音量调节：<span id="volVal">50</span></label>
        <div class="range-row"><input type="range" id="vol" min="0" max="100" step="1" value="50" /><span class="muted">小 ←→ 大</span></div>

        <label style="margin-top:14px">随机种子（同一 seed 可复现；换一个就是换一版效果，不满意点「随机」再抽一次）</label>
        <div class="row" style="margin-top:6px;align-items:center;gap:8px">
          <input id="seedInput" type="number" min="0" max="65535" step="1" value="0" style="width:120px" />
          <button class="btn ghost sm" id="btnRollSeed">随机</button>
          <button class="btn ghost sm" id="btnResetSeed">归零</button>
        </div>
      </div>

      <button class="btn" style="margin-top:18px" id="btnGenDub"${usingGen ? "" : " disabled style=\"opacity:.45;cursor:not-allowed\""}>生成配音</button>
      <div id="dubResult" style="margin-top:16px"></div>
    </div>
  </div>`;
  bindLogout();
  const vb = document.getElementById("btnViewDubText");
  if (vb) vb.onclick = () => modal("本次配音文案", `<div class="preview-box" style="max-height:420px">${esc(dubText)}</div>`, `<button class="btn" onclick="closeModal()">关闭</button>`);
  const gr = document.getElementById("btnGoRewrite");
  if (gr) gr.onclick = () => go("#/rewrite?sid=" + sid);
  // 上传音色折叠 / 展开按钮（默认折叠，有音色时不打扰用户；空态自动展开）
  // 按钮文案永远保持「＋ 上传新音色」，展开用 .on 激活样式指示（避免空态自动展开时"收起"语义错配）
  const _upBtn = document.getElementById("btnToggleUp");
  const _upArea = document.getElementById("upArea");
  const _setUpArea = (open) => {
    _upArea.style.display = open ? "block" : "none";
    if (_upBtn) { _upBtn.textContent = "＋ 上传新音色"; _upBtn.classList.toggle("on", open); }
  };
  if (_upBtn) _upBtn.onclick = () => _setUpArea(_upArea.style.display === "none");
  document.getElementById("emoChips").querySelectorAll(".chip").forEach(c => c.onclick = () => {
    _dubState.emotion = c.dataset.e;
    document.getElementById("emoChips").querySelectorAll(".chip").forEach(x => x.classList.remove("on"));
    c.classList.add("on");
  });
  // 高级参数（语速/音调/音量）默认折叠，状态记 localStorage
  const advParams = document.getElementById("advParams");
  const btnToggleAdv = document.getElementById("btnToggleAdv");
  const syncAdvToggle = () => {
    const expanded = advParams.style.display !== "none";
    btnToggleAdv.textContent = "高级参数 " + (expanded ? "▼" : "▶");
    try { localStorage.setItem("dubAdvExpanded", expanded ? "1" : "0"); } catch (e) {}
  };
  try {
    advParams.style.display = (localStorage.getItem("dubAdvExpanded") === "1") ? "block" : "none";
  } catch (e) { advParams.style.display = "none"; }
  syncAdvToggle();
  btnToggleAdv.onclick = () => {
    advParams.style.display = advParams.style.display === "none" ? "block" : "none";
    syncAdvToggle();
  };
  const sp = document.getElementById("speed");
  sp.oninput = () => { _dubState.speed = parseFloat(sp.value); spVal.textContent = sp.value; };
  const pt = document.getElementById("pitch");
  pt.oninput = () => { _dubState.pitch = parseFloat(pt.value); ptVal.textContent = pt.value; };
  const vol = document.getElementById("vol");
  vol.oninput = () => { _dubState.volume = parseInt(vol.value, 10); volVal.textContent = vol.value; };
  const seedInput = document.getElementById("seedInput");
  // 统一设置 seed（生成、回显、随机都走这里，保证 UI 与状态一致）
  const setSeed = (v) => { _dubState.seed = Math.max(0, Math.min(65535, parseInt(v, 10) || 0)); seedInput.value = _dubState.seed; };
  seedInput.oninput = () => setSeed(seedInput.value);
  document.getElementById("btnRollSeed").onclick = () => setSeed(Math.floor(Math.random() * 65536));
  document.getElementById("btnResetSeed").onclick = () => setSeed(0);
  // 美化后的文件选择：点击虚线框或拖拽均可
  const drop = document.getElementById("timbreDrop");
  const fName = document.getElementById("timbreFileName");
  drop.onclick = (e) => { if (e.target.closest("button")) return; timbreFile.click(); };
  timbreFile.onchange = () => {
    const f = timbreFile.files[0];
    fName.textContent = f ? f.name : "点击选择音频文件，或拖拽到此处（WAV / MP3 / M4A）";
    fName.classList.toggle("muted", !f);
  };
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("drag"); };
  drop.ondragleave = () => drop.classList.remove("drag");
  drop.ondrop = (e) => {
    e.preventDefault(); drop.classList.remove("drag");
    if (e.dataTransfer.files.length) {
      timbreFile.files = e.dataTransfer.files;
      timbreFile.onchange();
    }
  };
  document.getElementById("btnUpTimbre").onclick = (e) => {
    e.stopPropagation();
    const f = timbreFile.files[0];
    if (!f) { toast("请先选择音频文件"); return; }
    // 与百炼 CosyVoice 声音复刻官方要求对齐：仅 WAV / MP3 / M4A，≤10MB，≤60s（推荐 10~20s）
    const _allowed = ["wav", "mp3", "m4a"];
    const _fe = (f.name.split(".").pop() || "").toLowerCase();
    if (!_allowed.includes(_fe)) { toast("格式不支持，只能选 WAV / MP3 / M4A"); return; }
    const _maxSize = 10 * 1024 * 1024;
    if (f.size > _maxSize) {
      toast(`文件不能超过 10MB（当前 ${(f.size / 1024 / 1024).toFixed(1)}MB），建议裁剪到 10~20 秒`);
      return;
    }
    const defName = f.name.replace(/\.[^.]+$/, "");
    modal("上传音色", `
      <label>音色名称（自定义，方便以后区分）</label>
      <input id="tbName" placeholder="如：低沉男声 / 老潘本音" value="${esc(defName)}" />
      <div class="muted" style="margin-top:6px;font-size:12px">文件：${esc(f.name)}（${(f.size / 1024 / 1024).toFixed(2)}MB）</div>
      <div class="muted" style="margin-top:4px;font-size:12px">官方要求：WAV / MP3 / M4A，≤10MB；时长 10~20 秒最佳（最长 60 秒），需含 5 秒以上连续清晰朗读、无背景音</div>
    `, `<button class="btn ghost" onclick="closeModal()">取消</button><button class="btn" id="tbOk">确认上传</button>`);
    document.getElementById("tbOk").onclick = async () => {
      const name = document.getElementById("tbName").value.trim() || defName;
      try {
        const r = await API.postForm("/timbres/upload", { name, file: f });
        _dubState.timbre_id = r.timbre_id;
        // 上传成功后自动折叠整个上传区，避免占位
        const u = document.getElementById("upArea");
        const b = document.getElementById("btnToggleUp");
        if (u && b) { u.style.display = "none"; b.textContent = "＋ 上传新音色"; b.classList.remove("on"); }
        // 清空已选文件，避免下次打开页面还留着一个 stale 文件名
        timbreFile.value = "";
        const fn = document.getElementById("timbreFileName");
        if (fn) fn.textContent = "点击选择音频文件，或拖拽到此处（WAV / MP3 / M4A）";
        closeModal();
        toast("音色已上传");
        loadTimbres();
      } catch (e) { toast(e.message); }
    };
  };
  document.getElementById("btnGenDub").onclick = async () => {
    if (!usingGen) { toast("请先改写文案再去配音"); return; }
    try {
      const r = await genWithProgress("/dubbing/generate",
        { script_id: sid, timbre_id: _dubState.timbre_id, emotion: _dubState.emotion,
          speed: _dubState.speed, pitch: _dubState.pitch,
          volume: _dubState.volume, seed: _dubState.seed },
        "正在生成配音...");
      STATE.audio_id = r.audio_id;
      _saveGoodSeed(_dubState.seed);
      renderDubResult(r);
    } catch (e) {}
  };
  await loadTimbres();
  if (sid) loadExistingDubs();

  async function loadExistingDubs() {
    try {
      const list = await API.get(`/scripts/${sid}/audios`);
      if (!list || !list.length) {
        // 新文案没有历史音频：也要把记忆中的满意 seed 回显到 UI
        setSeed(_dubState.seed);
        return;
      }
      const latest = list[0];
      STATE.audio_id = latest.audio_id;
      // 同步上次配音的设置，避免 UI 显示和实际音频不一致
      if (latest.timbre_id) _dubState.timbre_id = +latest.timbre_id;
      if (latest.emotion) _dubState.emotion = latest.emotion;
      if (latest.speed) _dubState.speed = +latest.speed;
      // 旧数据没有这三列（迁移前生成的音频），值为 null，保持默认即可
      if (latest.pitch != null) _dubState.pitch = +latest.pitch;
      if (latest.volume != null) _dubState.volume = +latest.volume;
      if (latest.seed != null) _dubState.seed = +latest.seed;
      // 无论当前脚本有没有音频，都优先用 localStorage 里用户"满意"过的 seed 兜底
      const stored = +(localStorage.getItem("lastGoodSeed") || 0);
      if (stored > 0) _dubState.seed = stored;
      // 更新音色选中态
      document.querySelectorAll("#timbreList .chip").forEach(c => {
        c.classList.toggle("on", +c.dataset.id === _dubState.timbre_id);
      });
      // 更新情绪选中态
      document.querySelectorAll("#emoChips .chip").forEach(c => {
        c.classList.toggle("on", c.dataset.e === _dubState.emotion);
      });
      // 更新语速滑块
      const sp = document.getElementById("speed");
      const spVal = document.getElementById("spVal");
      if (sp) sp.value = _dubState.speed;
      if (spVal) spVal.textContent = _dubState.speed;
      // 更新音调滑块
      const ptEl = document.getElementById("pitch");
      const ptValEl = document.getElementById("ptVal");
      if (ptEl) ptEl.value = _dubState.pitch;
      if (ptValEl) ptValEl.textContent = _dubState.pitch;
      // 更新音量滑块
      const volEl = document.getElementById("vol");
      const volValEl = document.getElementById("volVal");
      if (volEl) volEl.value = _dubState.volume;
      if (volValEl) volValEl.textContent = _dubState.volume;
      // 更新随机种子
      setSeed(_dubState.seed);
      renderDubResult(latest);
    } catch (e) {}
  }

  async function loadTimbres() {
    try {
      const list = await API.get("/timbres");
      const box = document.getElementById("timbreList");
      if (!list.length) {
        box.innerHTML = `<div class="check-summary" style="background:rgba(255,204,77,.1);border-color:rgba(255,204,77,.4);color:var(--warn);display:flex;align-items:center;gap:8px">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:15px;height:15px;flex-shrink:0"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg><span>暂无音色，请先上传</span>
        </div>`;
        // 空态：自动展开上传区（pageDubbing 渲染时可拿到 upArea/btnToggleUp）
        const u = document.getElementById("upArea");
        const b = document.getElementById("btnToggleUp");
        if (u && b) { u.style.display = "block"; b.textContent = "＋ 上传新音色"; b.classList.add("on"); }
        return;
      }
      // 库里有音色但未选中 -> 自动选中第一条，避免每次都要重新上传
      const ids = list.map(t => +t.id);
      if (!ids.includes(+_dubState.timbre_id)) _dubState.timbre_id = ids[0];
      // 同名音色加序号便于分辨；名字靠上传时自定义，不再显示时间
      const labelOf = (t, i) => `${i + 1}. ${t.name}`;
      box.innerHTML = list.map((t, i) => `<div class="chip ${+t.id === +_dubState.timbre_id ? "on" : ""}" data-id="${t.id}"><span>${esc(labelOf(t, i))}</span><button class="chip-del" data-del="${t.id}" title="删除该音色">×</button></div>`).join("");
      box.querySelectorAll(".chip").forEach(c => c.onclick = (ev) => {
        if (ev.target.closest(".chip-del")) return;
        _dubState.timbre_id = +c.dataset.id;
        box.querySelectorAll(".chip").forEach(x => x.classList.remove("on"));
        c.classList.add("on");
      });
      box.querySelectorAll(".chip-del").forEach(b => b.onclick = async (ev) => {
        ev.stopPropagation();
        const id = +b.dataset.del;
        const item = list.find(t => +t.id === id);
        if (!confirm(`确定删除音色「${item ? item.name : id}」？\n将同时清理阿里云百炼上的云端音色，删除后不可恢复。`)) return;
        try {
          const r = await API.req("/timbres/" + id, { method: "DELETE" });
          if (+_dubState.timbre_id === id) _dubState.timbre_id = 0;
          if (r && r.cloud_deleted) toast("已删除，云端音色已同步清理");
          else if (r && r.cloud_msg) toast(`本地已删除；云端清理失败：${r.cloud_msg}`);
          else toast("已删除");
          loadTimbres();
        } catch (e2) { toast(e2.message); }
      });
    } catch (e) {}
  }
  function renderDubResult(r) {
    document.getElementById("dubResult").innerHTML = `
      <div class="card" style="margin:0"><h2 style="font-size:16px">配音完成</h2>
        <div class="player">
          <button class="player-btn" id="plPlay" aria-label="播放">▶</button>
          <span class="player-time" id="plCur">0:00</span>
          <div class="player-bar" id="plBar"><div class="player-bar-fill" id="plFill"></div></div>
          <span class="player-time" id="plDur">0:00</span>
          <div class="player-vol-wrap">
            <button class="player-vol" id="plVol" aria-label="音量">🔊</button>
            <input type="range" class="player-vol-slider" id="plVolSlider" min="0" max="100" value="80">
          </div>
        </div>
        <audio id="plAudio" src="${r.url}"></audio>
        <div class="row">
          <button class="btn ghost sm" id="btnRedub">重新配音</button>
          <a class="btn ghost sm" href="${r.url}" download>下载配音</a>
          <button class="btn sm" id="btnToDH">满意 前往数字人</button>
        </div>
      </div>`;

    const audio = document.getElementById("plAudio");
    const btn = document.getElementById("plPlay");
    const bar = document.getElementById("plBar");
    const fill = document.getElementById("plFill");
    const cur = document.getElementById("plCur");
    const dur = document.getElementById("plDur");
    const vol = document.getElementById("plVol");
    const volSlider = document.getElementById("plVolSlider");

    const fmt = (s) => {
      if (!isFinite(s)) return "0:00";
      s = Math.floor(s);
      const m = Math.floor(s / 60);
      const sec = String(s % 60).padStart(2, "0");
      return m + ":" + sec;
    };

    audio.volume = 0.8;
    audio.addEventListener("loadedmetadata", () => { dur.textContent = fmt(audio.duration); });
    audio.addEventListener("timeupdate", () => {
      cur.textContent = fmt(audio.currentTime);
      if (audio.duration) fill.style.width = (audio.currentTime / audio.duration * 100) + "%";
    });
    audio.addEventListener("ended", () => { btn.textContent = "▶"; });

    btn.onclick = () => {
      if (audio.paused) { audio.play(); btn.textContent = "⏸"; }
      else { audio.pause(); btn.textContent = "▶"; }
    };

    const seek = (e) => {
      const rect = bar.getBoundingClientRect();
      const x = Math.min(Math.max(e.clientX - rect.left, 0), rect.width);
      if (audio.duration) audio.currentTime = x / rect.width * audio.duration;
    };
    bar.addEventListener("pointerdown", (e) => {
      seek(e);
      const move = (ev) => seek(ev);
      const up = () => {
        bar.removeEventListener("pointermove", move);
        bar.removeEventListener("pointerup", up);
      };
      bar.addEventListener("pointermove", move);
      bar.addEventListener("pointerup", up);
    });

    volSlider.addEventListener("input", () => {
      audio.volume = volSlider.value / 100;
      audio.muted = false;
      vol.textContent = audio.volume === 0 ? "🔇" : "🔊";
    });
    vol.onclick = () => {
      if (audio.muted || audio.volume === 0) {
        audio.muted = false; audio.volume = 0.8; volSlider.value = 80; vol.textContent = "🔊";
      } else {
        audio.muted = true; vol.textContent = "🔇";
      }
    };

    document.getElementById("btnRedub").onclick = document.getElementById("btnGenDub").onclick;
    document.getElementById("btnToDH").onclick = () => go("#/digital?sid=" + sid + "&audio=" + r.audio_id);
  }
}

// ===== 数字人页 =====
let _dhState = { avatar_id: 0, history: [] };
async function pageDigital(app) {
  const { sid, audio } = qp();
  STATE.script_id = sid; STATE.audio_id = audio;
  app.innerHTML = `<div class="wrap cols-rewrite">
    <div class="card dh-card">
      <div class="dh-head">
        <div><h2>数字人</h2><div class="sub">上传你的形象，生成口播视频</div></div>
        <span class="dh-stat warn" id="dhStat">待上传形象</span>
      </div>

      <div class="dropzone" id="avatarDrop">
        <input type="file" id="avatarFile" accept="video/*" hidden />
        <div class="dz-ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="6" width="18" height="12" rx="2"/><path d="M3 10h6l-1-4M9 10 8 6"/><circle cx="15" cy="12" r="1.4"/></svg></div>
        <div class="dz-main" id="dzMain">点击选择驱动视频，或拖拽到此处</div>
        <div class="dz-sub">支持 MP4 / MOV / WEBM，正脸效果最佳</div>
        <div class="dz-file" id="dzFile" hidden>
          <span class="dot"></span><span class="fn" id="avatarFileName"></span>
        </div>
        <button class="btn sm" id="btnUpAvatar" disabled>上传形象</button>
      </div>

      <div id="avatarList" class="avatar-grid"></div>

      <button class="btn cta" id="btnGenDH" disabled>
        <svg class="cta-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:18px;height:18px"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>生成口播视频
      </button>
    </div>

    <div class="card" style="margin-top:0">
      <h2 style="font-size:16px">口播视频预览<span class="muted" id="dhPreviewTag" style="font-size:12px;font-weight:400;margin-left:8px"></span></h2>
      <div id="dhPreviewCurrent"><div class="muted" style="padding:24px 0;text-align:center">点击左侧生成口播视频，结果会出现在这里</div></div>
    </div>
  </div>`;
  bindLogout();

  const fileInput = document.getElementById("avatarFile");
  const fileName = document.getElementById("avatarFileName");
  const dzFile = document.getElementById("dzFile");
  const dzMain = document.getElementById("dzMain");
  const dropEl = document.getElementById("avatarDrop");
  const btnUp = document.getElementById("btnUpAvatar");

  const setFile = (f) => {
    if (!f) return;
    fileName.textContent = f.name;
    dzFile.hidden = false;
    dzMain.textContent = "已选择，点击下方「上传形象」";
    dropEl.classList.add("has-file");
    btnUp.disabled = false;
  };
  fileInput.onchange = () => setFile(fileInput.files[0]);

  // 拖拽支持
  dropEl.onclick = (e) => { if (e.target !== btnUp) fileInput.click(); };
  ["dragenter", "dragover"].forEach(ev => dropEl.addEventListener(ev, e => {
    e.preventDefault(); dropEl.classList.add("drag");
  }));
  ["dragleave", "drop"].forEach(ev => dropEl.addEventListener(ev, e => {
    e.preventDefault(); dropEl.classList.remove("drag");
  }));
  dropEl.addEventListener("drop", e => {
    const f = e.dataTransfer.files[0];
    if (f) { fileInput.files = e.dataTransfer.files; setFile(f); }
  });

  btnUp.onclick = async () => {
    const f = fileInput.files[0];
    if (!f) { toast("请选择驱动视频"); return; }
    const defName = f.name.replace(/\.[^.]+$/, "");
    // 先弹框让用户给形象起名，再上传（参考配音页音色改名模式）
    modal("上传形象", `
      <label>形象名称（自定义，方便以后区分）</label>
      <input id="dhName" placeholder="如：正脸A / 户外主持老潘" value="${esc(defName)}" />
      <div class="muted" style="margin-top:6px;font-size:12px">文件：${esc(f.name)}（${(f.size / 1024 / 1024).toFixed(2)}MB）</div>
      <div class="muted" style="margin-top:4px;font-size:12px">建议：MP4 / MOV / WEBM，正脸清晰、肩部以上完整、含明显嘴部动作；过暗 / 侧脸 / 遮挡都会影响生成效果</div>
    `, `<button class="btn ghost" onclick="closeModal()">取消</button><button class="btn" id="dhOk">确认上传</button>`);
    document.getElementById("dhOk").onclick = async () => {
      const name = (document.getElementById("dhName").value.trim()) || defName;
      btnUp.disabled = true;
      const old = btnUp.textContent;
      btnUp.textContent = "上传中...";
      dropEl.classList.add("busy");
      try {
        const r = await API.postForm("/avatars/upload", { name, file: f });
        _dhState.avatar_id = r.avatar_id;
        fileInput.value = "";
        dzFile.hidden = true;
        dzMain.textContent = "点击选择驱动视频，或拖拽到此处";
        dropEl.classList.remove("has-file", "busy");
        btnUp.textContent = old;
        closeModal();
        toast("形象已上传");
        loadAvatars().then(() => {
          // 上传后默认选中新形象 → 同步右上角徽标文案，避免与"已就绪 N 个"混淆
          const dhStat = document.getElementById("dhStat");
          if (dhStat) { dhStat.textContent = "已选 1 个形象"; dhStat.className = "dh-stat ok"; }
        });
      } catch (err) {
        btnUp.textContent = old;
        dropEl.classList.remove("busy");
        btnUp.disabled = false;
        closeModal();
        toast(err.message || "上传失败");
      }
    };
  };

  document.getElementById("btnGenDH").onclick = async () => {
    if (!_dhState.avatar_id) { toast("请先选择要使用的形象"); return; }
    try {
      const r = await genWithProgress("/digital_human/generate",
        { audio_id: audio, avatar_id: _dhState.avatar_id }, "正在生成数字人视频...");
      STATE.video_id = r.video_id;
      renderDH(r);
      // 新生成后补进历史；row 末尾的"查看历史记录"按钮由 renderDH 一起渲染
      _dhState.history = [r, ...(_dhState.history || [])];
    } catch (e) {}
  };

  loadAvatars();

  async function loadAvatars() {
    const box = document.getElementById("avatarList");
    try {
      const list = await API.get("/avatars");
      const dhStat = document.getElementById("dhStat");
      if (!list.length) {
        box.innerHTML = `<div class="empty-tip" style="display:flex;align-items:center;justify-content:center;gap:6px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:15px;height:15px"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>暂无形象，请先上传驱动视频</div>`;
        dhStat.textContent = "待上传形象"; dhStat.className = "dh-stat warn";
        document.getElementById("btnGenDH").disabled = true;
        return;
      }
      box.innerHTML = list.map(a => `
        <div class="avatar-item ${a.id === _dhState.avatar_id ? "on" : ""}" data-id="${a.id}">
          <video src="${a.url}" muted playsinline preload="metadata"></video>
          <div class="name">${a.name}</div>
          <button class="del" data-id="${a.id}" title="删除">×</button>
        </div>`).join("");

      box.querySelectorAll(".avatar-item").forEach(item => {
        item.onclick = (e) => {
          if (e.target.classList.contains("del")) return;
          _dhState.avatar_id = +item.dataset.id;
          box.querySelectorAll(".avatar-item").forEach(x => x.classList.remove("on"));
          item.classList.add("on");
          document.getElementById("btnGenDH").disabled = false;
          const dhStat = document.getElementById("dhStat");
          dhStat.textContent = "已选 1 个形象"; dhStat.className = "dh-stat ok";
        };
      });

      // 徽标文案：根据当前是否仍持有有效选择决定，避免切页后被默认覆盖回"已就绪"
      const hasSel = list.some(a => +a.id === +_dhState.avatar_id);
      document.getElementById("btnGenDH").disabled = !hasSel && list.length === 0;
      if (hasSel) {
        dhStat.textContent = "已选 1 个形象";
        dhStat.className = "dh-stat ok";
      } else {
        dhStat.textContent = `已就绪 ${list.length} 个形象`;
        dhStat.className = "dh-stat ok";
      }
      box.querySelectorAll(".del").forEach(btn => {
        btn.onclick = async (e) => {
          e.stopPropagation();
          const aid = +btn.dataset.id;
          if (!confirm("确定删除这个形象？")) return;
          try {
            await API.delete("/avatars/" + aid);
            if (_dhState.avatar_id === aid) _dhState.avatar_id = 0;
            toast("已删除");
            loadAvatars();
          } catch (err) { toast(err.message); }
        };
      });
    } catch (e) { box.innerHTML = `<div class="empty-tip">加载失败，请刷新重试</div>`; }
  }

  function dhMediaHTML(r, cls = "media") {
    if (r.video_url) return `<video class="${cls} auto-pip" controls preload="metadata" src="${r.video_url}"></video>`;
    if (r.poster_url) return `<img class="${cls}" src="${r.poster_url}" /><div class="muted">（本环境无 ffmpeg，以静态形象+配音预览；上云可生成真实视频）</div>`;
    return `<div class="muted" style="padding:12px 0;text-align:center;display:flex;align-items:center;justify-content:center;gap:6px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:15px;height:15px"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>未产出可播放视频</div>`;
  }

  function renderDH(r) {
    document.getElementById("dhPreviewCurrent").innerHTML = `
      <div class="dh-current">
        ${dhMediaHTML(r)}
        <div class="row" style="margin-top:10px">
          <span class="dh-tag current">本次生成</span>
          <button class="btn sm" id="btnToEdit">满意 去剪辑</button>
          <button class="btn ghost sm" id="btnDHHistory" style="margin-left:auto">查看历史记录</button>
        </div>
      </div>`;
    document.getElementById("btnToEdit").onclick = () => go(`#/editing?vid=${r.video_id}`);
    const hisBtn = document.getElementById("btnDHHistory");
    if (hisBtn) hisBtn.onclick = openDHHistoryModal;
  }

  function dhHistoryItemHTML(r) {
    return `
      <div class="dh-history-item">
        <div class="dh-h-media">${dhMediaHTML(r, "dh-h-video")}</div>
        <div class="dh-h-info">
          <div class="dh-h-title">${r.script_title ? esc(r.script_title) : "未关联文案"}</div>
          <div class="dh-h-meta">${String(r.created_at || "").slice(0, 16)} · ${r.status || "done"}</div>
          <div class="row" style="margin-top:6px">
            <button class="btn sm" onclick="go('#/editing?vid=${r.video_id}')">去剪辑</button>
            <button class="dh-del" data-del="${r.video_id}" title="删除该视频">删除</button>
          </div>
        </div>
      </div>`;
  }

  async function deleteVideo(id) {
    if (!confirm("确定删除该视频？将同时删除它名下的所有剪辑成品，删除后不可恢复。")) return;
    try {
      await API.req("/videos/" + id, { method: "DELETE" });
      toast("已删除");
      await loadExistingDH();   // 必须等 /videos 拉完、_dhState.history 更新后再重渲，否则弹窗用旧数据渲染，删除项"假不消失"
      // 若历史弹窗开着，用刷新后的 _dhState.history 重渲，使列表即时更新
      const modalEl = document.getElementById("modal");
      if (modalEl && !modalEl.classList.contains("hidden")) openDHHistoryModal();
    } catch (e2) { toast(e2.message || "删除失败"); }
  }

  function openDHHistoryModal() {
    const list = _dhState.history || [];
    const body = list.length
      ? list.map(dhHistoryItemHTML).join("")
      : `<div class="muted" style="padding:20px 0;text-align:center">暂无历史记录</div>`;
    modal("历史生成记录", `<div class="dh-history-list">${body}</div>`,
      `<button class="btn ghost" onclick="closeModal()">关闭</button>`);
    // 绑历史项删除按钮（deleteVideo 非全局，需手动绑）
    document.querySelectorAll("#modalBody .dh-del").forEach(b => {
      b.onclick = () => deleteVideo(+b.dataset.del);
    });
  }

  // 页面一进来就拉"全部历史口播视频"回显：左侧导航直接点数字人也能看到记录
  async function loadExistingDH() {
    try {
      const list = await API.get(`/videos`);
      if (!list || !list.length) {
        document.getElementById("dhPreviewCurrent").innerHTML =
          `<div class="muted" style="padding:24px 0;text-align:center">暂无口播视频记录，点击左侧生成</div>`;
        return;
      }
      // 有 sid 时，优先把属于当前文案的最新一条放到 current 区显示为"上次生成"
      const current = sid ? list.find(r => String(r.script_id) === String(sid)) : null;
      if (current) {
        document.getElementById("dhPreviewCurrent").innerHTML = `
          <div class="dh-current">
            ${dhMediaHTML(current)}
            <div class="row" style="margin-top:10px">
              <span class="dh-tag last">上次生成 · ${String(current.created_at || "").slice(0, 16)}</span>
              <button class="btn sm" id="btnToEditLast">去剪辑</button>
              <button class="btn ghost sm" id="btnDHHistory" style="margin-left:auto">查看历史记录</button>
            </div>
          </div>`;
        document.getElementById("btnToEditLast").onclick = () => go(`#/editing?vid=${current.video_id}`);
        STATE.video_id = current.video_id;
      }
      // 历史列表：全部记录存起来，只通过 row 末尾"查看历史记录"按钮弹窗查看（不在此平铺）
      const history = current ? list.filter(r => r.video_id !== current.video_id) : list;
      _dhState.history = history;
      const btn = document.getElementById("btnDHHistory");
      if (btn) {
        btn.style.display = history.length ? "" : "none";
        btn.onclick = openDHHistoryModal;
      } else if (history.length) {
        // 没有"上次"时（首次进来无 sid 视频），按钮不显示即可
      }
    } catch (e) {
      document.getElementById("dhPreviewCurrent").innerHTML =
        `<div class="muted" style="padding:24px 0;text-align:center">加载记录失败，请刷新重试</div>`;
    }
  }

  loadExistingDH();
}

// ===== 剪辑页 =====
async function pageEditing(app) {
  const { vid } = qp();
  STATE.video_id = vid;
  // 剪辑效果开关。定版（老板 2026-09-03）：
  // - 默认只勾选"自动调色"，其余 3 项需用户手动开启
  // - "网感大字"不勾选 = 普通字幕（底部小字），勾选 = 爆款标题大字
  const opts = { color: true, bigtext: false, mg: false, bgm: false };
  const fxOpts = [
    { k: "color", label: "自动调色", desc: "智能色彩增强", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4a7 7 0 0 0-7 7c0 3 2 4 3 4h2a2 2 0 0 0 2-2V9a2 2 0 0 1 2-2 2 2 0 0 1 2 2 2 2 0 0 0 4 0 7 7 0 0 0-8-7z"/><circle cx="7.5" cy="11" r="1"/><circle cx="10" cy="8" r="1"/></svg>' },
    { k: "bigtext", label: "网感大字", desc: "爆款标题字幕", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M6 4v16M18 4v16M6 12h12"/></svg>' },
    { k: "mg", label: "MG动画", desc: "图形动态包装", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4l1.6 4.4L18 10l-4.4 1.6L12 16l-1.6-4.4L6 10l4.4-1.6z"/></svg>' },
    { k: "bgm", label: "背景音乐", desc: "AI 配乐匹配", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="7" cy="17" r="2.4"/><circle cx="17" cy="15" r="2.4"/><path d="M9.4 17V6l9.6-2v11"/></svg>' }
  ];
  app.innerHTML = `<div class="wrap">
    <div class="card"><h2>自动剪辑</h2><div class="sub">一键应用以下智能剪辑效果</div>
      <div class="fx-grid" id="optList">
        ${fxOpts.map(o => `
          <div class="fx-card on" data-k="${o.k}">
            <div class="fx-top">
              <span class="fx-icon">${o.icon}</span>
              <input type="checkbox" id="op_${o.k}" ${opts[o.k] ? "checked" : ""} class="fx-cb" />
            </div>
            <div class="fx-title">${o.label}</div>
            <div class="fx-desc">${o.desc}</div>
          </div>`).join("")}
      </div>
      <button class="btn" style="margin-top:18px" id="btnEdit">开始剪辑</button>
      <div id="editResult" style="margin-top:16px"></div>
    </div>
  </div>`;
  bindLogout();
  document.querySelectorAll("#optList .fx-card").forEach(card => {
    const cb = card.querySelector(".fx-cb");
    const toggle = () => {
      cb.checked = !cb.checked;
      card.classList.toggle("on", cb.checked);
    };
    card.onclick = e => { if (e.target !== cb) toggle(); };
    cb.onchange = () => card.classList.toggle("on", cb.checked);
  });
  document.getElementById("btnEdit").onclick = async () => {
    const o = {
      color: op_color.checked, bigtext: op_bigtext.checked,
      mg: op_mg.checked, bgm: op_bgm.checked
    };
    try {
      const r = await genWithProgress("/editing/generate",
        { video_id: vid, color: o.color, bigtext: o.bigtext, mg: o.mg, bgm: o.bgm }, "正在智能剪辑...");
      STATE.edit_id = r.edit_id;
      document.getElementById("editResult").innerHTML = renderEditCard(r, "本次生成");
      bindEditResult(r);
    } catch (e) {}
  };
  // 切走再回来：回显上次剪辑产物
  loadExistingEdits(vid);
}

// 渲染剪辑结果卡片（本次/上次 共用）
function renderEditCard(r, tag) {
  const media = r.video_url
    ? `<video class="media auto-pip" controls preload="metadata" src="${r.video_url}" onerror="this.style.display='none';this.nextElementSibling.style.display='block'"></video>
       <div class="muted edit-video-err" style="display:none;padding:12px 0">视频加载失败，可能文件已被清理或编码不兼容</div>`
    : `<img class="media" src="${r.poster_url}" /><div class="muted">（无 ffmpeg，以静态预览；上云生成真实剪辑视频）</div>`;
  const note = r.note
    ? `<div class="edit-note ${r.note.includes("已生成") ? "ok" : "warn"}">${r.note}</div>`
    : `<div class="edit-note ok">剪辑完成</div>`;
  const cmp = r.source_url
    ? `<button class="btn sm ghost" id="btnCmp">对比原片</button>
       <div id="cmpBox" style="margin-top:10px;display:none"><div class="muted" style="margin-bottom:6px">原片</div>
         <video class="media auto-pip" controls preload="metadata" src="${r.source_url}"></video></div>`
    : "";
  return `<div class="card" style="margin:0">
    <div class="edit-tag">${tag}</div>
    <h2 style="font-size:16px">剪辑完成</h2>${media}${note}${cmp}
    <button class="btn sm" id="btnToCover">前往生成标题封面</button></div>`;
}

function bindEditResult(r) {
  const cmpBtn = document.getElementById("btnCmp");
  if (cmpBtn) cmpBtn.onclick = () => {
    const box = document.getElementById("cmpBox");
    box.style.display = box.style.display === "none" ? "block" : "none";
  };
  const tc = document.getElementById("btnToCover");
  if (tc) tc.onclick = () => go(`#/cover?eid=${r.edit_id}`);
}

async function loadExistingEdits(vid) {
  const box = document.getElementById("editResult");
  if (!box) return;
  try {
    const d = await API.get("/videos/" + vid + "/edits");
    if (d.edits && d.edits.length) {
      const r = d.edits[0];   // 最新一条
      box.innerHTML = renderEditCard(r, "上次生成");
      bindEditResult(r);
    } else {
      box.innerHTML = `<div class="card" style="margin:0"><div class="muted" style="padding:12px 0">暂无该视频的剪辑记录</div></div>`;
    }
  } catch (e) {
    box.innerHTML = `<div class="card" style="margin:0"><div class="muted" style="padding:12px 0">加载剪辑记录失败：${esc(e.message)}</div>
      <button class="btn sm" onclick="loadExistingEdits('${vid}')">重试</button></div>`;
  }
}

// ===== 封面页 =====
async function pageCover(app) {
  const { eid } = qp();
  STATE.edit_id = eid;
  let styles = ["大字标题型", "对比型", "悬念型", "表情包型"];
  let title = "";
  try { styles = (await API.get("/covers/styles")).styles; } catch (e) {}
  // 尝试取文案标题，并清洗掉套路前缀（如【揭秘型】老板视角：）
  if (STATE.script_id) {
    try {
      const s = await API.get("/scripts/" + STATE.script_id);
        title = (s.title || "").replace(/^【[^】]+】\s*/, "").replace(/^[^：:]{1,10}[视角][：:]\s*/, "").trim();
      } catch (e) {}
    }
    // 优先用改写阶段生成的封面标题/副标题，没生成过则回退到清洗后的文案标题
    let coverTitle0 = STATE.cover_title || title;
    let coverSub0 = STATE.cover_subtitle || "";
  let sel = styles[0];
  app.innerHTML = `<div class="wrap cols-rewrite">
    <div class="card dh-card">
      <h2>标题封面</h2><div class="sub">选择封面风格，一键生成</div>
      <label>封面风格</label>
      <div class="chips" id="styleChips">${styles.map(s => `<div class="chip ${s === sel ? "on" : ""}" data-s="${s}">${s}</div>`).join("")}</div>
      <label>封面标题</label><input id="coverTitle" value="${esc(coverTitle0)}" />
      <label>副标题（可选）</label><input id="coverSub" value="${esc(coverSub0)}" placeholder="如：老板亲测" />
      <button class="btn" style="margin-top:18px" id="btnCover">生成封面</button>
    </div>

    <div class="card" style="margin-top:0">
      <h2 style="font-size:16px">封面预览</h2>
      <div id="coverResult"><div class="muted" style="padding:24px 0;text-align:center">点击左侧生成封面，结果会出现在这里</div></div>
    </div>
  </div>`;
  bindLogout();
  document.getElementById("styleChips").querySelectorAll(".chip").forEach(c => c.onclick = () => {
    sel = c.dataset.s;
    document.getElementById("styleChips").querySelectorAll(".chip").forEach(x => x.classList.remove("on"));
    c.classList.add("on");
  });
  const renderPreview = (cid, url) => {
    document.getElementById("coverResult").innerHTML = `
      <div style="text-align:center">
        <img class="media" style="max-width:280px;border-radius:12px" src="${url}" />
        <button class="btn sm" style="margin-top:14px" id="btnToPub">满意 前往发布</button>
      </div>`;
    document.getElementById("btnToPub").onclick = () => go(`#/publish?cid=${cid}`);
  };
  // 刷新/切回页面：如果 STATE.edit_id 与当前 eid 一致，且已有 cover_url，自动恢复右侧预览
  if (STATE.edit_id && String(STATE.edit_id) === String(eid) && STATE.cover_url) {
    renderPreview(STATE.cover_id, STATE.cover_url);
  }

  document.getElementById("btnCover").onclick = async () => {
    try {
      const r = await API.postForm("/covers/generate",
        { edit_id: eid, style: sel, title: coverTitle.value, subtitle: coverSub.value });
      STATE.cover_id = r.cover_id;
      STATE.cover_url = r.url;
      saveState();
      renderPreview(r.cover_id, r.url);
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
  let coverTitle = "";
  try { const c = await API.get("/covers/" + cid); coverUrl = c.url || ""; coverTitle = c.title || ""; } catch (e) {}
  app.innerHTML = `<div class="wrap">
    <div class="card"><h2>发布</h2><div class="sub">选择发布平台，一键发布</div>
      <div class="chips" id="pfChips">${platforms.map(p => `<div class="chip ${p === sel ? "on" : ""}" data-p="${p}">${p}</div>`).join("")}</div>
      <div id="coverPrev" style="margin-top:12px">${coverUrl
        ? `<img class="media" style="max-width:240px" src="${coverUrl}" /><div class="muted" style="margin-top:6px">${esc(coverTitle)}</div>`
        : '<span class="muted">封面预览将显示在发布卡片中</span>'}</div>
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
        <div class="card" style="margin:0"><h2 style="font-size:16px;color:var(--ok);display:flex;align-items:center;gap:7px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:17px;height:17px"><polyline points="20 6 9 17 4 12"/></svg>已发布到 ${esc(sel)}</h2>
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
