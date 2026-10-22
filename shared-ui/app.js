  const AVATARS = {
    praca:       'assets/bob-praca.png',
    ucenie:      'assets/bob-ucenie.png',
    sideprojekt: 'assets/bob-sideprojekt.png'
  };

  const MODE_LABELS = {
    praca:       'WORK',
    ucenie:      'STUDY',
    sideprojekt: 'SIDE'
  };

  const MODE_NAMES = {
    praca:       'Práca',
    ucenie:      'Učenie',
    sideprojekt: 'Sideprojekt'
  };

  const API_MODE_TO_UI = {
    work: 'praca',
    learning: 'ucenie',
    sideproject: 'sideprojekt'
  };

  const UI_MODE_TO_API = {
    praca: 'work',
    ucenie: 'learning',
    sideprojekt: 'sideproject'
  };

  let activeProject = null;
  let activeChat = null;
  let activeRun = null;
  let activeMessages = [];
  let activeArtifacts = [];
  let activeRunStages = [];
  let activeTeamWorkflows = [];
  let selectedArtifact = null;
  let workspaceRoot = '';
  let currentUiMode = 'praca';
  let safetyPolicyEnabled = true;
  let currentThemeId = 'helm-default';
  let currentDailyPlan = null;
  let selectedConsoleEventKey = '';
  let consoleActivityTab = localStorage.getItem('helm-console-activity-tab') || 'tools';
  let lastConsoleEvents = [];
  let lastConsoleRun = null;
  let consoleMode = localStorage.getItem('helm-console-mode') || 'basic';
  let runtimePollTimer = null;
  let runtimePollStartedAt = 0;
  let runtimePollRunId = null;
  let runtimeStartInFlight = false;
  let telegramSyncState = null;
  const DEMO_CONSOLE_EVENTS = [
    { id: 'd1', type: 'thought_summary', created_at: new Date().toISOString(), payload: { message: 'Review the requested change and identify the owning module.' } },
    { id: 'd2', type: 'thought_summary', created_at: new Date().toISOString(), payload: { message: 'Add a regression test before editing the implementation.' } },
    { id: 'd3', type: 'tool_call', created_at: new Date().toISOString(), payload: { name: 'read_file', icon: '📖', target: 'app/workspace.py', status: 'ok' } },
    { id: 'd4', type: 'tool_call', created_at: new Date().toISOString(), payload: { name: 'pytest', icon: '🧪', target: 'tests/test_api.py -q', count: 12, status: 'ok' } },
    { id: 'd5', type: 'verification', created_at: new Date().toISOString(), payload: { name: 'verify', icon: '✅', message: 'Focused tests passed', status: 'ok' } },
    { id: 'd6', type: 'result', created_at: new Date().toISOString(), payload: { message: 'Updated the workspace behavior and verified the regression.' } },
  ];
  function localIsoDate(date = new Date()) {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
  }
  const todayIsoDate = localIsoDate();
  const EDITABLE_ARTIFACTS = new Set(['input.md', 'run-contract.md', 'plan.md', 'output.md', 'review.md']);
  const CHAT_COMMANDS = [
    '/help — zobrazí tieto lokálne commandy',
    '/clear — vyčistí aktuálny chat view bez mazania server histórie',
    '/quest · /map · /chat — prepne stredný workspace tab',
    '/theme default|rpg — prepne appearance theme lokálne aj cez settings API, keď je dostupné'
  ];

  function normalizeMode(mode) {
    return API_MODE_TO_UI[mode] || mode || 'praca';
  }

  function projectMode(project) {
    return project?.mode_default || project?.mode || 'work';
  }

  function projectWorkspacePath(project) {
    if (project?.root_path) return project.root_path;
    const root = workspaceRoot || '/workspace';
    const mode = projectMode(project);
    const domain = mode === 'learning' ? 'school' : 'ai-it';
    return `${root}/domains/${domain}/active/${project?.slug || 'project'}`;
  }

  function setMode(mode) {
    mode = normalizeMode(mode);
    currentUiMode = mode;

    // Odstran vsetky mode triedy
    document.body.classList.remove('mode-praca', 'mode-ucenie', 'mode-sideprojekt');
    document.body.classList.add('mode-' + mode);

    // Aktualizuj avatar
    const avatarImg = document.querySelector('.avatar img');
    if (avatarImg) avatarImg.src = AVATARS[mode];

    // Aktualizuj mode badge
    document.querySelectorAll('.mode-badge').forEach(badge => {
      badge.textContent = MODE_LABELS[mode];
    });

    // Aktualizuj aktivny button
    document.querySelectorAll('.mode').forEach(btn => btn.classList.remove('active'));
    const modeMap = { 'Učenie': 'ucenie', 'Práca': 'praca', 'Sideprojekt': 'sideprojekt', 'LEARN': 'ucenie', 'WORK': 'praca', 'SIDE': 'sideprojekt' };
    document.querySelectorAll('.mode').forEach(btn => {
      if (modeMap[btn.textContent.trim()] === mode) btn.classList.add('active');
    });
  }

  function renderProject(project, activeSlug) {
    const card = document.createElement('div');
    card.className = 'project' + (project.slug === activeSlug ? ' active' : '');
    card.dataset.projectSlug = project.slug || '';

    const name = document.createElement('div');
    name.className = 'name';
    name.textContent = project.name || project.slug || 'Projekt';

    const meta = document.createElement('div');
    meta.className = 'meta';
    const mode = document.createElement('span');
    mode.textContent = MODE_NAMES[normalizeMode(projectMode(project))] || projectMode(project) || 'Projekt';
    const detail = document.createElement('span');
    detail.textContent = `${(project.chats || []).length} chaty`;
    meta.append(mode, detail);

    const deleteBtn = document.createElement('button');
    deleteBtn.className = 'mini-action project-delete';
    deleteBtn.type = 'button';
    deleteBtn.textContent = '×';
    deleteBtn.title = 'Zmazať projekt';
    deleteBtn.setAttribute('aria-label', `Zmazať projekt ${project.name || project.slug}`);
    deleteBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      deleteProject(project);
    });

    card.append(name, meta, deleteBtn);
    card.addEventListener('click', () => selectProject(project));
    return card;
  }

  function renderChats(project) {
    const chatList = document.querySelector('[data-chat-list]');
    if (!chatList || !project || !Array.isArray(project.chats)) return;
    if (!project.chats.length) {
      const row = document.createElement('div');
      row.className = 'chat-row';
      row.append(document.createTextNode('Žiadne chaty · klikni + chat'));
      chatList.replaceChildren(row);
      return;
    }
    chatList.replaceChildren(...project.chats.map(chat => {
      const row = document.createElement('div');
      row.className = 'chat-row' + (activeChat && activeChat.id === chat.id ? ' active' : '');
      row.dataset.chatId = chat.id;
      const dot = document.createElement('span');
      dot.className = 'dot';
      row.append(dot, document.createTextNode(chat.title || `Chat ${chat.id}`));
      row.addEventListener('click', () => selectChat(chat));

      const deleteBtn = document.createElement('button');
      deleteBtn.className = 'mini-action chat-delete';
      deleteBtn.type = 'button';
      deleteBtn.textContent = '×';
      deleteBtn.title = 'Zmazať chat';
      deleteBtn.setAttribute('aria-label', `Zmazať chat ${chat.title || chat.id}`);
      deleteBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        deleteChat(chat);
      });
      row.append(deleteBtn);
      return row;
    }));
  }

  function selectProject(project) {
    if (!project) return;
    activeProject = project; activeChat = null; activeMessages = []; activeRun = null;
    document.querySelectorAll('[data-project-list] .project').forEach(node => node.classList.remove('active'));
    const activeName = document.querySelector('[data-active-project-name]');
    if (activeName) activeName.textContent = project.name || project.slug;
    const activePath = document.querySelector('[data-active-project-path]');
    if (activePath) activePath.textContent = `Aktívny workspace · ${projectWorkspacePath(project)}`;
    setMode(projectMode(project)); renderChats(project);
    renderRunEvents([]);
    refreshMessages().catch(() => renderChatMessages([], 'Správy sa nepodarilo načítať.'));
    refreshProjectWorkspace().catch(() => renderProjectWorkspace(null));
    renderFolderTree(null, project?.id).catch(() => {});
    refreshContextMeter().catch(() => renderContextMeter(null));
  }
  function selectChat(chat) {
    if (!chat) return;
    activeChat = chat; activeMessages = [];
    if (chat?.mode) setMode(chat.mode);
    renderChats(activeProject); updateChatHeader();
    refreshMessages().catch(() => renderChatMessages([], 'Správy sa nepodarilo načítať.'));
    refreshLatestRun().catch(() => renderNoActiveRun('Active run API nedostupné.'));
    refreshContextMeter().catch(() => renderContextMeter(null));
  }
  function updateChatHeader() {
    const title = document.querySelector('[data-chat-title]');
    if (title) title.textContent = activeChat?.title || 'Chat s Bobom';
    const summary = document.querySelector('[data-bob-summary]');
    if (summary) {
      const projectName = activeProject?.name || activeProject?.slug || 'projekt';
      summary.textContent = activeChat
        ? `${projectName} · plain text chat. Agent Console vpravo je debug/run pohľad.`
        : 'Vyber chat alebo vytvor nový cez + chat.';
    }
  }

  async function refreshContextMeter() {
    if (window.location.protocol === 'file:' || !activeChat) return renderContextMeter(null);
    const data = await apiFetch(`/api/chats/${activeChat.id}/context-meter`);
    renderContextMeter(data);
  }

  function renderContextMeter(data) {
    const percent = Math.max(0, Math.min(100, Number(data?.percent || 0)));
    const label = document.querySelector('[data-context-label]');
    const meter = document.querySelector('[data-context-meter]');
    const wrap = document.querySelector('[data-context-inline]');
    if (label) label.textContent = `Context ${percent}%`;
    if (meter) meter.style.width = `${percent}%`;
    if (wrap) {
      wrap.classList.toggle('is-warn', percent >= 50 && percent < 75);
      wrap.classList.toggle('is-hot', percent >= 75);
      if (data) wrap.title = `${data.used_chars || 0}/${data.limit_chars || 0} chars · ${data.message_count || 0} messages · ${data.project_context_chars || 0} project context chars`;
    }
  }

  async function refreshEverydayWorkspace() {
    // Today UI is intentionally removed; keep this shim so older handlers stay harmless.
    await refreshTelegramSyncPanel().catch(() => renderTelegramSyncPanel(null));
  }

  async function refreshTelegramSyncPanel() {
    if (window.location.protocol === 'file:') return renderTelegramSyncPanel(null);
    const data = await apiFetch('/api/telegram/sync');
    telegramSyncState = data;
    renderTelegramSyncPanel(data);
  }

  function renderTelegramSyncPanel(data) {
    const status = document.querySelector('[data-telegram-latest-sync]');
    const inline = document.querySelector('[data-telegram-sync-status]');
    const inboxCount = data?.counts?.inbox_new || 0;
    const outboxCount = data?.counts?.outbox_draft || 0;
    if (status) status.textContent = data ? `Auto-read · inbox new ${inboxCount} · outbox drafts ${outboxCount}` : 'Auto-read preview · API nedostupné';
    if (inline) inline.textContent = 'Practical Autonomy: Telegram read/sync beží, auto-send je stále blokovaný.';
    const tgChip = document.querySelector('[data-status-telegram]');
    if (tgChip) {
      const enabled = data?.auto_sync?.enabled;
      const last = data?.auto_sync?.last_sync_status || 'idle';
      tgChip.textContent = data ? `TG ${enabled ? 'auto' : 'off'} · ${last} · ${inboxCount} new` : 'TG sync —';
    }
    const memChip = document.querySelector('[data-status-memory]');
    if (memChip) memChip.textContent = 'MEM persistent';
    renderTelegramList('[data-telegram-inbox]', data?.inbox || [], item => ({
      title: item.content || 'Telegram item',
      chip: item.status || 'new',
      meta: [item.sender_name || item.sender_username || item.source || 'telegram', item.created_at || ''].filter(Boolean).join(' · ')
    }));
    renderTelegramList('[data-telegram-outbox]', data?.outbox || [], item => ({
      title: item.kind === 'run_report' ? `Run report · ${(item.content || '').split('\n')[1] || 'draft'}` : (item.content || 'Draft'),
      chip: item.status || 'draft',
      meta: item.target_label || 'Telegram target unresolved'
    }));
  }

  function renderTelegramList(selector, items, mapItem) {
    const node = document.querySelector(selector);
    if (!node) return;
    const rows = (items || []).slice(0, 5).map(item => {
      const mapped = mapItem(item);
      const row = el('div', '', 'telegram-row');
      const text = el('span', `${String(mapped.title || '').slice(0, 90)}${mapped.meta ? ` · ${mapped.meta}` : ''}`);
      row.append(text, el('span', mapped.chip || 'draft', 'telegram-chip'));
      return row;
    });
    node.replaceChildren(...(rows.length ? rows : [el('p', '—')]));
  }

  async function simulateTelegramIngestFromUi() {
    if (window.location.protocol === 'file:') return;
    const input = document.querySelector('[data-telegram-capture-input]');
    const status = document.querySelector('[data-telegram-sync-status]');
    const content = (input?.value || '').trim();
    if (!content) return;
    if (status) status.textContent = 'Ukladám Telegram item lokálne…';
    try {
      await apiFetch('/api/telegram/inbox', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content, source: 'helm_ui_simulation', sender_name: 'Telegram local simulation', chat_id: activeChat?.id || null, metadata: { local_only: true, dry_run: true } })
      });
      if (input) input.value = '';
      if (status) status.textContent = 'Zachytené lokálne. Nič nebolo odoslané.';
      await refreshTelegramSyncPanel();
    } catch (error) {
      if (status) status.textContent = `Telegram capture zlyhal: ${error.message || 'API error'}`;
      throw error;
    }
  }

  async function prepareTelegramReportOutboxFromUi() {
    if (window.location.protocol === 'file:' || !activeRun) return;
    const status = document.querySelector('[data-telegram-sync-status]');
    if (status) status.textContent = 'Pripravujem lokálny Telegram report draft…';
    try {
      await apiFetch(`/api/runs/${activeRun.id}/telegram-report`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_label: 'Telegram Home (draft)', include_artifact_path: true })
      });
      if (status) status.textContent = 'Report draft pripravený lokálne. External send stále blokovaný.';
      await refreshTelegramSyncPanel();
      await refreshMessages().catch(() => {});
      await refreshLatestRun().catch(() => {});
    } catch (error) {
      if (status) status.textContent = `Report draft zlyhal: ${error.message || 'API error'}`;
      throw error;
    }
  }

  async function refreshProjectWorkspace() {
    if (window.location.protocol === 'file:' || !activeProject) return renderProjectWorkspace(null);
    const projectId = activeProject.id;
    const data = await apiFetch(`/api/projects/${projectId}/workspace`);
    if (!activeProject || activeProject.id !== projectId) return;
    renderProjectWorkspace(data);
  }

  function renderProjectWorkspace(data) {
    const wrap = document.querySelector('[data-project-workspace]');
    if (!wrap) return;
    const context = document.querySelector('[data-project-context]');
    if (context) context.value = data?.context?.content || '';
    renderProjectGoals(data?.goals || []);
    renderWorkspaceList('[data-project-tasks]', (data?.tasks || []).filter(item => item.status !== 'done').slice(0, 5), item => `${item.title || 'task'} · P${item.priority || 2}`);
    renderWorkspaceList('[data-project-files]', (data?.files || []).filter(item => item.pinned !== false).slice(0, 5), item => `${item.label || item.path || 'file'}`);
    renderWorkspaceList('[data-project-notes]', (data?.notes || []).slice(-5), item => `${item.title || 'note'}`);
    renderWorkspaceList('[data-project-run-history]', (data?.run_history || []).slice(0, 5), item => `#${item.id} · ${item.status || 'run'} · ${(item.prompt || '').slice(0, 28)}`);
    renderWorkspaceList('[data-project-run-summaries]', (data?.run_summaries || []).slice(0, 5), item => {
      const label = `#${item.run_id}${item.summary ? ' · ' + item.summary.slice(0, 40) : ''}`;
      const next = item.next_actions ? ` → ${item.next_actions.slice(0, 30)}` : '';
      return label + next;
    });
    renderWorkspaceList('[data-project-stage-defaults]', (data?.stage_defaults || []).filter(item => item.instructions || item.artifact_template), item => `${stageLabel(item.stage)} · custom`);
  }

  function questXpForTask(item) {
    const priority = Number(item?.priority || 2);
    if (priority >= 4) return 150;
    if (priority <= 1) return 25;
    return 80;
  }

  function renderQuestTaskRows(tasks) {
    const node = document.querySelector('[data-project-tasks]');
    if (!node) return;
    const rows = (tasks || []).slice(0, 8).map(item => {
      const xp = questXpForTask(item);
      const row = el('div', '', `quest-row ${item.status === 'done' ? 'done' : xp >= 150 ? 'boss' : xp <= 25 ? 'small' : 'main'}`);
      const check = el('span', '', 'quest-check' + (item.status === 'done' ? ' filled' : ''));
      row.append(check, el('span', item.title || 'task', 'quest-title'), el('span', item.status === 'done' ? 'done' : `+${xp}`, 'quest-xp'));
      return row;
    });
    if (!rows.length) {
      rows.push(
        questDemoRow('Helm UI mockup', '+80', 'main'),
        questDemoRow('School mini quest', '+25', 'small'),
        questDemoRow('Boss task', '+150', 'boss'),
        questDemoRow('Clean shell', 'done', 'done')
      );
    }
    node.replaceChildren(...rows);
  }

  function questDemoRow(title, xp, type) {
    const row = el('div', '', `quest-row ${type}`);
    row.append(el('span', '', 'quest-check' + (type === 'done' ? ' filled' : '')), el('span', title, 'quest-title'), el('span', xp, 'quest-xp'));
    return row;
  }

  function renderWorkspaceList(selector, items, labelFn) {
    if (selector === '[data-project-tasks]') return renderQuestTaskRows(items);
    const node = document.querySelector(selector);
    if (!node) return;
    const rows = (items || []).map(item => el('p', labelFn(item)));
    node.replaceChildren(...(rows.length ? rows : [el('p', '—')]));
  }

  function renderProjectGoals(goals) {
    const node = document.querySelector('[data-project-goals]');
    if (!node) return;
    const rows = (goals || []).map(goal => {
      const row = el('div', '', 'goal-row');
      row.append(el('strong', goal.title || 'goal'));
      const meta = [
        goal.horizon || 'outcome',
        goal.status || 'active',
        goal.vision_title ? `vision: ${goal.vision_title}` : '',
        goal.parent_goal_title ? `parent: ${goal.parent_goal_title}` : ''
      ].filter(Boolean).join(' · ');
      row.append(el('div', meta, 'goal-meta'));
      return row;
    });
    node.replaceChildren(...(rows.length ? rows : [el('p', '—')]));
  }

  async function createProjectGoalFromUi() {
    if (window.location.protocol === 'file:' || !activeProject) return;
    const input = document.querySelector('[data-new-goal-title]');
    const horizon = document.querySelector('[data-new-goal-horizon]')?.value || 'outcome';
    const title = (input?.value || '').trim();
    if (!title) return;
    await apiFetch(`/api/projects/${activeProject.id}/goals`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, horizon })
    });
    if (input) input.value = '';
    await refreshProjectWorkspace();
  }

  async function saveProjectContext() {
    if (window.location.protocol === 'file:' || !activeProject) return;
    const textarea = document.querySelector('[data-project-context]');
    const content = textarea?.value || '';
    await apiFetch(`/api/projects/${activeProject.id}/context`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content })
    });
    await refreshProjectWorkspace();
  }

  async function refreshMessages() {
    if (window.location.protocol === 'file:' || !activeChat) return renderChatMessages([], 'Vyber chat alebo napíš správu dole.');
    const chatId = activeChat.id;
    const response = await apiFetch(`/api/chats/${chatId}/messages`);
    if (!activeChat || activeChat.id !== chatId) return null;
    activeMessages = response.messages || [];
    renderChatMessages(activeMessages);
  }

  function renderChatMessages(messages = activeMessages, emptyText = '') {
    const thread = document.querySelector('[data-chat-thread]');
    if (!thread) return;
    const rows = (Array.isArray(messages) ? messages : []).map(message => renderMessageBubble(message));
    if (!rows.length) {
      if (emptyText) {
        rows.push(renderMessageBubble({ role: 'assistant', content: emptyText }));
      } else {
        rows.push(
          renderMessageBubble({ role: 'user', content: "Let's add the chat view to the workspace." }),
          renderMessageBubble({ role: 'assistant', content: 'Quest, Map, and Chat are now the three workspace views.' }),
          renderMessageBubble({ role: 'user', content: 'Explain how the planner works.' }),
          renderMessageBubble({ role: 'assistant', content: 'The planner coordinates tasks and priorities. I can turn a requested change into a reviewable run.' })
        );
      }
    }
    thread.replaceChildren(...rows);
    scrollChatThreadAfterRender(thread);
  }

  function scrollChatThreadAfterRender(thread) {
    const latest = thread?.lastElementChild;
    if (!thread || !latest) return;
    const isAssistant = latest.classList.contains('assistant');
    const isTallAssistant = isAssistant && latest.scrollHeight > thread.clientHeight * 0.72;
    if (!isTallAssistant) {
      thread.scrollTop = thread.scrollHeight;
      return;
    }
    const threadTop = thread.getBoundingClientRect().top;
    const latestTop = latest.getBoundingClientRect().top;
    thread.scrollTop += latestTop - threadTop - 12;
  }

  function renderMessageBubble(message) {
    const role = message?.role === 'assistant' ? 'assistant' : 'user';
    const bubble = el('div', '', `msg ${role}`);
    const label = el('div', role === 'assistant' ? 'BOB' : 'YOU', 'role');
    const content = String(message?.content || '');
    const body = el('div', '', 'msg-body');
    body.innerHTML = renderMarkdown(content);
    bubble.append(label, body);
    return bubble;
  }

  function renderMarkdown(text) {
    if (!text) return '';
    const escaped = text
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
    return escaped
      .replace(/```([\s\S]*?)```/g, '<pre><code>$1</code></pre>')
      .replace(/`([^`\n]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/\*([^*]+)\*/g, '<em>$1</em>')
      .replace(/^### (.+)$/gm, '<h4>$1</h4>')
      .replace(/^## (.+)$/gm, '<h3>$1</h3>')
      .replace(/^# (.+)$/gm, '<h2>$1</h2>')
      .replace(/^- (.+)$/gm, '<li>$1</li>')
      .replace(/(<li>.*<\/li>\n?)+/g, match => `<ul>${match}</ul>`)
      .replace(/\n/g, '<br>');
  }

  function renderTypingBubble(text = 'Bob píše…') {
    renderChatMessages([
      ...activeMessages,
      { role: 'assistant', content: text, created_at: '', pending: true }
    ]);
    const latest = document.querySelector('.msg.assistant:last-child > div:not(.role)');
    if (latest) latest.classList.add('typing');
  }

  function appendLocalMessage(role, content) {
    activeMessages = [...activeMessages, { role, content, created_at: new Date().toISOString() }];
    renderChatMessages(activeMessages);
  }

  function handleSlashCommand(prompt) {
    const text = String(prompt || '').trim();
    if (!text.startsWith('/')) return false;
    const [rawCommand, ...args] = text.slice(1).split(/\s+/);
    const command = (rawCommand || '').toLowerCase();
    const reply = message => {
      activeMessages = [...activeMessages, { role: 'assistant', content: message, created_at: new Date().toISOString() }];
      renderChatMessages(activeMessages);
    };

    switch (command) {
      case 'clear':
        activeMessages = [];
        renderChatMessages([], 'Chat view vyčistený lokálne. Server história ostáva nezmazaná.');
        return true;
      case 'help':
      case 'commands':
        reply(`Dostupné lokálne commandy:\n${CHAT_COMMANDS.join('\n')}`);
        return true;
      case 'quest':
      case 'map':
      case 'chat':
        setWorkspaceView(command);
        reply(`Prepnuté na ${command.toUpperCase()} tab.`);
        return true;
      case 'theme': {
        const requested = (args[0] || '').toLowerCase();
        const nextTheme = requested === 'rpg' || requested === 'helm-rpg'
          ? 'helm-rpg'
          : requested === 'default' || requested === 'helm-default'
            ? 'helm-default'
            : currentThemeId === 'helm-rpg' ? 'helm-default' : 'helm-rpg';
        updateTheme(nextTheme).catch(error => renderNoActiveRun(error.message || 'Theme command zlyhal.'));
        reply(`Theme prepnutý na ${nextTheme === 'helm-rpg' ? 'Helm RPG' : 'Helm Default'}.`);
        return true;
      }
      default:
        reply(`Neznámy command /${command || ''}. Skús /help.`);
        return true;
    }
  }

  function renderNoActiveRun(message = 'Žiadny aktívny run pre vybraný chat.') {
    activeRun = null; activeArtifacts = []; activeRunStages = [];
    activeTeamWorkflows = []; selectedArtifact = null;
    renderRunEvents([]); renderProgressPanel([], null);
  }
  function el(tag, text = '', className = '') {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = text;
    return node;
  }

  function stageLabel(stage) {
    const labels = { input: 'Input', plan: 'Plan', execute: 'Execute', review: 'Review', final_output: 'Final output' };
    return labels[stage] || stage || 'Stage';
  }

  function isRuntimeTerminalStatus(run) {
    const status = String(run?.status || '').toLowerCase();
    return ['review', 'completed', 'failed', 'approved', 'denied'].includes(status);
  }

  function formatElapsed(ms) {
    const total = Math.max(0, Math.floor(ms / 1000));
    const minutes = Math.floor(total / 60);
    const seconds = String(total % 60).padStart(2, '0');
    return minutes ? `${minutes}:${seconds}` : `${total}s`;
  }

  function stopRuntimePolling() {
    if (runtimePollTimer) clearInterval(runtimePollTimer);
    runtimePollTimer = null;
    runtimePollRunId = null;
    runtimePollStartedAt = 0;
  }

  function startRuntimePolling(runId) {
    stopRuntimePolling();
    runtimePollRunId = runId;
    runtimePollStartedAt = Date.now();

    const tick = async () => {
      if (!activeRun || activeRun.id !== runId) {
        stopRuntimePolling();
        return;
      }
      const elapsed = formatElapsed(Date.now() - runtimePollStartedAt);
      try {
        const run = await refreshLatestRun();
        await refreshMessages().catch(() => {});
        if (run && isRuntimeTerminalStatus(run)) {
          stopRuntimePolling();
          await refreshMessages().catch(() => {});
          await refreshProjectWorkspace().catch(() => {});
          await refreshContextMeter().catch(() => {});
        }
      } catch (error) {
      }
    };

    tick();
    runtimePollTimer = setInterval(tick, 2000);
  }

  async function refreshLatestRun() {
    if (window.location.protocol === 'file:' || !activeChat) return renderNoActiveRun();
    const chatId = activeChat.id;
    const latest = await apiFetch(`/api/chats/${chatId}/runs/latest`);
    if (!activeChat || activeChat.id !== chatId) return;
    if (!latest.run) { renderNoActiveRun(); return null; }
    try {
      const stageResponse = await apiFetch(`/api/runs/${latest.run.id}/stages`);
      activeRunStages = stageResponse.stages || [];
      if (stageResponse.run) latest.run = stageResponse.run;
    } catch (_) {}
    if (!activeChat || activeChat.id !== chatId) return null;
    try {
      const eventResponse = await apiFetch(`/api/runs/${latest.run.id}/events`);
      renderRunEvents(eventResponse.events || [], latest.run);
    } catch (_) { renderRunEvents([], latest.run); }
    refreshTelegramSyncPanel().catch(() => {});
    const liveStatus = String(latest.run.status || '');
    if (liveStatus === 'running' || liveStatus === 'queued') { startLiveProgressPolling(); }
    else { stopLiveProgressPolling(); }
    return latest.run;
  }
  function renderRunEvents(events, run = null) {
    const useDemo = new URLSearchParams(window.location.search).has('consoleDemo');
    const rawEvents = useDemo ? DEMO_CONSOLE_EVENTS : (Array.isArray(events) ? events : []);
    const normalized = rawEvents.map(event => ({
      ...event, payload: event?.payload || safeParsePayload(event?.payload_json)
    }));
    const displayRun = useDemo ? { id: 'demo', status: 'completed', mode: 'SOLO+CRITIC', prompt: 'Review a workspace change' } : run;
    lastConsoleEvents = normalized; lastConsoleRun = displayRun;
    renderHeaderStatus(normalized, displayRun);
    renderToolbox(normalized, displayRun);
    renderProgressPanel(normalized, displayRun);
    applyConsoleMode();
  }
  function applyConsoleMode() {
    const mode = consoleMode === 'expert' ? 'expert' : 'basic';
    const panel = document.querySelector('[data-agent-console]');
    if (panel) {
      panel.classList.toggle('is-console-basic', mode === 'basic');
      panel.classList.toggle('is-console-expert', mode === 'expert');
    }
    document.querySelectorAll('[data-console-mode]').forEach(button => {
      const active = button.dataset.consoleMode === mode;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    document.querySelectorAll('[data-console-basic-only]').forEach(node => node.classList.toggle('is-hidden', mode !== 'basic'));
    document.querySelectorAll('[data-console-expert-only]').forEach(node => node.classList.toggle('is-hidden', mode !== 'expert'));
    applyConsoleActivityTab();
  }
  const ACTIVITY_TABS = ['tools', 'actions', 'terminal', 'thinking', 'diff'];
  const ACTIVITY_TAB_TITLES = { tools: 'Tools', actions: 'Actions', terminal: 'Terminal', thinking: 'Thinking', diff: 'Diff' };

  function normalizeActivityTab(tab) {
    return ACTIVITY_TABS.includes(tab) ? tab : 'tools';
  }

  function applyConsoleActivityTab() {
    consoleActivityTab = normalizeActivityTab(consoleActivityTab);
    document.querySelectorAll('[data-console-activity-tab]').forEach(button => {
      const active = button.dataset.consoleActivityTab === consoleActivityTab;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    const title = document.querySelector('[data-activity-title]');
    if (title) title.textContent = ACTIVITY_TAB_TITLES[consoleActivityTab] || 'Tools';
    const isTerminal = consoleActivityTab === 'terminal';
    const isDiff = consoleActivityTab === 'diff';
    const feed = document.querySelector('[data-activity-feed]');
    const terminal = document.querySelector('[data-terminal-output]');
    if (feed) feed.hidden = isTerminal || isDiff;
    if (terminal) terminal.hidden = !isTerminal;
    if (isTerminal) { setupTerminalScrollTracking(); refreshTerminalOutput().catch(() => {}); }
    if (isDiff) { refreshDiffOutput().catch(() => {}); }
    syncTerminalPolling();
  }
  function setConsoleActivityTab(tab) {
    consoleActivityTab = normalizeActivityTab(tab);
    selectedConsoleEventKey = '';
    localStorage.setItem('helm-console-activity-tab', consoleActivityTab);
    renderRunEvents(lastConsoleEvents, lastConsoleRun);
    applyConsoleActivityTab();
  }

  // ── M18b/M24: terminal tail ──────────────────────────────────────────
  let terminalPollTimer = null;
  let terminalUserScrolled = false;

  function renderTerminalOutput(data) {
    const target = document.querySelector('[data-terminal-output]');
    if (!target) return;
    const output = (data && data.output) || '';
    const stderr = (data && data.stderr_tail) || '';
    const status = (data && data.status) || '';

    // Build output: stdout first, then stderr (if any), then status line
    let content = '';
    if (!output.trim() && !stderr.trim()) {
      content = status === 'running' ? 'Run beží, ziatil bez výstupu…' : 'Žiadny runtime výstup.';
    } else {
      const meta = data && data.truncated
        ? `— posledných ${data.line_count} z ${data.total_lines} riadkov —\n`
        : '';
      content = meta + output;
      if (stderr.trim()) {
        content += `\n── stderr ──\n${stderr}`;
      }
      // Status line for completed/failed runs
      if (status && status !== 'running' && status !== 'queued') {
        content += `\n── run ${status} ──`;
      }
    }

    target.textContent = content;

    // Auto-scroll to bottom unless user scrolled up
    if (!terminalUserScrolled) {
      target.scrollTop = target.scrollHeight;
    }

    // Update line count badge
    const count = document.querySelector('[data-terminal-tab-count]');
    if (count) {
      const total = (data && data.total_lines) || 0;
      const errCount = (data && data.stderr_lines) || 0;
      count.textContent = errCount ? `${total} +${errCount}err` : String(total);
    }

    // Update polling indicator
    const tab = document.querySelector('[data-console-activity-tab="terminal"]');
    if (tab) {
      const isPolling = terminalPollTimer !== null;
      tab.classList.toggle('is-polling', isPolling);
    }
  }

  // Track user scroll to pause auto-scroll
  function setupTerminalScrollTracking() {
    const target = document.querySelector('[data-terminal-output]');
    if (!target || target.dataset.scrollTracked) return;
    target.dataset.scrollTracked = '1';
    target.addEventListener('scroll', () => {
      const atBottom = target.scrollHeight - target.scrollTop - target.clientHeight < 40;
      terminalUserScrolled = !atBottom;
    });
  }

  async function refreshTerminalOutput() {
    const run = lastConsoleRun;
    if (!run || !run.id) {
      renderTerminalOutput(null);
      return;
    }
    try {
      renderTerminalOutput(await apiFetch(`/api/runs/${run.id}/terminal?lines=200`));
    } catch (error) {
      renderTerminalOutput(null);
    }
  }

  function syncTerminalPolling() {
    const shouldPoll = consoleActivityTab === 'terminal'
      && lastConsoleRun
      && ['running', 'queued'].includes(lastConsoleRun.status);
    if (shouldPoll && !terminalPollTimer) {
      terminalPollTimer = setInterval(() => refreshTerminalOutput().catch(() => {}), 2000);
      terminalUserScrolled = false; // reset on (re)start
    } else if (!shouldPoll && terminalPollTimer) {
      clearInterval(terminalPollTimer);
      terminalPollTimer = null;
    }
    // Update indicator
    const tab = document.querySelector('[data-console-activity-tab="terminal"]');
    if (tab) tab.classList.toggle('is-polling', shouldPoll);
  }

  function closeActivityDetail() {
    selectedConsoleEventKey = '';
    renderToolbox(lastConsoleEvents, lastConsoleRun);
  }

  function setConsoleMode(mode) {
    consoleMode = mode === 'expert' ? 'expert' : 'basic';
    localStorage.setItem('helm-console-mode', consoleMode);
    applyConsoleMode();
  }

  function renderToolbox(events, run) {
    const toolbox = document.querySelector('[data-toolbox]');
    const feed = document.querySelector('[data-activity-feed]');
    const count = document.querySelector('[data-tool-count]');
    if (!toolbox || !feed) return;
    applyConsoleActivityTab();
    const toolEvents = events.filter(isToolCallEvent);
    const actionEvents = events.filter(isActionishEvent);
    const thinkingEvents = events.filter(isThinkingEvent);
    const visibleEvents = consoleActivityTab === 'tools' ? toolEvents
      : consoleActivityTab === 'actions' ? actionEvents
      : consoleActivityTab === 'thinking' ? thinkingEvents : [];
    const groups = groupToolEvents(visibleEvents).slice(consoleMode === 'expert' ? -10 : -8);
    const toolsCount = document.querySelector('[data-tools-tab-count]');
    const actionsCount = document.querySelector('[data-actions-tab-count]');
    const thinkingCount = document.querySelector('[data-thinking-tab-count]');
    if (toolsCount) toolsCount.textContent = String(toolEvents.length);
    if (actionsCount) actionsCount.textContent = String(actionEvents.length);
    if (thinkingCount) thinkingCount.textContent = String(thinkingEvents.length);
    if (count) count.textContent = String(visibleEvents.length);
    if (!groups.length) {
      const row = el('button', '', 'activity-row'); row.type = 'button';
      row.append(el('span', '·', 'activity-icon'), el('span', 'idle', 'activity-name'),
        el('span', consoleActivityTab === 'thinking' ? 'no thinking events' : (consoleActivityTab === 'tools' ? 'no tool events' : 'no action events'), 'activity-target'),
        el('span', '—', 'activity-count'));
      feed.replaceChildren(row); return;
    }
    const selected = groups.find(group => group.key === selectedConsoleEventKey) || null;
    const rows = groups.map(group => {
      const event = group.events[group.events.length - 1];
      const key = group.key;
      const item = el('div', '', 'activity-item');
      const row = el('button', '', 'activity-row' + (selectedConsoleEventKey === key ? ' is-active' : ''));
      row.type = 'button'; row.dataset.eventKey = key;
      row.append(el('span', group.icon, 'activity-icon'), el('span', group.name, 'activity-name'),
        el('span', group.target, 'activity-target'),
        el('span', group.count > 1 ? `×${group.count}` : eventTag(event, run), 'activity-count'));
      row.title = `${group.name}: ${group.target}`;
      row.addEventListener('click', () => {
        selectedConsoleEventKey = selectedConsoleEventKey === key ? '' : key;
        renderToolbox(events, run);
      });
      item.append(row);
      if (selectedConsoleEventKey === key) {
        item.append(el('code', eventDetailText(event, group), 'activity-inline-detail'));
      }
      return item;
    });
    feed.replaceChildren(...rows);
  }

  function renderHeaderStatus(events, run) {
    const teamChip = document.querySelector('[data-status-team]');
    if (teamChip) teamChip.textContent = run ? `TEAM ${run.mode || 'idle'}` : 'TEAM idle';
    const tools = events.filter(isToolCallEvent).length;
    const actions = events.filter(isActionishEvent).length;
    const status = normalizedRunStatus(run, events);
    const running = ['queued', 'running'].includes(String(run?.status || '').toLowerCase());
    const dot = document.querySelector('[data-console-status-dot]');
    if (dot) dot.className = `status-dot ${status.className}`;
    const subline = document.querySelector('[data-console-subline]');
    if (subline) {
      const runtimePart = runtimePollRunId === run?.id ? ` · ${formatElapsed(Date.now() - runtimePollStartedAt)}` : '';
      subline.textContent = `${status.label}${runtimePart} · ${run?.mode || currentApiMode() || 'SOLO'} · ${tools} tools · ${actions} actions`;
    }
  }

  function renderProgressPanel(events, run) {
    const badge = document.querySelector('[data-progress-badge]');
    const stageEl = document.querySelector('[data-stage-progress]');
    const box = document.querySelector('[data-progress-box]');
    if (!badge || !box) return;
    if (!run) {
      badge.textContent = 'idle'; badge.classList.remove('is-running');
      if (stageEl) stageEl.innerHTML = '';
      box.textContent = 'Zatiaľ žiadny bežiaci run.';
      return;
    }
    const status = String(run.status || 'idle');
    const running = status === 'running' || status === 'queued';
    badge.textContent = `${status} · ${events.filter(isToolCallEvent).length} tools`;
    badge.classList.toggle('is-running', running);
    if (stageEl) {
      const stages = ['input', 'plan', 'execute', 'review', 'output'];
      const labels = { input: 'Input', plan: 'Plan', execute: 'Run', review: 'Check', output: 'Done' };
      const currentIdx = stages.indexOf(run.current_stage || 'input');
      stageEl.replaceChildren(...stages.map((stage, i) => {
        const step = document.createElement('span');
        step.className = 'stage-step';
        step.textContent = labels[stage] || stage;
        if (i < currentIdx || status === 'completed') step.classList.add('done');
        else if (i === currentIdx) step.classList.add(running ? 'active' : 'done');
        else step.classList.add('pending');
        return step;
      }));
    }
    const tail = String(run.stdout_tail || '').trim();
    const resultEvents = events.filter(isResultEvent).slice(-3);
    const lines = [];
    if (tail) lines.push(tail);
    resultEvents.forEach(e => lines.push(`✓ ${eventTitle(e.type)}`));
    box.textContent = lines.join('\n') || 'Run beží, zatiaľ bez výstupu…';
  }

  async function refreshDiffOutput() {
    const feed = document.querySelector('[data-activity-feed]');
    if (!feed) return;
    if (window.location.protocol === 'file:' || !activeRun) {
      feed.innerHTML = '<div class="diff-empty">Žiadny aktívny run.</div>';
      return;
    }
    try {
      const data = await apiFetch(`/api/runs/${activeRun.id}/diff`);
      const files = Array.isArray(data.files) ? data.files : [];
      if (!data.available) { feed.innerHTML = '<div class="diff-empty">Git diff nie je dostupný.</div>'; return; }
      if (!files.length) { feed.innerHTML = '<div class="diff-empty">Žiadne zmeny v repo.</div>'; return; }
      feed.innerHTML = files.slice(0, 40).map(f =>
        `<div class="diff-row"><span class="diff-path">${escapeHtml(f.path)}</span>` +
        `<span class="diff-added">+${Number(f.added) || 0}</span>` +
        `<span class="diff-removed">−${Number(f.removed) || 0}</span></div>`).join('');
    } catch (error) {
      feed.innerHTML = `<div class="diff-empty">Diff zlyhal: ${escapeHtml(error.message || 'error')}</div>`;
    }
  }
  function groupToolEvents(events) {
    const grouped = [];
    const byKey = new Map();
    events.forEach(event => {
      const payload = event?.payload || {};
      const name = toolName(event);
      const target = toolTarget(event);
      const key = `${name}::${target}`;
      if (!byKey.has(key)) {
        const group = { key, name, target, icon: toolIcon(event), count: 0, events: [] };
        byKey.set(key, group);
        grouped.push(group);
      }
      const group = byKey.get(key);
      group.count += Number(payload.count || 1);
      group.events.push(event);
    });
    return grouped;
  }

  function eventDetailText(event, group = null) {
    if (!event) return 'Žiadny vybraný action detail.';
    const payload = event.payload || {};
    return [
      `type: ${event.type || 'event'}`,
      group ? `group: ${group.name}` : '',
      group ? `target: ${group.target}` : payloadDetail(event),
      group && group.count > 1 ? `count: ×${group.count}` : '',
      eventTime(event) ? `time: ${eventTime(event)}` : '',
      payload.status ? `status: ${payload.status}` : '',
      payload.command ? `command: ${payload.command}` : '',
      payload.path ? `path: ${payload.path}` : '',
    ].filter(Boolean).join('\n');
  }

  function stepBody(title, detail) {
    const body = el('div');
    body.append(el('h4', title), el('p', detail || 'bez detailu'));
    return body;
  }

  function safeParsePayload(payloadJson) {
    if (!payloadJson) return {};
    if (typeof payloadJson === 'object') return payloadJson;
    try {
      const parsed = JSON.parse(payloadJson);
      return parsed && typeof parsed === 'object' ? parsed : { value: String(parsed) };
    } catch (_) {
      return { raw: String(payloadJson) };
    }
  }

  function eventTitle(type) {
    const titles = {
      thought_summary: 'Thinking summary',
      reasoning_summary: 'Reasoning summary',
      plan_note: 'Plan note',
      tool_call: 'Tool call',
      verification: 'Verification',
      result: 'Result',
      run_created: 'Run vytvorený',
      safety_note: 'Safety policy',
      artifacts_initialized: 'Artifacts pripravené',
      artifact_updated: 'Artifact uložený',
      approved: 'Review approved',
      rerun_stage_requested: 'Rerun stage',
      stage_started: 'Stage spustený',
      stage_advanced: 'Stage posunutý',
      runtime_started: 'Bob runtime spustený',
      runtime_completed: 'Bob runtime dokončený',
      runtime_failed: 'Bob runtime zlyhal',
      runtime_contract_loaded: 'Runtime contract loaded',
      run_contract_initialized: 'Run contract initialized',
      workflow_policy_loaded: 'Workflow policy loaded',
      team_workflow_created: 'TEAM workflow created',
      team_workflow_template_initialized: 'TEAM workflow initialized',
      team_workflow_step_created: 'TEAM step created',
      team_workflow_step_updated: 'TEAM step updated',
      team_agent_task_updated: 'Agent task updated',
      tool_activity: 'Tool activity',
      safety_policy_disabled: 'Safety vypnutá',
      run_finalized: 'Run finalizovaný'
    };
    return titles[type] || String(type || 'Event');
  }

  function payloadDetail(event) {
    const payload = event?.payload || {};
    if (isThinkingEvent(event)) return payload.message || payload.summary || payload.note || 'thinking update';
    if (event?.type === 'tool_call' || event?.type === 'tool_activity') return [payload.name, payload.target || payload.path || payload.command].filter(Boolean).join(': ');
    if (event?.type === 'verification') return payload.message || payload.name || 'verified';
    if (event?.type === 'result') return payload.message || 'done';
    if (event?.type === 'artifact_updated') return [payload.name || 'artifact', formatBytes(payload.size)].filter(Boolean).join(' · ');
    if (event?.type === 'rerun_stage_requested') return [payload.stage ? `stage ${payload.stage}` : '', payload.reason].filter(Boolean).join(' · ');
    if (event?.type === 'stage_started') return payload.label || payload.stage || 'stage active';
    if (event?.type === 'stage_advanced') return [payload.from, '→', payload.to, payload.note].filter(Boolean).join(' ');
    if (event?.type === 'runtime_started') return `command: ${payload.command || 'runtime'} · ${payload.stage || 'execute'}`;
    if (event?.type === 'runtime_completed') return `exit ${payload.exit_code ?? 0} · stdout ${formatBytes(payload.stdout_size || 0)}`;
    if (event?.type === 'run_contract_initialized') return payload.artifact || 'run-contract.md';
    if (event?.type === 'workflow_policy_loaded') return shortenPath(payload.path || payload.name || 'policy');
    if (event?.type === 'team_workflow_template_initialized') return `roles: ${(payload.roles || []).join(' → ')}`;
    if (event?.type === 'team_workflow_created') return payload.title || `workflow #${payload.workflow_id || ''}`;
    if (event?.type === 'team_workflow_step_created') return [payload.role, `step #${payload.step_id || ''}`].filter(Boolean).join(' · ');
    if (event?.type === 'team_workflow_step_updated') return [payload.role, payload.status, `step #${payload.step_id || ''}`].filter(Boolean).join(' · ');
    if (event?.type === 'team_agent_task_updated') return [payload.role, payload.agent_status, payload.verified_evidence_present ? 'verified evidence' : 'self-report only'].filter(Boolean).join(' · ');
    if (event?.type === 'runtime_failed') return payload.timed_out ? 'timeout' : `exit ${payload.exit_code ?? '?'}`;
    if (event?.type === 'run_finalized') return payload.note ? `final · ${payload.note}` : 'final output completed';
    if (event?.type === 'approved') return payload.note ? `note: ${payload.note}` : 'approved';
    if (event?.type === 'safety_note') return payload.message || payload.note || payload.raw || 'policy active';
    if (event?.type === 'artifacts_initialized') return payload.artifact_path || 'artifacts ready';
    if (event?.type === 'run_created') return payload.mode ? `mode ${payload.mode}` : 'created';
    if (payload.message) return payload.message;
    if (payload.raw) return payload.raw;
    const entries = Object.entries(payload).slice(0, 3).map(([key, value]) => `${key}: ${String(value)}`);
    return entries.join(' · ') || 'event recorded';
  }

  function isThinkingEvent(event) {
    return ['thought_summary', 'reasoning_summary', 'plan_note'].includes(event?.type);
  }

  function isResultEvent(event) {
    return ['verification', 'result', 'runtime_completed', 'runtime_failed', 'run_finalized', 'approved'].includes(event?.type);
  }

  function isToolishEvent(event) {
    return ['tool_call', 'tool_activity', 'artifact_updated', 'safety_policy_disabled', 'rerun_stage_requested', 'artifacts_initialized', 'run_contract_initialized', 'workflow_policy_loaded', 'runtime_contract_loaded', 'team_workflow_created', 'team_workflow_template_initialized', 'team_workflow_step_created', 'team_workflow_step_updated', 'team_agent_task_updated', 'stage_started', 'stage_advanced', 'runtime_started', 'runtime_completed', 'runtime_failed', 'run_finalized'].includes(event?.type);
  }

  function isToolCallEvent(event) {
    return ['tool_call', 'tool_activity'].includes(event?.type);
  }

  function isActionishEvent(event) {
    return isToolishEvent(event) && !isToolCallEvent(event);
  }

  function toolIcon(event) {
    const payload = event?.payload || {};
    if (payload.icon) return payload.icon;
    const name = toolName(event);
    const icons = { skill_view: '📚', session_search: '🔍', search_files: '🔎', read_file: '📖', todo: '📋', terminal: '💻', write_file: '✍️', patch: '🔧', memory: '🧠', verify: '✅', runtime_start: '▶️', runtime_contract_loaded: '📜', run_contract_initialized: '📋', workflow_policy_loaded: '📎', team_workflow_created: '👥', team_workflow_template_initialized: '👥', team_workflow_step_created: '👥', team_workflow_step_updated: '👥', team_agent_task_updated: '🧩', runtime_completed: '✅', runtime_failed: '⚠️', artifact_updated: '📄' };
    return icons[name] || '•';
  }

  function toolName(event) {
    const payload = event?.payload || {};
    if (payload.name) return payload.name;
    if (event?.type === 'artifact_updated') return 'artifact_updated';
    if (event?.type === 'stage_started') return 'stage_started';
    if (event?.type === 'stage_advanced') return 'stage_advanced';
    if (event?.type === 'runtime_started') return 'runtime_start';
    if (event?.type === 'runtime_contract_loaded') return 'runtime_contract_loaded';
    if (event?.type === 'run_contract_initialized') return 'run_contract_initialized';
    if (event?.type === 'workflow_policy_loaded') return 'workflow_policy_loaded';
    if (event?.type === 'team_workflow_created') return 'team_workflow_created';
    if (event?.type === 'team_workflow_template_initialized') return 'team_workflow_template_initialized';
    if (event?.type === 'team_workflow_step_created') return 'team_workflow_step_created';
    if (event?.type === 'team_workflow_step_updated') return 'team_workflow_step_updated';
    if (event?.type === 'team_agent_task_updated') return 'team_agent_task_updated';
    if (event?.type === 'runtime_completed') return 'runtime_completed';
    if (event?.type === 'runtime_failed') return 'runtime_failed';
    if (event?.type === 'run_finalized') return 'run_finalized';
    if (event?.type === 'rerun_stage_requested') return 'rerun_stage';
    return event?.type || 'event';
  }

  function toolTarget(event) {
    const payload = event?.payload || {};
    if (payload.target) return String(payload.target);
    if (payload.path) return shortenPath(payload.path);
    if (payload.command) return truncateMiddle(payload.command, 46);
    if (event?.type === 'artifact_updated') return payload.name || 'artifact';
    if (event?.type === 'stage_started') return payload.stage || payload.label || 'stage';
    if (event?.type === 'stage_advanced') return [payload.from, '→', payload.to].filter(Boolean).join(' ');
    if (event?.type === 'runtime_started') return truncateMiddle(payload.command || 'runtime', 46);
    if (event?.type === 'runtime_contract_loaded') return shortenPath(payload.path || payload.name || 'contract');
    if (event?.type === 'run_contract_initialized') return payload.artifact || 'run-contract.md';
    if (event?.type === 'workflow_policy_loaded') return shortenPath(payload.path || payload.name || 'policy');
    if (event?.type === 'team_workflow_created' || event?.type === 'team_workflow_template_initialized') return `workflow #${payload.workflow_id || ''}`;
    if (event?.type === 'team_workflow_step_created' || event?.type === 'team_workflow_step_updated') return [payload.role, payload.status, `#${payload.step_id || ''}`].filter(Boolean).join(' · ');
    if (event?.type === 'team_agent_task_updated') return [payload.role, payload.agent_status, payload.verified_evidence_present ? 'verified' : 'unverified'].filter(Boolean).join(' · ');
    if (event?.type === 'runtime_completed') return `exit ${payload.exit_code ?? 0}`;
    if (event?.type === 'runtime_failed') return payload.timed_out ? 'timeout' : `exit ${payload.exit_code ?? '?'}`;
    return truncateMiddle(payloadDetail(event), 58);
  }

  function eventTag(event, run) {
    const payload = event?.payload || {};
    if (payload.status === 'ok') return 'OK';
    if (event?.type === 'rerun_stage_requested') return 'RERUN';
    if (event?.type === 'runtime_started') return 'RUN';
    if (event?.type === 'runtime_contract_loaded') return 'LOAD';
    if (event?.type === 'run_contract_initialized') return 'CONTRACT';
    if (event?.type === 'workflow_policy_loaded') return 'POLICY';
    if (event?.type === 'team_workflow_created' || event?.type === 'team_workflow_template_initialized') return 'TEAM';
    if (event?.type === 'team_workflow_step_created') return 'STEP';
    if (event?.type === 'team_workflow_step_updated') return 'UPDATE';
    if (event?.type === 'team_agent_task_updated') return 'AGENT';
    if (event?.type === 'safety_policy_disabled') return 'OFF';
    if (event?.type === 'runtime_completed') return 'OK';
    if (event?.type === 'runtime_failed') return 'FAIL';
    if (event?.type === 'stage_started') return 'ACTIVE';
    if (event?.type === 'stage_advanced') return 'NEXT';
    if (event?.type === 'run_finalized') return 'DONE';
    if (event?.type === 'artifact_updated' || event?.type === 'artifacts_initialized') return 'OK';
    if (['queued', 'running'].includes(String(run?.status || '').toLowerCase())) return 'RUN';
    return `#${event?.id || ''}`;
  }

  function normalizedRunStatus(run, events = []) {
    const status = String(run?.status || '').toLowerCase();
    if (status === 'failed' || events.some(event => event.type === 'runtime_failed')) return { label: 'Failed', className: 'failed' };
    if (status === 'queued' || status === 'running') return { label: 'Running', className: 'running' };
    if (status === 'completed' || status === 'approved' || status === 'review') return { label: status === 'review' ? 'Review' : 'Done', className: '' };
    return run ? { label: status || 'Active', className: 'running' } : { label: 'Idle', className: 'idle' };
  }

  function truncateMiddle(value, max = 64) {
    const text = String(value || '');
    if (text.length <= max) return text;
    const keep = Math.max(8, Math.floor((max - 3) / 2));
    return `${text.slice(0, keep)}...${text.slice(-keep)}`;
  }

  function shortenPath(value) {
    const text = String(value || '');
    return text.replace(/^\/home\/[^/]+\//, '~/');
  }

  function eventTagClass(event, run) {
    if (event?.type === 'runtime_failed') return 'tag-orange';
    if (event?.type === 'stage_started' || event?.type === 'stage_advanced' || event?.type === 'runtime_started' || event?.type === 'runtime_contract_loaded') return 'tag-run';
    if (event?.type === 'runtime_completed' || event?.type === 'run_finalized' || event?.type === 'tool_call' || event?.type === 'tool_activity' || event?.type === 'verification' || event?.type === 'result') return 'tag-ok';
    if (['queued', 'running'].includes(String(run?.status || '').toLowerCase())) return 'tag-run';
    return 'tag-queued';
  }

  function eventTime(event) {
    if (!event?.created_at) return '';
    const date = new Date(event.created_at);
    if (Number.isNaN(date.getTime())) return String(event.created_at);
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  // ── M17: context tier badge ──────────────────────────────────────────
  // Shows which context tier the prompt will get, before sending. Click to
  // cycle through manual overrides (auto -> XS -> S -> M -> L -> auto).
  const TIER_CYCLE = [null, 'XS', 'S', 'M', 'L'];
  let manualTier = null;
  let tierPreviewTimer = null;
  let lastAutoTier = 'M';

  function renderTierBadge(tier, reason, isManual) {
    const badge = document.querySelector('[data-tier-badge]');
    if (!badge) return;
    badge.textContent = tier;
    badge.dataset.tier = tier;
    badge.classList.toggle('is-manual', !!isManual);
    const prefix = isManual ? 'Manuálny tier' : 'Auto tier';
    badge.title = `${prefix}: ${tier} — ${reason || ''}\nKlikni pre prepnutie.`;
  }

  function scheduleTierPreview(text) {
    if (tierPreviewTimer) clearTimeout(tierPreviewTimer);
    tierPreviewTimer = setTimeout(() => previewTier(text), 220);
  }

  async function previewTier(text) {
    if (manualTier) {
      renderTierBadge(manualTier, 'nastavené ručne', true);
      return;
    }
    try {
      const data = await apiFetch('/api/context/classify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt: text || '' })
      });
      lastAutoTier = data.tier;
      renderTierBadge(data.tier, data.reason, false);
    } catch (error) {
      // Offline/file:// fallback — keep the last known tier, never break input.
      renderTierBadge(lastAutoTier, 'offline fallback', false);
    }
  }

  function initTierBadge() {
    const badge = document.querySelector('[data-tier-badge]');
    if (!badge) return;
    badge.addEventListener('click', () => {
      const index = TIER_CYCLE.indexOf(manualTier);
      manualTier = TIER_CYCLE[(index + 1) % TIER_CYCLE.length];
      const input = document.querySelector('[data-prompt-input]');
      if (manualTier) renderTierBadge(manualTier, 'nastavené ručne', true);
      else previewTier(input ? input.value : '');
    });
  }

  function consumeTierOverride() {
    return manualTier;
  }

  async function createMessageAndRun() {
    if (window.location.protocol === 'file:') return;
    const input = document.querySelector('[data-prompt-input]');
    const prompt = (input?.value || '').trim();
    if (!prompt) return;
    if (handleSlashCommand(prompt)) {
      if (input) { input.value = ''; input.style.height = 'auto'; } return;
    }
    if (!activeChat) return;
    const chatId = activeChat.id;
    const button = document.querySelector('[data-send-run]');
    if (button) button.disabled = true;
    if (input) { input.value = ''; input.style.height = 'auto'; }
    setWorkspaceView('chat'); appendLocalMessage('user', prompt);
    renderTypingBubble('Bob run-loop sa spúšťa…');
    try {
      const tierOverride = consumeTierOverride();
      const runBody = { content: prompt, start_runtime: true, timeout_seconds: 900 };
      if (tierOverride) runBody.context_tier = tierOverride;
      const data = await apiFetch(`/api/chats/${chatId}/run-loop`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(runBody)
      });
      const run = data.run;
      const summary = document.querySelector('[data-bob-summary]');
      if (summary && run) { summary.textContent = `Run #${run.id} beží. Agent Console ukazuje reálny priebeh.`; }
      await refreshMessages().catch(() => {}); await refreshLatestRun();
      if (run) startLiveProgressPolling();
      if (run && data.runtime?.started) startRuntimePolling(run.id);
      await refreshProjectWorkspace().catch(() => {}); await refreshContextMeter().catch(() => {});
    } catch (error) {
      if (activeChat && activeChat.id === chatId) {
        activeMessages = activeMessages.filter(message => !message.pending);
        renderChatMessages([...activeMessages, { role: 'assistant', content: `Nepodarilo sa spustiť run-loop: ${error.message || 'runtime/API error'}`, created_at: new Date().toISOString() }]);
      }
      renderNoActiveRun(error.message || 'Run-loop sa nepodarilo vytvoriť/spustiť.');
    } finally { if (button) button.disabled = false; }
  }
  async function apiFetch(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) throw new Error(`API error ${response.status}`);
    return response.json();
  }

  function applyTheme(themeId = 'helm-default') {
    const safeTheme = ['helm-default', 'helm-rpg'].includes(themeId) ? themeId : 'helm-default';
    currentThemeId = safeTheme;
    document.body.classList.remove('theme-helm-default', 'theme-helm-rpg');
    document.body.classList.add(`theme-${safeTheme}`);
    document.querySelectorAll('[data-theme-select]').forEach(select => {
      select.value = safeTheme;
    });
    try { localStorage.setItem('helm.themeId', safeTheme); } catch (_) {}
  }

  function renderAppearanceSettings(settings = {}) {
    const themes = Array.isArray(settings.themes) && settings.themes.length
      ? settings.themes
      : [{ id: 'helm-default', name: 'Helm Default' }, { id: 'helm-rpg', name: 'Helm RPG' }];
    const selects = document.querySelectorAll('[data-theme-select]');
    selects.forEach(select => {
      const currentValue = select.value || currentThemeId;
      select.replaceChildren(...themes.map(theme => {
        const option = document.createElement('option');
        option.value = theme.id;
        option.textContent = theme.name;
        return option;
      }));
      select.value = settings.theme_id || currentValue || 'helm-default';
    });
    applyTheme(settings.theme_id || currentThemeId || 'helm-default');
  }

  function setWorkspaceView(view = 'chat') {
    const safeView = ['quest', 'map', 'chat'].includes(view) ? view : 'chat';
    document.querySelectorAll('[data-workspace-tab]').forEach(button => {
      const active = button.dataset.workspaceTab === safeView;
      button.classList.toggle('active', active);
      button.setAttribute('aria-selected', String(active));
    });
    document.querySelectorAll('[data-view]').forEach(panel => {
      panel.classList.toggle('active', panel.dataset.view === safeView);
    });
    if (safeView === 'chat') {
      requestAnimationFrame(() => {
        const thread = document.querySelector('[data-chat-thread]');
        if (thread) thread.scrollTop = thread.scrollHeight;
      });
    }
    if (safeView === 'map') {
      // M21: the MAP tab no longer owns a file tree; the sidebar tree is the
      // single source. Nothing to refresh here.
    }
    try { localStorage.setItem('helm.workspaceView', safeView); } catch (_) {}
  }

  // ── M16/M24: live run progress ─────────────────────────────────────────
  let liveProgressTimer = null;
  let liveProgressStartTime = null;

  function renderStageSteps(el, currentStage, runStatus) {
    const stages = ['input', 'plan', 'execute', 'review', 'output'];
    const labels = { input: 'Input', plan: 'Plan', execute: 'Run', review: 'Check', output: 'Done' };
    const currentIdx = stages.indexOf(currentStage || 'input');
    el.replaceChildren(...stages.map((stage, i) => {
      const step = document.createElement('span');
      step.className = 'stage-step';
      step.textContent = labels[stage] || stage;
      if (i < currentIdx || runStatus === 'completed') step.classList.add('done');
      else if (i === currentIdx) step.classList.add((runStatus === 'running' || runStatus === 'queued') ? 'active' : 'done');
      else step.classList.add('pending');
      return step;
    }));
  }

  function startLiveProgressPolling() {
    stopLiveProgressPolling();

  }

  function stopLiveProgressPolling() {
    if (liveProgressTimer) {
      clearInterval(liveProgressTimer);
      liveProgressTimer = null;
    }
  }

  // ── M21: live folder tree in the left sidebar ───────────────────────────
  const expandedFolders = new Set();

  function folderTreeRow(entry, isExpanded) {
    const indent = Math.max(0, (Number(entry.depth) || 1) - 1) * 12;
    const isDir = entry.type === 'dir';
    const icon = isDir ? (isExpanded ? '▾' : '▸') : '·';
    return `<div class="folder-tree-row ${entry.type}${isExpanded ? ' is-expanded' : ''}"
      style="padding-left:${indent}px"
      data-folder-entry="${escapeHtml(entry.path || '')}"
      data-folder-type="${entry.type}">
      <span class="folder-tree-icon">${icon}</span>
      <span class="folder-tree-name">${escapeHtml(entry.name)}</span>
    </div>`;
  }

  async function renderFolderTree(selectPath = null, projectId = null) {
    const box = document.querySelector('[data-folder-tree]');
    const pathLabel = document.querySelector('[data-folder-path]');
    if (!box) return;
    if (window.location.protocol === 'file:') {
      box.innerHTML = '<div class="folder-tree-empty">Workspace API nie je dostupné cez file://.</div>';
      return;
    }

    // Selecting a folder expands it rather than only highlighting it.
    if (selectPath !== null) {
      if (expandedFolders.has(selectPath)) expandedFolders.delete(selectPath);
      else expandedFolders.add(selectPath);
    }

    try {
      const params = new URLSearchParams({ depth: '1' });
      if (projectId) params.set('project_id', String(projectId));
      const data = await apiFetch(`/api/workspace/tree?${params}`);
      if (pathLabel) {
        pathLabel.textContent = data.root || '—';
        pathLabel.dataset.fullPath = data.root || '';
      }
      let entries = Array.isArray(data.entries) ? data.entries : [];

      // Fetch children for each expanded folder and splice them in beneath it.
      for (const path of Array.from(expandedFolders)) {
        try {
          const subParams = new URLSearchParams({ path, depth: '1' });
          if (projectId) subParams.set('project_id', String(projectId));
          const sub = await apiFetch(`/api/workspace/tree?${subParams}`);
          const children = (Array.isArray(sub.entries) ? sub.entries : []).map(child => ({
            ...child,
            depth: (Number(child.depth) || 1) + path.split('/').length
          }));
          const at = entries.findIndex(e => e.path === path);
          if (at >= 0) entries.splice(at + 1, 0, ...children);
        } catch (_) { /* a folder that vanished should not break the tree */ }
      }

      if (!entries.length) {
        box.innerHTML = '<div class="folder-tree-empty">Prázdny workspace.</div>';
        return;
      }

      box.innerHTML = entries
        .slice(0, 300)
        .map(entry => folderTreeRow(entry, expandedFolders.has(entry.path)))
        .join('');

      box.querySelectorAll('[data-folder-type="dir"]').forEach(row => {
        row.addEventListener('click', () => renderFolderTree(row.dataset.folderEntry, projectId));
      });

      box.querySelectorAll('[data-folder-type="file"]').forEach(row => {
        row.style.cursor = 'pointer';
        row.title = 'Otvoriť súbor vo VS Code';
        row.addEventListener('click', () => {
          const root = pathLabel?.dataset?.fullPath || '';
          const filePath = root ? `${root}/${row.dataset.folderEntry}` : row.dataset.folderEntry;
          window.open(`vscode://file/${filePath}`, '_blank');
        });
      });
    } catch (error) {
      box.innerHTML = `<div class="folder-tree-empty">Tree zlyhal: ${escapeHtml(error.message || 'error')}</div>`;
    }
  }

  function setSettingsOpen(open) {
    const overlay = document.querySelector('[data-settings-overlay]');
    if (!overlay) return;
    overlay.classList.toggle('open', Boolean(open));
    overlay.setAttribute('aria-hidden', String(!open));
  }

  async function refreshSettings() {
    if (window.location.protocol === 'file:') {
      renderAppearanceSettings({ theme_id: localStorage.getItem('helm.themeId') || 'helm-default' });
      return;
    }
    const settings = await apiFetch('/api/settings');
    renderAppearanceSettings(settings);
  }

  async function updateTheme(themeId) {
    applyTheme(themeId);
    if (window.location.protocol === 'file:') return;
    const settings = await apiFetch('/api/settings', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ theme_id: currentThemeId })
    });
    renderAppearanceSettings(settings);
  }

  function currentApiMode() {
    return UI_MODE_TO_API[normalizeMode(currentUiMode)] || 'sideproject';
  }

  function formatBytes(size) {
    if (typeof size !== 'number') return '';
    return size < 1024 ? `${size} B` : `${(size / 1024).toFixed(1)} KB`;
  }

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>'"]/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
  }

  async function hydrateState(mode = currentUiMode, preferredProjectSlug = null, preferredChatId = null) {
    const apiMode = UI_MODE_TO_API[normalizeMode(mode)] || 'work';
    const response = await fetch(`/api/state?mode=${encodeURIComponent(apiMode)}`);
    if (!response.ok) throw new Error('State API unavailable');
    const state = await response.json();
    const projectList = document.querySelector('[data-project-list]');
    if (projectList) {
      const title = projectList.querySelector('.section-title-row') || projectList.querySelector('.section-title');
      const children = title ? [title] : [];
      if (Array.isArray(state.projects) && state.projects.length) {
        children.push(...state.projects.map(project => renderProject(project, preferredProjectSlug || state.active_project_slug)));
      } else { children.push(el('div', 'Žiadne projekty v tomto móde · klikni + folder', 'project')); }
      projectList.replaceChildren(...children);
    }
    if (!Array.isArray(state.projects) || state.projects.length === 0) {
      activeProject = null; activeChat = null;
      const activeName = document.querySelector('[data-active-project-name]');
      if (activeName) activeName.textContent = `Žiadny projekt · ${MODE_NAMES[currentUiMode] || currentUiMode}`;
      const activePath = document.querySelector('[data-active-project-path]');
      if (activePath) activePath.textContent = 'Vytvor nový working folder cez + folder';
      const summary = document.querySelector('[data-bob-summary]');
      if (summary) summary.textContent = 'V tomto móde zatiaľ nemáš žiadny projekt. Klikni + folder a vytvor nový workspace.';
      activeMessages = [];
      renderChatMessages([], 'V tomto móde zatiaľ nie je projekt. Vytvor + folder.');
      renderChats({ chats: [] }); renderRunEvents([]); renderProjectWorkspace(null);
      return;
    }
    const nextProject = state.projects.find(project => project.slug === preferredProjectSlug)
      || state.projects.find(project => project.slug === state.active_project_slug) || state.projects[0];
    selectProject(nextProject);
    if (preferredChatId) {
      const nextChat = (nextProject.chats || []).find(chat => chat.id === preferredChatId);
      if (nextChat) selectChat(nextChat);
    }
  }
  async function createNewChat() {
    if (window.location.protocol === 'file:' || !activeProject) return;
    const title = window.prompt('Názov nového chatu:', 'Nový chat');
    if (!title || !title.trim()) return;
    try {
      const chat = await apiFetch(`/api/projects/${activeProject.id}/chats`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: title.trim(), mode: currentApiMode() })
      });
      await hydrateState(currentUiMode, activeProject.slug, chat.id);
    } catch (error) { renderNoActiveRun(error.message || 'Chat sa nepodarilo vytvoriť.'); }
  }
  function openProjectModal() {
    if (window.location.protocol === 'file:') return;
    const modal = document.querySelector('[data-project-modal]');
    const choices = document.querySelector('.project-modal-choices');
    const browser = document.querySelector('[data-project-browser]');
    if (!modal) return;
    choices.hidden = false;
    browser.hidden = true;
    browserSelectedPath = '';
    browserCurrentPath = '';
    modal.classList.add('open');
    modal.setAttribute('aria-hidden', 'false');
  }

  function closeProjectModal() {
    const modal = document.querySelector('[data-project-modal]');
    if (!modal) return;
    modal.classList.remove('open');
    modal.setAttribute('aria-hidden', 'true');
  }

  async function createNewProject() {
    if (window.location.protocol === 'file:') return;
    const name = window.prompt(`Názov nového folderu/projektu pre mód ${MODE_NAMES[currentUiMode] || currentUiMode}:`, 'Nový projekt');
    if (!name || !name.trim()) return;
    try {
      const project = await apiFetch('/api/projects', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name.trim(), mode_default: currentApiMode(), description: MODE_NAMES[currentUiMode] || '' })
      });
      await hydrateState(currentUiMode, project.slug); hydrateWorkspaceTree().catch(() => {});
    } catch (error) { renderNoActiveRun(error.message || 'Projekt sa nepodarilo vytvoriť.'); }
  }
  async function showDirectoryBrowser() {
    const choices = document.querySelector('.project-modal-choices');
    const browser = document.querySelector('[data-project-browser]');
    if (!browser) return;
    choices.hidden = true;
    browser.hidden = false;
    await browseTo('');
  }

  async function browseTo(path) {
    const list = document.querySelector('[data-browser-list]');
    const pathEl = document.querySelector('[data-browser-path]');
    const upBtn = document.querySelector('[data-browser-up]');
    if (!list) return;
    list.replaceChildren(el('div', 'Načítavam...', 'browser-item'));
    try {
      const data = await apiFetch(`/api/browse${path ? '?path=' + encodeURIComponent(path) : ''}`);
      browserCurrentPath = data.path;
      browserSelectedPath = '';
      if (pathEl) pathEl.textContent = data.path;
      if (upBtn) upBtn.disabled = !data.parent;
      list.replaceChildren();
      if (data.directories.length === 0) {
        list.appendChild(el('div', 'Žiadne podadresáre', 'browser-item'));
      } else {
        data.directories.forEach(dir => {
          const item = el('div', '', 'browser-item');
          item.dataset.path = dir.path;
          item.append(el('span', '📁', 'icon'), el('span', dir.name));
          item.addEventListener('click', () => {
            document.querySelectorAll('.browser-item.selected').forEach(i => i.classList.remove('selected'));
            item.classList.add('selected');
            browserSelectedPath = dir.path;
          });
          item.addEventListener('dblclick', () => browseTo(dir.path));
          list.appendChild(item);
        });
      }
    } catch (error) {
      list.replaceChildren(el('div', `Chyba: ${error.message}`, 'browser-item'));
    }
  }

  async function browserGoUp() {
    if (!browserCurrentPath) return;
    try {
      const data = await apiFetch(`/api/browse?path=${encodeURIComponent(browserCurrentPath)}`);
      if (data.parent) {
        await browseTo(data.parent);
      }
    } catch (_) {}
  }

  async function openSelectedFolder() {
    const path = browserSelectedPath || browserCurrentPath;
    const nameInput = document.querySelector('[data-browser-project-name]');
    const name = nameInput?.value?.trim() || '';
    if (!path) return;
    try {
      const project = await apiFetch('/api/projects/open', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, name, mode_default: currentApiMode() })
      });
      closeProjectModal();
      await hydrateState(currentUiMode, project.slug);
      hydrateWorkspaceTree().catch(() => {});
    } catch (error) {
      renderNoActiveRun(error.message || 'Adresár sa nepodarilo otvoriť.');
    }
  }

  async function deleteProject(project) {
    if (window.location.protocol === 'file:') return;
    const confirmed = window.confirm(`Odstrániť projekt "${project.name || project.slug}"? Z Helmu sa odstránia jeho projektové dáta (chaty, správy, runy, ciele, úlohy, poznámky, pamäte, TEAM workflowy a metadáta súborov). Denné položky a Telegram inbox/outbox záznamy zostanú, ale bez väzby na projekt/chat/správu/run. Priečinok a súbory na disku zostanú nedotknuté.`);
    if (!confirmed) return;
    try {
      await apiFetch(`/api/projects/${project.id}`, { method: 'DELETE' });
      await hydrateState(currentUiMode); hydrateWorkspaceTree().catch(() => {});
    } catch (error) { renderNoActiveRun(error.message || 'Projekt sa nepodarilo zmazať.'); }
  }
  async function deleteChat(chat) {
    if (window.location.protocol === 'file:' || !activeProject) return;
    const confirmed = window.confirm(`Naozaj chceš zmazať chat "${chat.title || chat.id}"?`);
    if (!confirmed) return;
    try {
      await apiFetch(`/api/projects/${activeProject.id}/chats/${chat.id}`, { method: 'DELETE' });
      await hydrateState(currentUiMode, activeProject.slug);
    } catch (error) { renderNoActiveRun(error.message || 'Chat sa nepodarilo zmazať.'); }
  }
  async function hydrateWorkspaceTree() {
    const response = await fetch('/api/workspace/tree');
    if (!response.ok) throw new Error('Workspace API unavailable');
    const tree = await response.json();
    if (!tree.root) return;
    workspaceRoot = tree.root;

    const activePath = document.querySelector('[data-active-project-path]');
    if (activePath) {
      activePath.textContent = activeProject
        ? `Aktívny workspace · ${projectWorkspacePath(activeProject)}`
        : `Aktívny workspace · ${tree.root}`;
    }
  }

  function setProjectWorkspaceCollapsed(collapsed) {
    const panel = document.querySelector('[data-project-workspace]');
    const button = document.querySelector('[data-toggle-project-workspace]');
    if (!panel || !button) return;
    panel.classList.toggle('is-collapsed', collapsed);
    button.textContent = collapsed ? '+' : '−';
    button.setAttribute('aria-expanded', String(!collapsed));
    try { localStorage.setItem('helm.projectWorkspaceCollapsed', collapsed ? '1' : '0'); } catch (_) {}
  }

  function setPanelHidden(panelName, hidden) {
    const panel = document.querySelector('[data-project-workspace]');
    const button = document.querySelector('[data-toggle-project-panel]');
    if (!panel || !button || panelName !== 'project') return;
    panel.classList.toggle('is-hidden', hidden);
    button.classList.toggle('active', !hidden);
    button.setAttribute('aria-pressed', String(!hidden));
    try { localStorage.setItem('helm.projectPanelHidden', hidden ? '1' : '0'); } catch (_) {}
  }

  function setAgentConsoleCollapsed(collapsed) {
    const app = document.querySelector('.app');
    const consolePanel = document.querySelector('[data-agent-console]');
    const button = document.querySelector('[data-toggle-agent-console]');
    if (!app || !consolePanel || !button) return;
    app.classList.toggle('console-collapsed', collapsed);
    consolePanel.classList.toggle('is-collapsed', collapsed);
    button.textContent = collapsed ? '‹' : '›';
    button.setAttribute('aria-expanded', String(!collapsed));
    if (collapsed) app.style.setProperty('--right-panel-width', '0px');
    else if ((getComputedStyle(app).getPropertyValue('--right-panel-width') || '').trim() === '0px') app.style.setProperty('--right-panel-width', '430px');
    const resizer = document.querySelector('[data-resize-agent-console]');
    if (resizer) {
      const width = parseInt(getComputedStyle(app).getPropertyValue('--right-panel-width'), 10) || 0;
      resizer.setAttribute('aria-valuenow', String(width));
    }
    try {
      localStorage.setItem('helm.agentConsoleCollapsed', collapsed ? '1' : '0');
      localStorage.setItem('helm.agentConsoleWidth', collapsed ? '0' : String(parseInt(getComputedStyle(app).getPropertyValue('--right-panel-width'), 10) || 430));
    } catch (_) {}
  }

  function initLayoutControls() {
    const app = document.querySelector('.app');
    const workspaceToggle = document.querySelector('[data-toggle-project-workspace]');
    const projectPanelToggle = document.querySelector('[data-toggle-project-panel]');
    const consoleToggle = document.querySelector('[data-toggle-agent-console]');
    const resizer = document.querySelector('[data-resize-agent-console]');
    if (!app) return;

    try {
      const demoConsole = new URLSearchParams(window.location.search).has('consoleDemo');
      if (localStorage.getItem('helm.layoutVersion') !== 'm11-run-loop-v1') {
        localStorage.setItem('helm.layoutVersion', 'm11-run-loop-v1');
        localStorage.removeItem('helm.projectWorkspaceCollapsed');
        localStorage.removeItem('helm.agentConsoleCollapsed');
        localStorage.removeItem('helm.agentConsoleWidth');
        localStorage.removeItem('helm.todayPanelHidden');
        localStorage.removeItem('helm.projectPanelHidden');
      }
      if (demoConsole) {
        localStorage.removeItem('helm.agentConsoleCollapsed');
        localStorage.setItem('helm.agentConsoleWidth', '430');
      }
      const storedWidthRaw = localStorage.getItem('helm.agentConsoleWidth');
      const storedWidth = storedWidthRaw === null ? NaN : Number(storedWidthRaw);
      if (Number.isFinite(storedWidth) && storedWidth > 0 && storedWidth <= 620) {
        app.style.setProperty('--right-panel-width', `${storedWidth}px`);
      }
      setProjectWorkspaceCollapsed(localStorage.getItem('helm.projectWorkspaceCollapsed') === '1');
      setPanelHidden('project', localStorage.getItem('helm.projectPanelHidden') === '1');
      setAgentConsoleCollapsed(localStorage.getItem('helm.agentConsoleCollapsed') === '1' || storedWidthRaw === '0');
    } catch (_) {}

    if (workspaceToggle) {
      workspaceToggle.addEventListener('click', () => {
        const panel = document.querySelector('[data-project-workspace]');
        setProjectWorkspaceCollapsed(!panel?.classList.contains('is-collapsed'));
      });
    }

    if (projectPanelToggle) {
      projectPanelToggle.addEventListener('click', () => {
        const panel = document.querySelector('[data-project-workspace]');
        setPanelHidden('project', !panel?.classList.contains('is-hidden'));
      });
    }

    if (consoleToggle) {
      consoleToggle.addEventListener('click', () => {
        const panel = document.querySelector('[data-agent-console]');
        setAgentConsoleCollapsed(!panel?.classList.contains('is-collapsed'));
      });
    }

    if (resizer) {
      let dragging = false;
      const applyConsoleWidth = nextWidth => {
        if (nextWidth <= 24) {
          app.style.setProperty('--right-panel-width', '0px');
          setAgentConsoleCollapsed(true);
          resizer.setAttribute('aria-valuenow', '0');
          try { localStorage.setItem('helm.agentConsoleWidth', '0'); } catch (_) {}
          return;
        }
        if (app.classList.contains('console-collapsed')) setAgentConsoleCollapsed(false);
        const visibleWidth = Math.max(260, Math.min(620, nextWidth));
        app.style.setProperty('--right-panel-width', `${visibleWidth}px`);
        resizer.setAttribute('aria-valuenow', String(visibleWidth));
        try { localStorage.setItem('helm.agentConsoleWidth', String(visibleWidth)); } catch (_) {}
      };
      const updateWidth = event => {
        if (!dragging) return;
        applyConsoleWidth(window.innerWidth - event.clientX - 26);
      };
      const currentWidth = () => parseInt(getComputedStyle(app).getPropertyValue('--right-panel-width'), 10) || 0;
      resizer.setAttribute('aria-valuenow', String(currentWidth()));
      resizer.addEventListener('keydown', event => {
        const step = event.shiftKey ? 48 : 24;
        if (event.key === 'ArrowLeft') {
          applyConsoleWidth(currentWidth() + step);
          event.preventDefault();
        } else if (event.key === 'ArrowRight') {
          applyConsoleWidth(currentWidth() - step);
          event.preventDefault();
        } else if (event.key === 'Home') {
          applyConsoleWidth(0);
          event.preventDefault();
        } else if (event.key === 'End') {
          applyConsoleWidth(430);
          event.preventDefault();
        }
      });
      resizer.addEventListener('pointerdown', event => {
        dragging = true;
        resizer.classList.add('is-dragging');
        resizer.setPointerCapture?.(event.pointerId);
        event.preventDefault();
      });
      window.addEventListener('pointermove', updateWidth);
      window.addEventListener('pointerup', () => {
        dragging = false;
        resizer.classList.remove('is-dragging');
      });
    }
  }

  async function applyEnvBadge() {
    // Add an environment marker when the service reports a non-default label.
    // Fail softly so a missing health field never blocks the UI from booting.
    let info = null;
    try {
      const response = await fetch('/health');
      if (!response.ok) return;
      info = await response.json();
    } catch (err) {
      return;
    }
    if (!info || !info.env || String(info.env).toLowerCase() === 'live') return;

    const brand = document.querySelector('.brand');
    if (!brand || brand.querySelector('[data-env-badge]')) return;

    const badge = el('span', String(info.label || info.env).toUpperCase());
    badge.setAttribute('data-env-badge', String(info.env));
    badge.className = 'env-badge';
    badge.title = `Environment: ${info.label || info.env}`;
    brand.appendChild(badge);
    document.body.setAttribute('data-helm-env', String(info.env));
  }

  document.addEventListener('DOMContentLoaded', () => {
    // Mark non-default environments clearly so operators can distinguish deployments.
    if (window.location.protocol !== 'file:') {
      applyEnvBadge().catch(() => {});
    }

    // Pridaj click handlery na mode buttony
    const modeMap = { 'Učenie': 'ucenie', 'Práca': 'praca', 'Sideprojekt': 'sideprojekt', 'LEARN': 'ucenie', 'WORK': 'praca', 'SIDE': 'sideprojekt' };
    document.querySelectorAll('.mode').forEach(btn => {
      btn.addEventListener('click', () => {
        const mode = modeMap[btn.textContent.trim()];
        if (!mode) return;
        setMode(mode);
        if (window.location.protocol !== 'file:') hydrateState(mode).catch(() => {});
      });
    });

    const newProjectButton = document.querySelector('[data-new-project]');
    if (newProjectButton) newProjectButton.addEventListener('click', openProjectModal);
    const newChatButton = document.querySelector('[data-new-chat]');
    if (newChatButton) newChatButton.addEventListener('click', createNewChat);

    const projectModalClose = document.querySelector('[data-project-modal-close]');
    if (projectModalClose) projectModalClose.addEventListener('click', closeProjectModal);
    const projectModalOverlay = document.querySelector('[data-project-modal]');
    if (projectModalOverlay) {
      projectModalOverlay.addEventListener('click', event => {
        if (event.target === projectModalOverlay) closeProjectModal();
      });
    }
    const projectChoiceNew = document.querySelector('[data-project-choice-new]');
    if (projectChoiceNew) projectChoiceNew.addEventListener('click', () => {
      closeProjectModal();
      createNewProject();
    });
    const projectChoiceOpen = document.querySelector('[data-project-choice-open]');
    if (projectChoiceOpen) projectChoiceOpen.addEventListener('click', showDirectoryBrowser);
    const browserUp = document.querySelector('[data-browser-up]');
    if (browserUp) browserUp.addEventListener('click', () => browserGoUp());
    const browserOpen = document.querySelector('[data-browser-open]');
    if (browserOpen) browserOpen.addEventListener('click', () => openSelectedFolder().catch(() => {}));

    const saveContextButton = document.querySelector('[data-save-project-context]');
    if (saveContextButton) saveContextButton.addEventListener('click', () => saveProjectContext().catch(() => {}));


    const telegramCaptureButton = document.querySelector('[data-telegram-capture-submit]');
    if (telegramCaptureButton) telegramCaptureButton.addEventListener('click', () => simulateTelegramIngestFromUi().catch(() => {}));
    const telegramCaptureInput = document.querySelector('[data-telegram-capture-input]');
    if (telegramCaptureInput) {
      telegramCaptureInput.addEventListener('keydown', event => {
        if (event.key === 'Enter') simulateTelegramIngestFromUi().catch(() => {});
      });
    }
    const telegramRefreshButton = document.querySelector('[data-refresh-telegram-sync]');
    if (telegramRefreshButton) telegramRefreshButton.addEventListener('click', () => refreshTelegramSyncPanel().catch(() => renderTelegramSyncPanel(null)));
    const telegramReportButton = document.querySelector('[data-telegram-prepare-report]');
    if (telegramReportButton) telegramReportButton.addEventListener('click', () => prepareTelegramReportOutboxFromUi().catch(() => {}));

    const createGoalButton = document.querySelector('[data-create-project-goal]');
    if (createGoalButton) createGoalButton.addEventListener('click', () => createProjectGoalFromUi().catch(() => {}));
    const newGoalInput = document.querySelector('[data-new-goal-title]');
    if (newGoalInput) {
      newGoalInput.addEventListener('keydown', event => {
        if (event.key === 'Enter') createProjectGoalFromUi().catch(() => {});
      });
    }

    const sendButton = document.querySelector('[data-send-run]');
    if (sendButton) sendButton.addEventListener('click', createMessageAndRun);    // M21: the MAP-tab tree refresh button is gone; the sidebar's `rescan map`
    // button now re-renders the folder tree.
    document.querySelectorAll('[data-console-mode]').forEach(button => {
      button.addEventListener('click', () => setConsoleMode(button.dataset.consoleMode));
    });
    document.querySelectorAll('[data-console-activity-tab]').forEach(button => {
      button.addEventListener('click', () => setConsoleActivityTab(button.dataset.consoleActivityTab));
    });
    const closeActivityButton = document.querySelector('[data-close-activity-detail]');
    if (closeActivityButton) closeActivityButton.addEventListener('click', closeActivityDetail);
    applyConsoleMode();    document.querySelectorAll('[data-theme-select]').forEach(themeSelect => {
      themeSelect.addEventListener('change', event => updateTheme(event.target.value).catch(error => renderNoActiveRun(error.message || 'Theme update zlyhal.')));
    });
    document.querySelectorAll('[data-workspace-tab]').forEach(tab => {
      tab.addEventListener('click', () => setWorkspaceView(tab.dataset.workspaceTab));
    });
    const settingsOpen = document.querySelector('[data-settings-open]');
    if (settingsOpen) settingsOpen.addEventListener('click', () => setSettingsOpen(true));
    const settingsClose = document.querySelector('[data-settings-close]');
    if (settingsClose) settingsClose.addEventListener('click', () => setSettingsOpen(false));
    const settingsOverlay = document.querySelector('[data-settings-overlay]');
    if (settingsOverlay) settingsOverlay.addEventListener('click', event => {
      if (event.target === settingsOverlay) setSettingsOpen(false);
    });
    const mapExpand = document.querySelector('[data-map-expand]');
    if (mapExpand) mapExpand.addEventListener('click', () => renderNoActiveRun('MAP preview je pripravený. Funkčný codebase map explorer bude ďalší krok.'));
    const rescanMap = document.querySelector('[data-rescan-map]');
    if (rescanMap) rescanMap.addEventListener('click', () => renderFolderTree(null, activeProject?.id).catch(() => {}));
    const openVscode = document.querySelector('[data-open-vscode]');
    if (openVscode) {
      openVscode.addEventListener('click', () => {
        const folderPath = document.querySelector('[data-folder-path]');
        const fullPath = folderPath?.dataset?.fullPath || folderPath?.textContent || '';
        if (fullPath) window.open(`vscode://file/${fullPath}`, '_blank');
      });
    }
    const folderPath = document.querySelector('[data-folder-path]');
    if (folderPath) {
      folderPath.addEventListener('click', () => {
        const fullPath = folderPath.dataset.fullPath || folderPath.textContent;
        navigator.clipboard.writeText(fullPath).then(() => {
          folderPath.classList.add('is-copied');
          const original = folderPath.textContent;
          folderPath.textContent = '✓ skopírované';
          setTimeout(() => {
            folderPath.classList.remove('is-copied');
            folderPath.textContent = original;
          }, 1200);
        }).catch(() => {});
      });
    }
    applyConsoleMode();
    initLayoutControls();
    const promptInput = document.querySelector('[data-prompt-input]');
    if (promptInput) {
      const autoGrow = () => {
        promptInput.style.height = 'auto';
        promptInput.style.height = Math.min(promptInput.scrollHeight, 200) + 'px';
      };
      promptInput.addEventListener('input', autoGrow);
      promptInput.addEventListener('input', () => scheduleTierPreview(promptInput.value));
      promptInput.addEventListener('keydown', event => {
        if (event.key === 'Enter' && !event.shiftKey) {
          event.preventDefault();
          createMessageAndRun();
          if (promptInput) promptInput.style.height = 'auto';
        }
      });
      autoGrow();
      scheduleTierPreview(promptInput.value);
    }
    initTierBadge();

    // Nastav default a skus hydrataciu cez API. Pri file:// zostane staticky fallback.
    applyTheme(localStorage.getItem('helm.themeId') || 'helm-default');
    setWorkspaceView('chat');
    setMode('sideprojekt');
    if (window.location.protocol !== 'file:') {
      hydrateState('sideprojekt').catch(() => {});
      hydrateWorkspaceTree().catch(() => {});
      renderFolderTree().catch(() => {});
      refreshTelegramSyncPanel().catch(() => {});
      refreshSettings().catch(() => {});
    } else {
      refreshSettings().catch(() => {});
      renderRunEvents([]);
    }
    if (new URLSearchParams(window.location.search).has('consoleDemo')) renderRunEvents([]);
  });

  // ── M24: SSE client + browser notifications ──────────────────────────
  let sseEventSource = null;
  let sseReconnectTimer = null;

  function setupNotifications() {
    if (!('Notification' in window)) return;
    if (Notification.permission === 'default') {
      // Ask on next user interaction to avoid annoying prompt
      document.addEventListener('click', function askOnce() {
        Notification.requestPermission().catch(() => {});
        document.removeEventListener('click', askOnce);
      }, { once: true });
    }
  }

  function showBrowserNotification(title, body, tag) {
    if (!('Notification' in window) || Notification.permission !== 'granted') return;
    try {
      new Notification(title, { body, tag, silent: false });
    } catch (_) { /* notification failed — ignore */ }
  }

  function connectSSE() {
    if (window.location.protocol === 'file:') return;
    if (sseEventSource) sseEventSource.close();

    const es = new EventSource('/api/events/stream');
    sseEventSource = es;

    es.addEventListener('connected', () => {
      console.log('[SSE] connected');
      const dot = document.querySelector('[data-console-status-dot]');
      if (dot) dot.classList.add('is-live');
    });

    es.addEventListener('run_completed', (e) => {
      const data = JSON.parse(e.data);
      showBrowserNotification(
        `Run #${data.run_id} dokončený`,
        data.reason || 'Hotovo',
        `run-${data.run_id}`
      );
      // Refresh UI if this is the active run
      if (lastConsoleRun && lastConsoleRun.id === data.run_id) {
        refreshTerminalOutput().catch(() => {});
      }
    });

    es.addEventListener('run_failed', (e) => {
      const data = JSON.parse(e.data);
      showBrowserNotification(
        `Run #${data.run_id} zlyhal`,
        data.error || 'Chyba',
        `run-${data.run_id}`
      );
      if (lastConsoleRun && lastConsoleRun.id === data.run_id) {
        refreshTerminalOutput().catch(() => {});
      }
    });

    es.addEventListener('run_needs_attention', (e) => {
      const data = JSON.parse(e.data);
      showBrowserNotification(
        `Run #${data.run_id} vyžaduje pozornosť`,
        data.reason || 'Potrebné schválenie',
        `run-${data.run_id}`
      );
    });

    es.addEventListener('run_waiting_for_tokens', (e) => {
      const data = JSON.parse(e.data);
      // Don't notify for this — it's expected, just update UI
      if (lastConsoleRun && lastConsoleRun.id === data.run_id) {
      }
    });

    es.onerror = () => {
      // EventSource auto-reconnects, but update the status dot
      const dot = document.querySelector('[data-console-status-dot]');
      if (dot) dot.classList.remove('is-live');
    };
  }

  // Initialize on load
  if (window.location.protocol !== 'file:') {
    setupNotifications();
    // Small delay to let hydration finish
    setTimeout(connectSSE, 1000);
  }

  // ── M24: Mobile console toggle ────────────────────────────────────────
  document.addEventListener('click', (e) => {
    const toggle = e.target.closest('[data-mobile-console-toggle]');
    if (toggle) {
      const right = document.querySelector('.right');
      if (right) right.classList.toggle('is-open');
    }
  });
