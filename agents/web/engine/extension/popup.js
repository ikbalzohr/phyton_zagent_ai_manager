const $ = id => document.getElementById(id);
let state = {}, busy = false;

function profileLabel(profile) {
  if (!profile) return "No trusted Chrome profile";
  const account = profile.userName || profile.boundEmail || "";
  return [profile.name, profile.directory ? `(${profile.directory})` : "", account].filter(Boolean).join(" - ");
}

function render() {
  const unlimited = state.lease?.expiresAt === null;
  const remaining = unlimited ? 0 : Math.max(0, Math.ceil(((state.lease?.expiresAt || 0) - Date.now()) / 1000));
  const active = state.authorized && (unlimited || remaining > 0);

  $("connection").textContent = state.connected ? "Connected to Zagent Web" : "Not connected";
  $("native").textContent = state.nativeAttached === false ? "Native Chrome is not attached" : "Automation engine: native CDP";
  $("profile").textContent = state.profileMismatch
    ? `Profile mismatch - Trusted: ${profileLabel(state.boundProfile)}`
    : state.profileBinding
      ? `Trusted profile: ${profileLabel(state.profileBinding)}`
      : state.boundProfile
        ? `Trusted profile: ${profileLabel(state.boundProfile)}`
        : state.localProfile?.email
          ? `This Chrome profile: ${state.localProfile.email}`
          : "This Chrome profile: account identity unavailable";

  $("dot").style.background = state.connected ? "#75e3c5" : state.pendingPair ? "#d9a514" : "#8796ac";
  $("lease").textContent = active
    ? unlimited
      ? `${state.lease.scope} - ${state.lease.targets.length} tab(s) - no time limit`
      : `${state.lease.scope} - ${state.lease.targets.length} tab(s) - ${Math.floor(remaining / 60)}m ${remaining % 60}s remaining`
    : "Access locked - No active authorization";
  $("error").textContent = [state.error, state.warning].filter(Boolean).join(" ");

  const pendingPair = state.pendingPair;
  $("pair-request-text").textContent = pendingPair
    ? `Pair only this Chrome profile as ${profileLabel(pendingPair.profile)}.`
    : state.pendingPairForDifferentProfile
      ? "A pairing request exists, but it targets a different Chrome profile."
      : state.connected
        ? `Already paired to ${profileLabel(state.profileBinding)}.`
        : state.profileMismatch
          ? `This profile cannot use the trusted pairing for ${profileLabel(state.boundProfile)}.`
          : "No pairing request from ChatGPT.";
  $("pair-request").disabled = busy || !pendingPair || state.profileMismatch;

  const pending = state.authorizationPending;
  $("auth-request-text").textContent = pending
    ? `Requested: ${pending.scope}, ${pending.seconds === 0 ? "no time limit" : `${Math.round(pending.seconds / 60)} minutes`}, navigation ${pending.allowNavigation ? "allowed" : "locked"}, new tabs ${pending.allowNewTabs ? "allowed" : "locked"}.`
    : "No automation request.";
  $("approve-request").disabled = busy || !state.connected || !pending;
  $("grant").disabled = busy || !state.connected;
}

async function send(message) {
  if (busy) return;
  busy = true;
  document.querySelectorAll("button").forEach(b => b.disabled = true);
  try { state = await chrome.runtime.sendMessage(message); }
  catch (e) { state = { error: e.message }; }
  finally {
    busy = false;
    document.querySelectorAll("button").forEach(b => b.disabled = false);
    render();
  }
}

async function refreshSilently() {
  if (busy) return;
  try {
    const next = await chrome.runtime.sendMessage({ action: "status" });
    if (next) {
      state = next;
      render();
    }
  } catch (e) {
    state = { ...state, error: e.message };
    render();
  }
}

$("pair-request").addEventListener("click", () => void send({
  action: "pair-request",
  requestId: state.pendingPair?.id
}));
$("approve-request").addEventListener("click", () => {
  const pending = state.authorizationPending;
  void send({
    action: "approve-request",
    requestId: pending?.id,
    scope: pending?.scope,
    seconds: pending?.seconds,
    allowNavigation: Boolean(pending?.allowNavigation),
    allowNewTabs: Boolean(pending?.allowNewTabs)
  });
});
$("pair-form").addEventListener("submit", event => {
  event.preventDefault();
  const code = $("code").value.trim();
  $("code").value = "";
  if (code) void send({ action: "pair", code });
});
$("grant-form").addEventListener("submit", event => {
  event.preventDefault();
  void send({
    action: "grant",
    scope: $("scope").value,
    seconds: Number($("duration").value),
    allowNavigation: $("allow-navigation").checked,
    allowNewTabs: $("allow-new-tabs").checked
  });
});
for (const action of ["revoke", "disconnect"]) {
  $(action).addEventListener("click", () => void send({ action }));
}
$("refresh").addEventListener("click", () => void send({ action: "status" }));
setInterval(() => void refreshSilently(), 1500);
void send({ action: "status" });
