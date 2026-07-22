const state = {
  data: null,
  activeView: "overview"
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options
  });
  const data = await response.json();
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || response.statusText);
  }
  return data;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function formatMode(mode) {
  return String(mode || "review").replaceAll("_", " ");
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (!bytes) return "0 bytes";
  if (bytes < 1024) return `${bytes} bytes`;
  return `${(bytes / 1024).toFixed(1)} KB`;
}

function compileStatusClass(status) {
  return ["compiled", "failed", "invalid", "timeout", "unavailable", "pending"].includes(status)
    ? status
    : "pending";
}

async function loadState() {
  state.data = await api("/api/state");
  render();
}

function render() {
  const data = state.data;
  if (!data) return;

  $("#modeBadge").textContent = formatMode(data.settings.mode);
  $("#latexBadge").textContent = data.latex_engine || "missing";
  $("#docCount").textContent = data.profile.documents.length;
  $("#jobCount").textContent = data.jobs.length;
  $("#appCount").textContent = data.applications.length;
  $("#eventCount").textContent = data.events.length;
  $("#docsPath").textContent = data.paths.docs;
  $("#dbPath").textContent = data.paths.database;
  $("#jobQueueMeta").textContent = `${data.jobs.length} tracked`;

  renderDocs(data.profile.documents);
  renderStructuredProfile(data.profile.structured || {});
  renderEvents("#recentEvents", data.events.slice(0, 8));
  renderEvents("#logsList", data.events);
  renderJobs(data.jobs);
  renderApplications(data.applications);
  renderRules(data.answer_rules || []);
  populateSettings(data.settings);
}

function renderDocs(docs) {
  const target = $("#docsList");
  if (!docs.length) {
    target.innerHTML = `<div class="empty">Add PDF, DOCX, or text files to docs/, then ingest.</div>`;
    return;
  }
  target.innerHTML = docs.map((doc) => `
    <div class="doc ${doc.ingest_status === "error" ? "document-error" : ""}">
      <div class="doc-heading">
        <strong>${escapeHtml(doc.name)}</strong>
        <span class="status">${escapeHtml(doc.kind || "source")}</span>
      </div>
      <p class="meta mono">${escapeHtml(doc.path)}</p>
      ${doc.ingest_status === "error"
        ? `<p class="error-text">${escapeHtml(doc.ingest_error || "Extraction failed")}</p>`
        : `<p>${escapeHtml(doc.summary || "")}</p>`}
      <p class="meta">${escapeHtml(doc.extractor || "unknown extractor")} · ${Number(doc.size_bytes || 0).toLocaleString()} bytes</p>
    </div>
  `).join("");
}

function renderStructuredProfile(profile) {
  const target = $("#structuredProfile");
  const contact = profile.contact || {};
  const groups = [
    ["Contact", [...(contact.emails || []), ...(contact.phones || []), ...(contact.links || [])]],
    ["Skills", profile.skills || []],
    ["Education", profile.education || []],
    ["Certifications", profile.certifications || []],
    ["Evidence highlights", profile.highlights || []]
  ];
  const hasContent = profile.name || groups.some(([, values]) => values.length);
  if (!hasContent) {
    target.innerHTML = `<div class="empty">Ingest a resume, transcript, or portfolio to build your profile.</div>`;
    return;
  }
  target.innerHTML = `
    ${profile.name ? `<div class="profile-name"><span>Candidate</span><strong>${escapeHtml(profile.name)}</strong></div>` : ""}
    ${groups.map(([label, values]) => `
      <section class="profile-group">
        <h4>${escapeHtml(label)}</h4>
        ${values.length
          ? `<ul>${values.map((value) => `<li>${escapeHtml(value)}</li>`).join("")}</ul>`
          : `<p class="meta">No evidence found.</p>`}
      </section>
    `).join("")}
  `;
}

function renderEvents(selector, events) {
  const target = $(selector);
  if (!events.length) {
    target.innerHTML = `<div class="empty">No activity yet.</div>`;
    return;
  }
  target.innerHTML = events.map((event) => `
    <div class="event ${escapeHtml(event.level)}">
      <p>${escapeHtml(event.message)}</p>
      <span class="meta">${escapeHtml(event.created_at)} · ${escapeHtml(event.level)}</span>
    </div>
  `).join("");
}

function renderJobs(jobs) {
  const target = $("#jobsList");
  if (!jobs.length) {
    target.innerHTML = `<div class="empty">Add a job manually or configure career URLs and scan.</div>`;
    return;
  }
  target.innerHTML = jobs.map((job) => `
    <article class="job-card">
      <div class="card-head">
        <div>
          <h4>${escapeHtml(job.title)}</h4>
          <p class="meta">${escapeHtml(job.company)} ${job.location ? "· " + escapeHtml(job.location) : ""}</p>
        </div>
        <span class="status">${escapeHtml(job.status)}</span>
      </div>
      <p>${escapeHtml((job.description || "").slice(0, 260))}</p>
      <p class="meta">Score <span class="score">${Number(job.score || 0)}</span> · ${escapeHtml(job.source)}</p>
      <div class="card-actions">
        <button data-action="draft" data-job="${job.id}">Draft package</button>
        <a href="${escapeHtml(job.url)}" target="_blank" rel="noreferrer"><button class="secondary">Open</button></a>
      </div>
    </article>
  `).join("");
}

function renderApplications(apps) {
  const target = $("#applicationsList");
  if (!apps.length) {
    target.innerHTML = `<div class="empty">No application packages drafted yet.</div>`;
    return;
  }
  target.innerHTML = apps.map((app) => {
    const compileStatus = String(app.resume_compile_status || "pending");
    const compileMeta = compileStatus === "compiled"
      ? `${Number(app.resume_pdf_pages || 0)} page${Number(app.resume_pdf_pages || 0) === 1 ? "" : "s"} · ${formatBytes(app.resume_pdf_bytes)} · ${escapeHtml(app.resume_compile_engine)}`
      : escapeHtml(app.resume_compile_message || "PDF has not been compiled yet.");
    const artifactBase = `/api/applications/artifact?application_id=${encodeURIComponent(app.id)}`;
    const compilerDetails = compileStatus !== "compiled" && app.resume_compile_log
      ? `<details class="compile-log"><summary>Compiler details</summary><pre>${escapeHtml(app.resume_compile_log)}</pre></details>`
      : "";
    return `
    <article class="app-card">
      <div class="card-head">
        <div>
          <h4>${escapeHtml(app.title)}</h4>
          <p class="meta">${escapeHtml(app.company)} · ${escapeHtml(formatMode(app.mode))}</p>
        </div>
        <span class="status">${escapeHtml(app.status)}</span>
      </div>
      <div class="compile-summary">
        <div>
          <strong>Tailored resume</strong>
          <p class="meta">${compileMeta}</p>
        </div>
        <span class="status compile-status ${compileStatusClass(compileStatus)}">${escapeHtml(compileStatus)}</span>
      </div>
      ${compilerDetails}
      <p>${escapeHtml((app.cover_letter || "").slice(0, 320))}</p>
      <div class="card-actions">
        ${app.resume_pdf_path ? `<a class="button-link" href="${artifactBase}&kind=pdf" target="_blank" rel="noreferrer">Open PDF</a>` : ""}
        ${app.resume_tex_path ? `<a class="button-link secondary" href="${artifactBase}&kind=tex">Download LaTeX</a>` : ""}
        <button data-action="compile" data-app="${app.id}" class="secondary">Recompile PDF</button>
        <button data-action="approve" data-app="${app.id}">Approve</button>
        <button data-action="apply" data-app="${app.id}" class="warn">Apply</button>
        <button data-action="submitted" data-app="${app.id}" class="secondary">Mark submitted</button>
      </div>
    </article>
  `;
  }).join("");
}

function renderRules(rules) {
  const target = $("#rulesList");
  if (!target) return;
  if (!rules.length) {
    target.innerHTML = `<div class="empty">No saved answer rules yet.</div>`;
    return;
  }
  target.innerHTML = rules.map((rule) => `
    <div class="doc">
      <strong>${escapeHtml(rule.question)}</strong>
      <p>${escapeHtml(rule.answer)}</p>
      <p class="meta">${rule.risky ? "Risky field" : "General"} · ${escapeHtml(rule.updated_at)}</p>
    </div>
  `).join("");
}

function populateSettings(settings) {
  const form = $("#settingsForm");
  for (const [key, value] of Object.entries(settings)) {
    const field = form.elements[key];
    if (field) field.value = value;
  }
}

function setView(view) {
  state.activeView = view;
  $$(".view").forEach((el) => el.classList.toggle("active", el.id === view));
  $$(".nav-item").forEach((el) => el.classList.toggle("active", el.dataset.view === view));
  const titles = {
    overview: ["Overview", "Track documents, jobs, applications, and agent actions."],
    jobs: ["Jobs", "Add postings, scan configured career pages, and draft packages."],
    applications: ["Applications", "Approve, submit, or queue browser automation."],
    settings: ["Settings", "Tune modes, limits, target companies, and source URLs."],
    logs: ["Logs", "Plain-English worker activity and blockers."]
  };
  $("#viewTitle").textContent = titles[view][0];
  $("#viewSubtitle").textContent = titles[view][1];
}

async function handleAction(action, button) {
  button.disabled = true;
  try {
    if (action === "draft") {
      await api("/api/applications/draft", {
        method: "POST",
        body: JSON.stringify({ job_id: Number(button.dataset.job) })
      });
      toast("Application package drafted.");
      setView("applications");
    } else if (action === "approve") {
      await api("/api/applications/approve", {
        method: "POST",
        body: JSON.stringify({ application_id: Number(button.dataset.app) })
      });
      toast("Application approved.");
    } else if (action === "submitted") {
      await api("/api/applications/submit", {
        method: "POST",
        body: JSON.stringify({ application_id: Number(button.dataset.app) })
      });
      toast("Application marked submitted.");
    } else if (action === "apply") {
      const result = await api("/api/applications/apply", {
        method: "POST",
        body: JSON.stringify({ application_id: Number(button.dataset.app) })
      });
      toast(result.message || "Apply action queued.");
    } else if (action === "compile") {
      const result = await api("/api/applications/compile", {
        method: "POST",
        body: JSON.stringify({ application_id: Number(button.dataset.app) })
      });
      toast(result.resume_compile_message || "Resume compilation finished.");
    }
    await loadState();
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}

function bindEvents() {
  $$(".nav-item").forEach((button) => {
    button.addEventListener("click", () => setView(button.dataset.view));
  });

  $("#refreshBtn").addEventListener("click", loadState);

  $("#ingestBtn").addEventListener("click", async () => {
    await api("/api/docs/ingest", { method: "POST", body: "{}" });
    toast("Documents ingested.");
    await loadState();
  });

  $("#scanBtn").addEventListener("click", async () => {
    const result = await api("/api/jobs/scan", { method: "POST", body: "{}" });
    toast(`Scan complete: ${result.inserted} new, ${result.seen} seen.`);
    await loadState();
  });

  $("#jobForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
    await api("/api/jobs", { method: "POST", body: JSON.stringify(payload) });
    event.currentTarget.reset();
    toast("Job saved.");
    await loadState();
  });

  $("#settingsForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
    await api("/api/settings", { method: "POST", body: JSON.stringify(payload) });
    toast("Settings saved.");
    await loadState();
  });

  $("#ruleForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
    await api("/api/rules", { method: "POST", body: JSON.stringify(payload) });
    event.currentTarget.reset();
    toast("Answer rule saved.");
    await loadState();
  });

  document.body.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (button) handleAction(button.dataset.action, button);
  });
}

bindEvents();
loadState().catch((error) => toast(error.message));
