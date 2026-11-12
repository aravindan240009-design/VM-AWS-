(() => {
  const state = {
    conversations: [],
    activeId: null,
    activeMode: "harness",
    streaming: false,
    abortController: null,
    settings: null,
  };

  const $ = (id) => document.getElementById(id);

  function escapeHtml(text) {
    return String(text ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function renderMarkdown(raw) {
    const codeBlocks = [];
    let text = String(raw ?? "").replace(/```([\w-]*)\n([\s\S]*?)```/g, (_, lang, code) => {
      const idx = codeBlocks.length;
      codeBlocks.push({ lang: lang || "text", code });
      return `@@CODEBLOCK_${idx}@@`;
    });

    text = escapeHtml(text);
    text = text.replace(/^### (.*)$/gm, "<h3>$1</h3>");
    text = text.replace(/^## (.*)$/gm, "<h2>$1</h2>");
    text = text.replace(/^# (.*)$/gm, "<h1>$1</h1>");
    text = text.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    text = text.replace(/\*(.+?)\*/g, "<em>$1</em>");
    text = text.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    text = text.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

    // Simple unordered lists and paragraphs
    const lines = text.split("\n");
    const html = [];
    let inList = false;
    for (const line of lines) {
      if (/^[-*] /.test(line)) {
        if (!inList) { html.push("<ul>"); inList = true; }
        html.push(`<li>${line.slice(2)}</li>`);
      } else {
        if (inList) { html.push("</ul>"); inList = false; }
        if (/^<h[1-3]>/.test(line)) {
          html.push(line);
        } else if (line.trim() === "") {
          html.push("");
        } else {
          html.push(`<p>${line}</p>`);
        }
      }
    }
    if (inList) html.push("</ul>");

    let out = html.join("\n");
    out = out.replace(/@@CODEBLOCK_(\d+)@@/g, (_, idxStr) => {
      const blk = codeBlocks[Number(idxStr)];
      return (
        `<div class="code-block">` +
        `<div class="code-head"><span>${escapeHtml(blk.lang)}</span>` +
        `<button class="code-copy" type="button" data-copy="${encodeURIComponent(blk.code)}">Copy</button></div>` +
        `<pre><code>${escapeHtml(blk.code)}</code></pre></div>`
      );
    });
    return out;
  }

  async function api(path, options = {}) {
    const resp = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`;
      try {
        const err = await resp.json();
        detail = err.detail || detail;
      } catch {}
      throw new Error(detail);
    }
    return resp.json();
  }

  async function refreshHealth() {
    try {
      const data = await api("/api/health");
      const dot = $("brainDot");
      const label = $("brainStatusText");
      $("activeModelLabel").textContent = data.active_model || "unconfigured";
      if (data.brain_connected) {
        dot.className = "status-dot online";
        label.textContent = "AWS Brain: Connected";
      } else {
        dot.className = "status-dot offline";
        label.textContent = "AWS Brain: Offline";
      }
      return data;
    } catch {
      $("brainDot").className = "status-dot offline";
      $("brainStatusText").textContent = "Server error";
      return null;
    }
  }

  async function refreshSettings() {
    const data = await api("/api/settings");
    state.settings = data;
    $("activeModelLabel").textContent = data.llm_model;
    $("toolsSummary").textContent =
      state.activeMode === "harness"
        ? `${data.available_tools.length} harness tools active`
        : "Fast direct chat (no tools)";
    return data;
  }

  async function refreshMemoriesCount() {
    const items = await api("/api/memories");
    $("memoryCountBadge").textContent = String(items.length);
    return items;
  }

  async function loadConversations() {
    state.conversations = await api("/api/conversations");
    renderConversationList();
    if (!state.activeId && state.conversations.length > 0) {
      await selectConversation(state.conversations[0].id);
    }
  }

  function renderConversationList() {
    const filter = $("chatSearchInput").value.trim().toLowerCase();
    const list = $("conversationList");
    list.innerHTML = "";
    const filtered = state.conversations.filter((c) =>
      c.title.toLowerCase().includes(filter)
    );
    for (const conv of filtered) {
      const row = document.createElement("div");
      row.className = `conv-item${conv.id === state.activeId ? " active" : ""}`;
      row.innerHTML = `<span class="conv-title">${escapeHtml(conv.title)}</span><button class="conv-del" type="button" title="Delete">✕</button>`;
      row.addEventListener("click", (e) => {
        if (e.target.classList.contains("conv-del")) return;
        selectConversation(conv.id);
        $("sidebar").classList.remove("open");
      });
      row.querySelector(".conv-del").addEventListener("click", async (e) => {
        e.stopPropagation();
        await api(`/api/conversations/${conv.id}`, { method: "DELETE" });
        if (state.activeId === conv.id) {
          state.activeId = null;
          $("messagesList").innerHTML = "";
          $("emptyState").classList.remove("hidden");
        }
        await loadConversations();
      });
      list.appendChild(row);
    }
  }

  async function selectConversation(convId) {
    state.activeId = convId;
    const data = await api(`/api/conversations/${convId}`);
    setModeUI(data.conversation.mode || "harness");
    $("convSystemPrompt").value = data.conversation.system_prompt || "";
    renderConversationList();
    renderMessages(data.messages);
  }

  function setModeUI(mode) {
    state.activeMode = mode;
    $("modeHarnessBtn").classList.toggle("active", mode === "harness");
    $("modeChatBtn").classList.toggle("active", mode === "chat");
    if (state.settings) {
      $("toolsSummary").textContent =
        mode === "harness"
          ? `${state.settings.available_tools.length} harness tools active`
          : "Fast direct chat (no tools)";
    }
  }

  function appendMessageElement(msg) {
    $("emptyState").classList.add("hidden");
    const list = $("messagesList");
    const row = document.createElement("div");
    row.className = `msg-row ${msg.role}`;

    if (msg.role === "user") {
      row.innerHTML = `<div class="user-bubble">${escapeHtml(msg.content)}</div>`;
      list.appendChild(row);
      scrollToBottom();
      return row;
    }

    const card = document.createElement("div");
    card.className = "assistant-card";

    if (msg.reasoning) {
      card.appendChild(buildReasoningDetails(msg.reasoning));
    }
    if (msg.tool_traces && msg.tool_traces.length) {
      card.appendChild(buildTracesDetails(msg.tool_traces));
    }

    const body = document.createElement("div");
    body.className = "md-body";
    body.innerHTML = renderMarkdown(msg.content);
    card.appendChild(body);

    if (msg.metrics && (msg.metrics.total_turn_ms || msg.metrics.ttft_ms)) {
      const m = document.createElement("div");
      m.className = "msg-metrics";
      const ttft = msg.metrics.ttft_ms ? `TTFT ${msg.metrics.ttft_ms}ms · ` : "";
      m.textContent = `${ttft}${msg.metrics.total_turn_ms || msg.metrics.llm_ms}ms · ${msg.metrics.model || ""}`;
      card.appendChild(m);
    }

    row.appendChild(card);
    list.appendChild(row);
    scrollToBottom();
    return row;
  }

  function buildReasoningDetails(text) {
    const det = document.createElement("details");
    det.className = "think-details";
    det.innerHTML = `<summary>Reasoning trace</summary><pre class="think-pre">${escapeHtml(text)}</pre>`;
    return det;
  }

  function buildTracesDetails(traces) {
    const det = document.createElement("details");
    det.className = "trace-details";
    const itemsHtml = traces
      .map(
        (t) =>
          `<div class="trace-item"><div class="trace-head"><span>${escapeHtml(t.name)}</span><span>${t.duration_ms ?? 0}ms</span></div>` +
          `<pre class="trace-pre">${escapeHtml(JSON.stringify(t.args, null, 2))}\n---\n${escapeHtml(t.output)}</pre></div>`
      )
      .join("");
    det.innerHTML = `<summary>Agent Harness Tools (${traces.length})</summary>${itemsHtml}`;
    return det;
  }

  function renderMessages(messages) {
    const list = $("messagesList");
    list.innerHTML = "";
    if (!messages.length) {
      $("emptyState").classList.remove("hidden");
      return;
    }
    $("emptyState").classList.add("hidden");
    for (const m of messages) {
      appendMessageElement(m);
    }
  }

  function scrollToBottom() {
    const sc = $("messagesScroll");
    sc.scrollTop = sc.scrollHeight;
  }

  async function ensureActiveConversation() {
    if (state.activeId) return state.activeId;
    const conv = await api("/api/conversations", {
      method: "POST",
      body: JSON.stringify({ title: "New chat", mode: state.activeMode }),
    });
    state.activeId = conv.id;
    await loadConversations();
    return conv.id;
  }

  async function sendMessage(text) {
    if (!text.trim() || state.streaming) return;
    const convId = await ensureActiveConversation();

    appendMessageElement({ role: "user", content: text });
    $("promptInput").value = "";
    $("promptInput").style.height = "auto";

    state.streaming = true;
    state.abortController = new AbortController();
    $("sendBtn").disabled = true;
    $("stopBtn").classList.remove("hidden");

    const row = document.createElement("div");
    row.className = "msg-row assistant";
    const card = document.createElement("div");
    card.className = "assistant-card";
    const statusEl = document.createElement("div");
    statusEl.className = "status-banner";
    statusEl.textContent = "Connecting to AWS brain...";
    const thinkWrap = document.createElement("div");
    const tracesWrap = document.createElement("div");
    const bodyEl = document.createElement("div");
    bodyEl.className = "md-body";
    const metricsEl = document.createElement("div");
    metricsEl.className = "msg-metrics";
    card.append(statusEl, thinkWrap, tracesWrap, bodyEl, metricsEl);
    row.appendChild(card);
    $("messagesList").appendChild(row);
    scrollToBottom();

    let accText = "";
    let accThink = "";
    const traces = [];

    try {
      const resp = await fetch("/api/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ conversation_id: convId, message: text }),
        signal: state.abortController.signal,
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const parts = buf.split("\n\n");
        buf = parts.pop() || "";
        for (const block of parts) {
          const line = block.trim();
          if (!line.startsWith("data:")) continue;
          const ev = JSON.parse(line.slice(5).trim());
          if (ev.type === "status") {
            statusEl.textContent = ev.message;
          } else if (ev.type === "title") {
            await loadConversations();
          } else if (ev.type === "reasoning") {
            accThink += ev.text;
            thinkWrap.innerHTML = "";
            thinkWrap.appendChild(buildReasoningDetails(accThink));
          } else if (ev.type === "token") {
            accText += ev.text;
            bodyEl.innerHTML = renderMarkdown(accText);
            scrollToBottom();
          } else if (ev.type === "tool_start") {
            statusEl.textContent = `Running tool: ${ev.name}...`;
          } else if (ev.type === "tool_end") {
            traces.push(ev);
            tracesWrap.innerHTML = "";
            tracesWrap.appendChild(buildTracesDetails(traces));
            scrollToBottom();
          } else if (ev.type === "error") {
            statusEl.remove();
            const errEl = document.createElement("div");
            errEl.className = "msg-error";
            errEl.textContent = ev.message;
            card.appendChild(errEl);
          } else if (ev.type === "done") {
            statusEl.remove();
            if (ev.message && ev.message.content) {
              bodyEl.innerHTML = renderMarkdown(ev.message.content);
            }
            if (ev.metrics) {
              const ttft = ev.metrics.ttft_ms ? `TTFT ${ev.metrics.ttft_ms}ms · ` : "";
              metricsEl.textContent = `${ttft}${ev.metrics.total_turn_ms}ms · ${ev.metrics.model}`;
            }
          }
        }
      }
    } catch (err) {
      statusEl.remove();
      if (err.name !== "AbortError") {
        const errEl = document.createElement("div");
        errEl.className = "msg-error";
        errEl.textContent = err.message;
        card.appendChild(errEl);
      }
    } finally {
      state.streaming = false;
      state.abortController = null;
      $("sendBtn").disabled = false;
      $("stopBtn").classList.add("hidden");
      scrollToBottom();
    }
  }

  // Event wiring
  $("newChatBtn").addEventListener("click", async () => {
    const conv = await api("/api/conversations", {
      method: "POST",
      body: JSON.stringify({ title: "New chat", mode: state.activeMode }),
    });
    await loadConversations();
    await selectConversation(conv.id);
    $("sidebar").classList.remove("open");
  });

  $("chatSearchInput").addEventListener("input", renderConversationList);
  $("openSidebarBtn").addEventListener("click", () => $("sidebar").classList.add("open"));
  $("closeSidebarBtn").addEventListener("click", () => $("sidebar").classList.remove("open"));

  for (const btn of [$("modeHarnessBtn"), $("modeChatBtn")]) {
    btn.addEventListener("click", async () => {
      const mode = btn.dataset.mode;
      setModeUI(mode);
      if (state.activeId) {
        await api(`/api/conversations/${state.activeId}`, {
          method: "PATCH",
          body: JSON.stringify({ mode }),
        });
      }
    });
  }

  $("composerForm").addEventListener("submit", (e) => {
    e.preventDefault();
    sendMessage($("promptInput").value);
  });

  $("promptInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage($("promptInput").value);
    }
  });

  $("promptInput").addEventListener("input", () => {
    const el = $("promptInput");
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 180)}px`;
  });

  $("stopBtn").addEventListener("click", () => {
    if (state.abortController) state.abortController.abort();
  });

  document.addEventListener("click", (e) => {
    const copyBtn = e.target.closest(".code-copy");
    if (copyBtn) {
      const raw = decodeURIComponent(copyBtn.dataset.copy || "");
      navigator.clipboard.writeText(raw);
      copyBtn.textContent = "Copied";
      setTimeout(() => { copyBtn.textContent = "Copy"; }, 1200);
    }
    const starter = e.target.closest(".starter-card");
    if (starter) {
      sendMessage(starter.dataset.prompt || "");
    }
    const closeBtn = e.target.closest(".modal-close");
    if (closeBtn) {
      $(closeBtn.dataset.close).classList.add("hidden");
    }
  });

  // Settings modal
  $("openSettingsBtn").addEventListener("click", async () => {
    const cfg = await refreshSettings();
    $("cfgBaseUrl").value = cfg.llm_base_url;
    $("cfgModel").value = cfg.llm_model;
    $("cfgApiKey").value = "";
    $("cfgMaxSteps").value = cfg.max_agent_steps;
    $("cfgShell").checked = cfg.enable_shell_tool;
    $("cfgPython").checked = cfg.enable_python_tool;
    $("cfgWeb").checked = cfg.enable_web_tools;
    $("cfgNoThink").checked = cfg.fast_no_think_default;
    $("settingsStatus").textContent = "";
    $("settingsModal").classList.remove("hidden");
    const health = await refreshHealth();
    const row = $("detectedModelsRow");
    row.innerHTML = "";
    if (health && health.brain_models && health.brain_models.length) {
      for (const m of health.brain_models) {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "model-chip";
        chip.textContent = m;
        chip.addEventListener("click", () => { $("cfgModel").value = m; });
        row.appendChild(chip);
      }
    }
  });

  $("testBrainBtn").addEventListener("click", async () => {
    $("settingsStatus").textContent = "Testing connection...";
    await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({
        llm_base_url: $("cfgBaseUrl").value.trim(),
        llm_model: $("cfgModel").value.trim(),
        llm_api_key: $("cfgApiKey").value.trim() || null,
      }),
    });
    const h = await refreshHealth();
    $("settingsStatus").textContent = h && h.brain_connected
      ? `Connected! Detected models: ${h.brain_models.join(", ") || "none"}`
      : `Failed: ${h ? h.brain_error : "Server error"}`;
  });

  $("settingsForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({
        llm_base_url: $("cfgBaseUrl").value.trim(),
        llm_model: $("cfgModel").value.trim(),
        llm_api_key: $("cfgApiKey").value.trim() || null,
        max_agent_steps: Number($("cfgMaxSteps").value),
        enable_shell_tool: $("cfgShell").checked,
        enable_python_tool: $("cfgPython").checked,
        enable_web_tools: $("cfgWeb").checked,
        fast_no_think_default: $("cfgNoThink").checked,
      }),
    });
    await refreshSettings();
    await refreshHealth();
    $("settingsModal").classList.add("hidden");
  });

  // System prompt modal
  $("editPromptBtn").addEventListener("click", async () => {
    await ensureActiveConversation();
    $("promptModal").classList.remove("hidden");
  });
  $("promptForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!state.activeId) return;
    await api(`/api/conversations/${state.activeId}`, {
      method: "PATCH",
      body: JSON.stringify({ system_prompt: $("convSystemPrompt").value }),
    });
    $("promptModal").classList.add("hidden");
  });

  // Memory modal
  async function renderMemories() {
    const items = await refreshMemoriesCount();
    const list = $("memoryList");
    list.innerHTML = "";
    for (const m of items) {
      const el = document.createElement("div");
      el.className = "memory-item";
      el.innerHTML = `<div><strong>[${escapeHtml(m.topic)}]</strong> ${escapeHtml(m.content)}</div><button class="btn-icon" type="button">✕</button>`;
      el.querySelector("button").addEventListener("click", async () => {
        await api(`/api/memories/${m.id}`, { method: "DELETE" });
        await renderMemories();
      });
      list.appendChild(el);
    }
  }
  $("openMemoryBtn").addEventListener("click", async () => {
    await renderMemories();
    $("memoryModal").classList.remove("hidden");
  });
  $("addMemoryForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    await api("/api/memories", {
      method: "POST",
      body: JSON.stringify({
        topic: $("memTopic").value.trim(),
        content: $("memContent").value.trim(),
      }),
    });
    $("memTopic").value = "";
    $("memContent").value = "";
    await renderMemories();
  });

  // Boot
  (async () => {
    await refreshSettings();
    await refreshHealth();
    await refreshMemoriesCount();
    await loadConversations();
  })();
})();
