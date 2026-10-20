const $ = (id) => document.getElementById(id);

const state = { url: "", info: null, tab: "video", busy: false };

function showError(msg) {
  $("error").textContent = msg;
  $("error").hidden = !msg;
}

async function api(path, body) {
  const res = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

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
        box.append(optionRow(o.label, o.size ? `~${o.size}` : "MP4", hdTag(o.height), () => startDownload("video", o.height)))
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

function setProgress(pct, text, speed, indeterminate = false) {
  $("progress").hidden = false;
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
        setProgress(100, "Done! Your download is starting…", "");
        const a = document.createElement("a");
        a.href = `/api/file/${job_id}`;
        a.download = p.filename || "";
        document.body.append(a);
        a.click();
        a.remove();
        break;
      }
      if (p.status === "processing") {
        setProgress(100, kind === "audio" ? "Converting to MP3…" : "Merging video & audio…", "", true);
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
