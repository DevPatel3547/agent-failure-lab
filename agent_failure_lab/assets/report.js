"use strict";
(() => {
  const data = JSON.parse(document.getElementById("report-data").textContent);
  const byId = id => document.getElementById(id);
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = String(text);
    if (className) element.className = className;
    return element;
  };
  const meta = data.experiment;
  const results = data.results;
  const scripted = meta.provider === "scripted";
  const replay = meta.provider === "replay";
  const labels = {plain: "Plain", guided: "Guided", guarded: "Guarded"};
  const hints = scripted ? {plain: "Scripted retry", guided: "Scripted read-back", guarded: "Retry + recovery layer"} : {plain: "Base instructions", guided: "Recovery instructions", guarded: "Base + recovery layer"};
  byId("experiment-id").textContent = meta.id;
  for (const [label, value] of [["Provider", meta.provider], ["Model", meta.model || (replay ? "No model · action replay" : "No model · scripted")], ["Environment", "Simulated ticket service"], ["Executed", `${results.length} / ${meta.scheduled_episodes}`], ["Created", new Date(meta.created).toLocaleString()]]) {
    byId("metadata").append(node("dt", label), node("dd", value));
  }
  const notice = byId("notice");
  notice.append(node("strong", scripted ? "Scripted reference experiment. " : replay ? "Recorded action replay. " : "Live model / simulated environment. "));
  notice.append(document.createTextNode(replay ? "Previously recorded actions ran against a fresh simulated service. This is a reproducibility check, not a new model evaluation." : scripted ? "No AI models were evaluated. These hand-authored cases demonstrate failure mechanisms; they do not measure real-world model failure rates." : "Model decisions came from the named API. Tool effects occurred in a controlled simulator. Results describe this harness and these fixtures, not general model safety."));
  if (results.length < meta.scheduled_episodes) notice.append(node("strong", ` Incomplete run: ${meta.scheduled_episodes - results.length} episodes did not start.`));
  for (const mode of meta.modes || []) {
    const summary = data.summary.find(item => item.mode === mode) || {episodes:0,task_success:0,duplicates:0,unknown:0,missing:0,errors:0,limits:0};
    const card = node("article", undefined, "summary-card");
    const top = node("div", undefined, "summary-top");
    top.append(node("span", labels[mode] || mode), node("span", hints[mode] || "", "strategy-hint"));
    const number = node("div", summary.task_success, "big-number");
    number.append(node("small", `/ ${summary.episodes}`));
    const small = node("div", undefined, "mini-metrics");
    for (const [label, value] of [["Duplicates", summary.duplicates], ["Unknown", summary.unknown], ["Unfinished", summary.missing], ["Errors / limits", `${summary.errors} / ${summary.limits}`]]) {
      const item = node("span"); item.append(node("b", value), document.createTextNode(label)); small.append(item);
    }
    card.append(top, number, node("p", "correctly reported task completions"), small); byId("summaries").append(card);
  }
  const verdict = row => {
    const m = row.metrics || {};
    if (row.execution_status === "error" || row.execution_status === "interrupted") return ["error", "Execution error"];
    if (m.duplicates) return ["issue", "Duplicate effects"];
    if (m.false_success) return ["issue", "False success"];
    if (m.wrong_payload) return ["issue", "Wrong payload"];
    if (m.false_failure) return ["issue", "False failure"];
    if (m.task_success) return ["pass", "Confirmed correctly"];
    if (m.unknown) return ["unknown", m.missing ? "Unknown · unfinished" : "Outcome unknown"];
    return ["unknown", "Not completed"];
  };
  let selected = results.find(row => row.scenario_id === "lost-ack" && row.mode === "plain") || results[0];
  function showDetail(row) {
    selected = row;
    const panel = byId("detail"); panel.replaceChildren();
    if (!row) { panel.append(node("p", "No cases match these filters.", "empty")); return; }
    const m = row.metrics || {}, decision = row.decision || {};
    const [kind, label] = verdict(row);
    const top = node("div", undefined, "detail-top");
    top.append(node("h3", row.scenario_name || row.scenario_id), node("span", label, `badge ${kind}`));
    panel.append(top, node("p", row.description || "Interrupted run; inspect the local journal.", "detail-description"));
    const facts = node("div", undefined, "effect-grid");
    for (const [label, value] of [["Tickets committed", m.effects ?? "—"], ["Create calls", m.create_calls ?? "—"], ["Agent reported", decision.status || "unknown"]]) {
      const box = node("div", label, "effect-box"); box.append(node("strong", value)); facts.append(box);
    }
    const claim = node("div", undefined, "claim"); claim.append(node("span", `${labels[row.mode] || row.mode} · trial ${(row.trial || 0) + 1}`, "claim-label"), node("p", decision.reason || "No final report"));
    panel.append(facts, claim);
    const head = node("div", undefined, "trace-header"); head.append(node("h4", "Execution trace"));
    const toggle = node("label", undefined, "toggle"), checkbox = node("input"); checkbox.type = "checkbox";
    checkbox.id = "show-world"; toggle.append(checkbox, document.createTextNode("Reveal world + grader")); head.append(toggle);
    const scrubber = node("div", undefined, "scrubber"), slider = node("input"), position = node("span");
    slider.type = "range"; slider.min = "0"; slider.setAttribute("aria-label", "Trace event position");
    scrubber.append(slider, position);
    const timeline = node("ol", undefined, "timeline");
    const note = node("p", "This tool trace includes gateway calls. The exact agent history below shows which observations reached the caller. Reveal world events to inspect committed effects.", "trace-note");
    function timelineData() { return (row.trace || []).filter(event => checkbox.checked || !["world", "grader"].includes(event.scope)); }
    function paintTrace(reset) {
      const events = timelineData(); slider.max = String(events.length); if (reset) slider.value = slider.max;
      const count = Number(slider.value); position.textContent = `${count} / ${events.length}`; timeline.replaceChildren();
      for (const event of events.slice(0, count)) {
        const item = node("li"), tick = node("div", `t${event.tick}`, "trace-tick"), body = node("div", undefined, `trace-event ${event.scope}`);
        body.append(node("span", event.event.replaceAll("_", " "), "trace-name"), node("span", event.scope, "trace-scope"));
        body.append(node("pre", JSON.stringify(event.details || {}, null, 2))); item.append(tick, body); timeline.append(item);
      }
    }
    checkbox.addEventListener("change", () => paintTrace(true)); slider.addEventListener("input", () => paintTrace(false));
    panel.append(head, scrubber, timeline, note);
    const extra = node("details", undefined, "details-extra"); extra.append(node("summary", "Run details & reproducibility"), node("pre", JSON.stringify({episode_id: row.episode_id, scenario_hash: row.scenario_hash, mode: row.mode, execution_status: row.execution_status, steps: row.steps, protocol_errors: row.protocol_errors, usage: row.usage, metrics: row.metrics}, null, 2))); panel.append(extra);
    const history = node("details", undefined, "details-extra");
    history.append(node("summary", "Exact observations supplied to the agent"), node("pre", JSON.stringify(row.visible_history || [], null, 2)));
    panel.append(history);
    paintTrace(true);
  }
  function renderList() {
    const query = byId("search").value.toLowerCase(), mode = byId("mode").value, outcome = byId("outcome").value;
    const matches = results.filter(row => (!mode || row.mode === mode) && (!query || `${row.scenario_id} ${row.scenario_name} ${row.description}`.toLowerCase().includes(query)) && (!outcome || verdict(row)[0] === outcome || (outcome === "issue" && verdict(row)[0] === "error"))).sort((a,b) => `${a.scenario_id}:${a.mode}:${a.trial}`.localeCompare(`${b.scenario_id}:${b.mode}:${b.trial}`));
    byId("count").textContent = `${matches.length} of ${results.length} runs`;
    if (!matches.includes(selected)) selected = matches[0];
    const list = byId("case-list"); list.replaceChildren();
    for (const row of matches) {
      const button = node("button", undefined, "case-button"); button.type = "button"; button.setAttribute("aria-pressed", String(row === selected));
      button.append(node("span", row.scenario_name || row.scenario_id, "case-title"));
      const bottom = node("span", undefined, "case-bottom"), [kind,label] = verdict(row);
      bottom.append(node("span", `${labels[row.mode] || row.mode} · trial ${(row.trial || 0) + 1}`, "case-mode"), node("span", label, `badge ${kind}`)); button.append(bottom);
      button.addEventListener("click", () => { selected = row; for (const sibling of list.children) sibling.setAttribute("aria-pressed", "false"); button.setAttribute("aria-pressed", "true"); showDetail(row); }); list.append(button);
    }
    if (!matches.length) list.append(node("p", "No matching cases.", "empty"));
    showDetail(selected);
  }
  for (const id of ["search", "mode", "outcome"]) byId(id).addEventListener("input", renderList);
  byId("download").addEventListener("click", () => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type:"application/json"}));
    const link = node("a"); link.href = url; link.download = `agent-failure-lab-${meta.id}.json`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  byId("provenance").textContent = `v${meta.version} · fixture ${meta.fixture_hash} · schedule seed ${meta.schedule_seed} · ${meta.max_steps} steps/episode`;
  renderList();
})();
