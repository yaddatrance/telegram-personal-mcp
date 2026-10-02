"use strict";
const initialKey = location.hash.slice(1);
if (initialKey) { sessionStorage.setItem("ken-owner-key", initialKey); history.replaceState(null, "", "/"); }
const key = sessionStorage.getItem("ken-owner-key") || "";
const $ = (id) => document.getElementById(id);
function node(tag, text, cls) { const n = document.createElement(tag); n.textContent = text; if (cls) n.className = cls; return n; }
async function api(path, body) {
  const headers = {"X-Owner-Key": key};
  const options = {headers};
  if (body) { options.method = "POST"; headers["Content-Type"] = "application/json"; options.body = JSON.stringify(body); }
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw Error(data.error || "The request could not be completed.");
  return data;
}
function notice(text, error=false) { $("notice").textContent=text; $("notice").className=error?"error":""; }
async function refresh() {
  try {
    const state = await api("/api/state");
    $("mode").textContent = state.mode === "demo" ? "DEMO · NO LIVE MESSAGES" : "LIVE · OWNER APPROVAL";
    $("account-id").textContent = "Account ID " + (state.account_id || "not connected");
    $("count").textContent = state.requests.filter(r => r.status === "prepared").length;
    $("requests").replaceChildren();
    if (!state.requests.length) $("requests").append(node("p", "Your outbox is clear. Ask your assistant to prepare a message using MCP.", "empty"));
    for (const item of state.requests) render(item);
  } catch (error) { notice(error.message, true); }
}
function render(item) {
  const card=node("article", "", "card");
  const top=node("div", "", "card-top");
  const recipient=node("div", "", "recipient");
  recipient.append(node("div", item.peer_name.slice(0,1), "mini-avatar"));
  const who=node("div", ""); who.append(node("div", item.peer_name, "person"), node("div", "PEER " + item.peer_id + (item.reply_to_message_id ? " · REPLY TO #" + item.reply_to_message_id : " · DIRECT MESSAGE"), "meta"));
  recipient.append(who); top.append(recipient, node("span", item.locally_approved ? "Approved locally" : item.status, "status"));
  card.append(top, node("div", item.text, "message"), node("p", "EXACT PREVIEW · " + item.preview_sha256, "hash"));
  const actions=node("div", "", "actions");
  actions.append(node("p", item.detail || "Approval is valid for 5 minutes. Your assistant still needs an explicit send instruction.", ""));
  if (item.status === "prepared") {
    const buttons=node("div", "");
    const cancel=node("button", "Cancel draft", "cancel");
    const approve=node("button", item.locally_approved ? "Approved for 5 minutes" : "Approve this exact message →", "primary");
    approve.disabled=item.locally_approved;
    cancel.onclick=()=>action("/api/cancel", item, cancel);
    approve.onclick=()=>action("/api/approve", item, approve);
    buttons.append(cancel, approve); actions.append(buttons);
  }
  card.append(actions); $("requests").append(card);
}
async function action(path, item, button) {
  button.disabled=true;
  try {
    await api(path, {draft_id:item.draft_id, preview_sha256:item.preview_sha256, confirmation:"APPROVE"});
    notice(path.endsWith("approve") ? "Approved locally. Tell your assistant to send this exact prepared message. No message was sent by this window." : "Draft cancelled. Nothing was sent.");
    await refresh();
  } catch(error) { notice(error.message, true); button.disabled=false; }
}
$("refresh").onclick=refresh;
refresh();
