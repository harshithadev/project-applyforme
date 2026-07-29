const state = {
  data: null,
  activeView: "overview",
  workerPoll: null,
  workflowTab: "jobs",
  scanResult: null,
  scanRunning: false
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

const careerSourceTypes = {
  greenhouse: {
    label: "Greenhouse",
    placeholder: "https://boards.greenhouse.io/company",
    hosts: ["greenhouse.io"]
  },
  lever: {
    label: "Lever",
    placeholder: "https://jobs.lever.co/company",
    hosts: ["lever.co"]
  },
  ashby: {
    label: "Ashby",
    placeholder: "https://jobs.ashbyhq.com/company",
    hosts: ["ashbyhq.com"]
  },
  smartrecruiters: {
    label: "SmartRecruiters",
    placeholder: "https://jobs.smartrecruiters.com/company",
    hosts: ["smartrecruiters.com"]
  },
  workday: {
    label: "Workday",
    placeholder: "https://company.wd5.myworkdayjobs.com/Careers",
    hosts: ["myworkdayjobs.com", "myworkdaysite.com"]
  },
  generic: {
    label: "Generic career page",
    placeholder: "https://company.example/careers",
    hosts: []
  }
};

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

async function uploadDocuments(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  const form = new FormData();
  files.forEach((file) => form.append("documents", file, file.name));
  const response = await fetch("/api/documents/upload", {
    method: "POST",
    body: form
  });
  const result = await response.json();
  if (!response.ok || result.ok === false) {
    throw new Error(result.error || response.statusText);
  }
  const parts = [`${Number(result.saved || 0)} saved`];
  if (result.duplicates) parts.push(`${Number(result.duplicates)} duplicate`);
  if (result.rejected) parts.push(`${Number(result.rejected)} rejected`);
  toast(`Document upload: ${parts.join(", ")}.`);
  await loadState();
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

function formatJobDate(value, precision = "unknown") {
  if (!value) return "Date not listed";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "Date not listed";
  if (precision === "date") {
    const parts = String(value).slice(0, 10).split("-").map(Number);
    const localDate = new Date(parts[0], parts[1] - 1, parts[2]);
    return `Posted/updated ${localDate.toLocaleDateString()} · date only`;
  }
  return `Posted/updated ${parsed.toLocaleString()}`;
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
  $("#pipelineBadge").textContent = data.pipeline?.policy?.enabled
    ? `${Number(data.pipeline.active || 0)} active`
    : "Disabled";
  $("#serviceBadge").textContent = data.service?.running
    ? "Running"
    : data.service?.installed ? "Stopped" : "Not installed";
  $("#emailBadge").textContent = data.email?.configured ? formatMode(data.email.mode) : "Not configured";
  $("#approvalNavCount").textContent = Number(data.approvals?.summary?.pending || 0);
  $("#readinessNavBadge").textContent = `${Number(data.readiness?.score || 0)}%`;
  $("#dbPath").textContent = data.paths.database;
  $("#jobQueueMeta").textContent = `${data.jobs.length} tracked`;

  renderDocumentInbox(data.profile.documents, data.document_inbox || {});
  renderEvents("#recentEvents", data.events.slice(0, 8));
  renderEvents("#logsList", data.events);
  renderWorkflow(data);
  renderJobs(data.jobs);
  renderAssistedSearches(data.settings);
  renderSourceStates(data.job_source_states || []);
  renderPipeline(data.pipeline || {});
  renderApprovals(data.approvals || {});
  renderReadiness(data.readiness || {});
  renderApplications(data.applications, data.application_tasks || []);
  renderBrowserSessions(
    data.browser_sessions || [],
    data.automation?.sessions || {}
  );
  renderAdapterHealth(
    data.browser_diagnostics || {},
    data.browser_recovery || {}
  );
  renderAdapterRegistry(data.ats_adapters || {});
  renderBrowserRecovery(data.browser_recovery || {});
  renderOutreach(
    data.outreach || [],
    data.contacts || [],
    data.applications || [],
    data.contact_discovery_runs || []
  );
  renderRules(data.answer_rules || []);
  renderService(data.service || {});
  populateSettings(data.settings);
  scheduleBackgroundRefresh(
    data.applications,
    data.outreach || [],
    data.application_tasks || [],
    data.pipeline || {},
    data.browser_sessions || []
  );
}

function scheduleBackgroundRefresh(applications, outreach, applicationTasks, pipeline, browserSessions) {
  clearTimeout(state.workerPoll);
  const writingActive = applications.some((app) => ["queued", "running"].includes(app.writing?.task?.status));
  const outreachActive = outreach.some((thread) => ["queued", "sending"].includes(thread.status));
  const browserActive = applicationTasks.some((task) => ["queued", "running", "retry_wait"].includes(task.status));
  const pipelineActive = (pipeline.items || []).some((item) =>
    ["queued", "running", "writing", "approved", "applying"].includes(item.status)
  );
  const sessionActive = browserSessions.some((session) => session.active);
  const active = writingActive || outreachActive || browserActive || pipelineActive || sessionActive;
  state.workerPoll = active ? setTimeout(() => loadState().catch((error) => toast(error.message)), 3000) : null;
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

function renderDocumentInbox(docs, inbox) {
  const active = docs.filter((doc) => doc.ingest_status !== "archived");
  const pending = docs.filter((doc) => doc.ingest_status === "pending_review");
  const errors = docs.filter((doc) => ["error", "duplicate"].includes(doc.ingest_status));
  const ocr = inbox.ocr || {};
  $("#documentActiveCount").textContent = active.length;
  $("#documentReviewCount").textContent = pending.length;
  $("#documentErrorCount").textContent = errors.length;
  $("#documentOcrState").textContent = ocr.available ? "Ready" : "Unavailable";
  $("#documentWatcherState").textContent = inbox.running
    ? `Watching every ${Number(inbox.interval_seconds || 15)} seconds`
    : "Watcher stopped";
  $("#documentInboxMeta").textContent = `${docs.length} managed`;

  const target = $("#documentInboxList");
  if (!docs.length) {
    target.innerHTML = `<div class="empty">No managed documents.</div>`;
    return;
  }
  const kinds = [
    ["resume", "Resume"],
    ["transcript", "Transcript"],
    ["cover_letter", "Cover letter"],
    ["portfolio", "Portfolio"],
    ["work_authorization", "Work authorization"],
    ["certification", "Certification"],
    ["source", "Other source"]
  ];
  target.innerHTML = docs.map((doc) => {
    const status = String(doc.ingest_status || "error");
    const confidence = Number(doc.extraction_confidence || 0);
    const classification = Number(doc.classification_confidence || 0);
    const archived = status === "archived";
    const artifactUrl = `/api/documents/artifact?document_id=${encodeURIComponent(doc.id)}`;
    const kindOptions = kinds.map(([value, label]) =>
      `<option value="${value}" ${value === doc.kind ? "selected" : ""}>${label}</option>`
    ).join("");
    return `
      <article class="document-row ${escapeHtml(status)}">
        <div class="document-row-head">
          <div>
            <h4>${escapeHtml(doc.name)}</h4>
            <p class="meta">${escapeHtml(doc.source || "folder")} · ${escapeHtml(doc.extractor || "not extracted")} · ${formatBytes(doc.size_bytes)}</p>
          </div>
          <span class="status">${escapeHtml(formatMode(status))}</span>
        </div>
        ${doc.ingest_error ? `<p class="error-text">${escapeHtml(doc.ingest_error)}</p>` : ""}
        <div class="document-facts">
          <span>Extraction ${Math.round(confidence * 100)}%</span>
          <span>Classification ${Math.round(classification * 100)}%</span>
          <span>${escapeHtml(doc.updated_at)}</span>
        </div>
        ${doc.content_preview
          ? `<details class="document-preview"><summary>Extracted text</summary><pre>${escapeHtml(doc.content_preview)}</pre></details>`
          : ""}
        <div class="document-edit">
          <label>Filename
            <input data-document-name value="${escapeHtml(doc.name)}" ${archived ? "disabled" : ""} />
          </label>
          <label>Classification
            <select data-document-kind ${archived ? "disabled" : ""}>${kindOptions}</select>
          </label>
        </div>
        <div class="card-actions">
          <a class="button-link secondary" href="${artifactUrl}" target="_blank" rel="noreferrer">Open file</a>
          ${!archived ? `<button data-action="document-update" data-document="${doc.id}" class="secondary">Save details</button>` : ""}
          ${status === "pending_review" ? `<button data-action="document-approve" data-document="${doc.id}">Approve evidence</button>` : ""}
          ${["error", "duplicate"].includes(status) ? `<button data-action="document-retry" data-document="${doc.id}">Retry</button>` : ""}
          ${archived
            ? `<button data-action="document-restore" data-document="${doc.id}">Restore</button>`
            : `<button data-action="document-archive" data-document="${doc.id}" class="secondary">Archive</button>`}
          <button data-action="document-remove" data-document="${doc.id}" class="secondary">Remove</button>
        </div>
      </article>
    `;
  }).join("");
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

function latestTaskFor(applicationId, tasks) {
  return tasks.find(
    (task) => Number(task.application_id) === Number(applicationId)
  );
}

function renderWorkflowJobs(target, jobs) {
  if (!jobs.length) {
    target.innerHTML = `<div class="empty">No jobs are waiting. Run a scan to refresh the queue.</div>`;
    return;
  }
  target.innerHTML = jobs.map((job) => {
    const authorization = job.work_authorization || {};
    const authorizationClass = ["confirmed", "cpt_opt", "incompatible"].includes(
      authorization.status
    ) ? authorization.status : "unknown";
    return `
      <article class="workflow-job-row">
        <div class="workflow-job-main">
          <div class="workflow-job-heading">
            <div>
              <h4>${escapeHtml(job.title)}</h4>
              <p>${escapeHtml(job.company)} · ${escapeHtml(job.location || "Location needs verification")}</p>
            </div>
            <strong class="workflow-score">${Number(job.score || 0)}</strong>
          </div>
          <div class="workflow-job-facts">
            <span>${escapeHtml(formatJobDate(job.posted_at, job.metadata?.posted_at_precision))}</span>
            <span>${escapeHtml(job.source)}</span>
            <span class="authorization ${escapeHtml(authorizationClass)}">${escapeHtml(authorization.label || "Sponsorship needs verification")}</span>
            ${job.status === "maybe" ? `<span class="status">Maybe</span>` : ""}
          </div>
          <div class="match-reasons">
            ${(job.match_reasons || []).map((reason) => `<span>${escapeHtml(reason)}</span>`).join("")}
          </div>
          ${authorization.evidence ? `<p class="authorization-evidence">${escapeHtml(authorization.evidence)}</p>` : ""}
        </div>
        <div class="workflow-job-actions">
          <button data-action="workflow-tailor" data-job="${job.id}">Approve &amp; tailor</button>
          <button data-action="workflow-job-decision" data-decision="${job.status === "maybe" ? "reconsider" : "maybe"}" data-job="${job.id}" class="secondary">${job.status === "maybe" ? "Move to new" : "Maybe"}</button>
          <button data-action="workflow-job-decision" data-decision="reject" data-job="${job.id}" class="text-button">Reject</button>
          <a href="${escapeHtml(job.url)}" target="_blank" rel="noreferrer">Open posting</a>
        </div>
      </article>
    `;
  }).join("");
}

function renderWorkflowSubmitted(target, applications) {
  if (!applications.length) {
    target.innerHTML = `<div class="empty">Submitted applications will appear here.</div>`;
    return;
  }
  target.innerHTML = applications.map((app) => `
    <article class="workflow-submitted-row">
      <div>
        <strong>${escapeHtml(app.title)}</strong>
        <p>${escapeHtml(app.company)}</p>
      </div>
      <div>
        <span class="status">Submitted</span>
        <small>${escapeHtml(app.updated_at)}</small>
      </div>
    </article>
  `).join("");
}

function renderWorkflowAttention(data, tasks) {
  const target = $("#workflowAttentionList");
  const approvals = data.approvals?.items || [];
  const taskAttention = tasks.filter(
    (task) => ["checkpoint", "failed"].includes(task.status)
  );
  const writingAttention = data.applications.filter(
    (app) => app.writing?.task?.status === "failed"
  );
  const total = approvals.length + taskAttention.length + writingAttention.length;
  $("#workflowAttentionCount").textContent = `${total} item${total === 1 ? "" : "s"}`;
  if (!total) {
    target.innerHTML = `<div class="empty compact-empty">Nothing needs your attention.</div>`;
    return;
  }
  const rows = [
    ...approvals.slice(0, 4).map((item) => `
      <button class="workflow-attention-row" data-action="readiness-open" data-view="approvals">
        <span>${escapeHtml(item.title || "Approval required")}</span>
        <small>${escapeHtml(item.priority || "normal")}</small>
      </button>
    `),
    ...taskAttention.slice(0, 4).map((task) => `
      <button class="workflow-attention-row" data-action="workflow-open-applying">
        <span>${escapeHtml(task.message || "Application needs input")}</span>
        <small>${escapeHtml(formatMode(task.checkpoint_kind || task.status))}</small>
      </button>
    `),
    ...writingAttention.slice(0, 4).map((app) => `
      <button class="workflow-attention-row" data-action="workflow-open-materials">
        <span>${escapeHtml(app.title)} · writing failed</span>
        <small>${escapeHtml(app.company)}</small>
      </button>
    `)
  ];
  target.innerHTML = rows.slice(0, 8).join("");
}

function renderWorkflow(data) {
  const tasks = data.application_tasks || [];
  const reviewJobs = data.jobs.filter((job) => ["new", "maybe"].includes(job.status));
  const applyingApplications = data.applications.filter((app) => {
    const task = latestTaskFor(app.id, tasks);
    return task && ["queued", "running", "retry_wait", "checkpoint", "failed"].includes(task.status)
      && app.status !== "submitted";
  });
  const materialApplications = data.applications.filter((app) => {
    const task = latestTaskFor(app.id, tasks);
    return app.status !== "submitted"
      && !(task && ["queued", "running", "retry_wait", "checkpoint", "failed"].includes(task.status));
  });
  const submittedApplications = data.applications.filter(
    (app) => app.status === "submitted"
  );

  $("#workflowJobCount").textContent = `${reviewJobs.length} waiting`;
  $("#workflowMaterialCount").textContent = `${materialApplications.length} waiting`;
  $("#workflowApplyingCount").textContent = `${applyingApplications.length} active`;
  $("#workflowSubmittedCount").textContent = `${submittedApplications.length} complete`;
  $("#workflowJobsTabCount").textContent = reviewJobs.length;
  $("#workflowMaterialsTabCount").textContent = materialApplications.length;
  $("#workflowApplyingTabCount").textContent = applyingApplications.length;
  $("#workflowSubmittedTabCount").textContent = submittedApplications.length;

  const preset = `${data.settings.posted_age_mode || "days"}:${
    data.settings.posted_age_mode === "hours"
      ? data.settings.posted_within_hours || "24"
      : data.settings.posted_within_days || "3"
  }`;
  const presetSelect = $("#workflowAgePreset");
  if ([...presetSelect.options].some((option) => option.value === preset)) {
    presetSelect.value = preset;
  }
  $("#workflowUnknownDates").checked = data.settings.include_unknown_posted_at === "true";
  $("#scanBtn").disabled = state.scanRunning;
  $("#scanBtn").textContent = state.scanRunning ? "Scanning..." : "Scan now";

  const scanMessage = $("#workflowScanMessage");
  if (state.scanRunning) {
    scanMessage.textContent = "Refreshing every enabled source.";
  } else if (state.scanResult) {
    const result = state.scanResult;
    scanMessage.textContent = `${Number(result.inserted || 0)} new · ${Number(result.seen || 0)} refreshed · ${Number(result.filtered || 0)} filtered · ${Number(result.errors || 0)} errors`;
  } else {
    scanMessage.textContent = data.job_source_states.length
      ? "Ready to refresh all enabled sources."
      : "No source scan has completed yet.";
  }

  $("#workflowSourceProgress").innerHTML = (data.job_source_states || []).map((source) => {
    const metadata = source.metadata || {};
    return `
      <div>
        <span class="source-dot ${escapeHtml(source.status)}"></span>
        <strong>${escapeHtml(metadata.provider_label || source.source_kind)}</strong>
        <small>${Number(source.jobs_seen || 0)} checked · ${escapeHtml(source.status)}</small>
      </div>
    `;
  }).join("") || `<span class="meta">Sources will appear after the first scan.</span>`;

  $$(".workflow-tabs button").forEach((button) => {
    const active = button.dataset.workflowTab === state.workflowTab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
  const queue = $("#workflowQueueList");
  if (state.workflowTab === "jobs") {
    renderWorkflowJobs(queue, reviewJobs);
  } else if (state.workflowTab === "materials") {
    renderApplicationCards("#workflowQueueList", materialApplications, tasks);
  } else if (state.workflowTab === "applying") {
    renderApplicationCards("#workflowQueueList", applyingApplications, tasks);
  } else {
    renderWorkflowSubmitted(queue, submittedApplications);
  }

  renderWorkflowAttention(data, tasks);
  const today = new Date().toDateString();
  const submittedToday = submittedApplications.filter(
    (app) => new Date(app.updated_at).toDateString() === today
  ).length;
  $("#workflowToday").innerHTML = `
    <div><span>New matches</span><strong>${reviewJobs.length}</strong></div>
    <div><span>Submitted</span><strong>${submittedToday}</strong></div>
    <div><span>Daily target</span><strong>${Number(data.settings.daily_application_limit || 5)}</strong></div>
  `;
}

function renderJobs(jobs) {
  const target = $("#jobsList");
  if (!jobs.length) {
    target.innerHTML = `<div class="empty">Run a job scan or add a posting manually.</div>`;
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
      <p class="job-facts">${escapeHtml(job.location || "Location not listed")} · ${escapeHtml(formatJobDate(job.posted_at, job.metadata?.posted_at_precision))}</p>
      <p>${escapeHtml((job.description || "").slice(0, 420))}</p>
      <div class="match-reasons">
        ${(job.match_reasons || []).map((reason) => `<span>${escapeHtml(reason)}</span>`).join("")}
      </div>
      <p class="meta">Match <span class="score">${Number(job.score || 0)}</span> · ${escapeHtml(job.source)}</p>
      <div class="card-actions">
        <button data-action="draft" data-job="${job.id}" ${job.status === "new" ? "" : "disabled"}>Draft package</button>
        <a class="button-link secondary" href="${escapeHtml(job.url)}" target="_blank" rel="noreferrer">Open posting</a>
      </div>
    </article>
  `).join("");
}

function assistedSearchLabel(url) {
  const parts = url.pathname.split("/").filter(Boolean);
  const roleIndex = parts.indexOf("role");
  if (roleIndex < 0) return "Wellfound jobs";
  const mode = parts[roleIndex + 1];
  const role = parts[roleIndex + 2] || parts[roleIndex + 1] || "jobs";
  const location = mode === "l" ? parts[roleIndex + 3] : mode === "r" ? "remote" : "";
  const title = (value) => String(value || "")
    .split("-")
    .filter(Boolean)
    .map((word) => word[0].toUpperCase() + word.slice(1))
    .join(" ");
  return [title(role), title(location)].filter(Boolean).join(" · ");
}

function renderAssistedSearches(settings) {
  const target = $("#assistedSearchList");
  const searches = String(settings.wellfound_search_urls || "")
    .split(/[\n,]+/)
    .map((value) => value.trim())
    .filter(Boolean)
    .map((value) => {
      try {
        const url = new URL(value);
        const host = url.hostname.toLowerCase();
        return url.protocol === "https:"
          && (host === "wellfound.com" || host.endsWith(".wellfound.com"))
          ? url
          : null;
      } catch {
        return null;
      }
    })
    .filter(Boolean);
  $("#assistedSearchMeta").textContent = `${searches.length} on demand`;
  target.innerHTML = searches.length
    ? searches.map((url) => `
        <a class="button-link secondary" href="${escapeHtml(url.toString())}" target="_blank" rel="noreferrer">
          ${escapeHtml(assistedSearchLabel(url))}
        </a>
      `).join("")
    : `<div class="empty">No assisted marketplace searches configured.</div>`;
}

function renderSourceStates(sources) {
  const target = $("#sourceStatesList");
  $("#sourceStateMeta").textContent = `${sources.length} tracked`;
  if (!sources.length) {
    target.innerHTML = `<div class="empty">Discovery providers and company sources appear after their first scan.</div>`;
    return;
  }
  target.innerHTML = sources.map((source) => {
    const metadata = source.metadata || {};
    const total = Number(metadata.total || 0);
    const complete = Boolean(metadata.complete_cycle);
    const sourceLabel = metadata.provider_label || source.source_kind;
    const attribution = String(metadata.attribution_url || "");
    const attributionLink = attribution.startsWith("https://")
      ? `<a href="${escapeHtml(attribution)}" target="_blank" rel="noreferrer">Source</a>`
      : "";
    return `
      <div class="source-state-row">
        <div>
          <strong>${escapeHtml(sourceLabel)}</strong>
          <p class="meta mono">${escapeHtml(source.source_url)}</p>
          ${source.last_error ? `<p class="error-text">${escapeHtml(source.last_error)}</p>` : ""}
        </div>
        <div class="source-state-facts">
          <span class="status">${escapeHtml(source.status)}</span>
          <span>${Number(source.jobs_seen || 0)} jobs</span>
          <span>${Number(source.pages_scanned || 0)} page${Number(source.pages_scanned || 0) === 1 ? "" : "s"}</span>
          ${total ? `<span>${total} total</span>` : ""}
          <span>${complete ? "Cycle complete" : `Next offset ${escapeHtml(source.cursor || "0")}`}</span>
          ${metadata.minimum_interval_minutes
            ? `<span>${Number(metadata.minimum_interval_minutes)}m minimum</span>`
            : ""}
          ${attributionLink}
          <span>${escapeHtml(source.last_success_at || source.last_scanned_at || "")}</span>
        </div>
      </div>
    `;
  }).join("");
}

function renderPipeline(pipeline) {
  const items = pipeline.items || [];
  const policy = pipeline.policy || {};
  const counts = pipeline.counts || {};
  const target = $("#pipelineList");
  $("#pipelinePolicyMeta").textContent = policy.enabled
    ? `Enabled · score ${Number(policy.minimum_score || 0)}+ · ${formatMode(policy.mode)}`
    : "Disabled";
  $("#pipelineSummary").innerHTML = [
    ["Active", Number(pipeline.active || 0)],
    ["Needs attention", Number(pipeline.attention || 0)],
    ["Review", Number(counts.review || 0)],
    ["Submitted", Number(counts.submitted || 0)]
  ].map(([label, value]) => `
    <div><span>${escapeHtml(label)}</span><strong>${value}</strong></div>
  `).join("");
  $("#pipelineRunBtn").disabled = !policy.enabled;

  if (!items.length) {
    target.innerHTML = `<div class="empty">No jobs have entered the application pipeline.</div>`;
    return;
  }
  target.innerHTML = items.map((item) => {
    const retryable = ["blocked", "failed"].includes(item.status);
    const terminal = ["submitted", "skipped", "cancelled"].includes(item.status);
    const eventRows = (item.events || []).slice(0, 6).map((event) => `
      <div class="pipeline-event">
        <span>${escapeHtml(event.message)}</span>
        <small>${escapeHtml(event.created_at)}</small>
      </div>
    `).join("");
    return `
      <article class="pipeline-row ${escapeHtml(item.status)}">
        <div class="pipeline-indicator" aria-hidden="true"></div>
        <div class="pipeline-main">
          <div class="pipeline-heading">
            <div>
              <h4>${escapeHtml(item.title)}</h4>
              <p class="meta">${escapeHtml(item.company)} · score ${Number(item.score || 0)} · ${escapeHtml(item.source)}</p>
            </div>
            <span class="status">${escapeHtml(item.status)}</span>
          </div>
          <p class="pipeline-message">${escapeHtml(item.message)}</p>
          <div class="pipeline-facts">
            <span>${escapeHtml(formatMode(item.stage))}</span>
            <span>Attempts ${Number(item.attempt_count || 0)}</span>
            <span>${escapeHtml(item.updated_at)}</span>
          </div>
          <div class="card-actions">
            ${item.application_id ? `<button data-action="pipeline-open" data-pipeline="${item.id}" class="secondary">Open application</button>` : ""}
            ${retryable ? `<button data-action="pipeline-retry" data-pipeline="${item.id}">Retry</button>` : ""}
            ${!terminal ? `<button data-action="pipeline-skip" data-pipeline="${item.id}" class="secondary">Skip</button>` : ""}
          </div>
          <details class="pipeline-history">
            <summary>Activity (${(item.events || []).length})</summary>
            ${eventRows}
          </details>
        </div>
      </article>
    `;
  }).join("");
}

function renderApprovals(inbox) {
  const items = inbox.items || [];
  const history = inbox.history || [];
  const summary = inbox.summary || {};
  const notifications = inbox.notifications || {};
  $("#approvalPendingCount").textContent = Number(summary.pending || 0);
  $("#approvalUrgentCount").textContent = Number(summary.urgent || 0);
  $("#approvalDecisionCount").textContent = history.length;
  $("#notificationState").textContent = !notifications.enabled
    ? "Disabled"
    : notifications.quiet ? "Quiet hours" : notifications.supported ? "Active" : "Unsupported";
  $("#notificationTestBtn").disabled = !notifications.enabled || !notifications.supported;

  const target = $("#approvalList");
  if (!items.length) {
    target.innerHTML = `<div class="empty">No decisions are waiting. Background work will appear here when it needs review.</div>`;
  } else {
    target.innerHTML = items.map((item) => {
      const payload = item.payload || {};
      const fields = payload.fields || [];
      const answerFields = fields.map((field, index) => {
        const question = field.question || `Required field ${index + 1}`;
        const options = (field.options || []).filter(Boolean);
        const control = options.length
          ? `<select data-approval-answer data-question="${escapeHtml(question)}">
              <option value="">Choose an answer</option>
              ${options.map((option) => `<option value="${escapeHtml(option)}" ${option === field.suggested_answer ? "selected" : ""}>${escapeHtml(option)}</option>`).join("")}
            </select>`
          : `<input data-approval-answer data-question="${escapeHtml(question)}" value="${escapeHtml(field.suggested_answer || "")}" />`;
        return `<label>${escapeHtml(question)}${control}</label>`;
      }).join("");
      const actions = (item.actions || []).map((candidate) => {
        const style = candidate.style === "secondary"
          ? "secondary"
          : candidate.style === "warning" ? "warn" : "";
        return `
          <button
            class="${style}"
            data-action="approval-resolve"
            data-approval="${Number(item.id)}"
            data-resolution="${escapeHtml(candidate.id)}"
            data-confirm="${escapeHtml(candidate.confirmation || "")}"
          >${escapeHtml(candidate.label)}</button>
        `;
      }).join("");
      const artifactLink = payload.resume_url
        ? `<a class="button-link secondary" href="${escapeHtml(payload.resume_url)}" target="_blank" rel="noreferrer">Open tailored resume</a>`
        : payload.artifact_url
          ? `<a class="button-link secondary" href="${escapeHtml(payload.artifact_url)}" target="_blank" rel="noreferrer">Open document</a>`
        : "";
      const screenshot = payload.screenshot_url
        ? `<a class="approval-screenshot" href="${escapeHtml(payload.screenshot_url)}" target="_blank" rel="noreferrer"><img src="${escapeHtml(payload.screenshot_url)}" alt="Browser checkpoint screenshot" /></a>`
        : "";
      const targetLink = payload.target_url
        ? `<a class="button-link secondary" href="${escapeHtml(payload.target_url)}" target="_blank" rel="noreferrer">Open application</a>`
        : "";
      const outreachPreview = payload.subject || payload.body
        ? `<details class="approval-preview">
            <summary>Message preview</summary>
            <strong>${escapeHtml(payload.subject || "")}</strong>
            <pre>${escapeHtml(payload.body || "")}</pre>
          </details>`
        : "";
      const documentPreview = payload.content_preview
        ? `<details class="approval-preview">
            <summary>Extracted text</summary>
            <pre>${escapeHtml(payload.content_preview)}</pre>
          </details>`
        : "";
      return `
        <article class="approval-row priority-${Number(item.priority || 0) >= 90 ? "urgent" : "normal"}">
          <div class="approval-heading">
            <div>
              <span class="approval-kind">${escapeHtml(formatMode(item.kind))}</span>
              <h4>${escapeHtml(item.title)}</h4>
            </div>
            <span class="status">${Number(item.priority || 0) >= 90 ? "urgent" : "pending"}</span>
          </div>
          <p>${escapeHtml(item.summary)}</p>
          ${screenshot}
          ${answerFields ? `<div class="approval-fields">${answerFields}</div>` : ""}
          ${outreachPreview}
          ${documentPreview}
          <label class="approval-note">Decision note
            <input data-approval-note placeholder="Optional" />
          </label>
          <div class="card-actions">${artifactLink}${targetLink}${actions}</div>
          <p class="meta">Created ${escapeHtml(item.created_at)}</p>
        </article>
      `;
    }).join("");
  }

  const historyTarget = $("#approvalHistory");
  historyTarget.innerHTML = history.length
    ? history.map((decision) => `
        <div class="event">
          <div>
            <strong>${escapeHtml(decision.title)}</strong>
            <p>${escapeHtml(formatMode(decision.action))}${decision.note ? ` · ${escapeHtml(decision.note)}` : ""}</p>
          </div>
          <span>${escapeHtml(decision.created_at)}</span>
        </div>
      `).join("")
    : `<div class="empty">No inbox decisions have been recorded yet.</div>`;
}

function renderReadiness(readiness) {
  const checks = readiness.checks || [];
  const modes = readiness.modes || [];
  const summary = readiness.summary || {};
  const checkMap = new Map(checks.map((check) => [check.id, check]));
  $("#readinessScore").textContent = `${Number(readiness.score || 0)}%`;
  $("#readinessRequired").textContent = `${Number(summary.required_passed || 0)}/${Number(summary.required_total || 0)}`;
  $("#readinessBlocking").textContent = (summary.blocking || []).length;
  $("#readinessCompletion").textContent = readiness.setup_completed_at ? "Complete" : "Open";
  $("#readinessEvaluatedAt").textContent = readiness.evaluated_at
    ? `Evaluated ${readiness.evaluated_at}`
    : "";
  const reviewMode = modes.find((mode) => mode.id === "review_automation");
  $("#readinessCompleteBtn").disabled = !reviewMode?.ready || Boolean(readiness.setup_completed_at);

  $("#readinessModes").innerHTML = modes.map((mode) => `
    <div class="readiness-mode ${mode.ready ? "ready" : "blocked"}">
      <div>
        <strong>${escapeHtml(mode.title)}</strong>
        <p>${escapeHtml(mode.message)}</p>
      </div>
      <span class="status">${mode.ready ? "ready" : `${(mode.missing || []).length} blocked`}</span>
    </div>
  `).join("");

  $("#readinessChecks").innerHTML = checks.map((check) => `
    <div class="readiness-check ${escapeHtml(check.status)}">
      <div class="readiness-check-state" aria-hidden="true"></div>
      <div>
        <strong>${escapeHtml(check.title)}</strong>
        <p>${escapeHtml(check.message)}</p>
      </div>
      <button
        class="secondary"
        data-action="readiness-open"
        data-view="${escapeHtml(check.view)}"
      >Open</button>
    </div>
  `).join("");

  const codex = checkMap.get("codex") || {};
  const email = checkMap.get("email") || {};
  $("#readinessConnections").innerHTML = [codex, email].map((check) => `
    <div class="connection-row">
      <div>
        <strong>${escapeHtml(check.title || "Connection")}</strong>
        <p>${escapeHtml(check.message || "Status unavailable.")}</p>
      </div>
      <span class="status">${escapeHtml(check.status || "unknown")}</span>
    </div>
  `).join("");
  $("#readinessEmailBtn").disabled = !Boolean(email.detail?.configured);

  const history = readiness.history || [];
  $("#readinessHistory").innerHTML = history.length
    ? history.map((run) => `
        <div class="event">
          <div>
            <strong>${Number(run.score || 0)}% · ${escapeHtml(run.status)}</strong>
            <p>${(run.blocking || []).length
              ? `Blocked: ${escapeHtml((run.blocking || []).join(", "))}`
              : "All review-automation checks passed."}</p>
          </div>
          <span>${escapeHtml(run.created_at)}</span>
        </div>
      `).join("")
    : `<div class="empty">No explicit preflight runs recorded.</div>`;
}

function renderBrowserTask(task) {
  if (!task) return "";
  const checkpoint = task.checkpoint || {};
  const fields = checkpoint.fields || [];
  const session = task.browser_session || {};
  const takeoverKinds = [
    "captcha",
    "unsupported_site",
    "unsupported_form",
    "submit_control",
    "step_limit",
    "adapter_quarantined"
  ];
  const takeoverResumeKinds = takeoverKinds.filter((kind) =>
    !["unsupported_site", "adapter_quarantined"].includes(kind)
  );
  const handoffOwned = Number(session.active_task_id || 0) === Number(task.id);
  const manualTakeover = takeoverKinds.includes(task.checkpoint_kind);
  const latestScreenshot = (task.screenshots || []).at(-1);
  const latestDiagnostic = (task.diagnostics || [])[0] || {};
  const recovery = task.recovery || {};
  const circuit = recovery.circuit || {};
  const screenshotUrl = latestScreenshot
    ? `/api/applications/task-artifact?task_id=${encodeURIComponent(task.id)}&name=${encodeURIComponent(latestScreenshot)}`
    : "";
  const eventRows = (task.events || []).slice(-6).reverse().map((event) => `
    <div class="task-event">
      <span>${escapeHtml(event.message)}</span>
      <small>${escapeHtml(event.created_at)}</small>
    </div>
  `).join("");
  const answerFields = fields.map((field, index) => {
    const options = field.options || [];
    const question = field.question || `Required field ${index + 1}`;
    const control = options.length
      ? `<select data-checkpoint-answer data-question="${escapeHtml(question)}">
          <option value="">Choose an answer</option>
          ${options.filter(Boolean).map((option) => `<option value="${escapeHtml(option)}" ${option === field.suggested_answer ? "selected" : ""}>${escapeHtml(option)}</option>`).join("")}
        </select>`
      : `<input data-checkpoint-answer data-question="${escapeHtml(question)}" value="${escapeHtml(field.suggested_answer || "")}" />`;
    return `<label>${escapeHtml(question)}${control}</label>`;
  }).join("");
  const canContinue = ![
    "final_review",
    "submission_uncertain",
    "unsupported_site",
    "unsupported_form",
    "submit_control",
    "step_limit",
    "adapter_quarantined",
    "captcha",
    "login"
  ].includes(task.checkpoint_kind);
  return `
    <section class="browser-task ${escapeHtml(task.status)}">
      <div class="browser-task-head">
        <div>
          <strong>Browser task #${task.id}</strong>
          <p class="meta">${escapeHtml(task.adapter)} · attempt ${Number(task.attempt_count || 0)}</p>
        </div>
        <span class="status">${escapeHtml(task.status)}</span>
      </div>
      <p>${escapeHtml(task.message)}</p>
      ${task.status === "retry_wait" ? `
        <div class="recovery-status">
          <strong>${escapeHtml(formatMode(recovery.category || recovery.reason || "retry waiting"))}</strong>
          <span>${recovery.next_attempt_at
            ? `Next attempt ${escapeHtml(recovery.next_attempt_at)}`
            : "Waiting for recovery approval"}</span>
        </div>
      ` : ""}
      ${session.id ? `
        <div class="browser-session-inline">
          <span>${escapeHtml(session.hostname || "ATS session")}</span>
          <strong>${escapeHtml(formatMode(session.status || "new"))}</strong>
        </div>
      ` : ""}
      ${screenshotUrl ? `<a class="task-screenshot" href="${screenshotUrl}" target="_blank" rel="noreferrer"><img src="${screenshotUrl}" alt="Latest browser task screenshot" /></a>` : ""}
      ${latestDiagnostic.id ? `
        <details class="task-diagnostic">
          <summary>
            ${escapeHtml(formatMode(latestDiagnostic.category || "diagnostic"))}
            · ${escapeHtml(formatMode(latestDiagnostic.severity || "info"))}
          </summary>
          <p>${escapeHtml(latestDiagnostic.recommendation || "Review the browser task details before retrying.")}</p>
          <div class="diagnostic-meta">
            <span>${latestDiagnostic.retryable ? "Retry supported" : "Manual verification required"}</span>
            ${latestDiagnostic.download_available
              ? `<a href="/api/applications/task-diagnostic?bundle_id=${encodeURIComponent(latestDiagnostic.id)}">Download sanitized bundle</a>`
              : ""}
          </div>
        </details>
      ` : ""}
      ${answerFields ? `<div class="checkpoint-fields">${answerFields}</div>` : ""}
      <div class="card-actions">
        ${task.status === "checkpoint" && task.checkpoint_kind === "final_review"
          ? `<button data-action="task-submit" data-task="${task.id}" class="warn">Submit application</button>`
          : ""}
        ${task.status === "checkpoint" && canContinue
          ? `<button data-action="task-resolve" data-task="${task.id}">Save answers and continue</button>`
          : ""}
        ${task.status === "checkpoint" && task.checkpoint_kind === "login"
          && !session.active
          ? `<button data-action="task-login-start" data-task="${task.id}">Open sign-in window</button>`
          : ""}
        ${task.status === "checkpoint" && task.checkpoint_kind === "login"
          && handoffOwned && session.status === "awaiting_user"
          ? `<button data-action="task-login-complete" data-session="${session.id}">Sign-in complete</button>`
          : ""}
        ${task.status === "checkpoint" && task.checkpoint_kind === "login"
          && handoffOwned && ["handoff_opening", "awaiting_user"].includes(session.status)
          ? `<button data-action="task-login-cancel" data-session="${session.id}" class="secondary">Cancel sign-in</button>`
          : ""}
        ${task.status === "checkpoint" && manualTakeover && !session.active
          ? `<button data-action="task-takeover-start" data-task="${task.id}">Open manual browser</button>`
          : ""}
        ${task.status === "checkpoint" && manualTakeover && handoffOwned
          && session.status === "awaiting_user"
          && takeoverResumeKinds.includes(task.checkpoint_kind)
          ? `<button data-action="task-takeover-resume" data-session="${session.id}">Manual step complete</button>`
          : ""}
        ${task.status === "checkpoint" && manualTakeover && handoffOwned
          && session.status === "awaiting_user"
          ? `<button data-action="task-takeover-submitted" data-session="${session.id}" class="warn">I submitted manually</button>`
          : ""}
        ${task.status === "checkpoint" && manualTakeover && handoffOwned
          && ["handoff_opening", "awaiting_user"].includes(session.status)
          ? `<button data-action="task-takeover-cancel" data-session="${session.id}" class="secondary">Cancel takeover</button>`
          : ""}
        ${task.status === "checkpoint" && checkpoint.target_url
          && task.checkpoint_kind !== "login" && !manualTakeover
          ? `<a class="button-link secondary" href="${escapeHtml(checkpoint.target_url)}" target="_blank" rel="noreferrer">Open application</a>`
          : ""}
        ${["failed", "retry_wait"].includes(task.status)
          ? `<button
              data-action="task-retry"
              data-task="${task.id}"
              data-reset-circuit="${["open", "half_open"].includes(circuit.effective_status) ? "true" : "false"}"
            >${["open", "half_open"].includes(circuit.effective_status) ? "Reset circuit and retry" : "Retry now"}</button>`
          : ""}
        ${["queued", "retry_wait", "checkpoint"].includes(task.status) && !session.active
          ? `<button data-action="task-cancel" data-task="${task.id}" class="secondary">Cancel task</button>`
          : ""}
      </div>
      <details class="task-history">
        <summary>Task activity (${(task.events || []).length})</summary>
        ${eventRows}
      </details>
    </section>
  `;
}

function renderBrowserSessions(sessions, summary) {
  $("#browserSessionMeta").textContent = `${Number(summary.ready || 0)} ready · ${Number(summary.active || 0)} active`;
  $("#browserSessionList").innerHTML = sessions.length
    ? sessions.map((session) => `
        <div class="browser-session-row">
          <div>
            <div class="browser-session-heading">
              <strong>${escapeHtml(formatMode(session.adapter || "ATS"))}</strong>
              <span class="status">${escapeHtml(formatMode(session.status || "new"))}</span>
            </div>
            <p>${escapeHtml(session.hostname || "Unknown host")}</p>
            <span>${escapeHtml(session.message || "Session status unavailable.")}</span>
          </div>
          <div class="browser-session-actions">
            <span>${session.last_verified_at
              ? `Verified ${escapeHtml(session.last_verified_at)}`
              : session.last_used_at
                ? `Used ${escapeHtml(session.last_used_at)}`
                : "Not verified"}</span>
            <button
              class="secondary"
              data-action="browser-session-clear"
              data-session="${session.id}"
              ${session.can_clear ? "" : "disabled"}
            >Clear</button>
          </div>
        </div>
      `).join("")
    : `<div class="empty">No ATS browser sessions have been created.</div>`;
}

function renderAdapterHealth(diagnostics, recovery) {
  const summary = diagnostics.summary || {};
  const health = diagnostics.adapter_health || [];
  const circuits = new Map(
    (recovery.circuits || []).map((item) => [
      `${item.adapter}::${item.hostname}`,
      item
    ])
  );
  $("#adapterHealthMeta").textContent = `${Number(summary.bundles || 0)} diagnostic bundles · ${Number(summary.critical || 0)} critical`;
  $("#adapterHealthList").innerHTML = health.length
    ? health.map((item) => {
        const circuit = circuits.get(`${item.adapter}::${item.hostname}`) || {};
        return `
        <div class="adapter-health-row">
          <div>
            <div class="browser-session-heading">
              <strong>${escapeHtml(formatMode(item.adapter || "unsupported"))}</strong>
              <span class="status">${escapeHtml(formatMode(circuit.effective_status || item.status || "attention"))}</span>
            </div>
            <p>${escapeHtml(item.hostname || "Unknown host")}</p>
            <span>${escapeHtml(item.last_message || "No recent outcome message.")}</span>
          </div>
          <div class="adapter-health-metrics">
            <strong>${Number(item.success_rate || 0)}%</strong>
            <span>${Number(item.submitted || 0)} submitted · ${Number(item.attempts || 0)} attempts</span>
            <small>Last: ${escapeHtml(formatMode(item.last_category || "none"))}</small>
          </div>
        </div>
      `;
      }).join("")
    : `<div class="empty">Adapter health will appear after the first browser application attempt.</div>`;
}

function renderAdapterRegistry(registry) {
  const summary = registry.summary || {};
  const policy = registry.policy || {};
  const adapters = registry.adapters || [];
  const replays = registry.recent_replays || [];
  $("#adapterRegistryMeta").textContent = `${Number(summary.adapters || 0)} versioned · ${Number(summary.quarantined || 0)} quarantined · threshold ${Number(policy.drift_threshold || 0)}`;
  const latestReplay = new Map();
  replays.forEach((replay) => {
    const key = `${replay.adapter}::${replay.hostname}`;
    if (!latestReplay.has(key)) latestReplay.set(key, replay);
  });
  const rows = adapters.flatMap((adapter) => {
    const hosts = adapter.hosts || [];
    if (!hosts.length) {
      return [`
        <div class="adapter-registry-row">
          <div>
            <div class="browser-session-heading">
              <strong>${escapeHtml(formatMode(adapter.name))}</strong>
              <span class="status">Not observed</span>
            </div>
            <p>Version ${escapeHtml(adapter.version)}</p>
            <span>${escapeHtml((adapter.capabilities || []).map(formatMode).join(" · "))}</span>
          </div>
          <div class="adapter-registry-actions">
            <span>No host history</span>
          </div>
        </div>
      `];
    }
    return hosts.map((host) => {
      const replay = latestReplay.get(`${adapter.name}::${host.hostname}`) || {};
      const status = host.effective_status || host.status || "active";
      return `
        <div class="adapter-registry-row">
          <div>
            <div class="browser-session-heading">
              <strong>${escapeHtml(formatMode(adapter.name))}</strong>
              <span class="status">${escapeHtml(formatMode(status))}</span>
            </div>
            <p>${escapeHtml(host.hostname)} · version ${escapeHtml(adapter.version)}</p>
            <span>${Number(host.consecutive_drift || 0)} consecutive drift · ${Number(host.total_drift || 0)} total · last ${escapeHtml(formatMode(host.last_category || "none"))}</span>
          </div>
          <div class="adapter-registry-actions">
            ${replay.download_available
              ? `<a href="/api/ats-adapters/replay?replay_id=${encodeURIComponent(replay.id)}">Replay #${Number(replay.id)}</a>`
              : `<span>No replay</span>`}
            ${["quarantined", "version_updated"].includes(status)
              ? `<button
                  class="secondary"
                  data-action="adapter-reactivate"
                  data-adapter="${escapeHtml(adapter.name)}"
                  data-hostname="${escapeHtml(host.hostname)}"
                >Reactivate</button>`
              : ""}
          </div>
        </div>
      `;
    });
  });
  $("#adapterRegistryList").innerHTML = rows.join("")
    || `<div class="empty">No ATS adapters are registered.</div>`;
}

function renderBrowserRecovery(recovery) {
  const summary = recovery.summary || {};
  const tasks = recovery.tasks || [];
  const circuits = (recovery.circuits || []).filter((item) =>
    ["open", "half_open", "probe_ready"].includes(item.effective_status)
  );
  $("#browserRecoveryMeta").textContent = `${Number(summary.waiting || 0)} waiting · ${Number(summary.open_circuits || 0)} open circuits`;
  const circuitRows = circuits.map((circuit) => `
    <div class="browser-recovery-row">
      <div>
        <div class="browser-session-heading">
          <strong>${escapeHtml(formatMode(circuit.adapter || "ATS"))}</strong>
          <span class="status">${escapeHtml(formatMode(circuit.effective_status))}</span>
        </div>
        <p>${escapeHtml(circuit.hostname || "Unknown host")}</p>
        <span>${Number(circuit.consecutive_failures || 0)} consecutive failures${circuit.retry_after ? ` · cooldown until ${escapeHtml(circuit.retry_after)}` : ""}</span>
      </div>
      <button
        class="secondary"
        data-action="circuit-reset"
        data-adapter="${escapeHtml(circuit.adapter)}"
        data-hostname="${escapeHtml(circuit.hostname)}"
      >Reset circuit</button>
    </div>
  `);
  const taskRows = tasks.map((task) => `
    <div class="browser-recovery-row">
      <div>
        <div class="browser-session-heading">
          <strong>${escapeHtml(task.company)} · ${escapeHtml(task.title)}</strong>
          <span class="status">${escapeHtml(formatMode(task.status))}</span>
        </div>
        <p>Task #${Number(task.id)} · ${escapeHtml(formatMode(task.retry_category || task.retry_reason || "recovery"))}</p>
        <span>${escapeHtml(task.message)}${task.next_attempt_at ? ` · ${escapeHtml(task.next_attempt_at)}` : ""}</span>
      </div>
      <button
        data-action="task-retry"
        data-task="${Number(task.id)}"
        data-reset-circuit="${["open", "half_open"].includes(task.circuit?.effective_status) ? "true" : "false"}"
      >${["open", "half_open"].includes(task.circuit?.effective_status) ? "Reset and retry" : "Retry now"}</button>
    </div>
  `);
  $("#browserRecoveryList").innerHTML = [...circuitRows, ...taskRows].join("")
    || `<div class="empty">No browser retries or open ATS circuits need attention.</div>`;
}

function renderApplications(apps, tasks) {
  renderApplicationCards("#applicationsList", apps, tasks);
}

function renderApplicationCards(selector, apps, tasks) {
  const target = $(selector);
  if (!apps.length) {
    target.innerHTML = `<div class="empty">No application packages drafted yet.</div>`;
    return;
  }
  target.innerHTML = apps.map((app) => {
    const browserTask = tasks.find((task) => Number(task.application_id) === Number(app.id));
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
    const browserActive = browserTask
      && ["queued", "running", "retry_wait", "checkpoint"].includes(browserTask.status);
    const materialsReady = compileStatus === "compiled"
      && !taskActive
      && ["codex", "manual"].includes(current.origin)
      && validation.status !== "failed"
      && app.status !== "submitted";
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
      ${renderBrowserTask(browserTask)}
      <div class="card-actions">
        ${app.resume_pdf_path ? `<a class="button-link" href="${artifactBase}&kind=pdf" target="_blank" rel="noreferrer">Open PDF</a>` : ""}
        ${app.resume_tex_path ? `<a class="button-link secondary" href="${artifactBase}&kind=tex">Download LaTeX</a>` : ""}
        <button data-action="compile" data-app="${app.id}" class="secondary">Recompile PDF</button>
        <button data-action="approve-apply" data-app="${app.id}" class="warn" ${materialsReady && !browserActive ? "" : "disabled"}>Approve &amp; apply</button>
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

function companyKey(value) {
  return String(value || "").toLowerCase().replaceAll(/[^a-z0-9]/g, "");
}

function populateOutreachOptions(contacts, applications) {
  const applicationSelect = $("#outreachApplication");
  const previousApplication = applicationSelect.value;
  applicationSelect.innerHTML = applications.length
    ? applications.map((app) => `<option value="${app.id}">${escapeHtml(app.title)} · ${escapeHtml(app.company)}</option>`).join("")
    : `<option value="">No applications</option>`;
  if ([...applicationSelect.options].some((option) => option.value === previousApplication)) {
    applicationSelect.value = previousApplication;
  }
  updateOutreachContactOptions(contacts, applications);
}

function updateOutreachContactOptions(contacts = state.data.contacts || [], applications = state.data.applications || []) {
  const applicationId = Number($("#outreachApplication").value);
  const application = applications.find((item) => Number(item.id) === applicationId);
  const contactSelect = $("#outreachContact");
  const previousContact = contactSelect.value;
  const matching = application
    ? contacts.filter((contact) =>
        companyKey(contact.company) === companyKey(application.company)
        && ["manual", "published", "verified"].includes(contact.verification_status)
      )
    : [];
  contactSelect.innerHTML = matching.length
    ? matching.map((contact) => `<option value="${contact.id}">${escapeHtml(contact.name || contact.email)} · ${escapeHtml(contact.email)}</option>`).join("")
    : `<option value="">No matching contacts</option>`;
  if ([...contactSelect.options].some((option) => option.value === previousContact)) {
    contactSelect.value = previousContact;
  }
  $("#outreachForm").querySelector('button[type="submit"]').disabled = !application || !matching.length;
}

function renderContacts(contacts) {
  const target = $("#contactsList");
  if (!contacts.length) {
    target.innerHTML = `<div class="empty">No contacts yet.</div>`;
    return;
  }
  target.innerHTML = contacts.map((contact) => {
    const unverified = contact.verification_status === "unverified";
    const rejected = contact.verification_status === "rejected";
    const sourceLink = String(contact.source_url || "").startsWith("http")
      ? `<a href="${escapeHtml(contact.source_url)}" target="_blank" rel="noreferrer">Source</a>`
      : "";
    return `
      <div class="contact-row ${rejected ? "rejected" : ""}">
        <div>
          <strong>${escapeHtml(contact.name || contact.email)}</strong>
          <p>${escapeHtml(contact.role || "Contact")} · ${escapeHtml(contact.email)}</p>
          <span>${escapeHtml(contact.company)} · ${escapeHtml(contact.email_kind || "manual")} · relevance ${Number(contact.relevance_score || 0)} ${sourceLink}</span>
        </div>
        <div class="contact-actions">
          <span class="status contact-status ${escapeHtml(contact.verification_status)}">${escapeHtml(contact.verification_status)}</span>
          ${unverified ? `<button data-action="contact-verify" data-contact="${contact.id}" class="compact-btn">Verify</button>` : ""}
          ${!rejected && contact.email_kind !== "manual" ? `<button data-action="contact-reject" data-contact="${contact.id}" class="secondary compact-btn">Reject</button>` : ""}
        </div>
      </div>
    `;
  }).join("");
}

function renderOutreach(threads, contacts, applications, discoveryRuns) {
  $("#contactCount").textContent = `${contacts.length} contact${contacts.length === 1 ? "" : "s"}`;
  $("#outreachQueueMeta").textContent = `${threads.length} thread${threads.length === 1 ? "" : "s"}`;
  $("#emailLimitMeta").textContent = `${Number(state.data.email?.sent_today || 0)} / ${Number(state.data.email?.daily_limit || 0)} sent today`;
  const latestRun = discoveryRuns[0];
  $("#discoveryRunMeta").textContent = latestRun
    ? `${formatMode(latestRun.status)} · ${Number(latestRun.pages_scanned || 0)} pages · ${Number(latestRun.candidates_found || 0)} candidates`
    : "";
  $("#discoverContactsBtn").disabled = !applications.length;
  populateOutreachOptions(contacts, applications);
  renderContacts(contacts);

  const target = $("#outreachList");
  if (!threads.length) {
    target.innerHTML = `<div class="empty">No outreach drafts yet.</div>`;
    return;
  }
  const approvalMode = state.data.email?.mode === "approval";
  const deliveryAvailable = Boolean(state.data.email?.available);
  target.innerHTML = threads.map((thread) => {
    const revision = thread.active_revision || {};
    const editable = ["draft", "approved", "failed", "uncertain"].includes(thread.status);
    const stale = Number(thread.writing_version_id) !== Number(thread.current_writing_version_id);
    const trustedContact = ["manual", "published", "verified"].includes(thread.verification_status);
    const canApprove = thread.status === "draft" && !stale && trustedContact;
    const canQueue = !stale && deliveryAvailable && (
      ["approved", "failed", "uncertain"].includes(thread.status) || (!approvalMode && thread.status === "draft")
    ) && trustedContact;
    const revisions = (thread.revisions || []).map((item) => `
      <div class="version-row">
        <details>
          <summary>v${item.version} · ${escapeHtml(item.created_at)}</summary>
          <strong>${escapeHtml(item.subject)}</strong>
          <pre>${escapeHtml(item.body)}</pre>
        </details>
        ${Number(item.id) === Number(thread.active_revision_id) ? `<strong>Current</strong>` : ""}
      </div>
    `).join("");
    return `
      <article class="outreach-card">
        <div class="card-head">
          <div>
            <h4>${escapeHtml(thread.contact_name || thread.recipient_email)}</h4>
            <p class="meta">${escapeHtml(thread.contact_role || "Contact")} · ${escapeHtml(thread.company)} · ${escapeHtml(thread.recipient_email)}</p>
          </div>
          <span class="status outreach-status ${escapeHtml(thread.status)}">${escapeHtml(thread.status)}</span>
        </div>
        <p class="outreach-context">${escapeHtml(thread.title)} · writing v${thread.writing_version_id}${stale ? " · writing changed" : ""}</p>
        ${thread.last_error ? `<p class="error-text">${escapeHtml(thread.last_error)}</p>` : ""}
        <div class="outreach-editor">
          <label>Subject
            <input data-outreach-subject value="${escapeHtml(revision.subject || "")}" ${editable ? "" : "disabled"} />
          </label>
          <label>Message
            <textarea data-outreach-body class="tall-text" ${editable ? "" : "disabled"}>${escapeHtml(revision.body || "")}</textarea>
          </label>
        </div>
        <div class="card-actions">
          <button data-action="outreach-save" data-thread="${thread.id}" class="secondary" ${editable ? "" : "disabled"}>Save new version</button>
          <button data-action="outreach-approve" data-thread="${thread.id}" ${canApprove ? "" : "disabled"}>Approve</button>
          <button data-action="outreach-queue" data-thread="${thread.id}" class="warn" ${canQueue ? "" : "disabled"}>${["failed", "uncertain"].includes(thread.status) ? "Retry" : "Queue email"}</button>
        </div>
        <div class="delivery-meta">
          <span>Attempts ${Number(thread.attempt_count || 0)} / 3</span>
          <span>${thread.sent_at ? `Sent ${escapeHtml(thread.sent_at)}` : `Revision v${revision.version || 1}`}</span>
        </div>
        <details class="version-history">
          <summary>Revision history (${(thread.revisions || []).length})</summary>
          ${revisions}
        </details>
      </article>
    `;
  }).join("");
}

function collectOutreachRevision(button) {
  const card = button.closest(".outreach-card");
  return {
    subject: card.querySelector("[data-outreach-subject]").value,
    body: card.querySelector("[data-outreach-body]").value
  };
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

function renderService(service) {
  const target = $("#serviceDetails");
  $("#serviceRestartBtn").disabled = !service.loaded;
  target.innerHTML = `
    <div>
      <span class="status">${escapeHtml(service.running ? "running" : service.installed ? "installed" : "not installed")}</span>
      <strong>${escapeHtml(service.message || "Service status unavailable.")}</strong>
    </div>
    <div class="service-facts">
      <span>${service.pid ? `PID ${Number(service.pid)}` : "No process"}</span>
      <span>${escapeHtml(service.plist_path || "")}</span>
      <span>${escapeHtml(service.stderr_log || "")}</span>
    </div>
  `;
}

function populateSettings(settings) {
  for (const selector of ["#settingsForm", "#setupPolicyForm"]) {
    const form = $(selector);
    for (const [key, value] of Object.entries(settings)) {
      const fields = form.querySelectorAll(`[name="${key}"]`);
      const selectedValues = new Set(
        String(value).split(",").map((item) => item.trim()).filter(Boolean)
      );
      fields.forEach((field) => {
        if (field.type === "radio") {
          field.checked = field.value === value;
        } else if (field.type === "checkbox") {
          field.checked = fields.length > 1
            ? selectedValues.has(field.value)
            : value === "true";
        } else {
          field.value = value;
        }
      });
    }
    updateCareerStageControls(form);
    updatePostingAgeControls(form);
  }
}

function updateCareerStageControls(form) {
  if (!form) return;
  const selected = form.querySelector("input[name='career_stage_mode']:checked");
  const mode = selected?.value || "graduate";
  form.querySelectorAll("[data-career-stage-mode]").forEach((field) => {
    field.hidden = field.dataset.careerStageMode !== mode;
  });
}

function updatePostingAgeControls(form) {
  if (!form) return;
  const selected = form.querySelector("input[name='posted_age_mode']:checked");
  const mode = selected?.value || "days";
  form.querySelectorAll("[data-posted-age-mode]").forEach((field) => {
    field.hidden = field.dataset.postedAgeMode !== mode;
  });
}

function updateCareerSourceInput() {
  const config = careerSourceTypes[$("#careerSourceType")?.value];
  if ($("#careerSourceUrl") && config) {
    $("#careerSourceUrl").placeholder = config.placeholder;
  }
}

function careerSourceMatchesType(url, config) {
  if (!config.hosts.length) return true;
  const hostname = url.hostname.toLowerCase();
  const hostMatches = config.hosts.some(
    (host) => hostname === host || hostname.endsWith(`.${host}`)
  );
  const hasCompanyPath = url.pathname.split("/").some(Boolean);
  return hostMatches && hasCompanyPath;
}

function setView(view) {
  state.activeView = view;
  $$(".view").forEach((el) => el.classList.toggle("active", el.id === view));
  $$(".nav-item").forEach((el) => el.classList.toggle("active", el.dataset.view === view));
  const titles = {
    overview: ["Workflow", "Scan, approve, tailor, apply, and repeat."],
    setup: ["Setup", "Verify local capabilities and choose a guarded operating policy."],
    documents: ["Documents", "Manage source files, extraction review, OCR, and profile evidence."],
    jobs: ["Jobs", "Scan broad providers, open assisted searches, and draft packages."],
    pipeline: ["Pipeline", "Track automatic preparation, review, and guarded browser work."],
    approvals: ["Approvals", "Review packages, browser checkpoints, outreach, and failures in one queue."],
    applications: ["Applications", "Approve, submit, or queue browser automation."],
    outreach: ["Outreach", "Review contacts, approve messages, and track delivery."],
    settings: ["Settings", "Tune modes, limits, discovery providers, and company sources."],
    logs: ["Logs", "Plain-English worker activity and blockers."]
  };
  $("#viewTitle").textContent = titles[view][0];
  $("#viewSubtitle").textContent = titles[view][1];
}

async function handleAction(action, button) {
  button.disabled = true;
  try {
    if (action === "approval-resolve") {
      const confirmation = button.dataset.confirm;
      if (confirmation && !window.confirm(confirmation)) return;
      const item = button.closest(".approval-row");
      const answers = {};
      item.querySelectorAll("[data-approval-answer]").forEach((field) => {
        if (field.value.trim()) answers[field.dataset.question] = field.value.trim();
      });
      const result = await api("/api/approvals/action", {
        method: "POST",
        body: JSON.stringify({
          approval_item_id: Number(button.dataset.approval),
          action: button.dataset.resolution,
          note: item.querySelector("[data-approval-note]")?.value || "",
          payload: { answers, save_rules: true }
        })
      });
      toast(`Decision recorded: ${formatMode(result.item.resolution)}.`);
      if (["sign_in", "manual_takeover"].includes(button.dataset.resolution)) {
        setView("applications");
      }
    } else if (action === "readiness-open") {
      setView(button.dataset.view);
    } else if (action === "workflow-open-materials") {
      state.workflowTab = "materials";
      setView("overview");
    } else if (action === "workflow-open-applying") {
      state.workflowTab = "applying";
      setView("overview");
    } else if (action === "workflow-job-decision") {
      await api("/api/jobs/decision", {
        method: "POST",
        body: JSON.stringify({
          job_id: Number(button.dataset.job),
          decision: button.dataset.decision
        })
      });
      toast(button.dataset.decision === "reject" ? "Job removed from the queue." : "Job decision saved.");
    } else if (action === "workflow-tailor") {
      const application = await api("/api/applications/draft", {
        method: "POST",
        body: JSON.stringify({ job_id: Number(button.dataset.job) })
      });
      try {
        await api("/api/applications/writing/queue", {
          method: "POST",
          body: JSON.stringify({ application_id: Number(application.id) })
        });
        toast("Approved. Codex is tailoring the application package.");
      } catch (error) {
        toast(`Package created, but tailoring needs attention: ${error.message}`);
      }
      state.workflowTab = "materials";
      setView("overview");
    } else if (action === "document-update") {
      const item = button.closest(".document-row");
      const result = await api("/api/documents/update", {
        method: "POST",
        body: JSON.stringify({
          document_id: Number(button.dataset.document),
          name: item.querySelector("[data-document-name]").value,
          kind: item.querySelector("[data-document-kind]").value
        })
      });
      toast(`Updated ${result.name}.`);
    } else if (action === "document-approve") {
      await api("/api/documents/approve", {
        method: "POST",
        body: JSON.stringify({ document_id: Number(button.dataset.document) })
      });
      toast("Document evidence approved.");
    } else if (action === "document-retry") {
      const result = await api("/api/documents/retry", {
        method: "POST",
        body: JSON.stringify({ document_id: Number(button.dataset.document) })
      });
      toast(`Document extraction status: ${formatMode(result.ingest_status)}.`);
    } else if (action === "document-archive") {
      await api("/api/documents/archive", {
        method: "POST",
        body: JSON.stringify({ document_id: Number(button.dataset.document) })
      });
      toast("Document archived.");
    } else if (action === "document-restore") {
      await api("/api/documents/restore", {
        method: "POST",
        body: JSON.stringify({ document_id: Number(button.dataset.document) })
      });
      toast("Document restored.");
    } else if (action === "document-remove") {
      if (!window.confirm("Permanently remove this document from local storage?")) return;
      await api("/api/documents/remove", {
        method: "POST",
        body: JSON.stringify({ document_id: Number(button.dataset.document) })
      });
      toast("Document removed.");
    } else if (action === "draft") {
      await api("/api/applications/draft", {
        method: "POST",
        body: JSON.stringify({ job_id: Number(button.dataset.job) })
      });
      toast("Application package drafted.");
      setView("applications");
    } else if (action === "pipeline-open") {
      setView("applications");
    } else if (action === "pipeline-retry") {
      const result = await api("/api/pipeline/retry", {
        method: "POST",
        body: JSON.stringify({ pipeline_item_id: Number(button.dataset.pipeline) })
      });
      toast(result.message || "Pipeline item queued for retry.");
    } else if (action === "pipeline-skip") {
      if (!window.confirm("Skip this job in the automatic application pipeline?")) return;
      const result = await api("/api/pipeline/skip", {
        method: "POST",
        body: JSON.stringify({ pipeline_item_id: Number(button.dataset.pipeline) })
      });
      toast(result.message || "Pipeline item skipped.");
    } else if (action === "approve") {
      await api("/api/applications/approve", {
        method: "POST",
        body: JSON.stringify({ application_id: Number(button.dataset.app) })
      });
      toast("Application approved.");
    } else if (action === "approve-apply") {
      if (!window.confirm("Approve these materials and authorize submission to the employer?")) return;
      await api("/api/applications/approve", {
        method: "POST",
        body: JSON.stringify({ application_id: Number(button.dataset.app) })
      });
      const result = await api("/api/applications/apply", {
        method: "POST",
        body: JSON.stringify({
          application_id: Number(button.dataset.app),
          final_submit_approved: true
        })
      });
      state.workflowTab = result.status === "blocked" ? "materials" : "applying";
      setView("overview");
      toast(result.message || "Approved materials queued for browser application.");
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
    } else if (action === "task-resolve") {
      const section = button.closest(".browser-task");
      const answers = {};
      section.querySelectorAll("[data-checkpoint-answer]").forEach((field) => {
        if (field.value.trim()) answers[field.dataset.question] = field.value.trim();
      });
      const result = await api("/api/applications/task/resolve", {
        method: "POST",
        body: JSON.stringify({
          task_id: Number(button.dataset.task),
          answers,
          save_rules: true
        })
      });
      toast(result.message || "Browser task continued.");
    } else if (action === "task-submit") {
      if (!window.confirm("Submit this application now? This is the final external action.")) return;
      const result = await api("/api/applications/task/resolve", {
        method: "POST",
        body: JSON.stringify({
          task_id: Number(button.dataset.task),
          approve_submit: true
        })
      });
      toast(result.message || "Final submission approved.");
    } else if (action === "task-login-start") {
      const result = await api("/api/browser-sessions/login/start", {
        method: "POST",
        body: JSON.stringify({ task_id: Number(button.dataset.task) })
      });
      toast(result.message || "Sign-in window opened.");
    } else if (action === "task-login-complete") {
      const result = await api("/api/browser-sessions/login/complete", {
        method: "POST",
        body: JSON.stringify({ browser_session_id: Number(button.dataset.session) })
      });
      toast(result.message || "Sign-in completion received.");
    } else if (action === "task-login-cancel") {
      const result = await api("/api/browser-sessions/login/cancel", {
        method: "POST",
        body: JSON.stringify({ browser_session_id: Number(button.dataset.session) })
      });
      toast(result.message || "Sign-in cancelled.");
    } else if (action === "task-takeover-start") {
      const result = await api("/api/browser-sessions/takeover/start", {
        method: "POST",
        body: JSON.stringify({ task_id: Number(button.dataset.task) })
      });
      toast(result.message || "Manual browser opened.");
    } else if (action === "task-takeover-resume") {
      const result = await api("/api/browser-sessions/takeover/complete", {
        method: "POST",
        body: JSON.stringify({
          browser_session_id: Number(button.dataset.session),
          outcome: "resume"
        })
      });
      toast(result.message || "Manual step completion received.");
    } else if (action === "task-takeover-submitted") {
      if (!window.confirm("Confirm only after the employer site shows that the application was submitted.")) return;
      const result = await api("/api/browser-sessions/takeover/complete", {
        method: "POST",
        body: JSON.stringify({
          browser_session_id: Number(button.dataset.session),
          outcome: "submitted"
        })
      });
      toast(result.message || "Manual submission verification started.");
    } else if (action === "task-takeover-cancel") {
      const result = await api("/api/browser-sessions/takeover/cancel", {
        method: "POST",
        body: JSON.stringify({ browser_session_id: Number(button.dataset.session) })
      });
      toast(result.message || "Manual takeover cancelled.");
    } else if (action === "browser-session-clear") {
      if (!window.confirm("Clear local cookies and site data for this ATS session?")) return;
      const result = await api("/api/browser-sessions/clear", {
        method: "POST",
        body: JSON.stringify({ browser_session_id: Number(button.dataset.session) })
      });
      toast(result.message || "Browser session cleared.");
    } else if (action === "task-cancel") {
      if (!window.confirm("Cancel this browser application task?")) return;
      await api("/api/applications/task/cancel", {
        method: "POST",
        body: JSON.stringify({ task_id: Number(button.dataset.task) })
      });
      toast("Browser application task cancelled.");
    } else if (action === "task-retry") {
      const resetCircuit = button.dataset.resetCircuit === "true";
      if (resetCircuit && !window.confirm("Reset this ATS circuit and retry the pre-submit browser task now?")) return;
      const result = await api("/api/applications/task/retry", {
        method: "POST",
        body: JSON.stringify({
          task_id: Number(button.dataset.task),
          reset_circuit: resetCircuit
        })
      });
      toast(result.message || "Browser task re-queued.");
    } else if (action === "circuit-reset") {
      if (!window.confirm("Reset this ATS circuit and release its waiting browser tasks?")) return;
      const result = await api("/api/browser-recovery/circuit/reset", {
        method: "POST",
        body: JSON.stringify({
          adapter: button.dataset.adapter,
          hostname: button.dataset.hostname
        })
      });
      toast(result.message || "ATS circuit reset.");
    } else if (action === "adapter-reactivate") {
      if (!window.confirm("Reactivate this ATS adapter host and re-queue its paused applications?")) return;
      const result = await api("/api/ats-adapters/reactivate", {
        method: "POST",
        body: JSON.stringify({
          adapter: button.dataset.adapter,
          hostname: button.dataset.hostname
        })
      });
      toast(result.message || "ATS adapter reactivated.");
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
    } else if (action === "outreach-save") {
      const revision = collectOutreachRevision(button);
      const result = await api("/api/outreach/save", {
        method: "POST",
        body: JSON.stringify({ thread_id: Number(button.dataset.thread), ...revision })
      });
      toast(`Outreach revision ${result.active_revision.version} saved. Approval reset.`);
    } else if (action === "outreach-approve") {
      await api("/api/outreach/approve", {
        method: "POST",
        body: JSON.stringify({ thread_id: Number(button.dataset.thread) })
      });
      toast("Outreach message approved.");
    } else if (action === "outreach-queue") {
      const result = await api("/api/outreach/queue", {
        method: "POST",
        body: JSON.stringify({ thread_id: Number(button.dataset.thread) })
      });
      toast(result.status === "sent" ? "Outreach was already sent." : "Outreach queued for delivery.");
    } else if (action === "contact-verify") {
      if (!window.confirm("Confirm that you verified this inferred email address from a reliable source.")) return;
      await api("/api/contacts/verify", {
        method: "POST",
        body: JSON.stringify({ contact_id: Number(button.dataset.contact) })
      });
      toast("Contact verified for outreach.");
    } else if (action === "contact-reject") {
      if (!window.confirm("Reject this discovered contact? Existing unsent outreach will remain blocked.")) return;
      await api("/api/contacts/reject", {
        method: "POST",
        body: JSON.stringify({ contact_id: Number(button.dataset.contact) })
      });
      toast("Contact rejected.");
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
  $$(".workflow-tabs button").forEach((button) => {
    button.addEventListener("click", () => {
      state.workflowTab = button.dataset.workflowTab;
      renderWorkflow(state.data);
    });
  });

  $("#refreshBtn").addEventListener("click", loadState);

  $("#readinessRunBtn").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await api("/api/readiness/run", { method: "POST", body: "{}" });
      toast(`Preflight complete: ${Number(result.score || 0)}% ready.`);
      await loadState();
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  $("#readinessCompleteBtn").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      await api("/api/readiness/complete", { method: "POST", body: "{}" });
      toast("Readiness setup completed.");
      await loadState();
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  $("#readinessCodexBtn").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await api("/api/readiness/test-codex", {
        method: "POST",
        body: "{}"
      });
      toast(result.message || "Codex connection verified.");
      await loadState();
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  $("#readinessEmailBtn").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await api("/api/readiness/test-email", {
        method: "POST",
        body: "{}"
      });
      toast(result.message || "Email login verified.");
      await loadState();
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  $("#documentPickerBtn").addEventListener("click", () => $("#documentFileInput").click());

  $("#documentFileInput").addEventListener("change", async (event) => {
    try {
      await uploadDocuments(event.currentTarget.files);
      event.currentTarget.value = "";
    } catch (error) {
      toast(error.message);
    }
  });

  const dropzone = $("#documentDropzone");
  ["dragenter", "dragover"].forEach((name) => {
    dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      dropzone.classList.add("dragging");
    });
  });
  ["dragleave", "drop"].forEach((name) => {
    dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      dropzone.classList.remove("dragging");
    });
  });
  dropzone.addEventListener("drop", async (event) => {
    try {
      await uploadDocuments(event.dataTransfer.files);
    } catch (error) {
      toast(error.message);
    }
  });
  $("#documentUploadForm").addEventListener("submit", (event) => event.preventDefault());

  $("#ingestBtn").addEventListener("click", async () => {
    await api("/api/docs/ingest", { method: "POST", body: "{}" });
    toast("Documents ingested.");
    await loadState();
  });

  $("#scanBtn").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const [mode, value] = $("#workflowAgePreset").value.split(":");
    state.scanRunning = true;
    renderWorkflow(state.data);
    try {
      await api("/api/settings", {
        method: "POST",
        body: JSON.stringify({
          posted_age_mode: mode,
          posted_within_hours: mode === "hours" ? value : state.data.settings.posted_within_hours,
          posted_within_days: mode === "days" ? value : state.data.settings.posted_within_days,
          include_unknown_posted_at: $("#workflowUnknownDates").checked ? "true" : "false",
          locations: "United States",
          graduate_include_internships: "true",
          graduate_max_required_experience_years: "3",
          work_authorization_mode: "cpt_opt_future_sponsorship",
          sponsorship_unknown_handling: "review"
        })
      });
      const result = await api("/api/jobs/scan", { method: "POST", body: "{}" });
      state.scanResult = result;
      toast(`Scan complete: ${result.inserted} new, ${result.seen} refreshed, ${result.filtered} filtered, ${result.errors} errors, ${result.skipped || 0} rate-limited.`);
    } catch (error) {
      toast(error.message);
    } finally {
      state.scanRunning = false;
      await loadState().catch((error) => toast(error.message));
    }
  });

  $("#pipelineRunBtn").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await api("/api/pipeline/run", { method: "POST", body: "{}" });
      toast(`Pipeline checked ${result.checked}; ${result.queued} queued and ${result.advanced} advanced.`);
      await loadState();
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  $("#serviceRestartBtn").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await api("/api/service/restart", { method: "POST", body: "{}" });
      toast(result.message || "Background service restart scheduled.");
      setTimeout(() => loadState().catch(() => {}), 4000);
    } catch (error) {
      toast(error.message);
      button.disabled = false;
    }
  });

  $("#notificationTestBtn").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await api("/api/notifications/test", { method: "POST", body: "{}" });
      toast(result.message || "Test notification sent.");
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  $("#jobForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
    await api("/api/jobs", { method: "POST", body: JSON.stringify(payload) });
    event.currentTarget.reset();
    toast("Job saved.");
    await loadState();
  });

  $("#contactForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
      await api("/api/contacts", { method: "POST", body: JSON.stringify(payload) });
      event.currentTarget.reset();
      toast("Contact added.");
      await loadState();
    } catch (error) {
      toast(error.message);
    }
  });

  $("#outreachForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
      await api("/api/outreach/draft", { method: "POST", body: JSON.stringify(payload) });
      toast("Outreach draft ready for review.");
      await loadState();
    } catch (error) {
      toast(error.message);
    }
  });

  $("#outreachApplication").addEventListener("change", () => updateOutreachContactOptions());

  $("#discoverContactsBtn").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await api("/api/contacts/discover", {
        method: "POST",
        body: JSON.stringify({
          application_id: Number($("#outreachApplication").value),
          company_url: $("#contactDiscoveryUrl").value
        })
      });
      toast(`Discovery complete: ${result.contacts_added} new, ${result.contacts_updated} refreshed.`);
      await loadState();
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  $("#settingsForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
    payload.include_unknown_posted_at = event.currentTarget.elements
      .include_unknown_posted_at.checked ? "true" : "false";
    payload.graduate_include_internships = event.currentTarget.elements
      .graduate_include_internships.checked ? "true" : "false";
    payload.target_role_families = Array.from(
      event.currentTarget.querySelectorAll(
        "input[name='target_role_families']:checked"
      )
    ).map((field) => field.value).join(",");
    payload.discovery_providers = Array.from(
      event.currentTarget.querySelectorAll(
        "input[name='discovery_providers']:checked"
      )
    ).map((field) => field.value).join(",");
    await api("/api/settings", { method: "POST", body: JSON.stringify(payload) });
    toast("Settings saved.");
    await loadState();
  });

  $("#setupPolicyForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
    await api("/api/settings", { method: "POST", body: JSON.stringify(payload) });
    toast("Operating policy saved.");
    await loadState();
  });

  $("#settingsForm").querySelectorAll("input[name='posted_age_mode']").forEach((field) => {
    field.addEventListener("change", () => updatePostingAgeControls($("#settingsForm")));
  });

  $("#settingsForm").querySelectorAll("input[name='career_stage_mode']").forEach((field) => {
    field.addEventListener("change", () => updateCareerStageControls($("#settingsForm")));
  });

  $("#careerSourceType").addEventListener("change", updateCareerSourceInput);
  updateCareerSourceInput();

  $("#addCareerSourceBtn").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const type = $("#careerSourceType").value;
    const config = careerSourceTypes[type];
    const rawUrl = $("#careerSourceUrl").value.trim();
    let parsed;
    try {
      parsed = new URL(rawUrl);
      if (!["http:", "https:"].includes(parsed.protocol)) throw new Error();
    } catch {
      toast("Enter a valid company career URL.");
      return;
    }
    if (!careerSourceMatchesType(parsed, config)) {
      toast(`Enter a ${config.label} company board URL.`);
      return;
    }

    parsed.hash = "";
    const sourceUrl = parsed.toString();
    const sourceKey = sourceUrl.replace(/\/$/, "").toLowerCase();
    const field = $("#settingsForm").elements.career_urls;
    const sources = String(field.value || "")
      .split(/[\n,]+/)
      .map((item) => item.trim())
      .filter(Boolean);
    if (sources.some((item) => item.replace(/\/$/, "").toLowerCase() === sourceKey)) {
      toast("That career source is already configured.");
      return;
    }

    button.disabled = true;
    try {
      const careerUrls = [...sources, sourceUrl].join("\n");
      await api("/api/settings", {
        method: "POST",
        body: JSON.stringify({ career_urls: careerUrls })
      });
      $("#careerSourceUrl").value = "";
      toast(`${config.label} source added.`);
      await loadState();
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
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
