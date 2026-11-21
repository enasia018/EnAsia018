const $ = (selector) => document.querySelector(selector);
const tokenStorageKey = "enasia-office-db-session-token";
const apiBase = (document.body.dataset.apiBase || "/admin/database").replace(/\/$/, "");
const logoUrl = document.body.dataset.logoUrl || "/static/office_db/enasia-logo.jpg?v=2";
const enasia = createBrowserApi();
const states = {
  connected: false,
  setupRequired: false,
  signedIn: false,
  websiteAdminLoggedIn: false,
  role: "",
  activeMemberId: "",
  members: [],
};
let noticeTimer;
let searchTimer;

const brandLogo = $("#brand-logo");
brandLogo.src = logoUrl;
brandLogo.addEventListener("contextmenu", (event) => event.preventDefault());
brandLogo.addEventListener("error", () => {
  brandLogo.hidden = true;
}, { once: true });

function createBrowserApi() {
  let pendingWorkbook;

  async function request(path, { method = "GET", body, responseType = "json" } = {}) {
    const headers = {};
    const token = sessionStorage.getItem(tokenStorageKey);
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
        const result = await response.json().catch(() => ({}));
        throw new Error(result.error || `Server returned ${response.status}.`);
      }
      return response;
    }
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      const details = Array.isArray(result.errors) ? `\n${result.errors.slice(0, 25).join("\n")}` : "";
      throw new Error(`${result.error || `Server returned ${response.status}.`}${details}`);
    }
    return result;
  }

  function toBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.addEventListener("load", () => resolve(String(reader.result).split(",", 2)[1]));
      reader.addEventListener("error", () => reject(reader.error || new Error("Could not read the selected file.")));
      reader.readAsDataURL(file);
    });
  }

  async function uploadDocument(memberId, document, file) {
    if (!(file instanceof File)) throw new Error("Choose a document file to upload.");
    if (!file.size || file.size > 20 * 1024 * 1024) {
      throw new Error("Choose a document file between 1 byte and 20 MB.");
    }
    const mimeType = file.type || ({
      ".pdf": "application/pdf",
      ".png": "image/png",
      ".jpg": "image/jpeg",
      ".jpeg": "image/jpeg",
    })[file.name.slice(file.name.lastIndexOf(".")).toLowerCase()];
    return request(`/api/members/${encodeURIComponent(memberId)}/documents`, {
      method: "POST",
      body: {
        ...document,
        filename: file.name,
        mime_type: mimeType,
        content_base64: await toBase64(file),
      },
    });
  }

  async function previewImport() {
    const input = $("#import-file");
    const file = await new Promise((resolve) => {
      const finish = (selectedFile) => {
        input.removeEventListener("change", onChange);
        input.removeEventListener("cancel", onCancel);
        resolve(selectedFile);
      };
      const onChange = () => finish(input.files[0] || null);
      const onCancel = () => finish(null);
      input.addEventListener("change", onChange, { once: true });
      input.addEventListener("cancel", onCancel, { once: true });
      input.value = "";
      input.click();
    });
    if (!file) return null;
    if (!file.size || file.size > 20 * 1024 * 1024) {
      throw new Error("Excel workbooks must be between 1 byte and 20 MB.");
    }
    pendingWorkbook = { workbook_base64: await toBase64(file) };
    return request("/api/import/preview", { method: "POST", body: pendingWorkbook });
  }

  return {
    async serverStatus() {
      const status = await request("/api/status");
      return { ...status, connected: true, url: location.origin };
    },
    async adminResume() {
      sessionStorage.removeItem(tokenStorageKey);
      return request("/api/admin-resume", { method: "POST", body: {} });
    },
    async setup(credentials) {
      const result = await request("/api/setup", { method: "POST", body: credentials });
      sessionStorage.setItem(tokenStorageKey, result.token);
      return result;
    },
    async login(credentials) {
      const result = await request("/api/login", { method: "POST", body: credentials });
      sessionStorage.setItem(tokenStorageKey, result.token);
      return result;
    },
    async logout() {
      try {
        await request("/api/logout", { method: "POST", body: {} });
      } finally {
        sessionStorage.removeItem(tokenStorageKey);
      }
    },
    listUsers: () => request("/api/users"),
    createUser: (user) => request("/api/users", { method: "POST", body: user }),
    updateUser: (id, data) => request(`/api/users/${encodeURIComponent(id)}`, { method: "PUT", body: data }),
    deleteUser: (id) => request(`/api/users/${encodeURIComponent(id)}`, { method: "DELETE" }),
    listMembers: (search) => request(`/api/members?search=${encodeURIComponent(search || "")}`),
    getMember: (id) => request(`/api/members/${encodeURIComponent(id)}`),
    saveMember: (member) => request(member.id ? `/api/members/${encodeURIComponent(member.id)}` : "/api/members", {
      method: member.id ? "PUT" : "POST",
      body: member,
    }),
    addPayment: (payment) => request("/api/payments", { method: "POST", body: payment }),
    updatePayment: (id, payment) => request(`/api/payments/${encodeURIComponent(id)}`, { method: "PUT", body: payment }),
    deletePayment: (id) => request(`/api/payments/${encodeURIComponent(id)}`, { method: "DELETE" }),
    addMonthlyDues: (due) => request("/api/monthly-dues", { method: "POST", body: due }),
    updateMonthlyDues: (id, due) => request(`/api/monthly-dues/${encodeURIComponent(id)}`, { method: "PUT", body: due }),
    deleteMonthlyDues: (id) => request(`/api/monthly-dues/${encodeURIComponent(id)}`, { method: "DELETE" }),
    uploadDocument,
    updateDocument: (id, details) => request(`/api/documents/${encodeURIComponent(id)}`, { method: "PUT", body: details }),
    deleteDocument: (id) => request(`/api/documents/${encodeURIComponent(id)}`, { method: "DELETE" }),
    async exportExcel() {
      const response = await request("/api/export", { responseType: "blob" });
      const objectUrl = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = `Enasia-Member-Database-${new Date().toISOString().slice(0, 10)}.xlsx`;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
      return true;
    },
    async openDocument(id) {
      const popup = window.open("about:blank", "_blank");
      const response = await request(`/api/documents/${encodeURIComponent(id)}`, { responseType: "blob" });
      const disposition = response.headers.get("content-disposition") || "";
      const encodedName = /filename\*=UTF-8''([^;]+)/i.exec(disposition)?.[1];
      const fileName = encodedName ? decodeURIComponent(encodedName) : "member-document";
      const objectUrl = URL.createObjectURL(await response.blob());
      if (popup) {
        popup.location = objectUrl;
      } else {
        const link = document.createElement("a");
        link.href = objectUrl;
        link.download = fileName;
        link.click();
      }
      setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
    },
    previewImport,
    async commitImport() {
      if (!pendingWorkbook) throw new Error("Select and preview an Excel workbook before importing it.");
      try {
        return await request("/api/import/commit", { method: "POST", body: pendingWorkbook });
      } finally {
        pendingWorkbook = null;
      }
    },
  };
}

function showNotice(message, isError = false) {
  const notice = $("#notice");
  notice.textContent = message;
  notice.classList.toggle("error", isError);
  notice.classList.remove("hidden");
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => notice.classList.add("hidden"), 5000);
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

function show(element, visible) {
  if (element) element.classList.toggle("hidden", !visible);
}

function renderAuth(setupRequired) {
  states.setupRequired = setupRequired;
  show($("#auth-card"), true);
  show($("#admin-resume-wrap"), states.websiteAdminLoggedIn);
  $("#auth-kicker").textContent = setupRequired ? "FIRST-TIME SETUP" : "SECURE DATABASE";
  $("#auth-title").textContent = setupRequired ? "Create administrator account" : "Sign in";
  $("#auth-description").textContent = setupRequired
    ? "Create the first staff account on the office database. Use a strong password of at least 12 characters."
    : "Sign in to search member and payment records.";
  $("#auth-submit").textContent = setupRequired ? "Create administrator" : "Sign in";
  $("#password").setAttribute("autocomplete", setupRequired ? "new-password" : "current-password");
}

const userRoles = new Set(["Admin", "Editor", "View only"]);

function applyRolePermissions() {
  const canEdit = states.role === "Admin" || states.role === "Editor";
  show($("#new-member-button"), canEdit);
  show($("#import-button"), canEdit);
  show($("#manage-users-button"), states.role === "Admin");
}

async function enterSignedInState(username, role) {
  states.signedIn = true;
  states.role = userRoles.has(role) ? role : "";
  $("#signed-in-user").textContent = states.role
    ? `${username} · ${states.role}`
    : `${username} · Role unavailable`;
  applyRolePermissions();
  show($("#account-bar"), true);
  show($("#auth-card"), false);
  show($("#app-panel"), true);
  $("#password").value = "";
  await loadMembers();
}

function text(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

function money(value) {
  return new Intl.NumberFormat("en-PH", { style: "currency", currency: "PHP" }).format(value || 0);
}

function renderMembers(members) {
  states.members = members;
  $("#member-count").textContent = `${members.length} member${members.length === 1 ? "" : "s"}${members.length === 500 ? " (showing the first 500)" : ""}`;
  $("#member-rows").innerHTML = members.length ? members.map((member) => `
    <tr class="member-row${member.id === states.activeMemberId ? " selected" : ""}" data-member-id="${text(member.id)}">
      <td><span class="member-primary">${text(member.full_name)}</span><span class="member-secondary">${text(member.member_code || "No member code")}</span></td>
      <td>${text([member.unit_name, [member.vehicle_make, member.vehicle_model].filter(Boolean).join(" ")].filter(Boolean).join(" · ") || "—")}</td>
      <td>${text(member.plate_number || "—")}</td>
      <td><span class="pill ${text(member.status)}">${text(member.status)}</span></td>
    </tr>`).join("") :
    '<tr><td colspan="4" class="muted">No matching members found.</td></tr>';
  for (const row of $("#member-rows").querySelectorAll("[data-member-id]")) {
    row.addEventListener("click", () => selectMember(row.dataset.memberId));
  }
}

function renderDetail(result) {
  const { member, payments, monthlyDues, documents, totals } = result;
  const canEdit = states.role === "Admin" || states.role === "Editor";
  const fields = [
    ["Address", member.address],
    ["Member ID", member.id_number],
    ["Driver license", member.driver_license_number],
    ["Unit name", member.unit_name],
    ["Vehicle", [member.vehicle_make, member.vehicle_model, member.vehicle_year].filter(Boolean).join(" ")],
    ["Plate number", member.plate_number],
    ["MV file number", member.mv_file_number],
    ["Franchise case", member.franchise_case_number],
    ["Batch number", member.batch_number],
    ["Franchise start date", member.franchise_start_date],
    ["Monthly dues", money(member.monthly_dues_amount)],
    ["Due day", member.due_day || ""],
    ["MOA status", member.moa_status],
    ["Lease status", member.lease_status],
    ["PA status", member.pa_status],
    ["CPC status", member.cpc_status],
    ["Original OR/CR", member.original_or_cr],
    ["Facebook name", member.facebook_name],
    ["Terms", member.terms],
  ];
  const totalByType = Object.fromEntries(totals.map((entry) => [entry.payment_type, entry.total]));
  const monthlyDuesTotal = monthlyDues.reduce((total, due) => total + (Number(due.amount) || 0), 0);
  const paymentSummary = ["Membership Fee", "Franchise Processing Fee", "Monthly Dues", "Other Payment"]
    .map((type) => `<div class="summary-chip">${text(type)}<strong>${money(type === "Monthly Dues" ? monthlyDuesTotal : totalByType[type])}</strong></div>`).join("");
  $("#detail-panel").innerHTML = `
    <div class="profile-head">
      <div><p class="eyebrow">MEMBER PROFILE · ${text(member.status.toUpperCase())}</p><h2>${text(member.full_name)}</h2><span class="profile-subtitle">${text(member.member_code || "No member code")}</span></div>
      ${canEdit ? '<div class="profile-actions"><button id="edit-member-button" class="button button-secondary small-button">Edit</button></div>' : ""}
    </div>
    <section class="detail-section">
      <div class="section-head"><h3>Member and franchise details</h3></div>
      <div class="field-grid">${fields.map(([label, value]) => `<div class="data-field"><span>${text(label)}</span><strong>${text(value || "—")}</strong></div>`).join("")}</div>
      ${member.notes ? `<p class="muted">${text(member.notes)}</p>` : ""}
    </section>
    <section class="detail-section">
      <div class="section-head"><h3>Payments and fees</h3>${canEdit ? '<button id="add-payment-button" class="button button-secondary small-button">+ Record payment</button>' : ""}</div>
      <div class="summary-row">${paymentSummary}</div>
      <div class="payment-list">${payments.length ? payments.map((payment) => `
        <div class="record"><div><strong>${text(payment.payment_type)}${payment.payment_purpose ? ` · ${text(payment.payment_purpose)}` : ""}${payment.period_covered ? ` · ${text(payment.period_covered)}` : ""}</strong><small>${text(payment.payment_date || "Date not recorded")}${payment.receipt_number ? ` · AR ${text(payment.receipt_number)}` : ""}${payment.voucher ? ` · Voucher ${text(payment.voucher)}` : ""}${payment.balance !== null && payment.balance !== undefined ? ` · Balance ${money(payment.balance)}` : ""}${payment.payment_method ? ` · ${text(payment.payment_method)}` : ""}</small></div><span class="record-amount">${money(payment.amount)}</span>${canEdit ? `<div class="record-actions"><button class="button button-secondary small-button" data-payment-id="${text(payment.id)}">Edit</button><button class="button button-secondary small-button" data-delete-payment-id="${text(payment.id)}">Delete</button></div>` : ""}</div>
      `).join("") : '<p class="muted">No payments recorded.</p>'}</div>
    </section>
    <section class="detail-section">
      <div class="section-head"><h3>Monthly dues status</h3>${canEdit ? '<button id="add-dues-button" class="button button-secondary small-button">+ Record monthly dues</button>' : ""}</div>
      <div class="payment-list">${monthlyDues.length ? monthlyDues.map((due) => `
        <div class="record"><div><strong>${text(due.period)} · ${text(due.status)}</strong>${due.notes ? `<small>${text(due.notes)}</small>` : ""}</div><span class="record-amount">${money(due.amount)}</span>${canEdit ? `<div class="record-actions"><button class="button button-secondary small-button" data-monthly-dues-id="${text(due.id)}">Edit</button><button class="button button-secondary small-button" data-delete-monthly-dues-id="${text(due.id)}">Delete</button></div>` : ""}</div>
      `).join("") : '<p class="muted">No monthly dues status recorded.</p>'}</div>
    </section>
    <section class="detail-section">
      <div class="section-head"><h3>Documents</h3>${canEdit ? '<div class="section-head-actions"><button id="upload-pa-button" class="button button-secondary small-button">+ Upload PA</button><button id="upload-cpc-button" class="button button-secondary small-button">+ Upload CPC</button><button id="upload-or-button" class="button button-secondary small-button">+ Upload OR</button><button id="upload-cr-button" class="button button-secondary small-button">+ Upload CR</button><button id="upload-document-button" class="button button-secondary small-button">+ Upload document</button></div>' : ""}</div>
      <div class="document-list">${documents.length ? documents.map((document) => `
        <div class="record"><div><strong>${text(document.document_type)}</strong><small>${text(document.original_filename)}${document.expiry_date ? ` · Expires ${text(document.expiry_date)}` : ""}${document.uploaded ? "" : " · File not uploaded"}</small></div><div class="document-actions">${document.uploaded ? `<button class="button button-secondary small-button" data-document-id="${text(document.id)}">Open</button>` : canEdit ? `<button class="button button-secondary small-button" data-upload-document-id="${text(document.id)}">Attach file</button>` : ""}${canEdit ? `<button class="button button-secondary small-button" data-edit-document-id="${text(document.id)}">Edit</button>${document.uploaded ? `<button class="button button-secondary small-button" data-replace-document-id="${text(document.id)}">Replace file</button>` : ""}<button class="button button-remove small-button" data-delete-document-id="${text(document.id)}">Remove</button>` : ""}</div></div>
      `).join("") : '<p class="muted">No documents uploaded.</p>'}</div>
    </section>`;
  if (canEdit) {
    $("#edit-member-button").addEventListener("click", () => openMemberForm(member));
    $("#add-payment-button").addEventListener("click", () => openPaymentForm(member.id));
    $("#add-dues-button").addEventListener("click", () => openDuesForm(member.id));
    $("#upload-document-button").addEventListener("click", () => openDocumentUploadForm(member.id));
    $("#upload-pa-button").addEventListener("click", () => openDocumentUploadForm(member.id, { document_type: "PA" }));
    $("#upload-cpc-button").addEventListener("click", () => openDocumentUploadForm(member.id, { document_type: "CPC" }));
    $("#upload-or-button").addEventListener("click", () => openDocumentUploadForm(member.id, { document_type: "OR" }));
    $("#upload-cr-button").addEventListener("click", () => openDocumentUploadForm(member.id, { document_type: "CR" }));
  }
  for (const button of $("#detail-panel").querySelectorAll("[data-monthly-dues-id]")) {
    button.addEventListener("click", () => {
      const due = monthlyDues.find((entry) => entry.id === button.dataset.monthlyDuesId);
      if (due) openDuesForm(member.id, due);
    });
  }
  for (const button of $("#detail-panel").querySelectorAll("[data-delete-monthly-dues-id]")) {
    button.addEventListener("click", async () => {
      const due = monthlyDues.find((entry) => entry.id === button.dataset.deleteMonthlyDuesId);
      if (!due || !window.confirm(`Delete the ${due.period} monthly dues status (${due.status}, ${money(due.amount)}) for ${member.full_name}? This cannot be undone.`)) return;
      const result = await run(() => enasia.deleteMonthlyDues(due.id), "Monthly dues status deleted.");
      if (result) await selectMember(member.id);
    });
  }
  for (const button of $("#detail-panel").querySelectorAll("[data-payment-id]")) {
    button.addEventListener("click", () => {
      const payment = payments.find((entry) => entry.id === button.dataset.paymentId);
      if (payment) openPaymentForm(member.id, payment);
    });
  }
  for (const button of $("#detail-panel").querySelectorAll("[data-delete-payment-id]")) {
    button.addEventListener("click", async () => {
      const payment = payments.find((entry) => entry.id === button.dataset.deletePaymentId);
      if (!payment || !window.confirm(`Delete this ${payment.payment_type} payment of ${money(payment.amount)} dated ${payment.payment_date || "unknown date"} from ${member.full_name}? This cannot be undone.`)) return;
      const result = await run(() => enasia.deletePayment(payment.id), "Payment deleted.");
      if (result) await selectMember(member.id);
    });
  }
  for (const button of $("#detail-panel").querySelectorAll("[data-document-id]")) {
    button.addEventListener("click", () => run(() => enasia.openDocument(button.dataset.documentId)));
  }
  for (const button of $("#detail-panel").querySelectorAll("[data-upload-document-id]")) {
    button.addEventListener("click", () => {
      const document = documents.find((entry) => entry.id === button.dataset.uploadDocumentId);
      if (document) openDocumentUploadForm(member.id, documentPayload(document));
    });
  }
  for (const button of $("#detail-panel").querySelectorAll("[data-edit-document-id]")) {
    button.addEventListener("click", () => {
      const document = documents.find((entry) => entry.id === button.dataset.editDocumentId);
      if (document) openDocumentForm(document);
    });
  }
  for (const button of $("#detail-panel").querySelectorAll("[data-replace-document-id]")) {
    button.addEventListener("click", () => {
      const document = documents.find((entry) => entry.id === button.dataset.replaceDocumentId);
      if (document) openDocumentUploadForm(member.id, documentPayload(document));
    });
  }
  for (const button of $("#detail-panel").querySelectorAll("[data-delete-document-id]")) {
    button.addEventListener("click", async () => {
      const document = documents.find((entry) => entry.id === button.dataset.deleteDocumentId);
      if (!document || !window.confirm(`Remove ${document.document_type} (${document.original_filename}) from this member? The document record and uploaded file will be permanently removed.`)) return;
      const result = await run(() => enasia.deleteDocument(document.id), "Document deleted.");
      if (result) await selectMember(member.id);
    });
  }
}

async function loadUsers() {
  const result = await run(() => enasia.listUsers());
  if (!result) return;
  $("#user-list").innerHTML = result.users.map((user) => `
    <div class="record" style="flex-wrap: wrap;">
      <div>
        <strong>${text(user.username)}</strong>
        <small>Created ${text(user.created_at.slice(0, 10))}</small>
      </div>
      <div class="record-actions" style="align-items: center;">
        <select class="small-button" data-role-user-id="${text(user.id)}" style="width: auto; padding: 4px 8px;">
          <option value="Admin"${user.role === "Admin" ? " selected" : ""}>Admin</option>
          <option value="Editor"${user.role === "Editor" ? " selected" : ""}>Editor</option>
          <option value="View only"${user.role === "View only" ? " selected" : ""}>View only</option>
        </select>
        <button type="button" class="button button-secondary small-button" data-reset-pw-user-id="${text(user.id)}" data-reset-pw-username="${text(user.username)}">Reset password</button>
        <button type="button" class="button button-remove small-button" data-delete-user-id="${text(user.id)}" data-delete-username="${text(user.username)}">Delete</button>
      </div>
    </div>
  `).join("") || '<p class="muted">No staff user accounts created yet. You can create Admin, Editor, or View only accounts below.</p>';

  for (const select of $("#user-list").querySelectorAll("[data-role-user-id]")) {
    select.addEventListener("change", async () => {
      const updated = await run(() => enasia.updateUser(select.dataset.roleUserId, { role: select.value }), "User role updated.");
      if (updated) await loadUsers();
    });
  }
  for (const button of $("#user-list").querySelectorAll("[data-reset-pw-user-id]")) {
    button.addEventListener("click", async () => {
      const uname = button.dataset.resetPwUsername;
      const newPassword = window.prompt(`Enter new password for "${uname}" (at least 6 characters):`);
      if (!newPassword) return;
      const updated = await run(() => enasia.updateUser(button.dataset.resetPwUserId, { password: newPassword }), `Password updated for ${uname}.`);
      if (updated) await loadUsers();
    });
  }
  for (const button of $("#user-list").querySelectorAll("[data-delete-user-id]")) {
    button.addEventListener("click", async () => {
      const uname = button.dataset.deleteUsername;
      if (!window.confirm(`Delete user account "${uname}"?`)) return;
      const deleted = await run(() => enasia.deleteUser(button.dataset.deleteUserId), `Deleted user ${uname}.`);
      if (deleted) await loadUsers();
    });
  }
}

async function loadMembers(search = "") {
  const result = await run(() => enasia.listMembers(search));
  if (!result) return;
  renderMembers(result.members);
}

async function selectMember(id) {
  states.activeMemberId = id;
  renderMembers(states.members);
  const result = await run(() => enasia.getMember(id));
  if (result) renderDetail(result);
}

function openMemberForm(member) {
  const form = $("#member-form");
  form.reset();
  $("#member-id").value = member?.id || "";
  $("#member-form-title").textContent = member ? "Edit member" : "Add a member";
  for (const [key, value] of Object.entries(member || {})) {
    const input = form.elements.namedItem(key);
    if (input) input.value = value ?? "";
  }
  if (!member) form.elements.namedItem("status").value = "Active";
  $("#member-dialog").showModal();
}

function openPaymentForm(memberId, payment = null) {
  const form = $("#payment-form");
  form.reset();
  const paymentType = form.elements.namedItem("payment_type");
  paymentType.querySelector('option[value="Monthly Dues"]')?.remove();
  if (payment?.payment_type === "Monthly Dues") {
    paymentType.add(new Option("Monthly Dues (existing record)", "Monthly Dues"));
  }
  $("#payment-member-id").value = memberId;
  $("#payment-id").value = payment?.id || "";
  $("#payment-form-title").textContent = payment ? "Edit payment" : "Add payment";
  $("#payment-submit-button").textContent = payment ? "Save changes" : "Save payment";
  for (const [key, value] of Object.entries(payment || {})) {
    const input = form.elements.namedItem(key);
    if (input) input.value = value ?? "";
  }
  if (!payment) form.elements.namedItem("payment_date").value = new Date().toISOString().slice(0, 10);
  $("#payment-dialog").showModal();
}

function openDuesForm(memberId, due = null) {
  const form = $("#dues-form");
  form.reset();
  $("#dues-member-id").value = memberId;
  $("#dues-id").value = due?.id || "";
  $("#dues-form-title").textContent = due ? "Edit dues status" : "Record dues status";
  $("#dues-submit-button").textContent = due ? "Save changes" : "Save dues status";
  for (const [key, value] of Object.entries(due || {})) {
    const input = form.elements.namedItem(key);
    if (input) input.value = value ?? "";
  }
  if (!due) form.elements.namedItem("period").value = new Date().toISOString().slice(0, 7);
  $("#dues-dialog").showModal();
}

function documentPayload(document) {
  return {
    document_id: document.id,
    document_type: document.document_type,
    issue_date: document.issue_date || "",
    expiry_date: document.expiry_date || "",
  };
}

function openDocumentForm(document) {
  const form = $("#document-form");
  form.reset();
  $("#document-id").value = document.id;
  for (const [key, value] of Object.entries(document)) {
    const input = form.elements.namedItem(key);
    if (input) input.value = value ?? "";
  }
  $("#document-dialog").showModal();
}

function openDocumentUploadForm(memberId, existingDocument = null) {
  const form = $("#document-upload-form");
  form.reset();
  $("#document-upload-member-id").value = memberId;
  $("#document-upload-id").value = existingDocument?.document_id || "";
  $("#document-upload-title").textContent = existingDocument?.document_id
    ? "Attach or replace document file"
    : "Upload document";
  for (const [key, value] of Object.entries(existingDocument || {})) {
    const input = form.elements.namedItem(key);
    if (input) input.value = value ?? "";
  }
  updateDocumentUploadDateFields();
  $("#document-upload-dialog").showModal();
}

function updateDocumentUploadDateFields() {
  const documentType = $("#document-upload-form [name='document_type']").value.trim();
  const needsDates = /^(PA|CPC|OR|CR)(?:\b|$)/i.test(documentType);
  $("#document-upload-issue-date-label").hidden = !needsDates;
  $("#document-upload-expiry-date-label").hidden = !needsDates;
  if (!needsDates) {
    $("#document-upload-form [name='issue_date']").value = "";
    $("#document-upload-form [name='expiry_date']").value = "";
  }
}

function createImportResult(content) {
  const panel = $("#import-result");
  panel.replaceChildren();
  panel.classList.remove("hidden");
  for (const node of content) panel.append(node);
}

$("#admin-resume-button").addEventListener("click", async () => {
  const result = await run(() => enasia.adminResume());
  if (!result) return;
  await enterSignedInState(result.username, result.role);
});

$("#auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const credentials = { username: $("#username").value.trim(), password: $("#password").value };
  const result = await run(() => states.setupRequired
    ? enasia.setup(credentials)
    : enasia.login(credentials));
  if (!result) return;
  await enterSignedInState(result.username, result.role);
});

$("#logout-button").addEventListener("click", async () => {
  await run(() => enasia.logout());
  states.signedIn = false;
  states.role = "";
  states.activeMemberId = "";
  show($("#account-bar"), false);
  show($("#app-panel"), false);
  const status = await run(() => enasia.serverStatus());
  if (status) {
    states.websiteAdminLoggedIn = Boolean(status.websiteAdminLoggedIn);
    renderAuth(Boolean(status.setupRequired));
  } else {
    renderAuth(false);
  }
  $("#password").value = "";
});

$("#manage-users-button").addEventListener("click", async () => {
  $("#users-dialog").showModal();
  await loadUsers();
});

$("#user-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const user = Object.fromEntries(new FormData(event.currentTarget).entries());
  const result = await run(() => enasia.createUser(user), "User account created.");
  if (!result) return;
  event.currentTarget.reset();
  await loadUsers();
});

$("#member-search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadMembers($("#member-search").value.trim()), 180);
});

$("#new-member-button").addEventListener("click", () => openMemberForm());

$("#member-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const formData = new FormData(event.currentTarget);
  const member = Object.fromEntries(formData.entries());
  member.id = $("#member-id").value;
  member.monthly_dues_amount = Number(member.monthly_dues_amount || 0);
  member.due_day = member.due_day ? Number(member.due_day) : null;
  const result = await run(() => enasia.saveMember(member), "Member record saved.");
  if (!result) return;
  const id = member.id || result.id;
  $("#member-dialog").close();
  await loadMembers($("#member-search").value.trim());
  await selectMember(id);
});

$("#payment-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const fields = Object.fromEntries(new FormData(event.currentTarget).entries());
  fields.member_id = $("#payment-member-id").value;
  fields.amount = Number(fields.amount);
  if (!fields.period_covered) fields.period_covered = "";
  const paymentId = $("#payment-id").value;
  const result = await run(() => paymentId
    ? enasia.updatePayment(paymentId, fields)
    : enasia.addPayment(fields), paymentId ? "Payment updated." : "Payment recorded.");
  if (!result) return;
  const memberId = fields.member_id;
  $("#payment-dialog").close();
  await selectMember(memberId);
});

$("#dues-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const fields = Object.fromEntries(new FormData(event.currentTarget).entries());
  fields.member_id = $("#dues-member-id").value;
  fields.amount = Number(fields.amount || 0);
  const dueId = $("#dues-id").value;
  const result = await run(() => dueId
    ? enasia.updateMonthlyDues(dueId, fields)
    : enasia.addMonthlyDues(fields), dueId ? "Monthly dues status updated." : "Monthly dues status saved.");
  if (!result) return;
  const memberId = fields.member_id;
  $("#dues-dialog").close();
  await selectMember(memberId);
});

$("#document-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const documentId = $("#document-id").value;
  const details = Object.fromEntries(new FormData(event.currentTarget).entries());
  const result = await run(() => enasia.updateDocument(documentId, details), "Document details updated.");
  if (!result) return;
  $("#document-dialog").close();
  await selectMember(states.activeMemberId);
});

$("#document-upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const memberId = $("#document-upload-member-id").value;
  const documentId = $("#document-upload-id").value;
  const details = Object.fromEntries(new FormData(event.currentTarget).entries());
  const file = $("#document-upload-file").files[0];
  if (!file) {
    showNotice("Choose a PDF, JPG, or PNG file to upload.", true);
    return;
  }
  const result = await run(() => enasia.uploadDocument(
    memberId,
    {
      document_id: documentId,
      document_type: details.document_type.trim(),
      issue_date: details.issue_date,
      expiry_date: details.expiry_date,
    },
    file,
  ), documentId ? "Document file attached or replaced." : "Document uploaded.");
  if (!result) return;
  $("#document-upload-dialog").close();
  await selectMember(memberId);
});
$("#document-upload-form [name='document_type']").addEventListener("input", updateDocumentUploadDateFields);

$("#import-button").addEventListener("click", async () => {
  const preview = await run(() => enasia.previewImport());
  if (!preview) return;

  const heading = document.createElement("h3");
  heading.textContent = "Excel import preview";
  const counts = document.createElement("p");
  counts.textContent = `${preview.counts.members} member rows, ${preview.counts.payments} payment rows, ${preview.counts.documents} document records.`;
  const info = document.createElement("p");
  info.className = "muted";
  info.textContent = "The spreadsheet lists documents but does not contain the actual scans. Upload scans from each member profile after importing.";
  const actions = document.createElement("div");
  actions.className = "toolbar-actions";
  const cancel = document.createElement("button");
  cancel.className = "button button-quiet";
  cancel.textContent = "Cancel";
  cancel.addEventListener("click", () => $("#import-result").classList.add("hidden"));
  const commit = document.createElement("button");
  commit.className = "button button-primary";
  commit.textContent = preview.alreadyImported ? "This workbook was already imported" : "Confirm and import workbook";
  commit.disabled = preview.alreadyImported;
  commit.addEventListener("click", async () => {
    if (!window.confirm("Import these workbook records? Existing members with the same Member Code will be updated. Payment rows will be added; importing them again can create duplicate payments.")) return;
    const result = await run(() => enasia.commitImport());
    if (!result) return;
    createImportResult([Object.assign(document.createElement("h3"), { textContent: "Import complete" }),
      Object.assign(document.createElement("p"), { textContent: `${result.imported.members} members, ${result.imported.payments} payments, ${result.imported.monthlyDues} monthly dues statuses, and ${result.imported.documentsListed} document records imported. Upload document scans from member profiles.` })]);
    showNotice("Excel workbook imported.");
    states.activeMemberId = "";
    $("#detail-panel").innerHTML = '<div class="empty-state"><span class="empty-icon">◎</span><h3>Select a member</h3><p>Choose someone from the directory to view their records.</p></div>';
    await loadMembers($("#member-search").value.trim());
  });
  actions.append(cancel, commit);
  createImportResult([heading, counts, info, actions]);
});

$("#export-button").addEventListener("click", async () => {
  await run(() => enasia.exportExcel(), "Excel export downloaded.");
});

for (const button of document.querySelectorAll("[data-close]")) {
  button.addEventListener("click", () => document.getElementById(button.dataset.close).close());
}

enasia.serverStatus().then(async (status) => {
  states.connected = true;
  states.websiteAdminLoggedIn = Boolean(status.websiteAdminLoggedIn);
  if (status.autoSignedIn) {
    await enterSignedInState(status.username, status.role);
  } else {
    renderAuth(Boolean(status.setupRequired));
  }
}).catch((error) => showNotice(`Could not connect to the Enasia database: ${error.message}`, true));
