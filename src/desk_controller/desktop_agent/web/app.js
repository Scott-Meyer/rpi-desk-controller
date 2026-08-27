const token = document.querySelector('meta[name="desk-agent-token"]').content;
const apiHeaders = {
  "Content-Type": "application/json",
  "X-Desk-Agent-Token": token,
};

const byId = (id) => document.getElementById(id);
const iconGlyphs = {
  none: "·",
  kvm: "▣",
  speakers: "◖))",
  mute: "◖×",
  play: "▶",
  pause: "Ⅱ",
  link: "↗",
  keyboard: "⌨",
  headphones: "Ω",
  wireless_headphones: "Ω⌁",
  earbuds: "♩",
  bulb: "☼",
  moon: "◒",
  sun: "☀",
  shades_close: "▤",
  shades_privacy: "▥",
  shades_extra_light: "▦",
};

let state = null;
let slotDrafts = new Map();
let selectedSlotId = null;
let dirty = false;
let connectionRefreshInFlight = false;
let saveWaitingForSync = false;

function rgbToHex(rgb) {
  const values = Array.isArray(rgb) && rgb.length === 3 ? rgb : [80, 180, 255];
  return `#${values.map((value) => Number(value).toString(16).padStart(2, "0")).join("")}`.toUpperCase();
}

function hexToRgb(hex) {
  return [1, 3, 5].map((offset) => Number.parseInt(hex.slice(offset, offset + 2), 16));
}

function defaultSlot(slotId) {
  return {
    slot_id: String(slotId),
    label: "APP",
    icon: "none",
    accent_color: [80, 180, 255],
    action_type: "app",
    process_names: [],
    launch_target: "",
    shortcut: "",
  };
}

function normalizeLabel(label) {
  return String(label || "").replaceAll("\\n", "\n");
}

function updateConnection() {
  const connection = document.querySelector(".connection");
  const online = Boolean(state?.mqtt_connected);
  const authFailed = Boolean(state?.mqtt_auth_failed);

  connection.classList.toggle("online", online);
  connection.classList.toggle("auth-failed", authFailed && !online);

  const statusBar = byId("mqttStatusBar");
  const statusText = byId("mqttStatusText");

  if (online) {
    byId("connectionText").textContent = `${state.hostname} · MQTT connected`;
    if (statusBar && statusText) {
      statusBar.className = "mqtt-status-bar online";
      statusText.textContent = "● Connected to MQTT broker";
    }
    return;
  }
  if (authFailed) {
    const errorMsg = state?.mqtt_auth_error || "bad credentials";
    byId("connectionText").textContent = `${state?.hostname || "Desk Agent"} · MQTT auth failed (${errorMsg})`;
    if (statusBar && statusText) {
      statusBar.className = "mqtt-status-bar auth-failed";
      statusText.textContent = `● Authentication failed: ${errorMsg}. Please check username and password.`;
    }
    return;
  }
  const health = state?.mqtt_health || {};
  const disconnectedFor = Number(health.disconnected_seconds);
  const duration = Number.isFinite(disconnectedFor)
    ? ` · retrying for ${Math.round(disconnectedFor)}s`
    : "";
  const recoveries = Number(health.recovery_count) > 0
    ? ` · recovery ${health.recovery_count}`
    : "";
  byId("connectionText").textContent = `${state?.hostname || "Desk Agent"} · MQTT offline${duration}${recoveries}`;
  if (statusBar && statusText) {
    statusBar.className = "mqtt-status-bar offline";
    statusText.textContent = `● Offline${duration}. Retrying connection…`;
  }
}

async function refreshConnectionStatus() {
  if (document.hidden || connectionRefreshInFlight) return;
  connectionRefreshInFlight = true;
  try {
    const response = await fetch("/api/v1/buttons", {
      headers: { "X-Desk-Agent-Token": token },
      cache: "no-store",
    });
    if (!response.ok) return;
    const body = await response.json();
    state = {
      ...(state || {}),
      hostname: body.hostname,
      mqtt_connected: body.mqtt_connected,
      mqtt_auth_failed: body.mqtt_auth_failed,
      mqtt_auth_error: body.mqtt_auth_error,
      mqtt_health: body.mqtt_health,
      pi_sync_pending: body.pi_sync_pending,
      pi_web_url: body.pi_web_url,
    };
    const piWebBtn = byId("openPiWebBtn");
    if (piWebBtn) {
      if (body.pi_web_url) {
        piWebBtn.href = body.pi_web_url;
        piWebBtn.style.display = "inline-flex";
      } else {
        piWebBtn.style.display = "none";
      }
    }
    updateConnection();
    if (saveWaitingForSync && !body.pi_sync_pending) {
      saveWaitingForSync = false;
      if (!dirty) {
        document.querySelector(".actions").classList.add("success");
        byId("saveMessage").textContent = "Saved and synchronized with the Pi.";
      }
    }
  } catch {
    // The local editor server may be stopping; keep the last known status.
  } finally {
    connectionRefreshInFlight = false;
  }
}

function collectEditor() {
  if (!selectedSlotId || byId("editorFields").hidden) return;
  if (!byId("configured").checked) {
    slotDrafts.delete(selectedSlotId);
    return;
  }
  const actionType = byId("actionType").value;
  slotDrafts.set(selectedSlotId, {
    slot_id: selectedSlotId,
    label: byId("label").value.trim(),
    icon: byId("icon").value,
    accent_color: hexToRgb(byId("accentColor").value),
    action_type: actionType,
    process_names: byId("processNames").value
      .split(",")
      .map((name) => name.trim())
      .filter(Boolean),
    launch_target: actionType === "app"
      ? byId("launchTarget").value.trim()
      : actionType === "open"
        ? byId("openTarget").value.trim()
        : "",
    shortcut: actionType === "hotkey"
      ? byId("buttonShortcut").value.trim()
      : "",
  });
}

function setDirty(value = true) {
  dirty = value;
  byId("saveButton").disabled = !state || !dirty;
  document.querySelector(".actions").classList.remove("success", "error");
  byId("saveMessage").textContent = dirty
    ? "You have unsaved button changes."
    : "Changes apply immediately after saving.";
}

function populateDesktopKvm(settings) {
  const hotkey = settings?.hotkey || {};
  const monitor = settings?.monitor || {};
  const usb = settings?.usb || {};
  byId("hotkeyEnabled").checked = Boolean(hotkey.enabled);
  byId("hotkeyShortcut").value = hotkey.shortcut || "ctrl+option+command+k";
  byId("monitorControlEnabled").checked = Boolean(monitor.enabled);
  byId("monitorBackend").value = monitor.backend || "auto";
  byId("monitorDisplayName").value = monitor.display_name || "";
  byId("localPc1Input").value = monitor.inputs?.pc1 || "0x0f";
  byId("localPc2Input").value = monitor.inputs?.pc2 || "0x11";
  byId("usbControlEnabled").checked = Boolean(usb.enabled);
  byId("localUsbSerial").value = usb.serial_number == null
    ? ""
    : `0x${Number(usb.serial_number).toString(16)}`;
}

function desktopKvmPayload() {
  const current = state?.desktop_kvm || {};
  const serialText = byId("localUsbSerial").value.trim();
  const serialNumber = serialText ? Number(serialText) : null;
  if (serialText && !Number.isInteger(serialNumber)) {
    throw new Error("Acroname serial number must be decimal or start with 0x.");
  }
  return {
    hotkey: {
      enabled: byId("hotkeyEnabled").checked,
      shortcut: byId("hotkeyShortcut").value.trim(),
    },
    monitor: {
      enabled: byId("monitorControlEnabled").checked,
      backend: byId("monitorBackend").value,
      display_name: byId("monitorDisplayName").value.trim(),
      betterdisplay_path: current.monitor?.betterdisplay_path
        || "/Applications/BetterDisplay.app/Contents/MacOS/BetterDisplay",
      display_id: current.monitor?.display_id || 1,
      inputs: {
        pc1: byId("localPc1Input").value.trim(),
        pc2: byId("localPc2Input").value.trim(),
      },
    },
    usb: {
      enabled: byId("usbControlEnabled").checked,
      serial_number: serialNumber,
    },
  };
}

function updateActionFields(setDefaults = false) {
  const type = byId("actionType").value;
  byId("appFields").hidden = type !== "app";
  byId("openFields").hidden = type !== "open";
  byId("hotkeyFields").hidden = type !== "hotkey";
  byId("mediaHint").hidden = type !== "media_play_pause";
  byId("muteHint").hidden = type !== "audio_mute";
  if (!setDefaults) return;
  if (type === "audio_mute") {
    byId("label").value = "MUTE";
    byId("icon").value = "mute";
  } else if (type === "media_play_pause") {
    byId("label").value = "PLAY/PAUSE";
    byId("icon").value = "play";
  } else if (type === "open") {
    byId("label").value = "OPEN";
    byId("icon").value = "link";
  } else if (type === "hotkey") {
    byId("label").value = "SHORTCUT";
    byId("icon").value = "keyboard";
  } else {
    byId("label").value = "APP";
    byId("icon").value = "none";
  }
}

function renderEditor() {
  if (!selectedSlotId) {
    byId("emptyEditor").hidden = false;
    byId("editorFields").hidden = true;
    return;
  }

  const layoutButton = state.layout.buttons.find(
    (button) => String(button.slot_id) === selectedSlotId,
  );
  const configured = slotDrafts.has(selectedSlotId);
  const slot = configured ? slotDrafts.get(selectedSlotId) : defaultSlot(selectedSlotId);

  byId("emptyEditor").hidden = true;
  byId("editorFields").hidden = false;
  byId("slotId").textContent = selectedSlotId;
  byId("keyNumber").textContent = Number(layoutButton.key) + 1;
  byId("configured").checked = configured;
  byId("assignmentFields").classList.toggle("disabled", !configured);
  byId("actionType").value = slot.action_type;
  byId("label").value = slot.label;
  byId("icon").value = slot.icon;
  byId("accentColor").value = rgbToHex(slot.accent_color).toLowerCase();
  byId("accentColorValue").value = rgbToHex(slot.accent_color);
  byId("launchTarget").value = slot.launch_target || "";
  byId("openTarget").value = slot.action_type === "open"
    ? slot.launch_target || ""
    : "";
  byId("buttonShortcut").value = slot.shortcut || "";
  byId("processNames").value = (slot.process_names || []).join(", ");
  updateActionFields();
}

function keyPresentation(button) {
  const slotId = String(button.slot_id || "");
  const draft = slotDrafts.get(slotId);
  if (draft) {
    return {
      label: draft.label || `SLOT ${slotId}`,
      icon: draft.icon,
      accent: rgbToHex(draft.accent_color),
    };
  }
  if (button.action_type === "workstation_slot") {
    return { label: "", icon: "none", accent: null };
  }
  return {
    label: normalizeLabel(button.label) || (button.configured ? "PI BUTTON" : ""),
    icon: button.icon || "none",
    accent: null,
  };
}

function renderDeck() {
  const grid = byId("deckGrid");
  grid.innerHTML = "";
  const rows = state?.layout?.rows || 3;
  const columns = state?.layout?.columns || 5;
  grid.style.gridTemplateColumns = `repeat(${columns}, minmax(0, 1fr))`;
  const byKey = new Map((state?.layout?.buttons || []).map((button) => [button.key, button]));

  for (let key = 0; key < rows * columns; key += 1) {
    const button = byKey.get(key) || {
      key,
      configured: false,
      action_type: "none",
      label: "",
      icon: "none",
    };
    const isComputer = button.action_type === "workstation_slot" && button.enabled !== false;
    const slotId = String(button.slot_id || "");
    const assigned = isComputer && slotDrafts.has(slotId);
    const presentation = keyPresentation(button);
    const element = document.createElement("button");
    element.type = "button";
    element.className = "deck-key";
    element.classList.toggle("computer", isComputer);
    element.classList.toggle("assigned", assigned);
    element.classList.toggle("selected", isComputer && selectedSlotId === slotId);
    element.classList.toggle("empty", !button.configured);
    if (presentation.accent) element.style.setProperty("--key-accent", presentation.accent);
    element.disabled = !isComputer;
    element.innerHTML = `
      <span class="key-number">${key + 1}</span>
      <span class="key-icon">${iconGlyphs[presentation.icon] || "·"}</span>
      <span class="key-label"></span>
    `;
    element.querySelector(".key-label").textContent = presentation.label;
    if (isComputer) {
      element.setAttribute("aria-label", `Configure computer slot ${slotId} on key ${key + 1}`);
      element.addEventListener("click", () => {
        collectEditor();
        selectedSlotId = slotId;
        renderDeck();
        renderEditor();
      });
    }
    grid.appendChild(element);
  }

  byId("layoutMessage").classList.toggle("ready", Boolean(state?.layout_received));
  byId("layoutMessage").textContent = state?.layout_received
    ? ""
    : "Waiting for the Pi to advertise its retained layout…";
}

async function loadConfiguration() {
  byId("refreshButton").disabled = true;
  try {
    const response = await fetch("/api/v1/buttons", {
      headers: { "X-Desk-Agent-Token": token },
      cache: "no-store",
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || "Could not load button configuration.");
    state = body;
    if (Array.isArray(body.action_catalog) && body.action_catalog.length) {
      const actionType = byId("actionType");
      actionType.innerHTML = "";
      body.action_catalog.forEach((action) => {
        const option = document.createElement("option");
        option.value = action.type;
        option.textContent = action.label;
        actionType.append(option);
      });
    }
    slotDrafts = new Map(body.slots.map((slot) => [String(slot.slot_id), { ...slot }]));
    const availableIds = new Set(
      body.layout.buttons
        .filter((button) => button.action_type === "workstation_slot" && button.enabled !== false)
        .map((button) => String(button.slot_id)),
    );
    if (!availableIds.has(selectedSlotId)) {
      selectedSlotId = body.layout.buttons
        .find((button) => button.action_type === "workstation_slot" && button.enabled !== false)
        ?.slot_id;
      if (selectedSlotId != null) selectedSlotId = String(selectedSlotId);
    }
    updateConnection();
    populateDesktopKvm(body.desktop_kvm);
    renderDeck();
    renderEditor();
    setDirty(false);
  } catch (error) {
    document.querySelector(".actions").classList.add("error");
    byId("saveMessage").textContent = error.message;
  } finally {
    byId("refreshButton").disabled = false;
  }
}

async function saveConfiguration() {
  collectEditor();
  const slots = [...slotDrafts.values()];
  for (const slot of slots) {
    if (!slot.label.trim()) {
      document.querySelector(".actions").classList.add("error");
      byId("saveMessage").textContent = `Slot ${slot.slot_id} needs a label.`;
      return;
    }
    if (slot.action_type === "app" && !slot.launch_target.trim()) {
      document.querySelector(".actions").classList.add("error");
      byId("saveMessage").textContent = `Slot ${slot.slot_id} needs an application.`;
      return;
    }
    if (slot.action_type === "open" && !slot.launch_target.trim()) {
      document.querySelector(".actions").classList.add("error");
      byId("saveMessage").textContent = `Slot ${slot.slot_id} needs a file or URL.`;
      return;
    }
    if (slot.action_type === "hotkey" && !slot.shortcut.trim()) {
      document.querySelector(".actions").classList.add("error");
      byId("saveMessage").textContent = `Slot ${slot.slot_id} needs a keyboard shortcut.`;
      return;
    }
  }

  byId("saveButton").disabled = true;
  byId("saveMessage").textContent = "Saving and publishing buttons…";
  try {
    const desktopKvm = desktopKvmPayload();
    const response = await fetch("/api/v1/buttons", {
      method: "PUT",
      headers: apiHeaders,
      body: JSON.stringify({ slots, desktop_kvm: desktopKvm }),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || "Could not save buttons.");
    state = body;
    slotDrafts = new Map(body.slots.map((slot) => [String(slot.slot_id), { ...slot }]));
    updateConnection();
    renderDeck();
    renderEditor();
    populateDesktopKvm(body.desktop_kvm);
    dirty = false;
    saveWaitingForSync = Boolean(body.pi_sync_pending);
    document.querySelector(".actions").classList.remove("error");
    document.querySelector(".actions").classList.add("success");
    byId("saveMessage").textContent = body.pi_sync_pending
      ? "Saved locally. The agent will publish these buttons when MQTT reconnects."
      : "Saved. The Pi has the updated button manifest.";
  } catch (error) {
    document.querySelector(".actions").classList.remove("success");
    document.querySelector(".actions").classList.add("error");
    byId("saveMessage").textContent = error.message;
    byId("saveButton").disabled = false;
  }
}

byId("configured").addEventListener("change", () => {
  if (byId("configured").checked) {
    slotDrafts.set(selectedSlotId, defaultSlot(selectedSlotId));
  } else {
    slotDrafts.delete(selectedSlotId);
  }
  byId("assignmentFields").classList.toggle("disabled", !byId("configured").checked);
  setDirty();
  renderDeck();
  renderEditor();
});

for (const field of [
  "label",
  "icon",
  "accentColor",
  "launchTarget",
  "openTarget",
  "buttonShortcut",
  "processNames",
]) {
  byId(field).addEventListener("input", () => {
    collectEditor();
    if (field === "accentColor") {
      byId("accentColorValue").value = byId("accentColor").value.toUpperCase();
    }
    setDirty();
    renderDeck();
  });
}

byId("actionType").addEventListener("change", () => {
  updateActionFields(true);
  collectEditor();
  setDirty();
  renderDeck();
});
byId("refreshButton").addEventListener("click", loadConfiguration);
byId("saveButton").addEventListener("click", saveConfiguration);

for (const field of [
  "hotkeyEnabled",
  "hotkeyShortcut",
  "monitorControlEnabled",
  "monitorBackend",
  "monitorDisplayName",
  "localPc1Input",
  "localPc2Input",
  "usbControlEnabled",
  "localUsbSerial",
]) {
  byId(field).addEventListener("input", () => setDirty());
  byId(field).addEventListener("change", () => setDirty());
}

window.addEventListener("beforeunload", (event) => {
  if (!dirty) return;
  event.preventDefault();
  event.returnValue = "";
});

async function loadMqttConfiguration() {
  try {
    const response = await fetch("/api/v1/mqtt", {
      headers: { "X-Desk-Agent-Token": token },
      cache: "no-store",
    });
    if (!response.ok) return;
    const body = await response.json();
    byId("mqttBroker").value = body.broker || "homeassistant.local";
    byId("mqttPort").value = body.port || 1883;
    byId("mqttUsername").value = body.username || "";
    byId("mqttPassword").value = "";
    if (body.password_configured) {
      byId("mqttPassword").placeholder = "●●●●●●●● (leave blank to keep)";
    } else {
      byId("mqttPassword").placeholder = "Optional password";
    }
  } catch {
    // Ignore fetch errors during startup
  }
}

async function testMqtt() {
  const broker = byId("mqttBroker").value.trim();
  const port = Number.parseInt(byId("mqttPort").value, 10);
  const username = byId("mqttUsername").value.trim();
  const password = byId("mqttPassword").value;
  const feedback = byId("mqttSaveFeedback");
  const testBtn = byId("testMqttButton");

  if (!broker) {
    feedback.style.color = "var(--danger)";
    feedback.textContent = "Broker address is required.";
    return;
  }

  testBtn.disabled = true;
  feedback.style.color = "var(--cyan)";
  feedback.textContent = "Testing connection to broker…";

  try {
    const payload = { broker, port, username };
    if (password) payload.password = password;

    const response = await fetch("/api/v1/mqtt/test", {
      method: "POST",
      headers: apiHeaders,
      body: JSON.stringify(payload),
    });
    const body = await response.json();
    if (body.success) {
      feedback.style.color = "var(--green)";
      feedback.textContent = "✓ Connected successfully!";
    } else {
      feedback.style.color = "var(--danger)";
      feedback.textContent = `✗ ${body.message || "Connection failed."}`;
    }
  } catch (error) {
    feedback.style.color = "var(--danger)";
    feedback.textContent = `✗ ${error.message}`;
  } finally {
    testBtn.disabled = false;
  }
}

async function saveMqtt() {
  const broker = byId("mqttBroker").value.trim();
  const port = Number.parseInt(byId("mqttPort").value, 10);
  const username = byId("mqttUsername").value.trim();
  const password = byId("mqttPassword").value;
  const feedback = byId("mqttSaveFeedback");
  const saveBtn = byId("saveMqttButton");

  if (!broker) {
    feedback.style.color = "var(--danger)";
    feedback.textContent = "Broker address is required.";
    return;
  }

  saveBtn.disabled = true;
  feedback.style.color = "var(--cyan)";
  feedback.textContent = "Testing & saving credentials…";

  try {
    const payload = { broker, port, username };
    if (password) payload.password = password;

    const response = await fetch("/api/v1/mqtt", {
      method: "PUT",
      headers: apiHeaders,
      body: JSON.stringify(payload),
    });
    const body = await response.json();
    if (!response.ok) {
      feedback.style.color = "var(--danger)";
      feedback.textContent = `✗ ${body.detail || "Could not save MQTT settings."}`;
      return;
    }
    feedback.style.color = "var(--green)";
    feedback.textContent = "✓ Saved & reconnected!";
    await refreshConnectionStatus();
  } catch (error) {
    feedback.style.color = "var(--danger)";
    feedback.textContent = `✗ ${error.message}`;
  } finally {
    saveBtn.disabled = false;
  }
}

byId("testMqttButton").addEventListener("click", testMqtt);
byId("saveMqttButton").addEventListener("click", saveMqtt);

loadConfiguration();
loadMqttConfiguration().then(() => {
  if (window.location.hash === "#mqtt" || window.location.hash === "#mqttSection") {
    byId("mqtt")?.scrollIntoView({ behavior: "smooth" });
    byId("mqttBroker")?.focus();
  }
});
window.setInterval(refreshConnectionStatus, 3000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshConnectionStatus();
});
