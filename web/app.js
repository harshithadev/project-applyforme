const state = {
  data: null,
  activeView: "overview",
  writingPoll: null
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

function formatJobDate(value) {
  if (!value) return "Date not listed";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "Date not listed" : `Posted/updated ${parsed.toLocaleDateString()}`;
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
  $("#codexBadge").textContent = data.codex?.ready ? "ChatGPT ready" : "Unavailable";
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
  scheduleWritingRefresh(data.applications);
}

function scheduleWritingRefresh(applications) {
  clearTimeout(state.writingPoll);
  const active = applications.some((app) => ["queued", "running"].includes(app.writing?.task?.status));
  state.writingPoll = active ? setTimeout(() => loadState().catch((error) => toast(error.message)), 3000) : null;
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
      <p class="job-facts">${escapeHtml(job.location || "Location not listed")} · ${escapeHtml(formatJobDate(job.posted_at))}</p>
      <p>${escapeHtml((job.description || "").slice(0, 420))}</p>
      <div class="match-reasons">
        ${(job.match_reasons || []).map((reason) => `<span>${escapeHtml(reason)}</span>`).join("")}
      </div>
      <p class="meta">Match <span class="score">${Number(job.score || 0)}</span> · ${escapeHtml(job.source)}</p>
      <div class="card-actions">
        <button data-action="draft" data-job="${job.id}">Draft package</button>
        <a class="button-link secondary" href="${escapeHtml(job.url)}" target="_blank" rel="noreferrer">Open posting</a>
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
    const writing = app.writing || {};
    const current = writing.current || {};
    const content = current.content || {};
    const resume = content.resume || {};
    const email = content.email || {};
    const validation = current.validation || {};
    const evidence = new Map((current.evidence || []).map((item) => [String(item.id), item]));
    const referencedEvidence = [...new Set((content.claims || []).flatMap((claim) => claim.evidence_ids || []))]
      .map((id) => evidence.get(String(id)))
      .filter(Boolean);
    const taskActive = ["queued", "running"].includes(writing.task?.status);
    const versionRows = (writing.versions || []).map((version) => `
      <div class="version-row">
        <span>v${version.version} · ${escapeHtml(version.origin)} · ${escapeHtml(version.validation?.status || version.status)}</span>
        ${Number(version.id) === Number(current.id)
          ? `<strong>Current</strong>`
          : `<button class="secondary compact-btn" data-action="writing-activate" data-app="${app.id}" data-version="${version.id}" ${app.status === "submitted" ? "disabled" : ""}>Use</button>`}
      </div>
    `).join("");
    const validationMessages = [...(validation.errors || []), ...(validation.warnings || [])];
    const writingWorkspace = current.id ? `
      <details class="writing-workspace">
        <summary>
          <span>Writing workspace · v${current.version}</span>
          <span class="status validation-status ${escapeHtml(validation.status || "pending")}">${escapeHtml(validation.status || "pending")}</span>
        </summary>
        <div class="writing-status-line">
          <span>${escapeHtml(writing.message || "Draft ready for review.")}</span>
          <span>Evidence ${Number(validation.evidence_references || 0)} · Keywords ${Number(validation.keyword_coverage || 0)}%</span>
        </div>
        ${validationMessages.length ? `<ul class="validation-messages">${validationMessages.map((message) => `<li>${escapeHtml(message)}</li>`).join("")}</ul>` : ""}
        <div class="writing-grid">
          <label>Resume summary
            <textarea data-writing-field="summary">${escapeHtml(resume.summary || "")}</textarea>
          </label>
          <div class="writing-field full-width">
            <span class="label">Resume bullets</span>
            <div class="writing-bullets">
              ${(resume.bullets || []).map((bullet, index) => `
                <label>Bullet ${index + 1} · ${escapeHtml((bullet.evidence_ids || []).join(", "))}
                  <textarea data-writing-bullet="${index}">${escapeHtml(bullet.text || "")}</textarea>
                </label>
              `).join("")}
            </div>
          </div>
          <label class="full-width">Cover letter
            <textarea data-writing-field="cover_letter" class="tall-text">${escapeHtml(content.cover_letter || "")}</textarea>
          </label>
          ${(content.statements || []).map((statement, index) => `
            <label class="full-width">${escapeHtml(statement.question || `Statement ${index + 1}`)}
              <textarea data-writing-statement="${index}">${escapeHtml(statement.answer || "")}</textarea>
            </label>
          `).join("")}
          <label>Email subject
            <input data-writing-field="email_subject" value="${escapeHtml(email.subject || "")}" />
          </label>
          <label class="full-width">Outreach email
            <textarea data-writing-field="email_body">${escapeHtml(email.body || "")}</textarea>
          </label>
        </div>
        <div class="card-actions">
          <button data-action="writing-save" data-app="${app.id}" ${app.status === "submitted" ? "disabled" : ""}>Save new version</button>
          <button data-action="writing-queue" data-app="${app.id}" class="secondary" ${taskActive || !state.data.codex?.ready || app.status === "submitted" ? "disabled" : ""}>Generate with Codex</button>
        </div>
        <details class="evidence-details">
          <summary>Evidence references (${referencedEvidence.length})</summary>
          <div class="evidence-list">
            ${referencedEvidence.map((item) => `<p><strong>${escapeHtml(item.id)}</strong> ${escapeHtml(item.text)} <span>${escapeHtml(item.source)}</span></p>`).join("") || `<p>No evidence references.</p>`}
          </div>
        </details>
        <details class="version-history">
          <summary>Version history (${(writing.versions || []).length})</summary>
          ${versionRows}
        </details>
      </details>
    ` : "";
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
      ${writingWorkspace}
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

function collectWritingContent(button) {
  const applicationId = Number(button.dataset.app);
  const app = state.data.applications.find((item) => Number(item.id) === applicationId);
  const article = button.closest(".app-card");
  const original = app.writing.current.content;
  const content = JSON.parse(JSON.stringify(original));
  const oldSummary = content.resume.summary;
  content.resume.summary = article.querySelector('[data-writing-field="summary"]').value;
  article.querySelectorAll("[data-writing-bullet]").forEach((field) => {
    const index = Number(field.dataset.writingBullet);
    const oldText = content.resume.bullets[index].text;
    content.resume.bullets[index].text = field.value;
    content.claims.forEach((claim) => {
      if (claim.text === oldText) claim.text = field.value;
    });
  });
  content.claims.forEach((claim) => {
    if (claim.text === oldSummary) claim.text = content.resume.summary;
  });
  content.cover_letter = article.querySelector('[data-writing-field="cover_letter"]').value;
  article.querySelectorAll("[data-writing-statement]").forEach((field) => {
    content.statements[Number(field.dataset.writingStatement)].answer = field.value;
  });
  content.email.subject = article.querySelector('[data-writing-field="email_subject"]').value;
  content.email.body = article.querySelector('[data-writing-field="email_body"]').value;
  return content;
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
    } else if (action === "writing-queue") {
      const result = await api("/api/applications/writing/queue", {
        method: "POST",
        body: JSON.stringify({ application_id: Number(button.dataset.app) })
      });
      toast(result.message || "Codex writing task queued.");
    } else if (action === "writing-save") {
      const result = await api("/api/applications/writing/save", {
        method: "POST",
        body: JSON.stringify({
          application_id: Number(button.dataset.app),
          content: collectWritingContent(button)
        })
      });
      toast(result.validation?.status === "failed" ? "Draft saved but blocked by evidence validation." : `Writing version ${result.version} saved.`);
    } else if (action === "writing-activate") {
      await api("/api/applications/writing/activate", {
        method: "POST",
        body: JSON.stringify({
          application_id: Number(button.dataset.app),
          version_id: Number(button.dataset.version)
        })
      });
      toast("Writing version restored. Review and approve it before applying.");
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
    toast(`Scan complete: ${result.inserted} new, ${result.seen} refreshed, ${result.filtered} filtered, ${result.errors} errors.`);
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
