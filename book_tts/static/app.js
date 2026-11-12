"use strict";

function toast(message, error = false) {
  const node = document.getElementById("toast");
  node.textContent = message;
  node.className = error ? "error" : "";
  node.hidden = false;
  clearTimeout(window.toastTimer);
  window.toastTimer = setTimeout(
    () => {
      node.hidden = true;
    },
    error ? 10000 : 3500,
  );
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `Ошибка ${response.status}`;
    try {
      const data = await response.json();
      message =
        typeof data.detail === "string"
          ? data.detail
          : "Проверьте заполнение полей";
    } catch {
      /* Preserve the HTTP error. */
    }
    throw new Error(message);
  }
  return response.json();
}

function action(handler) {
  return async (event) => {
    const button = event?.currentTarget;
    if (button) button.disabled = true;
    try {
      await handler(event);
    } catch (error) {
      toast(error.message, true);
    } finally {
      if (button) button.disabled = button.dataset.idleDisabled === "true";
    }
  };
}

function setDisabled(button, value) {
  button.dataset.idleDisabled = String(value);
  button.disabled = value;
}

const zone = document.getElementById("uploadZone");
if (zone) {
  let uploading = false;
  const upload = async (file) => {
    if (!file || uploading) return;
    uploading = true;
    const status = document.getElementById("uploadStatus");
    status.hidden = false;
    status.textContent = "Читаем книгу и собираем главы…";
    try {
      if (file.size > 40 * 1024 * 1024)
        throw new Error("Лимит загрузки — 40 МБ");
      const form = new FormData();
      form.append("file", file);
      const result = await api("/api/books", { method: "POST", body: form });
      location.href = result.url;
    } catch (error) {
      status.textContent = error.message;
      toast(error.message, true);
      uploading = false;
      document.getElementById("bookFile").value = "";
    }
  };
  document
    .getElementById("bookFile")
    .addEventListener("change", (e) => upload(e.target.files[0]));
  for (const name of ["dragenter", "dragover"])
    zone.addEventListener(name, (e) => {
      e.preventDefault();
      zone.classList.add("dragover");
    });
  zone.addEventListener("dragleave", () => zone.classList.remove("dragover"));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("dragover");
    upload(e.dataTransfer.files[0]);
  });
}

const workspace = document.getElementById("workspace");
if (workspace) {
  const id = workspace.dataset.bookId;
  const endpoint = `/api/books/${id}`;
  const byId = (name) => document.getElementById(name);
  const catalog = JSON.parse(byId("engineCatalog").textContent);
  const chapters = [...document.querySelectorAll(".chapter")];
  let revision = Number(workspace.dataset.revision);
  let dirty = false;
  let busy = false;
  let saving = false;
  let editGeneration = 0;
  let offset = 0;
  let resultsSignature = "";

  function markDirty() {
    dirty = true;
    editGeneration += 1;
    byId("saveState").textContent = "Есть несохранённые изменения";
    byId("selectedCount").textContent =
      `${chapters.filter((c) => c.querySelector(".chapter-included").checked).length} выбрано`;
  }

  function updateVoices(preferred) {
    const engine = catalog.find((e) => e.id === byId("engine").value);
    const language = byId("language").value;
    const old = preferred || byId("voice").value;
    const voices = (engine?.voices || []).filter(
      (v) => v.language === language,
    );
    byId("voice").replaceChildren(
      ...voices.map((v) => new Option(v.label, v.id, false, v.id === old)),
    );
    if (!voices.length)
      byId("voice").append(new Option("Нет доступных голосов", ""));
    byId("engineHint").textContent = engine?.hint || "Worker не настроен";
    if (engine && !engine.languages.includes(language)) {
      byId("engineHint").textContent =
        "Этот движок не поддерживает выбранный язык. Выберите другой.";
    }
  }
  updateVoices(byId("voice").dataset.current);
  byId("engine").addEventListener("change", () => updateVoices());
  byId("language").addEventListener("change", () => updateVoices());
  byId("speed").addEventListener("input", () => {
    byId("speedValue").textContent =
      `${Number(byId("speed").value).toFixed(2)}×`;
  });
  workspace.addEventListener("input", (e) => {
    if (!e.target.closest("#onlyWarnings")) markDirty();
  });
  workspace.addEventListener("change", (e) => {
    if (e.target.matches("select, .chapter-included, #numbers")) markDirty();
  });
  document
    .querySelectorAll(".chapter-included")
    .forEach((input) =>
      input.addEventListener("click", (e) => e.stopPropagation()),
    );
  byId("selectedCount").textContent =
    `${chapters.filter((c) => c.querySelector(".chapter-included").checked).length} выбрано`;
  byId("selectAll").addEventListener("click", () => {
    chapters.forEach((c) => {
      c.querySelector(".chapter-included").checked = c.dataset.note === "0";
    });
    markDirty();
  });
  byId("selectNone").addEventListener("click", () => {
    chapters.forEach((c) => {
      c.querySelector(".chapter-included").checked = false;
    });
    markDirty();
  });

  async function save() {
    if (!dirty) return;
    if (saving) throw new Error("Подождите завершения сохранения");
    if (busy) throw new Error("Сначала приостановите обработку книги");
    saving = true;
    const generation = editGeneration;
    const data = {
      revision,
      title: byId("title").value.trim(),
      author: byId("author").value.trim(),
      language: byId("language").value,
      dictionary: byId("dictionary").value,
      profile: {
        engine: byId("engine").value,
        voice: byId("voice").value,
        speed: Number(byId("speed").value),
        numbers: byId("numbers").checked,
      },
      chapters: chapters.map((c) => ({
        id: Number(c.dataset.chapterId),
        title: c.querySelector(".chapter-title").value,
        text: c.querySelector(".chapter-text").value,
        included: c.querySelector(".chapter-included").checked,
      })),
    };
    try {
      const result = await api(endpoint, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
      });
      revision = result.revision;
      dirty = editGeneration !== generation;
      byId("saveState").textContent = dirty
        ? "Есть несохранённые изменения"
        : "Все изменения сохранены";
      byId("pageTitle").textContent = data.title;
      chapters.forEach((c) => {
        c.querySelector(".chapter-name").textContent =
          c.querySelector(".chapter-title").value;
      });
      resultsSignature = "";
      offset = 0;
      await refreshChunks();
    } finally {
      saving = false;
    }
  }
  byId("saveButton").addEventListener(
    "click",
    action(async () => {
      await save();
      toast("Настройки сохранены");
      await poll();
    }),
  );

  async function enqueue(kind) {
    await save();
    if (dirty)
      throw new Error(
        "Текст изменился во время сохранения. Сохраните изменения ещё раз",
      );
    await api(`${endpoint}/jobs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind }),
    });
    await poll();
  }
  byId("previewButton").addEventListener(
    "click",
    action(() => enqueue("preview")),
  );
  byId("renderButton").addEventListener(
    "click",
    action(() => enqueue("render")),
  );

  function makeButton(label, callback, className = "button secondary small") {
    const button = document.createElement("button");
    button.type = "button";
    button.className = className;
    button.textContent = label;
    button.addEventListener("click", action(callback));
    return button;
  }
  function link(label, href) {
    const node = document.createElement("a");
    node.className = "button secondary small";
    node.href = href;
    node.textContent = label;
    return node;
  }
  function section(title) {
    const node = document.createElement("div");
    node.className = "result-box";
    const heading = document.createElement("h3");
    heading.textContent = title;
    node.append(heading);
    return node;
  }

  async function poll() {
    const state = await api(`${endpoint}/status`);
    if (state.revision !== revision && !saving) {
      byId("saveState").textContent =
        "Книга изменена в другой вкладке. Обновите страницу";
    }
    byId("workerStatus").textContent = state.worker
      ? "Обработка доступна"
      : "Worker не запущен";
    const job =
      state.jobs.find((j) =>
        ["running", "queued", "pausing"].includes(j.status),
      ) || state.jobs[0];
    busy = !!job && ["running", "queued", "pausing"].includes(job.status);
    setDisabled(byId("previewButton"), busy || saving);
    setDisabled(byId("renderButton"), busy || saving);
    setDisabled(byId("saveButton"), busy || saving);
    const status = byId("jobStatus");
    status.hidden = !job;
    byId("jobActions").replaceChildren();
    if (job) {
      status.className =
        "job-status" + (job.status === "failed" ? " error" : "");
      const label = document.createElement("div");
      label.textContent = `${job.kind === "preview" ? "Проба" : "Аудиокнига"} · ${job.stage}`;
      const progress = document.createElement("progress");
      progress.max = job.total;
      progress.value = job.done;
      progress.setAttribute("aria-label", "Готовые фрагменты");
      const meta = document.createElement("div");
      meta.className = "job-meta";
      meta.textContent =
        `${job.done} / ${job.total} фрагментов` +
        (job.rtf ? ` · RTF ${job.rtf}` : "") +
        (state.chunks.warnings ? ` · замечаний: ${state.chunks.warnings}` : "");
      if (
        job.status === "running" &&
        job.audio_seconds > 30 &&
        job.done < job.total &&
        job.done > 0
      ) {
        const minutes = Math.ceil(
          ((job.synthesis_seconds / job.done) * (job.total - job.done)) / 60,
        );
        meta.textContent += ` · синтез ещё ~${minutes} мин, затем сборка`;
      }
      status.replaceChildren(label, progress, meta);
      if (job.error) {
        const error = document.createElement("p");
        error.textContent = job.error;
        status.append(error);
      }
      const control = async (name) => {
        await api(`/api/jobs/${job.id}/${name}`, { method: "POST" });
        await poll();
      };
      if (["running", "queued"].includes(job.status))
        byId("jobActions").append(
          makeButton("Приостановить", () => control("pause")),
        );
      if (["paused", "failed"].includes(job.status))
        byId("jobActions").append(
          makeButton(
            job.status === "failed" ? "Повторить с места ошибки" : "Продолжить",
            () => control("resume"),
          ),
        );
      if (
        ["running", "queued", "pausing", "paused", "failed"].includes(
          job.status,
        )
      )
        byId("jobActions").append(
          makeButton("Отменить", () => control("cancel")),
        );
    }
    const completed =
      state.revision === revision
        ? state.jobs.filter((j) => j.status === "completed")
        : [];
    const signature = completed.map((j) => j.id).join(",");
    if (resultsSignature !== signature) {
      resultsSignature = signature;
      byId("results").replaceChildren();
      const preview = completed.find((j) => j.kind === "preview");
      if (preview) {
        const box = section("Проба голоса");
        const audio = document.createElement("audio");
        audio.controls = true;
        audio.preload = "none";
        audio.src = `/download/${preview.id}/preview.wav`;
        box.append(audio);
        byId("results").append(box);
      }
      const render = completed.find((j) => j.kind === "render");
      if (render) {
        const box = section("Аудиокнига готова");
        const links = document.createElement("div");
        links.className = "action-row compact";
        links.append(
          link("Скачать M4B", `/download/${render.id}/audiobook.m4b`),
          link("MP3 по главам", `/download/${render.id}/chapters.zip`),
          link("Отчёт", `/download/${render.id}/manifest.json`),
        );
        box.append(links);
        byId("results").append(box);
      }
    }
  }

  async function refreshChunks() {
    const onlyWarnings = byId("onlyWarnings").checked;
    const chunks = await api(
      `${endpoint}/chunks?offset=${offset}&limit=30&warnings=${onlyWarnings}`,
    );
    byId("chunkList").replaceChildren();
    for (const chunk of chunks) {
      const card = document.createElement("div");
      card.className = "chunk-card";
      const header = document.createElement("div");
      header.className = "chunk-header";
      const label = document.createElement("span");
      label.className = "small-label";
      label.textContent =
        `Фрагмент ${chunk.position + 1}` +
        (chunk.duration
          ? ` · ${chunk.duration.toFixed(1)} с`
          : " · ещё без аудио");
      header.append(label);
      if (chunk.audio_key)
        header.append(
          makeButton(
            "Перегенерировать",
            async () => {
              if (dirty)
                throw new Error(
                  "Сначала сохраните правки книги, затем обновите список фрагментов",
                );
              const result = await api(
                `${endpoint}/chunks/${chunk.id}/regenerate`,
                { method: "POST" },
              );
              revision = result.revision;
              resultsSignature = "";
              await enqueue("render");
              await refreshChunks();
            },
            "text-button",
          ),
        );
      const speech = document.createElement("p");
      speech.textContent = chunk.speech;
      card.append(header, speech);
      if (chunk.text.trim() !== chunk.speech) {
        const original = document.createElement("details");
        original.className = "chunk-source";
        const summary = document.createElement("summary");
        summary.textContent = "Исходный текст фрагмента";
        const text = document.createElement("p");
        text.textContent = chunk.text;
        original.append(summary, text);
        card.append(original);
      }
      if (chunk.warnings.length) {
        const warnings = document.createElement("div");
        warnings.className = "chunk-warning";
        warnings.textContent = chunk.warnings.join(" · ");
        card.append(warnings);
      }
      if (chunk.audio_key) {
        const audio = document.createElement("audio");
        audio.controls = true;
        audio.preload = "none";
        audio.src = `/audio/${id}/${chunk.id}?v=${chunk.audio_key}`;
        card.append(audio);
      }
      byId("chunkList").append(card);
    }
    if (!chunks.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = onlyWarnings
        ? "Фрагментов с техническими замечаниями нет."
        : "Нет фрагментов. Выберите главы и сохраните настройки.";
      byId("chunkList").append(empty);
    }
    setDisabled(byId("previousChunks"), offset === 0);
    setDisabled(byId("nextChunks"), chunks.length < 30);
    byId("chunkPage").textContent = chunks.length
      ? `${offset + 1}–${offset + chunks.length}`
      : "—";
  }
  byId("onlyWarnings").addEventListener(
    "change",
    action(async () => {
      offset = 0;
      await refreshChunks();
    }),
  );
  byId("refreshChunks").addEventListener("click", action(refreshChunks));
  byId("previousChunks").addEventListener(
    "click",
    action(async () => {
      offset = Math.max(0, offset - 30);
      await refreshChunks();
    }),
  );
  byId("nextChunks").addEventListener(
    "click",
    action(async () => {
      offset += 30;
      await refreshChunks();
    }),
  );
  window.addEventListener("beforeunload", (event) => {
    if (dirty) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  refreshChunks().catch((error) => toast(error.message, true));
  async function pollingLoop() {
    try {
      await poll();
    } catch (error) {
      byId("workerStatus").textContent = "Нет связи с сервисом";
    }
    setTimeout(pollingLoop, 2000);
  }
  pollingLoop();
}
