/* Settings and lifecycle for capability services owned by the V2 plugin. */
"use strict";
window.CompanionNativeExtensionPanel = (() => {
  const names = {reality:"现实触及", together:"一起房间", game:"游戏棋桌", live:"直播与表演"};
  const descriptions = {
    reality:"连接手机、摄像头和家居设备；在当前会话中按授权使用。",
    together:"配置实时房间、语音、一起看和一起阅读。",
    game:"管理聊天内游戏、网页房间与棋类引擎。",
    live:"连接 VTube Studio、直播间、OBS 和字幕，调整角色的表演。",
  };
  /* mobile config groups */
  const MOBILE_SETTING_GROUPS = [
    {id:"gateway", title:"网关与访问", summary:"监听地址、端口、外部地址", keys:["enabled","host","port","public_base_url"]},
    {id:"pairing", title:"配对与身份", summary:"配对令牌、绑定用户与会话有效期", keys:["pairing_token","allowed_user_id","session_ttl_hours"]},
    {id:"location", title:"位置与地图", summary:"位置有效期、高德逆地理编码与请求参数", keys:["location_ttl_seconds","amap_reverse_geocode_enabled","amap_api_key","amap_cache_ttl_seconds","amap_request_timeout_seconds"]},
    {id:"activity", title:"前台活动摘要", summary:"前台活动上报与缓存时效", keys:["activity_enabled","activity_ttl_seconds"]},
    {id:"rooms", title:"房间与屏幕", summary:"统一房间代理与屏幕共享状态", keys:["proxy_rooms","screen_upload_enabled"]},
  ];
  function mobileSettingGroups(field) {
    /* Health ingestion is owned by the single switch under Perception. */
    const managedByPerception = new Set(["telemetry_enabled", "telemetry_ttl_seconds"]);
    const entries = Object.entries(field?.items || {}).filter(([key]) => !managedByPerception.has(key));
    const used = new Set();
    const groups = MOBILE_SETTING_GROUPS.map(group => {
      const fields = entries.filter(([key]) => group.keys.includes(key));
      fields.forEach(([key]) => used.add(key));
      return {...group, fields};
    }).filter(group => group.fields.length);
    const rest = entries.filter(([key]) => !used.has(key));
    if (rest.length) groups.push({id:"more", title:"更多设置", summary:"其他手机连接选项", fields:rest});
    return groups;
  }
  const sessions = new Map(), operations = new Map();
  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  const secret = name => /token|secret|password|cookie|api_key|access_key|sessdata|auth_code/i.test(name);
  const keyOf = path => JSON.stringify(path);
  const at = (value, path) => path.reduce((item, key) => item?.[key], value);
  function put(value, path, item) {
    let target = value;
    path.forEach((key, index) => {
      if (index === path.length - 1) target[key] = item;
      else target = target[key] ??= typeof path[index + 1] === "number" ? [] : {};
    });
  }
  function merge(base, updates) {
    for (const [key, value] of Object.entries(updates)) {
      if (value && typeof value === "object" && !Array.isArray(value)) base[key] = merge(base[key] || {}, value);
      else base[key] = clone(value);
    }
    return base;
  }
  function defaults(schema) {
    return Object.fromEntries(Object.entries(schema).map(([key, field]) => [key,
      field.type === "object" ? defaults(field.items || {}) : clone(field.default ??
        ({bool:false, int:0, float:0, list:[], template_list:[]}[field.type] ?? ""))]));
  }
  function label(field, key) { return field.description || field.title || key; }
  function groupsFor(name, schema) {
    const groups = new Map();
    const liveGroup = key => {
      if (/^subtitle_/.test(key)) return ["subtitle", "字幕与显示"];
      if (/^mouth_/.test(key)) return ["mouth", "语音嘴型"];
      if (/^soullink_/.test(key)) return ["soullink", "实时表演"];
      if (/^(autonomous_l2d|l2d_)/.test(key)) return ["l2d", "Live2D 表情"];
      if (/^twitch_/.test(key)) return ["twitch", "Twitch 直播"];
      if (/^(obs_|l2dstudio_)/.test(key)) return ["obs", "OBS 与推流"];
      if (/^(private_companion_|live_memory_)/.test(key)) return ["memory", "关系与直播记忆"];
      if (/^(bilibili_|bili_live_|laplace_|part_id$|area_id$)/.test(key)) return ["bilibili", "B站直播"];
      return ["connection", "连接与基本设置"];
    };
    for (const [key, field] of Object.entries(schema)) {
      const [id, title] = field.type === "object" ? [key, label(field, key)]
        : name === "live" ? liveGroup(key) : ["general", "基本设置"];
      if (!groups.has(id)) groups.set(id, {id, title, fields:[], hint:field.type === "object" ? field.hint : ""});
      groups.get(id).fields.push([key, field]);
    }
    if (name === "game" && groups.has("native_v2")) {
      const native = groups.get("native_v2");
      groups.delete("native_v2");
      return [native, ...groups.values()];
    }
    if (name === "live" && groups.has("connection")) {
      const connection = groups.get("connection");
      groups.delete("connection");
      return [connection, ...groups.values()];
    }
    return [...groups.values()];
  }
  function errorText(error) {
    const messages = {
      native_settings_revision_conflict:"设置已在其他窗口更新。请重新读取后再保存。",
      native_settings_conflict:"设置已在其他窗口更新。请重新读取后再保存。",
      native_settings_apply_failed:"设置已保存，功能重启失败。请检查连接设置并重新读取状态。",
      native_settings_unavailable:"此模块尚未加载，请确认 V2 已启用后重新读取。",
      capability_revision_conflict:"模块状态已经变化，请重新读取后再操作。",
    };
    return messages[error?.code] || window.CompanionProduct?.errorLabel?.(error?.code)
      || "操作没有完成，请重新读取状态后再试。";
  }
  function runtimeCopy(record, extension) {
    if (extension?.desired_state === "paused") return ["已暂停", "idle"];
    if (extension?.desired_state === "discarded" || extension?.assembled === false) return ["未装配", "idle"];
    const state = record?.runtime?.state;
    return ({running:["运行中", "ready"], starting:["正在启动", "pending"],
      inactive:["未运行", "idle"], failed:["启动失败", "error"]})[state]
      || [extension?.runtime_ready === true ? "已就绪" : "状态待读取", "idle"];
  }
  function validRecord(record) {
    return record && typeof record.settings === "object" && record.settings !== null
      && typeof record.schema === "object" && record.schema !== null && typeof record.revision === "string";
  }
  class Panel {
    constructor(root, name, options) {
      this.root = root; this.name = name; this.options = options;
      this.alive = true; this.ticket = 0; this.busy = false; this.dirty = false;
      this.mobileTokenNotice = "";
      this.mobileOpen = new Set(["gateway"]);
      this.group = ""; this.query = ""; this.record = null; this.fields = [];
      /* 只挂一组时（手机连接页复用现实触及的 mobile 分组），不显示分组栏，
         否则同一份设置会出现两条导航。 */
      this.onlyGroup = typeof options?.onlyGroup === "string" ? options.onlyGroup : "";
      this.click = event => this.onClick(event);
      this.input = event => this.onInput(event);
      this.toggle = event => {
        const section = event.target.closest?.("[data-mobile-config-section]");
        if (!section) return;
        if (section.open) this.mobileOpen.add(section.dataset.mobileConfigSection);
        else this.mobileOpen.delete(section.dataset.mobileConfigSection);
      };
      this.submit = event => { event.preventDefault(); this.save(); };
      root.addEventListener("click", this.click);
      root.addEventListener("input", this.input);
      root.addEventListener("change", this.input);
      root.addEventListener("toggle", this.toggle, true);
      root.addEventListener("submit", this.submit);
      this.load();
    }
    current(ticket = this.ticket) { return this.alive && this.root.isConnected !== false && this.ticket === ticket; }
    stop() {
      this.alive = false; this.ticket++;
      this.management?.stop();
      clearTimeout(this.statusTimer); this.statusTimer = null;
      this.root.removeEventListener("click", this.click);
      this.root.removeEventListener("input", this.input);
      this.root.removeEventListener("change", this.input);
      this.root.removeEventListener("toggle", this.toggle, true);
      this.root.removeEventListener("submit", this.submit);
    }
    async load() {
      if (this.busy) return;
      this.managementState = null;
      clearTimeout(this.statusTimer); this.statusTimer = null; this.statusReads = 0;
      const ticket = ++this.ticket;
      this.root.innerHTML = `<div class="native-extension-empty" role="status"><span class="native-extension-loading"></span>正在读取${escape(names[this.name])}设置…</div>`;
      try {
        const result = await window.CompanionProduct.request("GET", "extensions");
        if (!this.current(ticket)) return;
        const record = result?.extensions?.[this.name];
        if (!validRecord(record)) throw {code:"native_settings_unavailable"};
        this.record = record; this.draft = clone(record.settings); this.dirty = false;
        this.render();
      } catch (error) {
        if (!this.current(ticket)) return;
        this.root.innerHTML = `<div class="native-extension-empty"><strong>暂时读取不到设置</strong><p role="status">${escape(errorText(error))}</p><button class="button small" type="button" data-native-reload>重新读取</button></div>`;
      }
    }
    field(key, field, value, path, parent) {
      const title = label(field, key), kind = field.type, hint = field.hint || "";
      if (kind === "object") return `<fieldset class="native-extension-nested"><legend>${escape(title)}</legend>${hint ? `<p class="native-extension-hint">${escape(hint)}</p>` : ""}${Object.entries(field.items || {}).map(([child, descriptor]) =>
        this.field(child, descriptor, value?.[child], [...path, child], parent)).join("")}</fieldset>`;
      if (kind === "template_list") {
        const templateNames = Object.keys(field.templates || {}), rows = Array.isArray(value) ? value : [];
        const templateSelector = templateNames.length > 1 ? `<select aria-label="新增条目类型" data-native-template-kind="${escape(keyOf(path))}">${templateNames.map(name => `<option value="${escape(name)}">${escape(field.templates[name].name || name)}</option>`).join("")}</select>` : "";
        return `<fieldset class="native-extension-templates" data-native-field-label="${escape(title)}"><legend>${escape(title)}</legend>${hint ? `<p class="native-extension-hint">${escape(hint)}</p>` : ""}<div data-native-template-list="${escape(keyOf(path))}">${rows.map((row, index) => {
          const templateName = field.templates?.[row.__template_key] ? row.__template_key : templateNames[0];
          const template = field.templates?.[templateName] || {items:{}};
          return `<section class="native-extension-template"><header><strong>${escape(row.name || template.name || "条目")} <span>${index + 1}</span></strong><button class="button small" type="button" data-native-remove="${escape(keyOf([...path, index]))}" aria-label="移除第 ${index + 1} 个${escape(title)}">移除</button></header>${Object.entries(template.items || {}).map(([child, descriptor]) =>
            this.field(child, descriptor, row[child] ?? descriptor.default, [...path, index, child], parent)).join("")}</section>`;
        }).join("") || `<p class="native-extension-hint">还没有条目，添加后即可逐项配置。</p>`}</div><div class="native-extension-template-actions">${templateSelector}<button class="button small" type="button" data-native-add="${escape(keyOf(path))}">添加${escape(title)}</button></div></fieldset>`;
      }
      const index = this.fields.length, id = `native-${this.name}-${this.instance}-${index}`;
      const isSecret = secret(key), resolved = value ?? clone(field.default);
      this.fields.push({path, field, key, parent, secret:isSecret, id, original:resolved});
      const attrs = `id="${id}" data-native-field="${index}" aria-describedby="${id}-hint"`;
      let control;
      if (kind === "bool") control = `<input type="checkbox" ${attrs} ${resolved === true ? "checked" : ""}><span class="native-extension-switch-track" aria-hidden="true"></span>`;
      else if (Array.isArray(field.options)) control = `<select ${attrs}>${field.options.map((option, i) => `<option value="${i}" ${option === resolved ? "selected" : ""}>${escape(field.labels?.[i] || option)}</option>`).join("")}</select>`;
      else if (kind === "list") control = `<textarea ${attrs} rows="3" spellcheck="false">${escape(Array.isArray(resolved) ? resolved.join("\n") : "")}</textarea>`;
      else if (kind === "text" && !isSecret) control = `<textarea ${attrs} rows="${String(resolved || "").length > 200 ? 5 : 3}">${escape(resolved || "")}</textarea>`;
      else if (["int", "float"].includes(kind)) {
        const bounds = field.slider || field;
        control = `<input type="number" ${attrs} value="${escape(resolved ?? "")}" step="${escape(bounds.step ?? (kind === "int" ? 1 : "any"))}"${Number.isFinite(bounds.min) ? ` min="${bounds.min}"` : ""}${Number.isFinite(bounds.max) ? ` max="${bounds.max}"` : ""}>`;
      } else control = `<input type="${isSecret ? "password" : "text"}" ${attrs} value="${isSecret ? "" : escape(resolved || "")}" ${isSecret ? `autocomplete="new-password" placeholder="${this.isConfigured(path, resolved) ? "已配置，留空保持不变" : "尚未配置"}"` : ""} spellcheck="false">`;
      const notes = [hint, kind === "list" ? "每行一项；清空可移除所有条目。" : "", isSecret ? "留空保留已有密钥，填写新值才会替换。" : ""].filter(Boolean).join(" ");
      return `<div class="native-extension-field ${kind === "bool" ? "is-switch" : ""}" data-native-field-label="${escape(title)}"><label for="${id}">${escape(title)}</label>${kind === "bool" ? `<label class="native-extension-switch" for="${id}" aria-label="${escape(title)}">${control}</label>` : control}<small id="${id}-hint">${escape(notes)}</small></div>`;
    }
    isConfigured(path, value) {
      return at(this.record.settings, [...path.slice(0, -1), path.at(-1) + "_configured"]) === true || Boolean(value);
    }
    render() {
      this.management?.stop();
      this.fields = []; this.instance = ++instance;
      this.groups = groupsFor(this.name, this.record.schema);
      if (this.onlyGroup) this.groups = this.groups.filter(group => group.id === this.onlyGroup);
      if (!this.groups.some(group => group.id === this.group)) this.group = this.groups[0]?.id || "";
      const controls = this.groups.map(group => {
        const content = group.fields.map(([key, field]) => {
          if (field.type === "object" && this.onlyGroup === "mobile" && group.id === "mobile") {
            return this.mobileConfiguration(key, field, this.draft[key], group.id);
          }
          return field.type === "object"
            ? Object.entries(field.items || {}).map(([child, descriptor]) => this.field(child, descriptor, this.draft[key]?.[child], [key, child], group.id)).join("")
            : this.field(key, field, this.draft[key], [key], group.id);
        }).join("");
        return `<section class="native-extension-group" data-native-group-panel="${escape(group.id)}" ${group.id !== this.group ? "hidden" : ""}><h3>${escape(group.title)}</h3>${group.hint ? `<p class="native-extension-hint">${escape(group.hint)}</p>` : ""}${content}</section>`;
      }).join("");
      const mobile = this.onlyGroup === "mobile" ? this.mobileConnectionCard() : "";
      this.root.innerHTML = `<div class="native-extension-panel">${mobile}<header class="native-extension-head"><div><span class="native-extension-kicker">V2 内置能力</span><h3>${escape(names[this.name])}</h3><p>${escape(descriptions[this.name])}</p></div><div data-native-runtime></div></header><div class="native-extension-lifecycle" data-native-lifecycle></div>${["reality", "live"].includes(this.name) ? '<div data-native-management></div>' : ""}<form data-native-form><label class="native-extension-search"><span class="sr-only">查找设置项</span><input type="search" placeholder="${this.onlyGroup ? "查找手机连接设置项" : "查找设置项"}" aria-label="${this.onlyGroup ? "查找手机连接设置项" : "查找" + escape(names[this.name]) + "设置项"}" data-native-search value="${escape(this.query)}"></label><div class="native-extension-workspace${this.onlyGroup ? " is-single" : ""}">${this.onlyGroup ? "" : `<nav class="native-extension-nav" aria-label="${escape(names[this.name])}设置分组">${this.groups.map(group => `<button type="button" data-native-group="${escape(group.id)}" aria-pressed="${group.id === this.group}">${escape(group.title)}</button>`).join("")}</nav>`}<div class="native-extension-settings">${controls}<p class="native-extension-no-results" hidden>没有匹配的设置项，换个关键词试试。</p></div></div><footer class="native-extension-savebar"><p role="status" aria-live="polite" data-native-notice></p><div><button class="button small" type="button" data-native-reload>重新读取</button><button class="button small primary" type="submit" data-native-save ${this.dirty ? "" : "disabled"}>保存并应用</button></div></footer></form></div>`;
      const managementRoot = this.root.querySelector("[data-native-management]");
      this.management = managementRoot ? window.CompanionNativeExtensionManagement?.mount(managementRoot, {
        extensionId:this.name, record:this.record, instance:this.instance,
        onRead:record => { this.record.management = record.management; this.record.runtime = record.runtime; this.updateStatus(); },
        onRecord:record => this.receiveManagement(record),
        onBusy:value => { this.actionBusy = value; this.updateStatus(); this.updateDirty(); },
        getState:() => this.managementState,
        onState:state => { this.managementState = state; },
        onUseHotkey:hotkey => this.useHotkey(hotkey),
      }) : null;
      this.filter(); this.setBusy(this.busy);
    }
    mobileConfiguration(key, field, value, parent) {
      const groups = mobileSettingGroups(field);
      return `<div class="mobile-config-sections">${groups.map(group => {
        const open = this.mobileOpen.has(group.id);
        const body = group.fields.map(([child, descriptor]) =>
          this.field(child, descriptor, value?.[child], [key, child], parent)).join("");
        return `<details class="mobile-config-section" data-mobile-config-section="${escape(group.id)}" ${open ? "open" : ""}>`
          + `<summary><span><strong>${escape(group.title)}</strong><small>${escape(group.summary)}</small></span>`
          + `<span class="mobile-config-chevron" aria-hidden="true"></span></summary>`
          + `<div class="mobile-config-section-body">${body}</div></details>`;
      }).join("")}</div>`;
    }
    mobileConnectionCard() {
      const mobile = this.record?.settings?.mobile || {};
      const runtimeMobile = this.record?.management?.configuration?.mobile || {};
      const statusMobile = {...mobile, ...runtimeMobile};
      const host = String(statusMobile.host || "").trim();
      const port = Number(statusMobile.port || 6322);
      const configured = statusMobile.pairing_token_configured === true;
      const running = statusMobile.running === true;
      const bound = Number(statusMobile.bound_port || port);
      const address = String(statusMobile.public_base_url || "").trim() || (host && host !== "0.0.0.0" ? `http://${host}:${bound}` : "请填写电脑内网或组网地址");
      const state = !statusMobile.enabled ? ["未启用", "idle"] : !configured ? ["缺少配对令牌", "error"] : running ? ["网关运行中", "ready"] : ["等待启动", "pending"];
      const issued = this.mobileTokenNotice && typeof this.mobileTokenNotice === "object" ? this.mobileTokenNotice : null;
      const notice = issued ? (issued.running ? "网关已启动。请复制令牌，在手机端完成配对。" : "令牌已保存，但网关未能启动；请检查端口占用和模块状态。")
        : configured ? "令牌不会回显；需要新手机或令牌泄露时轮换。" : "生成令牌会同时启用手机网关。";
      const token = issued?.token ? `<div class="mobile-pairing-token"><input type="text" readonly aria-label="本次生成的手机配对令牌" value="${escape(issued.token)}"><button class="button small" type="button" data-mobile-copy-token>复制令牌</button></div>` : "";
      const action = configured
        ? `<button class="button small" type="button" data-mobile-token-action="rotate">轮换配对令牌</button>`
        : `<button class="button small primary" type="button" data-mobile-token-action="generate">生成并启用网关</button>`;
      const healthAddress = address === "请填写电脑内网或组网地址" ? "填写电脑内网 IP 后访问 /health" : `${address}/health`;
      return `<section class="mobile-gateway-card" aria-labelledby="mobileGatewayTitle">
        <header class="mobile-gateway-head">
          <div><span class="native-extension-kicker">手机陪伴终端</span><h3 id="mobileGatewayTitle">连接概览</h3><p>Android 会先检查健康地址，再用配对令牌换取本机加密会话。</p></div>
          <span class="native-extension-status ${state[1]}">${escape(state[0])}</span>
        </header>
        <div class="mobile-gateway-status" aria-label="手机网关状态">
          <dl class="mobile-gateway-facts">
            <div><dt>连接地址</dt><dd>${escape(address)}</dd></div>
            <div><dt>健康检查</dt><dd>${escape(healthAddress)}</dd></div>
            <div><dt>监听端口</dt><dd>${escape(String(bound))}</dd></div>
            <div><dt>绑定用户</dt><dd>${escape(String(statusMobile.allowed_user_id || "未明确绑定，需恰好一个 V2 私聊用户"))}</dd></div>
          </dl>
        </div>
        <div class="mobile-gateway-actionbar"><div class="mobile-gateway-actions">${action}</div><p class="native-extension-hint" data-mobile-token-result role="status" aria-live="polite">${escape(notice)}</p></div>
        ${token}
      </section>`;
    }
    receiveManagement(record) {
      if (!validRecord(record)) return;
      const changed = JSON.stringify(this.record.settings) !== JSON.stringify(record.settings);
      let edits = {};
      try { edits = this.changes(this.collect()); } catch (_) {}
      this.record = record;
      if (changed) {
        this.draft = merge(clone(record.settings), edits); this.actionBusy = false;
        this.render(); this.notice("管理操作已完成，未保存的设置修改已保留。");
      } else { this.updateStatus(); this.updateDirty(); }
    }
    useHotkey(hotkey) {
      const id = hotkey?.hotkeyID || hotkey?.hotkey_id || hotkey?.id;
      if (!id || !this.record.schema.l2d_hotkeys) return;
      try { this.draft = this.collect(); }
      catch (error) { this.notice(error.message, true); error.field?.focus(); return; }
      const field = this.record.schema.l2d_hotkeys, template = Object.keys(field.templates)[0];
      const rows = this.draft.l2d_hotkeys || [];
      if (!rows.some(row => row.hotkey_id === id)) rows.push({...defaults(field.templates[template].items),
        __template_key:template, name:hotkey.name || "表情", tag:hotkey.name || "表情", hotkey_id:id});
      this.draft.l2d_hotkeys = rows; this.group = "l2d"; this.query = "";
      this.render(); this.notice("热键已填入表情配置，保存后生效。");
    }
    updateStatus() {
      const product = window.CompanionProduct;
      const extension = product.extension?.(this.name) || product.capability?.(this.name)
        || product.provider?.(this.name);
      const [text, tone] = runtimeCopy(this.record, extension);
      const status = this.root.querySelector("[data-native-runtime]");
      if (status) status.innerHTML = `<span class="native-extension-status ${tone}">${escape(text)}</span>`;
      const lifecycle = this.root.querySelector("[data-native-lifecycle]");
      if (!lifecycle) return;
      const assembled = extension?.assembled === true && extension.desired_state !== "discarded";
      const paused = extension?.desired_state === "paused";
      const command = (action, caption) => `<button class="button small" type="button" data-native-lifecycle-action="${action}" ${this.busy || this.actionBusy ? "disabled" : ""}>${caption}</button>`;
      const buttons = extension?.owner === "companion_v2" ? (assembled
        ? (paused ? command("resume", "启用模块") : command("pause", "暂停模块")) + (extension.update_available ? command("update", "应用内置更新") : "")
        : command("assemble", "启用模块")) : "";
      const note = this.record.runtime?.state === "failed"
        ? "启动失败，请检查连接配置后重新启用模块。"
        : paused ? "模块已暂停，可继续编辑设置；保存后保持暂停。" : "模块启停作用于整个能力；下方开关控制各项功能。";
      lifecycle.innerHTML = `<p>${escape(note)}</p><div>${buttons}</div>`;
      if (this.record.runtime?.state === "starting" && !this.statusTimer && (this.statusReads || 0) < 20) {
        const ticket = this.ticket;
        this.statusTimer = setTimeout(async () => {
          this.statusTimer = null;
          this.statusReads = (this.statusReads || 0) + 1;
          try {
            const result = await window.CompanionProduct.request("GET", "extensions");
            if (!this.current(ticket)) return;
            const record = result?.extensions?.[this.name];
            if (!validRecord(record)) return;
            this.record.runtime = record.runtime;
            if (record.runtime?.state === "failed") this.notice("模块启动失败，请检查连接设置。", true);
            if (record.runtime?.state !== "starting") {
              try { await window.CompanionProduct.refreshIntegrations?.(); } catch (_) {}
            }
            if (this.current(ticket)) this.updateStatus();
          } catch (_) {
            if (this.current(ticket)) this.notice("运行状态暂时读取不到，可点击重新读取。", true);
          }
        }, 1500);
      } else if (this.record.runtime?.state !== "starting") {
        clearTimeout(this.statusTimer); this.statusTimer = null;
      }
    }
    collect() {
      const value = clone(this.draft);
      for (const item of this.fields) {
        const node = this.root.querySelector(`#${item.id}`);
        if (!node) continue;
        let result;
        if (item.secret && !node.value) continue;
        if (item.field.type === "bool") result = node.checked;
        else if (Array.isArray(item.field.options)) result = item.field.options[Number(node.value)];
        else if (["int", "float"].includes(item.field.type)) {
          result = node.value.trim() === "" ? NaN : Number(node.value);
          if (!Number.isFinite(result) || item.field.type === "int" && !Number.isInteger(result)) {
            const error = new Error(`${label(item.field, item.key)}需要填写${item.field.type === "int" ? "整数" : "数字"}。`);
            error.field = node; throw error;
          }
        } else if (item.field.type === "list") {
          const lines = node.value.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
          result = lines.map(line => {
            const existing = Array.isArray(item.original) ? item.original.find(entry => String(entry) === line) : undefined;
            return existing !== undefined ? existing : line;
          });
        }
        else result = node.value;
        put(value, item.path, result);
      }
      return value;
    }
    changes(value) {
      const diff = (schema, current, original) => {
        const result = {};
        for (const [key, field] of Object.entries(schema)) {
          if (field.type === "object") {
            const nested = diff(field.items || {}, current?.[key], original?.[key]);
            if (Object.keys(nested).length) result[key] = nested;
          } else if (secret(key) && !current?.[key]) continue;
          else {
            const fallback = field.default ?? ({bool:false, int:0, float:0, list:[], template_list:[]}[field.type] ?? "");
            const originalValue = original?.[key] ?? clone(fallback), currentValue = current?.[key] ?? clone(fallback);
            if (JSON.stringify(currentValue) !== JSON.stringify(originalValue)) result[key] = currentValue;
          }
        }
        return result;
      };
      return diff(this.record.schema, value, this.record.settings);
    }
    updateDirty() {
      if (!this.record) return;
      try { this.dirty = Object.keys(this.changes(this.collect())).length > 0; }
      catch (_) { this.dirty = true; }
      const save = this.root.querySelector("[data-native-save]");
      if (save) save.disabled = this.busy || this.actionBusy || !this.dirty;
      const reload = this.root.querySelector("[data-native-reload]");
      if (reload) { reload.disabled = this.busy || this.actionBusy; reload.textContent = this.dirty ? "放弃修改并重读" : "重新读取"; }
    }
    notice(message, failed = false) {
      const node = this.root.querySelector("[data-native-notice]");
      if (node) { node.textContent = message; node.classList.toggle("is-error", failed); }
    }
    setBusy(value) {
      this.busy = value;
      this.root.querySelectorAll("[data-native-field], [data-native-add], [data-native-remove]").forEach(node => { node.disabled = value; });
      this.root.setAttribute("aria-busy", String(value));
      this.management?.setBlocked(value);
      this.updateStatus(); this.updateDirty();
    }
    async save() {
      if (this.busy || this.actionBusy || !this.record) return;
      let updates;
      try { updates = this.changes(this.collect()); }
      catch (error) { this.notice(error.message, true); error.field?.focus(); return; }
      if (!Object.keys(updates).length) { this.notice("设置没有变化。"); return; }
      const ticket = this.ticket;
      this.statusReads = 0;
      this.setBusy(true); this.notice("正在保存并应用…");
      try {
        const record = await window.CompanionProduct.request("POST", "extensions/configure", {
          extension_id:this.name, updates, revision:this.record.revision,
        }, {timeoutMs:60000});
        if (!this.current(ticket)) return;
        if (!validRecord(record)) throw {code:"invalid_response"};
        this.record = record; this.draft = clone(record.settings); this.dirty = false;
        this.render();
        this.notice(record.runtime?.state === "failed" ? "设置已保存，模块启动失败，请检查连接设置。" : "设置已保存并应用。", record.runtime?.state === "failed");
        this.options.onApplied?.(this.name);
        try { await window.CompanionProduct.refreshIntegrations?.(); } catch (_) {}
        if (this.current(ticket)) this.updateStatus();
      } catch (error) { if (this.current(ticket)) this.notice(errorText(error), true); }
      finally { if (this.current(ticket)) this.setBusy(false); }
    }
    async lifecycle(action) {
      if (this.busy || this.actionBusy) return;
      const product = window.CompanionProduct;
      const extension = product.extension?.(this.name) || product.capability?.(this.name)
        || product.provider?.(this.name);
      if (extension?.owner !== "companion_v2") return;
      const revision = Number.isInteger(extension.installed_revision) ? extension.installed_revision : 0;
      const signature = `${this.name}:${action}:${revision}`;
      if (!operations.has(signature)) operations.set(signature, window.crypto?.randomUUID?.() || `extension-${Date.now()}-${Math.random().toString(16).slice(2)}`);
      const ticket = this.ticket;
      this.statusReads = 0;
      this.setBusy(true); this.notice(action === "pause" ? "正在暂停模块…" : "正在启用模块…");
      try {
        await product.manageExtension(this.name, action, revision, operations.get(signature));
        operations.delete(signature);
        const result = await product.request("GET", "extensions");
        if (!this.current(ticket)) return;
        if (validRecord(result?.extensions?.[this.name])) this.record.runtime = result.extensions[this.name].runtime;
        this.updateStatus();
        this.notice(this.record.runtime?.state === "failed" ? "模块启动失败，请检查连接设置后重试。" : action === "pause" ? "模块已暂停。" : "模块已启用。", this.record.runtime?.state === "failed");
      } catch (error) {
        if (error?.code === "capability_revision_conflict") operations.delete(signature);
        if (this.current(ticket)) this.notice(errorText(error), true);
      } finally { if (this.current(ticket)) this.setBusy(false); }
    }
    templateField(path) {
      let schema = this.record.schema, field;
      for (const key of path) { field = schema[key]; schema = field?.items || {}; }
      return field;
    }
    onClick(event) {
      const button = event.target.closest?.("button");
      if (!button || !this.root.contains(button) || button.disabled) return;
      if (button.hasAttribute("data-native-reload")) { this.load(); return; }
      if (button.dataset.nativeGroup) { this.group = button.dataset.nativeGroup; this.filter(); return; }
      if (button.dataset.nativeLifecycleAction) { this.lifecycle(button.dataset.nativeLifecycleAction); return; }
      if (button.dataset.mobileTokenAction) { this.mobileToken(button.dataset.mobileTokenAction); return; }
      if (button.hasAttribute("data-mobile-copy-token")) { this.copyMobileToken(); return; }
      if (button.dataset.nativeAdd || button.dataset.nativeRemove) {
        try { this.draft = this.collect(); }
        catch (error) { this.notice(error.message, true); error.field?.focus(); return; }
        if (button.dataset.nativeRemove) {
          const path = JSON.parse(button.dataset.nativeRemove);
          at(this.draft, path.slice(0, -1)).splice(path.at(-1), 1);
        } else {
          const path = JSON.parse(button.dataset.nativeAdd), field = this.templateField(path);
          const selector = [...this.root.querySelectorAll("[data-native-template-kind]")].find(node => node.dataset.nativeTemplateKind === keyOf(path));
          const template = selector?.value || Object.keys(field.templates || {})[0];
          const rows = at(this.draft, path) || [];
          rows.push({...defaults(field.templates[template].items || {}), __template_key:template});
          put(this.draft, path, rows);
        }
        this.dirty = true; this.render(); this.notice("修改尚未保存。");
      }
    }
    async mobileToken(action) {
      if (this.busy || this.actionBusy || this.name !== "reality") return;
      const node = this.root.querySelector("[data-mobile-token-result]");
      const configured = this.record?.settings?.mobile?.pairing_token_configured === true;
      const rotate = action === "rotate";
      if (rotate && !window.confirm("轮换后，已连接的手机会话将失效，需要重新配对。继续吗？")) return;
      this.actionBusy = true; this.updateStatus();
      if (node) node.textContent = rotate ? "正在轮换令牌…" : "正在生成令牌…";
      try {
        const result = await window.CompanionProduct.request("POST", "extensions/action", {
          extension_id:"reality", action:"mobile_pairing_token", payload:{rotate},
        }, {timeoutMs:60000});
        const value = result?.result?.pairing_token;
        if (configured && !value) throw {code:"native_management_failed"};
        this.mobileTokenNotice = value ? {token:value, running:result?.result?.running === true} : null;
        await this.load();
        if (!value) {
          const notice = this.root.querySelector("[data-mobile-token-result]");
          if (notice) notice.textContent = result?.result?.running
            ? "网关已启用；令牌不会回显。如需在新手机配对，请轮换令牌。"
            : "网关未能启动，请检查端口占用和模块状态。";
        }
      } catch (error) { if (node) node.textContent = errorText(error); }
      finally { this.actionBusy = false; this.updateStatus(); }
    }
    async copyMobileToken() {
      const input = this.root.querySelector(".mobile-pairing-token input");
      if (!input) return;
      input.select(); input.setSelectionRange(0, input.value.length);
      try {
        await navigator.clipboard.writeText(input.value);
        const notice = this.root.querySelector("[data-mobile-token-result]");
        if (notice) notice.textContent = "令牌已复制，可粘贴到 Android 配对页面。";
      } catch (_) {
        const notice = this.root.querySelector("[data-mobile-token-result]");
        if (notice) notice.textContent = "令牌已选中，请复制后粘贴到 Android 配对页面。";
      }
    }
    onInput(event) {
      if (event.target.hasAttribute?.("data-native-search")) { this.query = event.target.value; this.filter(); return; }
      if (event.target.hasAttribute?.("data-native-field")) { this.updateDirty(); this.notice(this.dirty ? "修改尚未保存。" : ""); }
    }
    filter() {
      const query = this.query.trim().toLocaleLowerCase(), matched = [];
      for (const panel of this.root.querySelectorAll("[data-native-group-panel]")) {
        const group = this.groups.find(item => item.id === panel.dataset.nativeGroupPanel);
        const groupMatch = group.title.toLocaleLowerCase().includes(query);
        const sections = [...panel.querySelectorAll("[data-mobile-config-section]")];
        let count = 0;
        for (const row of panel.querySelectorAll("[data-native-field-label]")) {
          const section = row.closest("[data-mobile-config-section]");
          const sectionMatch = Boolean(query && section
            && section.querySelector("summary")?.textContent.toLocaleLowerCase().includes(query));
          const match = !query || groupMatch || sectionMatch
            || row.textContent.toLocaleLowerCase().includes(query);
          row.hidden = !match;
          if (match) {
            count++;
            if (query && section) section.open = true;
          }
        }
        for (const section of sections) {
          const visible = [...section.querySelectorAll("[data-native-field-label]")].some(row => !row.hidden);
          section.hidden = Boolean(query) && !visible;
        }
        if (count || !query) matched.push(group.id);
      }
      if (!matched.includes(this.group)) this.group = matched[0] || "";
      this.root.querySelectorAll("[data-native-group]").forEach(button => {
        button.hidden = !matched.includes(button.dataset.nativeGroup);
        button.setAttribute("aria-pressed", String(button.dataset.nativeGroup === this.group));
      });
      this.root.querySelectorAll("[data-native-group-panel]").forEach(panel => { panel.hidden = panel.dataset.nativeGroupPanel !== this.group; });
      const empty = this.root.querySelector(".native-extension-no-results");
      if (empty) empty.hidden = matched.length > 0;
    }
  }
  let instance = 0;
  function prune() {
    for (const [root, panel] of sessions) if (root.isConnected === false) { panel.stop(); sessions.delete(root); }
  }
  function refreshStatus() { prune(); for (const panel of sessions.values()) if (panel.record) panel.updateStatus(); }
  function mount(root, {extensionId, ...options} = {}) {
    prune();
    if (!root || !names[extensionId]) return null;
    if (sessions.get(root)?.name === extensionId) return sessions.get(root);
    unmount(root);
    const panel = new Panel(root, extensionId, options); sessions.set(root, panel); return panel;
  }
  function unmount(root) {
    if (root) { sessions.get(root)?.stop(); sessions.delete(root); return; }
    for (const panel of sessions.values()) panel.stop();
    sessions.clear();
  }
  return {mount, unmount, prune, refreshStatus};
})();
