"use strict";
const form = document.querySelector("#chat-form");
const input = document.querySelector("#message");
const messages = document.querySelector("#messages");
const welcome = document.querySelector("#welcome");
const viewport = document.querySelector("#conversation");
const context = document.querySelector("#context");
const newChat = document.querySelector("#new-chat");
const keyInput = document.querySelector("#api-key");
const storageKey = "allianz-local-chat-v1";
let state = {session_id: null, product: null, messages: []};
let busy = false;
try { const saved = JSON.parse(sessionStorage.getItem(storageKey)); if (saved && Array.isArray(saved.messages)) state = saved; } catch (_) {}

function save() { try { sessionStorage.setItem(storageKey, JSON.stringify(state)); } catch (_) {} }
function headers() { const value = {"Content-Type": "application/json"}; if (keyInput.value) value["X-API-Key"] = keyInput.value; return value; }
function scrollBottom() { viewport.scrollTop = viewport.scrollHeight; }
function updateContext() { context.textContent = state.product ? `${state.product[0].toUpperCase()}${state.product.slice(1)} insurance · You can change topic anytime` : "Car · Home · Business"; }
function safeLink(url) { try { const parsed = new URL(url); return parsed.protocol === "https:" && parsed.hostname === "www.allianz.com.au" ? parsed.href : null; } catch (_) { return null; } }
function link(parent, url, label) { const href = safeLink(url); if (!href) return; const a = document.createElement("a"); a.href = href; a.textContent = label; a.className = "source-link"; a.target = "_blank"; a.rel = "noopener noreferrer"; parent.append(a); }
function render(turn, pending = false) {
  welcome.hidden = true;
  const row = document.createElement("article"); row.className = `turn ${turn.role}${turn.error ? " error" : ""}${pending ? " thinking" : ""}`;
  const avatar = document.createElement("div"); avatar.className = "avatar"; avatar.textContent = turn.role === "user" ? "You" : "a"; avatar.setAttribute("aria-hidden", "true");
  const bubble = document.createElement("div"); bubble.className = "bubble";
  const label = document.createElement("div"); label.className = "turn-label"; label.textContent = turn.role === "user" ? "You" : "Knowledge assistant";
  const text = document.createElement("div"); text.className = "text"; text.textContent = turn.text;
  bubble.append(label, text); row.append(avatar, bubble); messages.append(row);
  if (turn.role === "assistant" && !pending && !turn.error) {
    const sources = document.createElement("div"); sources.className = "sources";
    const seen = new Set();
    for (const source of turn.sources || []) { const href = safeLink(source.url); if (href && !seen.has(href)) { seen.add(href); link(sources, href, `Source ↗ ${new URL(href).pathname.split("/").pop()}`); } }
    for (const url of turn.sources_checked || []) { if (!seen.has(url)) { seen.add(url); link(sources, url, "Page checked ↗"); } }
    if (!seen.size) link(sources, turn.discovery_url || "https://www.allianz.com.au/sitemap.xml", "Allianz website ↗");
    bubble.append(sources);
    const labels = turn.choices?.length ? turn.choices : (turn.status === "clarification_required" && !turn.product ? ["Car insurance", "Home insurance", "Business insurance"] : []);
    if (labels.length) {
      const choices = document.createElement("div"); choices.className = "choices";
      for (const label of labels) { const b = document.createElement("button"); b.type = "button"; b.textContent = label; b.addEventListener("click", () => send(label)); choices.append(b); }
      bubble.append(choices);
    }
  }
  scrollBottom(); return {row, text};
}
function append(turn) { state.messages.push(turn); if (state.messages.length > 100) state.messages = state.messages.slice(-100); render(turn); save(); }
function setBusy(value) { busy = value; document.querySelector("#send").disabled = value; newChat.disabled = value; input.disabled = value; document.querySelectorAll(".choices button,.starters button").forEach(b => b.disabled = value); }
async function send(message) {
  message = message.trim(); if (busy || !message) return;
  append({role: "user", text: message}); input.value = ""; input.style.height = "auto"; setBusy(true);
  const pending = render({role: "assistant", text: "Checking the website knowledge…"}, true);
  const timer = setTimeout(() => { pending.text.textContent = "Still checking… broader questions can take a few minutes with the local model. Your conversation is retained."; }, 12000);
  try {
    const response = await fetch("/chat", {method: "POST", headers: headers(), body: JSON.stringify({message, session_id: state.session_id})});
    const result = await response.json();
    if (!response.ok) {
      if (response.status === 401) document.querySelector("#connection").open = true;
      if (response.status === 410) { state.session_id = null; state.product = null; save(); updateContext(); }
      throw new Error(typeof result.detail === "string" ? result.detail : "Could not process that message. Please try again.");
    }
    state.session_id = result.session_id; state.product = result.product; pending.row.remove();
    append({role: "assistant", text: result.answer, status: result.status, product: result.product, sources: result.sources, sources_checked: result.sources_checked, discovery_url: result.discovery_url, choices: result.choices}); updateContext();
  } catch (error) { pending.row.remove(); append({role: "assistant", text: error.message || "Connection lost. Check that the Python server is running, then resend your question.", error: true}); }
  finally { clearTimeout(timer); setBusy(false); input.focus(); }
}
form.addEventListener("submit", event => { event.preventDefault(); send(input.value); });
input.addEventListener("keydown", event => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); send(input.value); } });
input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, 150)}px`; });
document.querySelectorAll("[data-prompt]").forEach(button => button.addEventListener("click", () => send(button.dataset.prompt)));
newChat.addEventListener("click", async () => {
  if (busy) return;
  if (state.session_id) { try { const response = await fetch(`/chat/${encodeURIComponent(state.session_id)}`, {method: "DELETE", headers: headers()}); if (!response.ok) throw new Error("Could not clear the conversation. Check your connection or API key."); } catch (error) { append({role: "assistant", text: error.message, error: true}); return; } }
  state = {session_id: null, product: null, messages: []}; save(); messages.replaceChildren(); welcome.hidden = false; updateContext(); input.value = ""; input.focus();
});
for (const turn of state.messages) render(turn);
updateContext(); input.focus();
