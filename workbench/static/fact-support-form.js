(() => {
  "use strict";
  const node = (tag, text) => { const n = document.createElement(tag); if (text != null) n.textContent = text; return n; };
  const text = { type: "string", minLength: 1, maxLength: 200 };
  const object = (properties) => ({ type: "object", properties, required: Object.keys(properties), additionalProperties: false });
  const common = { reason: { type: "string", minLength: 1, maxLength: 1000 } };
  const baselineFields = {
    character_state: { entity_id: text, field: text, value: {} },
    location: { entity_id: text, location_id: text },
    inventory: { owner_id: text, item_id: text, quantity: { type: "integer", minimum: 0 } },
    counter: { counter_id: text, value: { type: "number" } },
    relationship: { relationship_id: text, source_entity_id: text, target_entity_id: text, relation_type: text, field: text, value: {} },
    roster: { roster_id: text, member_ids: { type: "array", items: text } }
  };
  const labels = { character: "人物", location: "地点", item: "物品", character_state: "角色初始状态", inventory: "初始库存", counter: "初始数值", relationship: "初始关系", roster: "初始名单" };
  let nextSelect = 0;
  const select = (values, label, selected) => {
    const field = node("label"); field.className = "field"; field.append(node("span", label));
    const input = node("select");
    input.name = `fact_support_${++nextSelect}`;
    values.forEach(([value, title]) => { const opt = node("option", title); opt.value = value; input.append(opt); });
    if (selected != null) input.value = selected;
    field.append(input); return { field, input };
  };
  const key = () => `author_${crypto.randomUUID().replaceAll("-", "")}`;
  function render(container, support, options = {}) {
    container.replaceChildren();
    const identities = [], baselines = [];
    const entities = options.entities || [];
    const readOnly = options.readOnly === true;
    const changed = () => container.dispatchEvent(new Event("input", { bubbles: true }));
    const identityList = node("div"), baselineList = node("div");
    const entityOptions = () => [...entities, ...identities.flatMap((row) => {
      try { const value = row.read(); return [{ entity_id: `local:${value.local_key}`, name: value.name || value.reference, kind: value.kind, aliases: [] }]; }
      catch { return []; }
    })];
    const add = (section, existing) => {
      const rows = section === "identities" ? identities : baselines;
      const list = section === "identities" ? identityList : baselineList;
      const article = node("article"); article.className = "fact-support-row";
      const localKey = existing?.local_key || key();
      const choices = section === "identities" ? ["character", "location", "item"] : Object.keys(baselineFields);
      const kind = select(choices.map(k => [k, labels[k]]), "补充种类", existing?.kind);
      const action = select([["declare", "声明新对象"], ["reuse", "选择已有对象并绑定称呼"]], "身份处理", existing?.action);
      article.append(kind.field);
      if (section === "identities") article.append(action.field);
      const fields = node("div"); article.append(fields);
      let form, value = existing ? structuredClone(existing) : {};
      const draw = () => {
        const properties = section === "baselines" ? baselineFields[kind.input.value]
          : action.input.value === "declare" ? { name: text, aliases: { type: "array", items: text } }
          : { reference: text, entity_id: text };
        const relevant = Object.fromEntries(Object.entries(value).filter(([k]) => k in properties || k in common));
        form = window.NovelAgentFactForm.render(fields, object({ ...properties, ...common }), relevant,
          { readOnly, entities: section === "identities" ? entities : entityOptions() });
      };
      kind.input.disabled = readOnly; action.input.disabled = readOnly;
      [kind.input, action.input].forEach(input => input.addEventListener("change", () => {
        try { value = form.read(); } catch { /* Preserve only valid fields when explicitly changing kind. */ }
        draw(); changed();
      }));
      const row = { read: () => ({ local_key: localKey, kind: kind.input.value, review_source: "author",
        ...(section === "identities" ? { action: action.input.value } : {}), ...form.read() }) };
      rows.push(row);
      const remove = node("button", "撤回这项补充"); remove.type = "button"; remove.className = "button button-danger-ghost"; remove.disabled = readOnly;
      remove.addEventListener("click", () => { rows.splice(rows.indexOf(row), 1); article.remove(); changed(); });
      article.append(remove); list.append(article); draw();
    };
    for (const [section, heading, list] of [["identities", "身份声明与称呼", identityList], ["baselines", "本章变化发生前的初始情况", baselineList]]) {
      container.append(node("h4", heading), list);
      (support?.[section] || []).forEach(value => add(section, value));
      const button = node("button", section === "identities" ? "添加身份或称呼" : "补充缺失的初始情况");
      button.type = "button"; button.className = "button button-secondary"; button.disabled = readOnly;
      button.addEventListener("click", () => { if ((section === "identities" ? identities : baselines).length >= 100) return; add(section); changed(); });
      container.append(button);
    }
    return { read: () => ({ version: "reviewed_fact_support_v1", identities: identities.map(row => row.read()), baselines: baselines.map(row => row.read()) }) };
  }
  window.NovelAgentFactSupportForm = Object.freeze({ render });
})();
