const navItems = document.querySelectorAll(".nav-item");
const refreshButton = document.querySelector("#refreshButton");
const exportButton = document.querySelector("#exportButton");
const articlePanel = document.querySelector(".article-panel");
const fileInput = document.querySelector("#fileInput");
const dropZone = document.querySelector("#dropZone");
const uploadStatus = document.querySelector("#uploadStatus");
const documentType = document.querySelector("#documentType");
const typeChips = document.querySelectorAll(".type-chip");
const fileList = document.querySelector("#fileList");
const mailPanel = document.querySelector(".mail-panel");
const toggleMail = document.querySelector("#toggleMail");
const mailTextarea = document.querySelector("#mailText");
const projectTitle = document.querySelector(".project-title h1");
const projectMeta = document.querySelector(".topbar p");
const kpiCards = document.querySelectorAll(".kpi-card");
const tableBody = document.querySelector("tbody");
const tableFoot = document.querySelector("tfoot");
const timeline = document.querySelector(".timeline");
const mailFields = document.querySelector(".mail-panel dl");
const newProjectButton = document.querySelector(".new-project");
const recalculateButton = document.querySelector("#recalculateButton");
const matchButton = document.querySelector("#matchButton");
const mailDraftButton = document.querySelector("#mailDraftButton");
const archiveButton = document.querySelector("#archiveButton");
const sendMailButton = document.querySelector("#sendMailButton");
const replaceDocsButton = document.querySelector("#replaceDocsButton");

let currentProjectId = "PRJ-START";
let selectedDocumentType = "Bestellung";

const contentShell = document.querySelector(".content-shell");

navItems.forEach((item) => {
  item.addEventListener("click", () => {
    navItems.forEach((entry) => entry.classList.remove("active"));
    item.classList.add("active");
    contentShell.dataset.view = item.dataset.view;
  });
});

refreshButton.addEventListener("click", async () => {
  await runAnalysis();
});

recalculateButton.addEventListener("click", runAnalysis);
matchButton.addEventListener("click", runAnalysis);

typeChips.forEach((chip) => {
  chip.addEventListener("click", () => {
    selectedDocumentType = chip.dataset.type;
    typeChips.forEach((entry) => {
      entry.classList.toggle("active", entry === chip);
      entry.setAttribute("aria-pressed", entry === chip ? "true" : "false");
    });
  });
});

exportButton.addEventListener("click", () => {
  window.location.href = `/api/projects/${currentProjectId}/export`;
});

fileInput.addEventListener("change", async () => {
  await uploadFiles(fileInput.files);
  fileInput.value = "";
});

dropZone.addEventListener("dragover", (event) => {
  event.preventDefault();
  dropZone.classList.add("dragging");
});

dropZone.addEventListener("dragleave", () => {
  dropZone.classList.remove("dragging");
});

dropZone.addEventListener("drop", async (event) => {
  event.preventDefault();
  dropZone.classList.remove("dragging");
  await uploadFiles(event.dataTransfer.files);
});

async function uploadFiles(files) {
  if (!files.length) {
    return;
  }

  const form = new FormData();
  form.append("document_type", selectedDocumentType || guessDocumentType(files[0].name));
  [...files].forEach((file) => form.append("files", file));

  uploadStatus.textContent = `${files.length} Datei(en) werden als ${selectedDocumentType} hochgeladen...`;
  uploadStatus.classList.remove("error");

  try {
    const response = await fetch(`/api/projects/${currentProjectId}/documents`, {
      method: "POST",
      body: form,
    });

    if (!response.ok) {
      throw new Error("Upload fehlgeschlagen");
    }

    const payload = await response.json();
    renderProject(payload.project);
    uploadStatus.textContent = "Upload abgeschlossen.";
  } catch (error) {
    uploadStatus.textContent = error.message;
    uploadStatus.classList.add("error");
  }
}

fileList.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-delete-document]");
  if (!button) {
    return;
  }
  const filename = button.dataset.filename || "diese Datei";
  if (!window.confirm(`${filename} wirklich entfernen? Daraus erkannte Positionen und Blockregeln werden ebenfalls gelöscht.`)) {
    return;
  }
  const payload = await api(`/api/projects/${currentProjectId}/documents/${button.dataset.deleteDocument}/delete`, {
    method: "POST",
  });
  renderProject(payload);
});

mailDraftButton.addEventListener("click", async () => {
  const payload = await api(`/api/projects/${currentProjectId}/mail-regenerate`, { method: "POST" });
  renderProject(payload);
  mailPanel.classList.add("editing");
  toggleMail.textContent = "Entwurf speichern";
});

sendMailButton.addEventListener("click", async () => {
  if (mailPanel.classList.contains("editing")) {
    window.alert("Bitte den Mailentwurf zuerst speichern.");
    return;
  }
  if (!window.confirm("Mail in Outlook öffnen und als gesendet markieren?")) {
    return;
  }

  const recipient = mailFields.querySelector("[data-mail-recipient]")?.value || "";
  const subject = mailFields.querySelector("[data-mail-subject]")?.value || "";
  const body = mailTextarea.value || "";
  window.location.href = `mailto:${recipient}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;

  try {
    const payload = await api(`/api/projects/${currentProjectId}/mail-send`, { method: "POST" });
    renderProject(payload);
  } catch (error) {
    window.alert(error.message);
  }
});

archiveButton.addEventListener("click", async () => {
  if (!window.confirm("Projekt archivieren?")) {
    return;
  }
  const payload = await api(`/api/projects/${currentProjectId}/archive`, { method: "POST" });
  renderProject(payload);
});

replaceDocsButton.addEventListener("click", async () => {
  if (!window.confirm("Bestellung und AB aus diesem Projekt entfernen? Die Blockunterlage bleibt erhalten.")) {
    return;
  }
  const payload = await api(`/api/projects/${currentProjectId}/clear-order-confirmation`, { method: "POST" });
  renderProject(payload);
});

newProjectButton.addEventListener("click", async () => {
  const name = window.prompt("Projektname", "Neue Küche");
  if (!name) {
    return;
  }
  const commission = window.prompt("Kommission", name) || name;
  const supplier = window.prompt("Lieferanten-E-Mail", "") || "";
  const payload = await api("/api/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name,
      commission,
      customer: commission,
      supplier,
      manufacturer: "Noch offen",
      catalog_year: new Date().getFullYear().toString(),
      owner: "Max Mustermann",
    }),
  });
  renderProject(payload);
});

toggleMail.addEventListener("click", async () => {
  const isEditing = mailPanel.classList.toggle("editing");
  toggleMail.textContent = isEditing ? "Entwurf speichern" : "Mail anzeigen & bearbeiten";

  if (!isEditing) {
    const subject = mailFields.querySelector("[data-mail-subject]")?.value || "";
    const recipient = mailFields.querySelector("[data-mail-recipient]")?.value || "";
    const payload = await api(`/api/projects/${currentProjectId}/mail-draft`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        recipient,
        subject,
        body: mailTextarea.value,
      }),
    });
    renderProject(payload);
  }
});

loadProject();

async function runAnalysis() {
  articlePanel.classList.remove("refreshing");
  requestAnimationFrame(() => articlePanel.classList.add("refreshing"));
  const payload = await api(`/api/projects/${currentProjectId}/analyze`, { method: "POST" });
  renderProject(payload);
}

async function loadProject() {
  const payload = await api("/api/projects/current");
  renderProject(payload);
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `API-Fehler: ${response.status}`;
    try {
      const payload = await response.json();
      message = payload.error || message;
    } catch {
      /* ignore non-json errors */
    }
    throw new Error(message);
  }
  return response.json();
}

function renderProject(payload) {
  const { project, summary, articles, documents, timeline: entries, mailDraft } = payload;
  currentProjectId = project.id;

  projectTitle.textContent = `Projekt: ${project.name}`;
  projectMeta.innerHTML = `Projekt-ID: ${project.id} <span></span> Erstellt am: ${formatDate(project.created_at)} <span></span> Erstellt von: ${project.owner}`;

  renderKpis(summary, project.status);
  renderArticles(articles, summary);
  renderDocuments(documents);
  renderTimeline(entries);
  renderMail(mailDraft);
}

function renderKpis(summary, status) {
  const values = [
    [formatMoney(summary.estimated_savings), "gegenüber Einzelpreisen"],
    [summary.position_count, "mit Einsparungspotenzial"],
    [formatMoney(summary.total_net), "nach Abzug der Einsparungen"],
    [status, `${summary.library_block_rule_count || 0} Regeln in Blockdatenbank`],
  ];

  kpiCards.forEach((card, index) => {
    card.querySelector("strong").textContent = values[index][0];
    card.querySelector("small").textContent = values[index][1];
  });
}

function renderArticles(articles, summary) {
  tableBody.innerHTML = articles
    .map((article) => {
      const saving = (article.single_price - article.block_price) * article.quantity;
      return `
        <tr>
          <td><span class="item-icon ${iconClass(article.category)}"></span></td>
          <td>${escapeHtml(article.article_number)}</td>
          <td>${escapeHtml(article.description)}</td>
          <td>${escapeHtml(article.category)}</td>
          <td>${article.quantity}</td>
          <td>${formatDimensions(article)}</td>
          <td>${formatMoney(article.single_price)}</td>
          <td>${formatMoney(article.block_price)}</td>
          <td class="${saving > 0 ? "saving" : ""}">${formatMoney(saving)}</td>
        </tr>
        <tr class="detail-row">
          <td></td>
          <td colspan="8">
            Status: <strong>${escapeHtml(article.status)}</strong>
            ${article.block_number ? ` · Block: ${escapeHtml(article.block_number)}` : ""}
            ${article.price_group ? ` · Preisgruppe: ${escapeHtml(article.price_group)}` : ""}
            ${article.dimension_status && article.dimension_status !== "offen" ? ` · Maße: ${escapeHtml(article.dimension_status)}` : ""}
            ${article.comment ? ` · Hinweis: ${escapeHtml(article.comment)}` : ""}
          </td>
        </tr>
      `;
    })
    .join("");

  tableFoot.innerHTML = `
    <tr>
      <td colspan="4">${summary.position_count} Positionen</td>
      <td colspan="4">Gesamteinsparung (geschätzt)</td>
      <td class="total">${formatMoney(summary.estimated_savings)}</td>
    </tr>
  `;
}

function formatDimensions(article) {
  const planned = article.planned_dimensions || "";
  const manufacturer = article.manufacturer_dimensions || "";
  if (planned && manufacturer && planned !== manufacturer) {
    return `${escapeHtml(planned)} → ${escapeHtml(manufacturer)}`;
  }
  return escapeHtml(planned || manufacturer || "offen");
}

function renderDocuments(documents) {
  if (!documents.length) {
    fileList.innerHTML = `<article class="file-row"><div></div><div><strong>Noch keine Dateien hochgeladen</strong><small>Hier Bestellung und AB laden. Blöcke kommen aus der zentralen Blockdatenbank.</small></div><span></span></article>`;
    return;
  }

  fileList.innerHTML = documents
    .map((document) => {
      const extension = document.filename.split(".").pop().toUpperCase();
      const isPdf = extension === "PDF";
      return `
        <article class="file-row">
          <span class="file-badge ${isPdf ? "pdf" : "xlsx"}">${isPdf ? "PDF" : "X"}</span>
          <div>
            <strong>${escapeHtml(document.filename)}</strong>
            <small>${escapeHtml(document.document_type)} • ${formatSize(document.size)} • ${formatDate(document.uploaded_at)}</small>
          </div>
          <button class="delete-file" type="button" data-delete-document="${escapeHtml(document.id)}" data-filename="${escapeHtml(document.filename)}" aria-label="Datei entfernen">×</button>
        </article>
      `;
    })
    .join("");
}

function renderTimeline(entries) {
  timeline.innerHTML = entries
    .map(
      (entry) => `
        <li>
          <time>${formatDate(entry.created_at)} <b>${formatTime(entry.created_at)}</b></time>
          <strong>${escapeHtml(entry.action)}</strong>
          <span>${escapeHtml(entry.user)}</span>
        </li>
      `,
    )
    .join("");
}

function renderMail(draft) {
  if (!draft) {
    return;
  }

  mailFields.innerHTML = `
    <dt>An:</dt>
    <dd><input class="mail-input" data-mail-recipient type="email" value="${escapeHtml(draft.recipient)}" placeholder="lieferant@example.de" /></dd>
    <dt>Betreff:</dt>
    <dd><input class="mail-input" data-mail-subject type="text" value="${escapeHtml(draft.subject)}" /></dd>
    <dt>Status:</dt>
    <dd>${escapeHtml(draft.status)}</dd>
  `;
  mailTextarea.value = draft.body;
  sendMailButton.textContent = draft.status === "Gesendet" ? "Erneut freigeben" : "Mail senden";
}

function iconClass(category) {
  const value = category.toLowerCase();
  if (value.includes("untersch")) return "drawer";
  if (value.includes("ober")) return "wide";
  if (value.includes("front")) return "front";
  if (value.includes("zubehör")) return "apothecary";
  return "tall";
}

function guessDocumentType(filename) {
  const lower = filename.toLowerCase();
  if (lower.includes("block")) return "Blockunterlage";
  if (lower.includes("auftrag") || lower.includes("ab")) return "Auftragsbestätigung";
  if (lower.includes("bestell")) return "Bestellung";
  return "Sonstiges";
}

function formatMoney(value) {
  return new Intl.NumberFormat("de-DE", { style: "currency", currency: "EUR" }).format(value || 0);
}

function formatSize(bytes) {
  if (bytes < 1024 * 1024) {
    return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  }

  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function formatDate(value) {
  return new Intl.DateTimeFormat("de-DE").format(new Date(value));
}

function formatTime(value) {
  return new Intl.DateTimeFormat("de-DE", { hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
