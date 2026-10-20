// Where the downloader API lives.
// The live page (on Vercel) uses the Render service; anywhere else (local development,
// or the Render copy itself) talks to the same server that served the page.
window.API_BASE = location.hostname === "save.amitmahata.com" ? "https://api.save.amitmahata.com" : "";
