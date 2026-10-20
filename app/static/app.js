const $ = (id) => document.getElementById(id);

const state = { url: "", info: null, tab: "video", busy: false };

// background glow follows the pointer (one update per frame at most)
(() => {
  const spot = document.querySelector(".spotlight");
  if (!spot || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  let frame = 0;
  addEventListener("pointermove", (e) => {
    if (frame) return;
    frame = requestAnimationFrame(() => {
      spot.style.setProperty("--mx", `${e.clientX}px`);
      spot.style.setProperty("--my", `${e.clientY}px`);
      frame = 0;
    });
  }, { passive: true });
})();

function showError(msg) {
  $("error").textContent = msg;
  $("error").hidden = !msg;
}

const API = (window.API_BASE || "").replace(/\/$/, "");

async function api(path, body) {
  let res;
  try {
    res = await fetch(API + path, {
      method: body ? "POST" : "GET",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new Error("Couldn't reach the download server. Check your connection and try again.");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

// ---------------------------------------------------------------- server wake-up
// The free Render plan sleeps when idle; wake it as soon as the page opens so it's
// usually ready by the time someone pastes a link.
const server = { ready: false, waking: null };

function setServerStatus(kind, text) {
  const el = $("server-status");
  el.dataset.state = kind;
  el.querySelector(".label").textContent = text;
}

function wakeServer() {
  if (server.ready) return Promise.resolve();
  if (server.waking) return server.waking;
  const slow = setTimeout(() => setServerStatus("waking", "Starting server… (first use can take up to a minute)"), 1500);
  server.waking = (async () => {
    for (let attempt = 0; attempt < 30; attempt++) {
      try {
        const res = await fetch(`${API}/api/health`, { cache: "no-store" });
        if (res.ok) {
          clearTimeout(slow);
          server.ready = true;
          setServerStatus("ready", "Free · No sign-up · No watermark");
          return;
        }
      } catch { /* still waking */ }
      await new Promise((r) => setTimeout(r, 3000));
    }
    clearTimeout(slow);
    setServerStatus("down", "Server is unavailable right now — please try again later");
    server.waking = null;
    throw new Error("The download server didn't respond. Please try again in a minute.");
  })();
  return server.waking;
}

wakeServer().catch(() => {});

// ---------------------------------------------------------------- fetch info
$("url-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = $("url").value.trim();
  if (!url || state.busy) return;

  showError("");
  $("result").hidden = true;
  $("loading").hidden = false;
  $("fetch").disabled = true;

  try {
    if (!server.ready) {
      $("loading").lastChild.textContent = " Waking up the server… this can take up to a minute the first time";
      await wakeServer();
    }
    $("loading").lastChild.textContent = " Fetching video details…";
    state.info = await api("/api/info", { url });
    state.url = url;
    renderInfo();
  } catch (err) {
    showError(err.message);
  } finally {
    $("loading").hidden = true;
    $("fetch").disabled = false;
  }
});

$("paste").addEventListener("click", async () => {
  try {
    $("url").value = (await navigator.clipboard.readText()).trim();
    $("url").focus();
  } catch {
    showError("Clipboard access was blocked — paste the link with Ctrl+V instead.");
  }
});

function renderInfo() {
  const i = state.info;
  $("thumb").src = i.thumbnail || "";
  $("thumb").style.visibility = i.thumbnail ? "visible" : "hidden";
  $("duration").textContent = i.duration || "";
  $("platform").textContent = i.platform;
  $("title").textContent = i.title;
  $("uploader").textContent = i.uploader ? `by ${i.uploader}` : "";
  $("progress").hidden = true;
  $("result").hidden = false;
  renderOptions();
}

// ---------------------------------------------------------------- options
document.querySelectorAll(".tab").forEach((btn) =>
  btn.addEventListener("click", () => {
    if (state.busy) return;
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    state.tab = btn.dataset.tab;
    renderOptions();
  })
);

function optionRow(label, sub, tag, onClick) {
  const row = document.createElement("div");
  row.className = "option";
  const left = document.createElement("div");
  left.innerHTML = `<strong></strong><small></small>${tag ? `<span class="tag">${tag}</span>` : ""}`;
  left.querySelector("strong").textContent = label;
  left.querySelector("small").textContent = sub || "";
  const btn = document.createElement("button");
  btn.textContent = "Download";
  btn.addEventListener("click", onClick);
  row.append(left, btn);
  return row;
}

function hdTag(h) {
  if (h >= 2160) return "4K";
  if (h >= 1440) return "2K";
  if (h >= 1080) return "Full HD";
  if (h >= 720) return "HD";
  return "";
}

function renderOptions() {
  const box = $("options");
  box.replaceChildren();
  const i = state.info;

  if (state.tab === "video") {
    if (!i.video_options.length) {
      box.append(optionRow("Best available", "MP4", "", () => startDownload("video", null)));
    } else {
      i.video_options.forEach((o) =>
        box.append(optionRow(
          o.label,
          o.converted ? "MP4 · smaller file" : o.size ? `~${o.size}` : "MP4",
          hdTag(o.height),
          () => startDownload("video", o.height)
        ))
      );
    }
  } else {
    i.audio_options.forEach((o) =>
      box.append(optionRow(o.label, o.bitrate === 320 ? "Best" : "", "", () => startDownload("audio", o.bitrate || null)))
    );
  }
}

// ---------------------------------------------------------------- download
function setBusy(busy) {
  state.busy = busy;
  document.querySelectorAll(".options button, .tab, #fetch").forEach((b) => (b.disabled = busy));
}

function showDone(filename, href, note) {
  setProgress(100, "", "");
  document.querySelector(".bar").classList.add("complete");
  const text = $("progress-text");
  text.replaceChildren();
  const ok = document.createElement("span");
  ok.className = "done-text";
  ok.textContent = `✓ Saved${filename ? `: ${filename}` : ""} — check your Downloads folder`;
  const again = document.createElement("a");
  again.href = href;
  again.download = filename || "";
  again.className = "again";
  again.textContent = "Download again";
  text.append(ok);
  if (note) {
    const small = document.createElement("small");
    small.className = "done-note";
    small.textContent = note;
    text.append(small);
  }
  $("progress-speed").replaceChildren(again);
}

function setProgress(pct, text, speed, indeterminate = false) {
  $("progress").hidden = false;
  document.querySelector(".bar").classList.remove("complete");
  $("bar-fill").style.width = indeterminate ? "" : `${pct}%`;
  document.querySelector(".bar").classList.toggle("indeterminate", indeterminate);
  $("progress-text").textContent = text;
  $("progress-speed").textContent = speed || "";
}

async function startDownload(kind, quality) {
  showError("");
  setBusy(true);
  setProgress(0, "Starting…", "", true);

  try {
    const { job_id } = await api("/api/download", { url: state.url, kind, quality });

    while (true) {
      await new Promise((r) => setTimeout(r, 800));
      const p = await api(`/api/progress/${job_id}`);

      if (p.status === "error") throw new Error(p.error);
      if (p.status === "done") {
        // the file is already complete on the server, so the browser saves it right away
        const href = `${API}/api/file/${job_id}`;
        const a = document.createElement("a");
        a.href = href;
        a.download = p.filename || "";
        document.body.append(a);
        a.click();
        a.remove();
        showDone(p.filename, href, p.note);
        break;
      }
      if (p.status === "processing") {
        const fallback = kind === "audio" ? "Converting to MP3…" : "Merging video & audio…";
        setProgress(100, p.stage || fallback, "", true);
      } else if (p.status === "downloading") {
        const eta = p.eta ? ` · ${p.eta} left` : "";
        setProgress(p.progress, `Downloading ${p.progress.toFixed(0)}%${eta}`, p.speed);
      }
    }
  } catch (err) {
    $("progress").hidden = true;
    showError(err.message);
  } finally {
    setBusy(false);
  }
}
