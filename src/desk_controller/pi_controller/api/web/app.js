const byId = (id) => document.getElementById(id);
const tabs = [...document.querySelectorAll(".tab")];
const panels = [...document.querySelectorAll(".panel")];
const message = byId("message");
const buttonFields = {
  enabled: byId("buttonEnabled"),
  label: byId("buttonLabel"),
  group: byId("buttonGroup"),
  action_type: byId("buttonAction"),
  slot_id: byId("buttonSlotId"),
  icon: byId("buttonIcon"),
  accent_color: byId("buttonColor"),
  target: byId("buttonTarget"),
  off_target: byId("buttonOffTarget"),
  mqtt_topic: byId("buttonMqttTopic"),
  mqtt_payload: byId("buttonMqttPayload"),
  state_topic: byId("buttonStateTopic"),
  state_payload: byId("buttonStatePayload"),
  service: byId("buttonService"),
  service_data: byId("buttonServiceData"),
};

let buttons = new Map();
let selectedKey = 0;
let hubLoading = false;
let connectionStatusLoading = false;
let externalMqttPort = 1883;
let desiredUsbHost = 0;

const blankButton = (key) => ({
  key,
  enabled: false,
  label: "",
  icon: "none",
  accent_color: [0, 200, 255],
  group: "",
  action_type: "none",
  slot_id: "",
  target: "",
  off_target: "",
  mqtt_topic: "",
  mqtt_payload: "",
  state_topic: "",
    state_payload: "",
    service: "",
    service_data: {},
});

const portRow = (index) => {
  const card = document.createElement("article");
  card.className = "port-row";
  card.dataset.port = index;
  card.innerHTML = `
    <div class="port-heading">
      <span class="port-number">${index}</span>
      <label>Friendly name<input data-field="name" required></label>
      <span class="port-attachment" data-live="attachment">Unknown</span>
    </div>
    <div class="device-identity">
      <strong data-live="product">No device detected</strong>
      <span data-live="descriptor">Waiting for USB descriptors</span>
      <span data-live="serial"></span>
    </div>
    <div class="port-metrics">
      <div><span>Voltage</span><strong data-live="voltage">—</strong></div>
      <div><span>Current</span><strong data-live="current">—</strong></div>
      <div><span>Power</span><strong data-live="power">—</strong></div>
      <div><span>Speed</span><strong data-live="speed">—</strong></div>
    </div>
    <div class="port-toggles">
      <label><input type="checkbox" data-control="enabled"> Port</label>
      <label><input type="checkbox" data-control="power_enabled"> Power</label>
      <label><input type="checkbox" data-control="usb2_data_enabled"> USB 2</label>
      <label><input type="checkbox" data-control="usb3_data_enabled"> USB 3</label>
    </div>
    <div class="port-settings">
      <label>Current limit (mA)<input type="number" min="0" max="4095" step="1" data-control="current_limit_ma"></label>
      <label>Charging mode
        <select data-control="port_mode">
          <option value="0">SDP</option>
          <option value="1">CDP</option>
        </select>
      </label>
    </div>
    <div class="port-errors" data-live="errors" hidden></div>
    <div class="port-actions">
      <button type="button" class="secondary" data-action="reset_data">Re-enumerate</button>
      <button type="button" class="secondary" data-action="reset_power">Power cycle</button>
      <button type="button" class="text-button" data-action="clear_errors">Clear errors</button>
    </div>`;
  return card;
};

for (let index = 0; index < 8; index += 1) {
  byId("usbPorts").append(portRow(index));
}

for (let key = 0; key < 15; key += 1) {
  const item = document.createElement("button");
  item.type = "button";
  item.className = "deck-key";
  item.dataset.key = key;
  item.addEventListener("click", () => selectKey(key));
  byId("deckGrid").append(item);
}

const defaultSection = tabs[0].dataset.section;
const sections = new Set(tabs.map((tab) => tab.dataset.section));

function sectionFromUrl() {
  const section = new URL(window.location.href).searchParams.get("tab");
  return sections.has(section) ? section : defaultSection;
}

function activateSection(section, historyAction = null) {
  const activeSection = sections.has(section) ? section : defaultSection;
  tabs.forEach((tab) => {
    const active = tab.dataset.section === activeSection;
    tab.classList.toggle("active", active);
    if (active) {
      tab.setAttribute("aria-current", "page");
    } else {
      tab.removeAttribute("aria-current");
    }
  });
  panels.forEach((panel) => panel.classList.toggle("active", panel.id === activeSection));

  if (historyAction) {
    const url = new URL(window.location.href);
    url.searchParams.set("tab", activeSection);
    window.history[historyAction]({}, "", url);
  }
}

tabs.forEach((tab) => {
  tab.addEventListener("click", (event) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (tab.dataset.section !== sectionFromUrl()) {
      activateSection(tab.dataset.section, "pushState");
    }
  });
});

window.addEventListener("popstate", () => activateSection(sectionFromUrl()));
activateSection(sectionFromUrl(), "replaceState");

byId("brightness").addEventListener("input", (event) => {
  byId("brightnessValue").textContent = `${event.target.value}%`;
});

const rgbToHex = (color) => `#${color.map((value) => Number(value).toString(16).padStart(2, "0")).join("")}`;
const hexToRgb = (value) => [1, 3, 5].map((offset) => parseInt(value.slice(offset, offset + 2), 16));

function setMessage(text, kind = "") {
  message.textContent = text;
  message.className = kind ? `message-${kind}` : "";
}

function setHubMessage(text, kind = "") {
  const output = byId("hubMessage");
  output.textContent = text;
  output.className = kind ? `note message-${kind}` : "note";
}

function setConnectionHealth(id, stateId, detailId, state, detail, healthy) {
  const container = byId(id);
  container.className = `connection-health ${healthy ? "healthy" : "unhealthy"}`;
  byId(stateId).textContent = state;
  byId(detailId).textContent = detail;
}

function renderConnectionStatus(status) {
  const mqtt = status.mqtt;
  setConnectionHealth(
    "mqttHealth",
    "mqttConnectionState",
    "mqttConnectionDetail",
    mqtt.connected ? "Connected" : "Disconnected",
    `${mqtt.detail} ${mqtt.mode === "local" ? "Pi-hosted broker" : `${mqtt.broker}:${mqtt.port}`}`,
    Boolean(mqtt.connected),
  );

  const homeAssistant = status.homeassistant;
  const haHealthy = !homeAssistant.enabled || homeAssistant.authenticated;
  const haState = !homeAssistant.enabled
    ? "Disabled"
    : (homeAssistant.authenticated ? "Authenticated" : "Not authenticated");
  setConnectionHealth(
    "haHealth",
    "haConnectionState",
    "haConnectionDetail",
    haState,
    homeAssistant.detail,
    haHealthy,
  );

  const allHealthy = Boolean(mqtt.connected && haHealthy);
  byId("connectionBadge").textContent = allHealthy ? "Healthy" : "Needs attention";
  byId("connectionBadge").className = `badge ${allHealthy ? "healthy" : "unhealthy"}`;
}

async function loadConnectionStatus() {
  if (connectionStatusLoading) return;
  connectionStatusLoading = true;
  try {
    const response = await fetch("/api/v1/config/status", { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Connection status is unavailable");
    renderConnectionStatus(data);
  } catch (error) {
    setConnectionHealth(
      "mqttHealth",
      "mqttConnectionState",
      "mqttConnectionDetail",
      "Status unavailable",
      error.message,
      false,
    );
    setConnectionHealth(
      "haHealth",
      "haConnectionState",
      "haConnectionDetail",
      "Status unavailable",
      error.message,
      false,
    );
    byId("connectionBadge").textContent = "Unavailable";
    byId("connectionBadge").className = "badge unhealthy";
  } finally {
    connectionStatusLoading = false;
  }
}

const metric = (value, suffix, digits = 3) => (
  value === null || value === undefined ? "—" : `${Number(value).toFixed(digits)} ${suffix}`
);

function renderPortState(port) {
  const row = document.querySelector(`.port-row[data-port="${port.index}"]`);
  if (!row) return;
  row.classList.toggle("unavailable", port.available === false);
  const device = port.device;
  const attached = Boolean(port.device_attached);
  const attachment = row.querySelector('[data-live="attachment"]');
  attachment.textContent = attached ? "Attached" : "Empty";
  attachment.classList.toggle("attached", attached);
  row.querySelector('[data-live="product"]').textContent = device?.product_name || (attached ? "USB device" : "No device detected");
  row.querySelector('[data-live="descriptor"]').textContent = device
    ? [device.manufacturer, device.vid_pid].filter(Boolean).join(" · ")
    : (attached ? "Device did not provide string descriptors" : "Waiting for USB descriptors");
  row.querySelector('[data-live="serial"]').textContent = device?.serial_number ? `Serial ${device.serial_number}` : "";
  row.querySelector('[data-live="voltage"]').textContent = metric(port.voltage_v, "V");
  row.querySelector('[data-live="current"]').textContent = metric(port.current_a, "A");
  row.querySelector('[data-live="power"]').textContent = metric(port.power_w, "W");
  const speeds = { 0: "None", 1: "USB 2 · 480 Mb/s", 2: "USB 3 · 5 Gb/s" };
  row.querySelector('[data-live="speed"]').textContent = speeds[port.data_speed] || String(port.data_speed ?? "—");
  ["enabled", "power_enabled", "usb2_data_enabled", "usb3_data_enabled"].forEach((field) => {
    const control = row.querySelector(`[data-control="${field}"]`);
    control.checked = Boolean(port[field]);
    control.disabled = port.available === false;
  });
  const currentLimit = row.querySelector('[data-control="current_limit_ma"]');
  currentLimit.value = port.current_limit_a === null || port.current_limit_a === undefined
    ? ""
    : Math.round(Number(port.current_limit_a) * 1000);
  currentLimit.disabled = port.available === false;
  const portMode = row.querySelector('[data-control="port_mode"]');
  portMode.value = String(port.mode ?? 0);
  portMode.disabled = port.available === false;
  const errors = row.querySelector('[data-live="errors"]');
  const errorNames = port.errors || [];
  errors.hidden = errorNames.length === 0;
  errors.textContent = errorNames.length ? `Error: ${errorNames.join(", ").replaceAll("_", " ")}` : "";
}

function renderHub(status) {
  byId("hubConnection").textContent = status.connected ? "Connected" : "Unavailable";
  byId("hubConnection").classList.toggle("healthy", Boolean(status.connected));
  const serial = typeof status.serial_number === "number"
    ? `0x${status.serial_number.toString(16).padStart(8, "0")}`
    : status.serial_number;
  byId("hubIdentity").textContent = [
    status.manufacturer,
    status.model || "USB switch",
    serial,
  ].filter(Boolean).join(" · ");
  const perPortControl = status.capabilities?.per_port_control !== false;
  byId("usbPorts").hidden = !perPortControl;
  byId("hubName").closest("label").hidden = !perPortControl;
  const syncStatus = status.sync_status || (
    status.state_feedback === false ? "unverified" : "synced"
  );
  byId("hubSyncStatus").textContent = syncStatus.replaceAll("_", " ");
  byId("hubSyncStatus").classList.toggle(
    "healthy",
    syncStatus === "synced",
  );
  if (document.activeElement !== byId("hubName")) {
    byId("hubName").value = status.name || "";
  }
  byId("hubFirmware").textContent = status.firmware_version ?? "—";
  const input = metric(status.input_voltage_v, "V");
  const inputPower = status.input_power_w === null || status.input_power_w === undefined
    ? ""
    : ` · ${metric(status.input_power_w, "W")}`;
  byId("hubInputPower").textContent = `${input}${inputPower}`;
  byId("hubTemperature").textContent = metric(status.temperature_c, "°C", 1);
  desiredUsbHost = status.desired_upstream ?? status.active_upstream ?? 0;
  byId("hubActiveUpstream").value = String(
    status.active_upstream ?? desiredUsbHost,
  );
  (status.ports || []).forEach(renderPortState);
  setHubMessage(
    status.connected
      ? (
        perPortControl
          ? "Live controller state. Device descriptors appear after the active host enumerates the device."
          : "Host switching is available. This toggle-only controller does not expose port telemetry or host feedback."
      )
      : "The USB controller is not connected.",
    status.connected ? "success" : "error",
  );
}

async function loadHub() {
  if (hubLoading) return;
  hubLoading = true;
  try {
    const response = await fetch("/api/v1/usb-hub", { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not read USB hub");
    renderHub(data);
  } catch (error) {
    byId("hubConnection").textContent = "Unavailable";
    setHubMessage(error.message, "error");
  } finally {
    hubLoading = false;
  }
}

async function sendHubCommand(command) {
  setHubMessage("Applying hub change…");
  const response = await fetch("/api/v1/usb-hub", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(command),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "Hub command failed");
  renderHub(data.state);
}

async function sendPortCommand(port, command) {
  const row = document.querySelector(`.port-row[data-port="${port}"]`);
  row.classList.add("busy");
  setHubMessage(`Applying change to port ${port}…`);
  try {
    const response = await fetch(`/api/v1/usb-hub/ports/${port}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(command),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Port command failed");
    setHubMessage(`Port ${port} updated.`, "success");
    await loadHub();
  } catch (error) {
    setHubMessage(error.message, "error");
    await loadHub();
  } finally {
    row.classList.remove("busy");
  }
}

function renderDeck() {
  document.querySelectorAll(".deck-key").forEach((element) => {
    const key = Number(element.dataset.key);
    const button = buttons.get(key) || blankButton(key);
    element.classList.toggle("selected", key === selectedKey);
    element.classList.toggle("empty", !button.enabled);
    element.style.setProperty("--key-accent", rgbToHex(button.accent_color));
    const dynamicLabel = {
      current_time: "Current time",
      current_date: "Current date",
    }[button.action_type];
    const label = button.enabled ? (dynamicLabel || button.label || "Empty") : "Empty";
    const position = document.createElement("span");
    position.className = "deck-position";
    position.textContent = key + 1;
    const title = document.createElement("strong");
    title.textContent = label;
    const detail = document.createElement("small");
    detail.textContent = button.enabled
      ? (button.group || button.action_type.replaceAll("_", " "))
      : "Available to selected computer";
    element.replaceChildren(position, title, detail);
    element.setAttribute("aria-label", `Key ${key + 1}: ${label.replaceAll("\n", " ")}`);
  });
}

function configureActionFields() {
  const action = buttonFields.action_type.value;
  const labels = {
    audio_output: "Audio output name",
    ha_scene: "Home Assistant scene",
    ha_service: "Home Assistant entity (optional)",
    mqtt: "Optional action target",
    current_time: "Action target",
    current_date: "Action target",
    kvm_toggle: "Action target",
    none: "Action target",
    workstation_slot: "Action target",
  };
  byId("targetField").firstChild.textContent = labels[action] || "Action target";
  byId("targetField").hidden = !["audio_output", "ha_scene", "ha_service"].includes(action);
  byId("offTargetField").hidden = action !== "ha_scene";
  byId("haServiceField").hidden = action !== "ha_service";
  byId("haServiceDataField").hidden = action !== "ha_service";
  byId("mqttTopicField").hidden = action !== "mqtt";
  byId("mqttPayloadField").hidden = action !== "mqtt";
  byId("slotIdField").hidden = action !== "workstation_slot";
}

function selectKey(key) {
  selectedKey = key;
  let button = buttons.get(key) || blankButton(key);
  if (
    button.action_type === "workstation_slot"
    && String(button.slot_id) === String(key + 1)
  ) {
    button = blankButton(key);
    buttons.set(key, button);
  }
  byId("selectedKeyNumber").textContent = key + 1;
  buttonFields.enabled.checked = button.enabled;
  buttonFields.label.value = button.label;
  buttonFields.group.value = button.group;
  buttonFields.action_type.value = button.action_type;
  buttonFields.slot_id.value = button.slot_id;
  buttonFields.icon.value = button.icon;
  buttonFields.accent_color.value = rgbToHex(button.accent_color);
  buttonFields.target.value = button.target;
  buttonFields.off_target.value = button.off_target;
  buttonFields.mqtt_topic.value = button.mqtt_topic;
  buttonFields.mqtt_payload.value = button.mqtt_payload;
  buttonFields.state_topic.value = button.state_topic;
  buttonFields.state_payload.value = button.state_payload;
  buttonFields.service.value = button.service || "";
  buttonFields.service_data.value = Object.keys(button.service_data || {}).length
    ? JSON.stringify(button.service_data, null, 2)
    : "";
  configureActionFields();
  renderDeck();
}

function updateSelectedButton() {
  let serviceData = {};
  const serviceDataText = buttonFields.service_data.value.trim();
  if (serviceDataText) {
    try {
      serviceData = JSON.parse(serviceDataText);
    } catch (_error) {
      serviceData = serviceDataText;
    }
  }
  const button = {
    key: selectedKey,
    enabled: buttonFields.enabled.checked,
    label: buttonFields.label.value,
    group: buttonFields.group.value,
    action_type: buttonFields.action_type.value,
    slot_id: buttonFields.slot_id.value,
    icon: buttonFields.icon.value,
    accent_color: hexToRgb(buttonFields.accent_color.value),
    target: buttonFields.target.value,
    off_target: buttonFields.off_target.value,
    mqtt_topic: buttonFields.mqtt_topic.value,
    mqtt_payload: buttonFields.mqtt_payload.value,
    state_topic: buttonFields.state_topic.value,
    state_payload: buttonFields.state_payload.value,
    service: buttonFields.service.value,
    service_data: serviceData,
  };
  buttons.set(selectedKey, button);
  configureActionFields();
  renderDeck();
}

Object.values(buttonFields).forEach((field) => {
  field.addEventListener("input", updateSelectedButton);
  field.addEventListener("change", updateSelectedButton);
});

byId("clearButton").addEventListener("click", () => {
  buttons.set(selectedKey, blankButton(selectedKey));
  selectKey(selectedKey);
});

function populate(config) {
  if (config.controller) {
    byId("controllerName").value = config.controller.name || "";
    byId("controllerDeviceId").value = config.controller.device_id || "";
  }
  byId("serverHost").value = config.server.host;
  byId("serverPort").value = config.server.port;
  byId("mqttMode").value = config.mqtt.mode;
  byId("mqttBroker").value = config.mqtt.broker;
  byId("mqttPort").value = config.mqtt.port;
  externalMqttPort = config.mqtt.port;
  byId("mqttUsername").value = config.mqtt.username;
  byId("mqttSecretState").dataset.configured = String(config.mqtt.password_configured);
  byId("haEnabled").checked = Boolean(config.homeassistant.enabled);
  byId("haUrl").value = config.homeassistant.url;
  byId("haSecretState").dataset.configured = String(config.homeassistant.token_configured);
  byId("simulateHardware").checked = Boolean(config.hardware.simulate);
  byId("monitorController").value = config.kvm.monitor_controller;
  byId("usbController").value = config.kvm.usb_controller;
  byId("remoteKvmTimeout").value = config.kvm.remote_timeout;
  byId("usbSwitchDriver").value = config.usb_switch?.driver || "acroname";
  byId("acronameSerial").value = config.acroname.serial_number;
  byId("defaultChannel").value = config.usb_switch?.default_channel
    ?? config.acroname.default_channel;
  byId("usbSwitchGpioPin").value = config.usb_switch?.gpio_pin ?? 17;
  byId("usbSwitchActiveHigh").checked = Boolean(
    config.usb_switch?.gpio_active_high,
  );
  byId("usbSwitchPulseMs").value = config.usb_switch?.gpio_pulse_ms ?? 200;
  byId("usbObservationEnabled").checked = Boolean(
    config.usb_switch?.observation_enabled,
  );
  byId("usbSentinelVendorId").value = (
    config.usb_switch?.sentinel_vendor_id || ""
  );
  byId("usbSentinelProductId").value = (
    config.usb_switch?.sentinel_product_id || ""
  );
  byId("usbSentinelSerial").value = (
    config.usb_switch?.sentinel_serial_number || ""
  );
  byId("usbObservationPollInterval").value = (
    config.usb_switch?.observation_poll_interval ?? 2
  );
  byId("usbObservationTimeout").value = (
    config.usb_switch?.observation_timeout ?? 8
  );
  byId("displayId").value = config.monitor.display_id;
  byId("pc1Input").value = config.monitor.pc1_input;
  byId("pc2Input").value = config.monitor.pc2_input;
  byId("brightness").value = config.streamdeck.brightness;
  byId("brightnessValue").textContent = `${config.streamdeck.brightness}%`;
  byId("pendingRequestTimeout").value = (
    config.streamdeck.pending_request_timeout ?? 300
  );
  byId("pendingRetryInterval").value = (
    config.streamdeck.pending_retry_interval ?? 5
  );
  byId("workstationPc1").value = config.workstations.pc1;
  byId("workstationPc2").value = config.workstations.pc2;
  byId("hubTelemetryInterval").value = config.usb_hub.telemetry_interval;
  syncConnectionFields();
  syncUsbSwitchFields();

  buttons = new Map();
  for (let key = 0; key < 15; key += 1) {
    buttons.set(key, blankButton(key));
  }
  config.streamdeck.buttons.forEach((button) => {
    const implicitSlotId = String(button.key + 1);
    if (
      button.action_type === "workstation_slot"
      && String(button.slot_id) === implicitSlotId
    ) {
      buttons.set(button.key, blankButton(button.key));
      return;
    }
    buttons.set(button.key, { ...blankButton(button.key), ...button });
  });
  config.usb_hub.ports.forEach((port) => {
    const row = document.querySelector(`.port-row[data-port="${port.index}"]`);
    if (row) row.querySelector('[data-field="name"]').value = port.name;
  });

  selectKey(0);
  byId("connectionBadge").textContent = "Checking…";
  byId("connectionBadge").className = "badge";
}

function payload() {
  return {
    controller: {
      name: byId("controllerName").value.trim() || "Raspberry Pi Desk Controller",
      device_id: byId("controllerDeviceId").value.trim() || "rpi_desk_controller",
    },
    server: {
      host: byId("serverHost").value,
      port: Number(byId("serverPort").value),
    },
    mqtt: {
      mode: byId("mqttMode").value,
      broker: byId("mqttBroker").value,
      port: Number(byId("mqttPort").value),
      username: byId("mqttUsername").value,
      password: byId("mqttPassword").value || null,
      clear_password: byId("clearMqttPassword").checked,
    },
    homeassistant: {
      enabled: byId("haEnabled").checked,
      url: byId("haUrl").value,
      token: byId("haToken").value || null,
      clear_token: byId("clearHaToken").checked,
    },
    hardware: { simulate: byId("simulateHardware").checked },
    kvm: {
      monitor_controller: byId("monitorController").value,
      usb_controller: byId("usbController").value,
      remote_timeout: Number(byId("remoteKvmTimeout").value),
    },
    usb_switch: {
      driver: byId("usbSwitchDriver").value,
      default_channel: Number(byId("defaultChannel").value),
      gpio_pin: Number(byId("usbSwitchGpioPin").value),
      gpio_active_high: byId("usbSwitchActiveHigh").checked,
      gpio_pulse_ms: Number(byId("usbSwitchPulseMs").value),
      observation_enabled: byId("usbObservationEnabled").checked,
      sentinel_vendor_id: byId("usbSentinelVendorId").value.trim() || null,
      sentinel_product_id: byId("usbSentinelProductId").value.trim() || null,
      sentinel_serial_number: byId("usbSentinelSerial").value.trim() || null,
      observation_poll_interval: Number(
        byId("usbObservationPollInterval").value,
      ),
      observation_timeout: Number(byId("usbObservationTimeout").value),
    },
    acroname: {
      serial_number: byId("acronameSerial").value || null,
      default_channel: Number(byId("defaultChannel").value),
    },
    monitor: {
      display_id: Number(byId("displayId").value),
      pc1_input: byId("pc1Input").value,
      pc2_input: byId("pc2Input").value,
    },
    streamdeck: {
      brightness: Number(byId("brightness").value),
      pending_request_timeout: Number(byId("pendingRequestTimeout").value),
      pending_retry_interval: Number(byId("pendingRetryInterval").value),
      buttons: [...buttons.values()].filter((button) => button.enabled),
    },
    usb_hub: {
      telemetry_interval: Number(byId("hubTelemetryInterval").value),
      ports: [...document.querySelectorAll(".port-row")].map((row) => ({
        index: Number(row.dataset.port),
        name: row.querySelector('[data-field="name"]').value,
      })),
    },
    workstations: {
      pc1: byId("workstationPc1").value,
      pc2: byId("workstationPc2").value,
    },
  };
}

function syncConnectionFields() {
  const localBroker = byId("mqttMode").value === "local";
  byId("mqttBroker").disabled = localBroker;
  byId("mqttPort").disabled = localBroker;
  byId("mqttUsername").disabled = localBroker;
  byId("mqttPassword").disabled = localBroker;
  byId("clearMqttPassword").disabled = localBroker;
  byId("localBrokerNote").hidden = !localBroker;
  byId("mqttSecretState").textContent = localBroker
    ? "Pi-hosted"
    : (byId("mqttSecretState").dataset.configured === "true"
      ? "Password saved"
      : "No password saved");

  const homeAssistantEnabled = byId("haEnabled").checked;
  byId("haUrl").disabled = !homeAssistantEnabled;
  byId("haUrl").required = homeAssistantEnabled;
  byId("haToken").disabled = !homeAssistantEnabled;
  byId("clearHaToken").disabled = !homeAssistantEnabled;
  byId("haSecretState").textContent = homeAssistantEnabled
    ? (byId("haSecretState").dataset.configured === "true"
      ? "A Home Assistant token is saved."
      : "No Home Assistant token is saved.")
    : "Home Assistant discovery, scene actions, and state polling are disabled.";
}

function syncUsbSwitchFields() {
  const isUgreen = byId("usbSwitchDriver").value === "ugreen_cm691_gpio";
  byId("acronameSerialField").hidden = isUgreen;
  document.querySelectorAll(".ugreen-setting").forEach((field) => {
    field.hidden = !isUgreen;
  });
  byId("ugreenWiringNote").hidden = !isUgreen;
  const observationEnabled = (
    isUgreen && byId("usbObservationEnabled").checked
  );
  document.querySelectorAll(".sentinel-setting").forEach((field) => {
    field.hidden = !observationEnabled;
  });
  byId("sentinelNote").hidden = !observationEnabled;
  byId("usbSentinelVendorId").required = observationEnabled;
  byId("usbSentinelProductId").required = observationEnabled;
}

async function save(restart) {
  const actionButtons = [...document.querySelectorAll(".actions button")];
  actionButtons.forEach((button) => { button.disabled = true; });
  setMessage("Saving configuration…");
  try {
    const response = await fetch("/api/v1/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload()),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail?.[0]?.msg || result.detail || "Save failed");

    if (restart) {
      setMessage("Saved. Restarting the controller…", "success");
      const restartResponse = await fetch("/api/v1/config/restart", { method: "POST" });
      if (!restartResponse.ok) throw new Error("Saved, but restart could not be requested");
    } else {
      setMessage("Saved. Restart the controller when ready.", "success");
    }
  } catch (error) {
    setMessage(error.message, "error");
  } finally {
    actionButtons.forEach((button) => { button.disabled = false; });
  }
}

byId("configForm").addEventListener("submit", (event) => {
  event.preventDefault();
  save(false);
});
byId("mqttMode").addEventListener("change", () => {
  if (byId("mqttMode").value === "local") {
    externalMqttPort = Number(byId("mqttPort").value) || 1883;
    byId("mqttPort").value = "1883";
  } else {
    byId("mqttPort").value = String(externalMqttPort);
  }
  syncConnectionFields();
});
byId("haEnabled").addEventListener("change", syncConnectionFields);
byId("usbSwitchDriver").addEventListener("change", syncUsbSwitchFields);
byId("usbObservationEnabled").addEventListener(
  "change",
  syncUsbSwitchFields,
);
byId("saveRestart").addEventListener("click", () => save(true));
byId("refreshHub").addEventListener("click", loadHub);
byId("hubName").addEventListener("change", async (event) => {
  try {
    await sendHubCommand({ name: event.target.value });
    setHubMessage("Hub name updated.", "success");
  } catch (error) {
    setHubMessage(error.message, "error");
    await loadHub();
  }
});
byId("hubActiveUpstream").addEventListener("change", async (event) => {
  try {
    await sendHubCommand({ active_upstream: Number(event.target.value) });
    setHubMessage(`KVM switched to PC ${Number(event.target.value) + 1}.`, "success");
  } catch (error) {
    setHubMessage(error.message, "error");
    await loadHub();
  }
});
byId("syncUsbHost").addEventListener("click", async () => {
  try {
    await sendHubCommand({ active_upstream: desiredUsbHost });
    setHubMessage(
      `USB synchronized to PC ${desiredUsbHost + 1}.`,
      "success",
    );
  } catch (error) {
    setHubMessage(error.message, "error");
    await loadHub();
  }
});
byId("usbPorts").addEventListener("change", (event) => {
  const field = event.target.dataset.control;
  if (!field) return;
  const port = Number(event.target.closest(".port-row").dataset.port);
  let value;
  if (event.target.type === "checkbox") {
    value = event.target.checked;
  } else {
    value = Number(event.target.value);
  }
  sendPortCommand(port, { [field]: value });
});
byId("usbPorts").addEventListener("click", (event) => {
  const action = event.target.dataset.action;
  if (!action) return;
  const port = Number(event.target.closest(".port-row").dataset.port);
  sendPortCommand(port, { action });
});

let latestReleaseTag = null;

async function checkSystemVersion() {
  const card = byId("systemVersionCard");
  const currentVer = byId("currentVersionText");
  const detailText = byId("versionDetailText");
  const badge = byId("versionStatusBadge");
  const notesBlock = byId("releaseNotesBlock");
  const notesText = byId("releaseNotesText");
  const applyBtn = byId("applyUpdateBtn");

  if (!card) return;

  try {
    card.className = "connection-health checking";
    badge.textContent = "Checking GitHub…";
    detailText.textContent = "Checking GitHub Releases for updates…";

    const response = await fetch("/api/v1/system/version", { cache: "no-store" });
    if (!response.ok) throw new Error("Could not check version");
    const data = await response.json();

    currentVer.textContent = `Desk Controller ${data.current_version}`;
    latestReleaseTag = data.latest_version;

    if (data.update_available) {
      card.className = "connection-health offline";
      badge.className = "status error";
      badge.textContent = `Update available (${data.latest_version})`;
      detailText.textContent = `A newer release ${data.latest_version} is available on GitHub.`;
      if (data.release_notes) {
        notesBlock.style.display = "block";
        notesText.textContent = data.release_notes;
      }
      applyBtn.style.display = "inline-block";
      applyBtn.textContent = `Update to ${data.latest_version}`;
    } else {
      card.className = "connection-health online";
      badge.className = "status success";
      badge.textContent = "Up to date";
      detailText.textContent = `You are running the latest version (${data.current_version}).`;
      notesBlock.style.display = "none";
      applyBtn.style.display = "none";
    }
  } catch (error) {
    card.className = "connection-health offline";
    badge.className = "status";
    badge.textContent = "Check failed";
    detailText.textContent = `Could not reach GitHub: ${error.message}`;
  }
}

async function applySystemUpdate() {
  const btn = byId("applyUpdateBtn");
  const msg = byId("updateMessage");
  btn.disabled = true;
  msg.style.color = "var(--cyan)";
  msg.textContent = `Fetching and applying ${latestReleaseTag || "latest release"}… Please wait.`;

  try {
    const response = await fetch("/api/v1/system/update", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target_tag: latestReleaseTag }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Update failed");

    msg.style.color = "var(--green)";
    msg.textContent = `✓ ${data.message}. The controller service is restarting now. Reconnecting…`;

    setTimeout(() => {
      const pollInterval = setInterval(async () => {
        try {
          const res = await fetch("/api/v1/config", { cache: "no-store" });
          if (res.ok) {
            clearInterval(pollInterval);
            window.location.reload();
          }
        } catch {}
      }, 2000);
    }, 2000);
  } catch (error) {
    btn.disabled = false;
    msg.style.color = "var(--danger)";
    msg.textContent = `✗ ${error.message}`;
  }
}

async function restartSystemService() {
  const btn = byId("restartServiceBtn");
  const msg = byId("updateMessage");
  if (!confirm("Are you sure you want to restart the Desk Controller service?")) return;

  btn.disabled = true;
  msg.style.color = "var(--cyan)";
  msg.textContent = "Restarting service…";

  try {
    const response = await fetch("/api/v1/system/restart", { method: "POST" });
    if (!response.ok) throw new Error("Restart failed");
    msg.style.color = "var(--green)";
    msg.textContent = "Service is restarting… Reconnecting…";

    setTimeout(() => {
      const pollInterval = setInterval(async () => {
        try {
          const res = await fetch("/api/v1/config", { cache: "no-store" });
          if (res.ok) {
            clearInterval(pollInterval);
            window.location.reload();
          }
        } catch {}
      }, 2000);
    }, 2000);
  } catch (error) {
    btn.disabled = false;
    msg.style.color = "var(--danger)";
    msg.textContent = `✗ ${error.message}`;
  }
}

byId("checkUpdatesBtn")?.addEventListener("click", checkSystemVersion);
byId("applyUpdateBtn")?.addEventListener("click", applySystemUpdate);
byId("restartServiceBtn")?.addEventListener("click", restartSystemService);

fetch("/api/v1/config", { cache: "no-store" })
  .then(async (response) => {
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not load configuration");
    populate(data);
    loadHub();
    loadConnectionStatus();
    checkSystemVersion();
  })
  .catch((error) => {
    byId("connectionBadge").textContent = "Unavailable";
    setMessage(error.message, "error");
  });

window.setInterval(loadHub, 5000);
window.setInterval(loadConnectionStatus, 15000);
