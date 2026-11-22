const $ = (selector) => document.querySelector(selector);
const tokenStorageKey = "enasia-office-db-session-token";
const apiBase = (document.body.dataset.apiBase || "/admin/database").replace(/\/$/, "");

const state = {
  role: "Admin",
  username: "admin",
  folders: [],
  activeFolderId: "",
  activeFolder: null,
  activeDocuments: [],
  docSearch: "",
};
let noticeTimer;

function text(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[ch]);
}

function formatBytes(bytes) {
  const b = Number(bytes || 0);
  if (b < 1024) return `${b} B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
  return `${(b / (1024 * 1024)).toFixed(2)} MB`;
}

function showNotice(message, isError = false) {
  const notice = $("#notice");
  notice.textContent = message;
  notice.classList.toggle("error", isError);
  notice.classList.remove("hidden");
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => notice.classList.add("hidden"), 5000);
}

async function apiRequest(path, { method = "GET", body, responseType = "json" } = {}) {
  const headers = {};
  const token = localStorage.getItem(tokenStorageKey) || sessionStorage.getItem(tokenStorageKey);
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(`${apiBase}${path}`, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (responseType === "blob") {
    if (!response.ok) {
      const err = await response.json().catch(() => ({}));
      throw new Error(err.error || `Server returned ${response.status}.`);
    }
    return response;
  }
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(result.error || `Server returned ${response.status}.`);
  }
  return result;
}

async function run(action, successMessage) {
  try {
    const result = await action();
    if (successMessage) showNotice(successMessage);
    return result;
  } catch (error) {
    showNotice(error.message, true);
    return null;
  }
}

function toBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => resolve(String(reader.result).split(",", 2)[1]));
    reader.addEventListener("error", () => reject(reader.error || new Error("Could not read file.")));
    reader.readAsDataURL(file);
  });
}

function canEdit() {
  return state.role === "Admin" || state.role === "Editor";
}

function renderFoldersList() {
  const q = ($("#folder-search").value || "").trim().toLowerCase();
  const filtered = state.folders.filter((f) =>
    !q || f.name.toLowerCase().includes(q) || (f.description || "").toLowerCase().includes(q)
  );
  $("#folder-count").textContent = `${filtered.length} folder${filtered.length === 1 ? "" : "s"}`;
  const container = $("#folder-list");
  if (!filtered.length) {
    container.innerHTML = `<p class="muted" style="padding: 16px 4px;">${
      state.folders.length ? "No matching folders." : "No folders created yet. Click '+ New folder' to create Batch 1, Batch 2, etc."
    }</p>`;
    return;
  }
  container.innerHTML = filtered.map((folder) => `
    <div class="folder-item${folder.id === state.activeFolderId ? " selected" : ""}" data-folder-id="${text(folder.id)}">
      <div class="folder-item-left">
        <div class="folder-icon">📁</div>
        <div style="min-width: 0;">
          <span class="folder-name">${text(folder.name)}</span>
          <span class="folder-meta">${text(folder.description || `${formatBytes(folder.total_bytes)}`)}</span>
        </div>
      </div>
      <span class="folder-count-badge">${folder.document_count} file${folder.document_count === 1 ? "" : "s"}</span>
    </div>
  `).join("");

  for (const el of container.querySelectorAll("[data-folder-id]")) {
    el.addEventListener("click", () => selectFolder(el.dataset.folderId));
  }
}

function renderFolderDetail() {
  const panel = $("#folder-detail-panel");
  const folder = state.activeFolder;
  if (!folder) {
    panel.innerHTML = `
      <div class="empty-state">
        <span class="empty-icon">📁</span>
        <h3>Select or create a folder</h3>
        <p>Create folders like <strong>Batch 1</strong>, <strong>Batch 2</strong>, <strong>Batch 3</strong> and upload shared documents once to save database space.</p>
      </div>`;
    return;
  }

  const editable = canEdit();
  const docs = state.activeDocuments.filter((d) => {
    if (!state.docSearch) return true;
    const hay = `${d.title} ${d.original_filename} ${d.notes || ""}`.toLowerCase();
    return hay.includes(state.docSearch);
  });

  panel.innerHTML = `
    <div class="folder-head-bar">
      <div>
        <p class="eyebrow">FOLDER · ${folder.document_count} FILE${folder.document_count === 1 ? "" : "S"} (${formatBytes(folder.total_bytes)})</p>
        <h2 style="margin: 4px 0;">📁 ${text(folder.name)}</h2>
        ${folder.description ? `<p class="muted" style="margin: 4px 0 0;">${text(folder.description)}</p>` : ""}
      </div>
      <div class="folder-actions">
        ${editable ? `
          <button id="upload-folder-docs-button" class="button button-primary small-button">+ Upload multiple documents</button>
        ` : ""}
        <button id="download-folder-zip-button" class="button button-secondary small-button" style="background: #dfd3cf; border-color: #581318; color: #581318; font-weight: 700;">⬇️ Download documents${folder.document_count ? ` (${folder.document_count})` : ""}</button>
        ${editable ? `
          <button id="rename-folder-button" class="button button-secondary small-button">Rename folder</button>
          <button id="delete-folder-button" class="button button-remove small-button">Delete folder</button>
        ` : ""}
      </div>
    </div>

    <label class="search-label" style="margin-bottom: 14px;">Search files in ${text(folder.name)}
      <input id="folder-doc-search" type="search" value="${text(state.docSearch)}" placeholder="Filter by document title, file name, or notes...">
    </label>

    <div class="doc-grid-list">
      ${docs.length ? docs.map((doc) => {
        const ext = (doc.original_filename.split(".").pop() || "FILE").toUpperCase();
        const isPdf = ext === "PDF";
        return `
          <div class="doc-row">
            <div class="doc-row-info">
              <div class="doc-badge-ext${isPdf ? " pdf" : ""}">${text(ext)}</div>
              <div style="min-width: 0;">
                <span class="doc-title">${text(doc.title)}</span>
                <span class="doc-sub">${text(doc.original_filename)} · ${formatBytes(doc.size_bytes)} · Uploaded ${text((doc.created_at || "").slice(0, 10))}${doc.notes ? ` · ${text(doc.notes)}` : ""}</span>
              </div>
            </div>
            <div class="document-actions">
              <button class="button button-secondary small-button" data-open-doc-id="${text(doc.id)}">Open</button>
              <button class="button button-secondary small-button" data-download-doc-id="${text(doc.id)}" data-download-name="${text(doc.original_filename)}" style="background:#581318; color:#fff; border-color:#581318;">⬇️ Download</button>
              ${editable ? `
                <button class="button button-secondary small-button" data-edit-doc-id="${text(doc.id)}">Rename</button>
                <button class="button button-remove small-button" data-delete-doc-id="${text(doc.id)}">Delete</button>
              ` : ""}
            </div>
          </div>
        `;
      }).join("") : `<p class="muted" style="padding: 24px 0; text-align: center;">No documents in this folder yet. Click <strong>+ Upload multiple documents</strong> to add files.</p>`}
    </div>
  `;

  $("#folder-doc-search").addEventListener("input", (e) => {
    state.docSearch = e.currentTarget.value.trim().toLowerCase();
    renderFolderDetail();
    const input = $("#folder-doc-search");
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  });

  const zipBtn = $("#download-folder-zip-button");
  if (zipBtn) {
    zipBtn.addEventListener("click", () => {
      if (!folder.document_count) {
        showNotice(`"${folder.name}" has no documents yet. Click "+ Upload multiple documents" first.`, true);
        return;
      }
      if (state.activeDocuments.length === 1) {
        const single = state.activeDocuments[0];
        run(() => openFolderDocument(single.id, true, single.original_filename), `Downloaded ${single.original_filename}.`);
        return;
      }
      run(() => downloadFolderZip(folder), `Downloaded all ${folder.document_count} documents from ${folder.name}.`);
    });
  }
  if (editable) {
    $("#upload-folder-docs-button").addEventListener("click", () => openFolderUploadDialog(folder));
    $("#rename-folder-button").addEventListener("click", () => openFolderDialog(folder));
    $("#delete-folder-button").addEventListener("click", async () => {
      if (!window.confirm(`Delete folder "${folder.name}" and all ${folder.document_count} document(s) inside it? This cannot be undone.`)) return;
      const res = await run(() => apiRequest(`/api/folders/${encodeURIComponent(folder.id)}`, { method: "DELETE" }), `Folder "${folder.name}" deleted.`);
      if (res) {
        state.activeFolderId = "";
        state.activeFolder = null;
        state.activeDocuments = [];
        await loadFolders();
        renderFolderDetail();
      }
    });
  }

  for (const btn of panel.querySelectorAll("[data-open-doc-id]")) {
    btn.addEventListener("click", () => run(() => openFolderDocument(btn.dataset.openDocId, false)));
  }
  for (const btn of panel.querySelectorAll("[data-download-doc-id]")) {
    btn.addEventListener("click", () => run(() => openFolderDocument(btn.dataset.downloadDocId, true, btn.dataset.downloadName)));
  }
  for (const btn of panel.querySelectorAll("[data-edit-doc-id]")) {
    btn.addEventListener("click", () => {
      const doc = state.activeDocuments.find((d) => d.id === btn.dataset.editDocId);
      if (doc) openDocEditDialog(doc);
    });
  }
  for (const btn of panel.querySelectorAll("[data-delete-doc-id]")) {
    btn.addEventListener("click", async () => {
      const doc = state.activeDocuments.find((d) => d.id === btn.dataset.deleteDocId);
      if (!doc || !window.confirm(`Delete "${doc.title}" (${doc.original_filename}) from ${folder.name}?`)) return;
      const res = await run(() => apiRequest(`/api/folder-documents/${encodeURIComponent(doc.id)}`, { method: "DELETE" }), "Document deleted.");
      if (res) {
        await loadFolders();
        await selectFolder(folder.id);
      }
    });
  }
}

async function downloadFolderZip(folder) {
  const response = await apiRequest(`/api/folders/${encodeURIComponent(folder.id)}/download`, { responseType: "blob" });
  const objectUrl = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = `${folder.name}-Documents.zip`;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
}

async function openFolderDocument(docId, download = false, fallbackName = "document") {
  const popup = download ? null : window.open("about:blank", "_blank");
  const query = download ? "?download=1" : "";
  const response = await apiRequest(`/api/folder-documents/${encodeURIComponent(docId)}${query}`, { responseType: "blob" });
  const objectUrl = URL.createObjectURL(await response.blob());
  if (popup) {
    popup.location = objectUrl;
  } else {
    const link = document.createElement("a");
    link.href = objectUrl;
    link.download = fallbackName;
    document.body.append(link);
    link.click();
    link.remove();
  }
  setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
}

async function loadFolders() {
  const result = await run(() => apiRequest("/api/folders"));
  if (!result) return;
  state.folders = result.folders || [];
  renderFoldersList();
}

async function selectFolder(folderId) {
  state.activeFolderId = folderId;
  state.docSearch = "";
  renderFoldersList();
  const result = await run(() => apiRequest(`/api/folders/${encodeURIComponent(folderId)}`));
  if (!result) return;
  state.activeFolder = result.folder;
  state.activeDocuments = result.documents || [];
  renderFolderDetail();
}

function openFolderDialog(folder = null) {
  const form = $("#folder-form");
  form.reset();
  $("#folder-id").value = folder?.id || "";
  $("#folder-form-title").textContent = folder ? `Rename "${folder.name}"` : "Create new folder";
  $("#folder-submit-button").textContent = folder ? "Save changes" : "Create folder";
  $("#folder-name-input").value = folder?.name || "";
  $("#folder-desc-input").value = folder?.description || "";
  $("#folder-dialog").showModal();
}

function renderUploadFilesPreview() {
  const files = Array.from($("#folder-upload-files").files || []);
  const list = $("#folder-upload-preview-list");
  if (!files.length) {
    list.innerHTML = '<p class="muted">No files chosen yet. Select one or more PDF, JPG, or PNG files above.</p>';
    return;
  }
  list.innerHTML = files.map((file, idx) => {
    const defaultTitle = file.name.replace(/\.[^.]+$/, "").trim() || file.name;
    return `
      <div class="record" data-upload-idx="${idx}" style="display: grid; grid-template-columns: 1.1fr 1.1fr 1fr; gap: 10px; align-items: end;">
        <div>
          <strong>${text(file.name)}</strong>
          <small>${formatBytes(file.size)}</small>
        </div>
        <label style="font-size: 11px;">Document title / label *
          <input type="text" class="upload-doc-title" value="${text(defaultTitle)}" required maxlength="200">
        </label>
        <label style="font-size: 11px;">Notes (optional)
          <input type="text" class="upload-doc-notes" maxlength="500" placeholder="Optional note">
        </label>
      </div>
    `;
  }).join("");
}

function openFolderUploadDialog(folder) {
  const form = $("#folder-upload-form");
  form.reset();
  $("#folder-upload-folder-id").value = folder.id;
  $("#folder-upload-title").textContent = `Upload documents to ${folder.name}`;
  renderUploadFilesPreview();
  $("#folder-upload-dialog").showModal();
}

function openDocEditDialog(doc) {
  const form = $("#folder-doc-edit-form");
  form.reset();
  $("#folder-doc-edit-id").value = doc.id;
  $("#folder-doc-edit-title").value = doc.title || "";
  $("#folder-doc-edit-notes").value = doc.notes || "";
  $("#folder-doc-edit-dialog").showModal();
}

$("#folder-search").addEventListener("input", renderFoldersList);
$("#new-folder-button").addEventListener("click", () => openFolderDialog());
$("#folder-upload-files").addEventListener("change", renderUploadFilesPreview);

$("#folder-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const folderId = $("#folder-id").value;
  const payload = {
    name: $("#folder-name-input").value.trim(),
    description: $("#folder-desc-input").value.trim(),
  };
  const result = await run(
    () => apiRequest(folderId ? `/api/folders/${encodeURIComponent(folderId)}` : "/api/folders", {
      method: folderId ? "PUT" : "POST",
      body: payload,
    }),
    folderId ? "Folder renamed." : "Folder created.",
  );
  if (!result) return;
  $("#folder-dialog").close();
  await loadFolders();
  await selectFolder(folderId || result.id);
});

$("#folder-upload-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const folderId = $("#folder-upload-folder-id").value;
  const files = Array.from($("#folder-upload-files").files || []);
  if (!files.length) {
    showNotice("Choose at least one PDF, JPG, or PNG file.", true);
    return;
  }
  const rows = Array.from($("#folder-upload-preview-list").querySelectorAll("[data-upload-idx]"));
  const btn = $("#folder-upload-submit-button");
  const origText = btn.textContent;
  btn.disabled = true;

  let uploaded = 0;
  try {
    for (let i = 0; i < files.length; i += 1) {
      const file = files[i];
      if (!file.size || file.size > 20 * 1024 * 1024) {
        throw new Error(`"${file.name}" must be between 1 byte and 20 MB.`);
      }
      const row = rows[i];
      const title = (row?.querySelector(".upload-doc-title")?.value || file.name).trim();
      const notes = (row?.querySelector(".upload-doc-notes")?.value || "").trim();
      const ext = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
      const mimeType = file.type || ({
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
      })[ext] || "application/octet-stream";

      btn.textContent = `Uploading ${i + 1} of ${files.length}...`;
      await apiRequest(`/api/folders/${encodeURIComponent(folderId)}/documents`, {
        method: "POST",
        body: {
          title,
          notes,
          filename: file.name,
          mime_type: mimeType,
          content_base64: await toBase64(file),
        },
      });
      uploaded += 1;
    }
    showNotice(`Uploaded ${uploaded} document${uploaded === 1 ? "" : "s"} to folder.`);
    $("#folder-upload-dialog").close();
    await loadFolders();
    await selectFolder(folderId);
  } catch (error) {
    showNotice(uploaded > 0 ? `Uploaded ${uploaded} of ${files.length} files before error: ${error.message}` : error.message, true);
    if (uploaded > 0) {
      await loadFolders();
      await selectFolder(folderId);
    }
  } finally {
    btn.disabled = false;
    btn.textContent = origText;
  }
});

$("#folder-doc-edit-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const docId = $("#folder-doc-edit-id").value;
  const payload = {
    title: $("#folder-doc-edit-title").value.trim(),
    notes: $("#folder-doc-edit-notes").value.trim(),
  };
  const res = await run(() => apiRequest(`/api/folder-documents/${encodeURIComponent(docId)}`, {
    method: "PUT",
    body: payload,
  }), "Document details updated.");
  if (!res) return;
  $("#folder-doc-edit-dialog").close();
  if (state.activeFolderId) await selectFolder(state.activeFolderId);
});

for (const button of document.querySelectorAll("[data-close]")) {
  button.addEventListener("click", () => document.getElementById(button.dataset.close).close());
}

apiRequest("/api/status").then(async (status) => {
  if (!status.autoSignedIn) {
    window.location.href = `${apiBase}?auto=1`;
    return;
  }
  state.username = status.username || "admin";
  state.role = status.role || "Admin";
  $("#signed-in-user").textContent = `${state.username} · ${state.role}`;
  if (!canEdit()) {
    $("#new-folder-button").classList.add("hidden");
  }
  await loadFolders();
  if (state.folders.length) {
    const requestedBatch = (new URLSearchParams(location.search).get("batch") || "").trim().toLowerCase();
    const normBatch = requestedBatch.replace(/^bacth\b/i, "batch").replace(/\s+/g, " ");
    const matchedFolder = normBatch
      ? state.folders.find((f) => f.name.trim().toLowerCase().replace(/^bacth\b/i, "batch").replace(/\s+/g, " ") === normBatch)
      : null;
    await selectFolder((matchedFolder || state.folders[0]).id);
  }
}).catch((err) => showNotice(`Could not connect: ${err.message}`, true));
