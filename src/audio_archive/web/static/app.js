(() => {
  "use strict";

  const pollInterval = Number(document.body.dataset.pollInterval || 2000);
  const addForm = document.querySelector("#add-form");
  const addMessage = document.querySelector("#add-message");
  const csvForm = document.querySelector("#csv-form");
  const csvPreview = document.querySelector("#csv-preview");
  const queueIndicator = document.querySelector("#queue-indicator");
  const queueList = document.querySelector("#queue-list");
  const queueEmpty = document.querySelector("#queue-empty");
  const reviewList = document.querySelector("#review-list");
  const reviewEmpty = document.querySelector("#review-empty");

  let pollTimer = null;
  let statusRequestActive = false;

  async function api(path, options = {}) {
    const response = await fetch(path, options);
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json") ? await response.json() : null;
    if (!response.ok) {
      const detail = payload && payload.detail ? payload.detail : `Request failed (${response.status})`;
      throw new Error(detail);
    }
    return payload;
  }

  function el(tag, options = {}) {
    const node = document.createElement(tag);
    if (options.className) node.className = options.className;
    if (options.text !== undefined && options.text !== null) node.textContent = String(options.text);
    if (options.type) node.type = options.type;
    if (options.href) node.href = options.href;
    if (options.placeholder) node.placeholder = options.placeholder;
    if (options.title) node.title = options.title;
    return node;
  }

  function requestedName(job) {
    const parts = [job.artist, job.title].filter(Boolean);
    return parts.length ? parts.join(" — ") : (job.requested_url || "Untitled request");
  }

  function formatDuration(seconds) {
    if (seconds === null || seconds === undefined) return "Duration unknown";
    const total = Math.max(0, Math.round(Number(seconds)));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    return hours
      ? `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`
      : `${minutes}:${String(secs).padStart(2, "0")}`;
  }

  function stateLabel(state) {
    return String(state || "unknown").replaceAll("_", " ");
  }

  function detailFor(job) {
    if (job.error_summary) return job.error_summary;
    if (job.warning_summary) return job.warning_summary;
    if (job.quality_status) return job.quality_status.replaceAll("_", " ");
    if (job.source_title && job.source_title !== requestedName(job)) return job.source_title;
    if (job.resolution_method === "automatic" && job.selected_score !== null) {
      return `Automatic match · score ${job.selected_score}`;
    }
    if (job.state === "pending") return "Waiting for source resolution";
    if (job.state === "needs_review") return "Source decision required";
    return "";
  }

  function button(text, className, handler) {
    const node = el("button", { className, text, type: "button" });
    node.addEventListener("click", handler);
    return node;
  }

  async function post(path, body = undefined) {
    return api(path, { method: "POST", body });
  }

  async function jobAction(path, body = undefined) {
    await post(path, body);
    await refreshStatus();
  }

  function renderQueueIndicator(queue) {
    queueIndicator.className = "status-pill";
    if (queue.last_error) {
      queueIndicator.textContent = `Queue error: ${queue.last_error}`;
      return;
    }
    if (queue.paused) {
      queueIndicator.textContent = queue.active ? "Pausing after current" : "Queue paused";
      queueIndicator.classList.add("paused");
      return;
    }
    if (queue.active) {
      queueIndicator.textContent = "Queue running";
      queueIndicator.classList.add("active");
      return;
    }
    queueIndicator.textContent = "Queue idle";
  }

  function renderJob(job) {
    const row = el("article", { className: "job-row" });

    const title = el("div", { className: "job-title" });
    title.append(
      el("strong", { text: requestedName(job) }),
      el("span", { text: job.source_creator || job.version || `Job ${job.id}` })
    );
    if (Array.isArray(job.ableton_paths) && job.ableton_paths.length) {
      title.append(el("div", { className: "path-note", text: job.ableton_paths[0] }));
    }

    const state = el("div", { className: `job-state ${job.state}`, text: stateLabel(job.state) });
    const profile = el("div", { className: "job-detail", text: job.profile });

    const detail = el("div", { className: "job-detail" });
    detail.append(el("span", { text: detailFor(job) }));
    if (job.progress_percent !== null && job.progress_percent !== undefined) {
      const track = el("div", { className: "progress-track" });
      const bar = el("div", { className: "progress-bar" });
      bar.style.width = `${Math.max(0, Math.min(100, Number(job.progress_percent)))}%`;
      track.append(bar);
      detail.append(track);
    }

    const actions = el("div", { className: "job-actions" });
    if (["failed", "interrupted"].includes(job.state)) {
      actions.append(button("Retry", "secondary", () => jobAction(`/api/jobs/${job.id}/retry`)));
    }
    if (["pending", "ready", "needs_review"].includes(job.state)) {
      actions.append(button("Cancel", "ghost", () => jobAction(`/api/jobs/${job.id}/cancel`)));
    }
    if (job.item_directory) {
      actions.append(button("Open folder", "ghost", () => jobAction(`/api/jobs/${job.id}/open-folder`)));
    }
    if (Array.isArray(job.ableton_paths) && job.ableton_paths.length) {
      actions.append(button("Copy WAV path", "ghost", async () => {
        await navigator.clipboard.writeText(job.ableton_paths[0]);
      }));
    }

    row.append(title, state, profile, detail, actions);
    return row;
  }

  function renderCandidate(job, candidate) {
    const card = el("article", { className: "candidate" });
    if (candidate.thumbnail_url) {
      const image = document.createElement("img");
      image.src = candidate.thumbnail_url;
      image.alt = "";
      image.loading = "lazy";
      image.referrerPolicy = "no-referrer";
      card.append(image);
    } else {
      card.append(el("div"));
    }

    const body = el("div", { className: "candidate-body" });
    body.append(
      el("div", { className: "candidate-title", text: candidate.title }),
      el("div", {
        className: "candidate-meta",
        text: `${candidate.channel || "Unknown channel"} · ${formatDuration(candidate.duration_seconds)}`,
      })
    );

    const score = el("div", { className: "score", text: `Score ${candidate.score}` });
    body.append(score);

    const warnings = el("div", { className: "candidate-warnings" });
    const warningText = Array.isArray(candidate.warnings) && candidate.warnings.length
      ? candidate.warnings.join(" · ")
      : candidate.disqualified ? "Resolver flagged this candidate" : "No resolver warnings";
    warnings.textContent = warningText;
    body.append(warnings);

    const actions = el("div", { className: "candidate-actions" });
    actions.append(button("Use this source", "primary", () => jobAction(
      `/api/jobs/${job.id}/approve/${encodeURIComponent(candidate.video_id)}`
    )));
    const youtube = el("a", { href: candidate.url, text: "Open on YouTube" });
    youtube.target = "_blank";
    youtube.rel = "noopener noreferrer";
    actions.append(youtube);
    body.append(actions);
    card.append(body);
    return card;
  }

  function renderReview(job) {
    const card = el("article", { className: "review-card" });
    const head = el("div", { className: "review-head" });
    const copy = el("div");
    copy.append(
      el("h3", { text: requestedName(job) }),
      el("p", { text: `Top score ${job.selected_score ?? "—"} · runner-up ${job.runner_up_score ?? "—"}` })
    );
    head.append(copy, el("span", { className: "badge warning", text: "Decision required" }));
    card.append(head);

    const candidates = el("div", { className: "candidate-grid" });
    for (const candidate of job.candidates || []) candidates.append(renderCandidate(job, candidate));
    card.append(candidates);

    const footer = el("div", { className: "review-footer" });
    const replacement = document.createElement("input");
    replacement.type = "url";
    replacement.placeholder = "Paste a replacement YouTube URL";
    replacement.setAttribute("aria-label", "Replacement YouTube URL");
    footer.append(replacement);
    footer.append(button("Use URL", "secondary", async () => {
      const form = new FormData();
      form.set("url", replacement.value);
      await jobAction(`/api/jobs/${job.id}/replace-source`, form);
    }));
    footer.append(button("Not found", "danger", () => jobAction(`/api/jobs/${job.id}/not-found`)));
    card.append(footer);
    return card;
  }

  function renderStatus(payload) {
    renderQueueIndicator(payload.queue);

    queueList.replaceChildren();
    const jobs = Array.isArray(payload.jobs) ? payload.jobs.slice().reverse() : [];
    queueEmpty.hidden = jobs.length > 0;
    for (const job of jobs) queueList.append(renderJob(job));

    reviewList.replaceChildren();
    const reviews = jobs.filter((job) => job.state === "needs_review");
    reviewEmpty.hidden = reviews.length > 0;
    for (const job of reviews) reviewList.append(renderReview(job));
  }

  async function refreshStatus() {
    if (statusRequestActive) return;
    statusRequestActive = true;
    try {
      renderStatus(await api("/api/status"));
    } catch (error) {
      queueIndicator.textContent = error.message;
      queueIndicator.className = "status-pill";
    } finally {
      statusRequestActive = false;
    }
  }

  function renderCsvPreview(payload) {
    csvPreview.hidden = false;
    csvPreview.replaceChildren();
    csvPreview.append(el("h3", { text: payload.filename }));

    const summary = el("div", { className: "csv-summary" });
    summary.append(
      el("span", { className: "badge", text: `${payload.accepted.length} accepted` }),
      el("span", { className: payload.rejected.length ? "badge error" : "badge", text: `${payload.rejected.length} rejected` }),
      el("span", { className: payload.duplicate_rows.length ? "badge warning" : "badge", text: `${payload.duplicate_rows.length} duplicates` })
    );
    csvPreview.append(summary);

    if (payload.rejected.length) {
      const errors = el("ul", { className: "csv-errors" });
      for (const item of payload.rejected) {
        errors.append(el("li", { text: `Row ${item.row}: ${item.message}` }));
      }
      csvPreview.append(errors);
    }

    if (payload.duplicate_rows.length) {
      csvPreview.append(el("p", {
        className: "form-message",
        text: `Duplicate rows skipped: ${payload.duplicate_rows.join(", ")}`,
      }));
    }

    if (payload.accepted.length) {
      const start = button("Start Queue", "primary", async () => {
        const form = new FormData();
        form.set("start_queue", "true");
        const result = await post(`/api/csv/import/${payload.token}`, form);
        csvPreview.replaceChildren(el("p", {
          className: "form-message",
          text: `Created ${result.job_ids.length} queue job${result.job_ids.length === 1 ? "" : "s"}.`,
        }));
        await refreshStatus();
      });
      csvPreview.append(start);
    }
  }

  addForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    addMessage.className = "form-message";
    addMessage.textContent = "Adding…";
    try {
      const result = await post("/api/jobs", new FormData(addForm));
      addMessage.textContent = `Job ${result.job_id} added.`;
      const profile = addForm.elements.profile.value;
      addForm.reset();
      addForm.elements.profile.value = profile;
      await refreshStatus();
    } catch (error) {
      addMessage.classList.add("error");
      addMessage.textContent = error.message;
    }
  });

  csvForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    csvPreview.hidden = false;
    csvPreview.replaceChildren(el("p", { className: "form-message", text: "Reading CSV…" }));
    try {
      renderCsvPreview(await post("/api/csv/preview", new FormData(csvForm)));
    } catch (error) {
      csvPreview.replaceChildren(el("p", { className: "form-message error", text: error.message }));
    }
  });

  document.querySelector("#queue-start").addEventListener("click", async () => {
    await post("/api/queue/start");
    await refreshStatus();
  });
  document.querySelector("#queue-pause").addEventListener("click", async () => {
    await post("/api/queue/pause");
    await refreshStatus();
  });
  document.querySelector("#queue-resume").addEventListener("click", async () => {
    await post("/api/queue/resume");
    await refreshStatus();
  });

  refreshStatus();
  pollTimer = window.setInterval(refreshStatus, Number.isFinite(pollInterval) ? pollInterval : 2000);
  window.addEventListener("beforeunload", () => window.clearInterval(pollTimer));
})();
