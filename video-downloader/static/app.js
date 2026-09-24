const $ = (id) => document.getElementById(id);

const state = {
  url: "",
  mode: "video",
  quality: "best",
  audioFormat: "mp3",
  bitrate: "320",
  polling: null,
};

/* ───────── الوضع الليلي ───────── */
const root = document.documentElement;
function applyTheme(t) {
  root.dataset.theme = t;
  $("themeToggle").textContent = t === "dark" ? "☀️" : "🌙";
}
let savedTheme = null;
try { savedTheme = localStorage.getItem("theme"); } catch {}
applyTheme(savedTheme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
$("themeToggle").onclick = () => {
  const t = root.dataset.theme === "dark" ? "light" : "dark";
  applyTheme(t);
  try { localStorage.setItem("theme", t); } catch {}
};

/* ───────── أدوات ───────── */
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove("show"), 2400);
}
function showError(el, msg) { el.textContent = msg; el.hidden = !msg; }
function fmtDuration(s) {
  if (!s) return "";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}
function fmtBytes(b) {
  if (!b) return "";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(1)} ${u[i]}/s`;
}
function fmtEta(s) {
  if (s == null) return "";
  return s < 60 ? `${s}s left` : `${Math.floor(s / 60)}m ${s % 60}s left`;
}
function qualityLabel(h) {
  if (h >= 2160) return ["4K", "UHD"];
  if (h >= 1440) return ["2K", "QHD"];
  if (h >= 1080) return ["1080p", "FHD"];
  if (h >= 720) return ["720p", "HD"];
  return [`${h}p`, ""];
}
function setLoading(btn, on) {
  btn.disabled = on;
  const sp = btn.querySelector(".spinner");
  if (sp) sp.hidden = !on;
}

/* ───────── لصق ───────── */
$("pasteBtn").onclick = async () => {
  try {
    const text = await navigator.clipboard.readText();
    if (text) { $("urlInput").value = text.trim(); toast("تم اللصق 📋"); }
  } catch {
    toast("اسمح بالوصول للحافظة أو الصق يدوياً");
    $("urlInput").focus();
  }
};

/* ───────── جلب معلومات المقطع ───────── */
$("urlForm").onsubmit = async (e) => {
  e.preventDefault();
  const url = $("urlInput").value.trim();
  if (!url) return;
  showError($("fetchError"), "");
  setLoading($("fetchBtn"), true);
  $("optionsCard").hidden = true;
  try {
    const res = await fetch("/api/info", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "صار خطأ");
    state.url = url;
    renderInfo(data);
  } catch (err) {
    showError($("fetchError"), err.message);
  } finally {
    setLoading($("fetchBtn"), false);
  }
};

function renderInfo(info) {
  resetDownloadUI();
  $("title").textContent = info.title;
  $("uploader").textContent = info.uploader ? `👤 ${info.uploader}` : "";
  $("source").textContent = info.source || "";
  $("duration").textContent = fmtDuration(info.duration);
  const img = $("thumb");
  img.hidden = !info.thumbnail;
  img.onerror = () => { img.hidden = true; img.parentElement.classList.add("empty"); };
  if (info.thumbnail) img.src = info.thumbnail;
  img.parentElement.classList.toggle("empty", !info.thumbnail);

  // الجودات المتاحة
  const box = $("qualities");
  box.innerHTML = "";
  const addChip = (val, html, active) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "chip" + (active ? " active" : "");
    b.dataset.val = val;
    b.innerHTML = html;
    box.appendChild(b);
  };
  const heights = info.heights || [];
  const top = heights[0];
  addChip("best", `أعلى جودة${top ? ` <small>${qualityLabel(top)[0]}</small>` : ""} <span class="tag">✨</span>`, true);
  heights.forEach((h) => {
    const [name, tag] = qualityLabel(h);
    addChip(String(h), `${name}${tag ? ` <small>${tag}</small>` : ""}`, false);
  });
  state.quality = "best";

  // إذا المقطع صوتي فقط نخفي خيارات الفيديو
  document.querySelectorAll(".mode").forEach((m) => {
    m.hidden = !info.has_video && m.dataset.mode !== "audio";
  });
  selectMode(info.has_video ? "video" : "audio");

  $("optionsCard").hidden = false;
  $("optionsCard").scrollIntoView({ behavior: "smooth", block: "start" });
}

/* ───────── اختيار الوضع والخيارات ───────── */
function selectMode(mode) {
  state.mode = mode;
  document.querySelectorAll(".mode").forEach((m) => {
    const on = m.dataset.mode === mode;
    m.classList.toggle("active", on);
    m.setAttribute("aria-checked", on);
  });
  $("videoOpts").hidden = mode === "audio";
  $("audioOpts").hidden = mode !== "audio";
}
document.querySelectorAll(".mode").forEach((m) => (m.onclick = () => selectMode(m.dataset.mode)));

function chipGroup(containerId, key, after) {
  $(containerId).addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    $(containerId).querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    state[key] = chip.dataset.val;
    after && after();
  });
}
chipGroup("qualities", "quality");
chipGroup("bitrates", "bitrate");
chipGroup("audioFormats", "audioFormat", () => {
  // WAV بدون ضغط فما يحتاج اختيار معدل البت
  $("bitrateWrap").hidden = state.audioFormat === "wav";
});

/* ───────── التحميل ───────── */
function resetDownloadUI() {
  clearInterval(state.polling);
  $("progressBox").hidden = true;
  $("doneBox").hidden = true;
  $("downloadBtn").hidden = false;
  $("downloadBtn").disabled = false;
  showError($("dlError"), "");
  setProgress(0, "جاري التحميل…");
}

function setProgress(p, stage, speed, eta) {
  $("barFill").style.width = `${p}%`;
  $("percent").textContent = `${Math.round(p)}%`;
  $("stage").textContent = stage;
  $("speed").textContent = fmtBytes(speed);
  $("eta").textContent = fmtEta(eta);
}

$("downloadBtn").onclick = async () => {
  resetDownloadUI();
  $("downloadBtn").disabled = true;
  $("progressBox").hidden = false;
  setProgress(0, "نبدأ… 🚀");
  try {
    const res = await fetch("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: state.url,
        mode: state.mode,
        quality: state.quality,
        audio_format: state.audioFormat,
        bitrate: state.bitrate,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "صار خطأ");
    $("downloadBtn").hidden = true;
    poll(data.job_id);
  } catch (err) {
    $("progressBox").hidden = true;
    $("downloadBtn").disabled = false;
    showError($("dlError"), err.message);
  }
};

function poll(jobId) {
  clearInterval(state.polling);
  state.polling = setInterval(async () => {
    let job;
    try {
      const res = await fetch(`/api/progress/${jobId}`);
      job = await res.json();
      if (!res.ok) throw new Error(job.error);
    } catch (err) {
      clearInterval(state.polling);
      showError($("dlError"), err.message || "انقطع الاتصال بالخادم");
      $("downloadBtn").hidden = false;
      $("downloadBtn").disabled = false;
      return;
    }
    setProgress(job.percent || 0, job.stage, job.speed, job.eta);

    if (job.status === "done") {
      clearInterval(state.polling);
      const href = `/api/file/${jobId}`;
      $("saveLink").href = href;
      $("progressBox").hidden = true;
      $("doneBox").hidden = false;
      toast("تم! 🎉");
      const a = document.createElement("a");
      a.href = href;
      document.body.appendChild(a);
      a.click();
      a.remove();
    } else if (job.status === "error") {
      clearInterval(state.polling);
      $("progressBox").hidden = true;
      $("downloadBtn").hidden = false;
      $("downloadBtn").disabled = false;
      showError($("dlError"), job.error || "صار خطأ أثناء التحميل");
    }
  }, 700);
}

$("againBtn").onclick = () => {
  resetDownloadUI();
  $("optionsCard").hidden = true;
  $("urlInput").value = "";
  $("urlInput").focus();
  window.scrollTo({ top: 0, behavior: "smooth" });
};
