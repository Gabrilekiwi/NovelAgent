/* Structured author input for chapter facts. The server remains the validator. */
(() => {
  "use strict";

  const LABELS = {
    event_type: "事件类型", status: "事件进度", occurrence_location: "发生地点",
    onsite_participants: "在场人物", remote_participants: "远程参与人物",
    transported_items: "运输物品", related_entities: "关联对象", entity_id: "人物或对象",
    role: "参与角色", channel: "联系渠道", item_id: "物品", from_location: "出发地点",
    to_location: "到达地点", carrier_id: "携带者", relation: "关联方式",
    canonical_name: "人物姓名", field: "状态字段", after: "变化后的值",
    source_event_key: "依据事件", relationship_id: "关系标识", source_entity_id: "关系起点",
    target_entity_id: "关系终点", relation_type: "关系类型", roster_id: "名册标识",
    operation: "变更方式", member_ids: "名册成员", owner_id: "持有者", delta: "数量增减",
    counter_id: "计数项标识", value: "数值", name: "名称", aliases: "别名", kind: "类型",
    local_key: "本章称呼标识", reason: "补充理由", location_id: "地点", quantity: "数量",
    member_count: "人数", title: "标题", description: "说明", summary: "事实概述",
    current_goal: "当前目标", condition: "身体或精神状况", identity: "身份", trust: "信任程度",
    current_value: "当前数值", minimum: "下限", maximum: "上限", unresolved_count: "身份未明确的人数",
  };
  const ENUM_LABELS = {
    started: "开始", ongoing: "进行中", completed: "已完成", actor: "行动者",
    recipient: "接受者", witness: "见证者", present_other: "其他在场人物",
    coordinator: "协调者", observer: "远程观察者", remote_other: "其他远程参与者",
    target: "目标", topic: "涉及对象", beneficiary: "受益者", cause: "起因",
    reference: "提及对象", related_other: "其他关联", add: "加入", remove: "移出",
    increment: "增加或减少", set: "设为明确数值", character: "人物", location: "地点",
    item: "物品", declare: "新增身份", reuse: "使用已有身份",
  };
  const TYPE_LABELS = {
    string: "文字", number: "数值", integer: "整数", boolean: "是或否",
    null: "未说明（空值）", object: "多个字段", array: "列表",
  };
  const TYPES = ["string", "number", "boolean", "null", "object", "array"];
  const MAX_DEPTH = 12;
  const MAX_NODES = 2000;
  const MAX_ITEMS = 200;
  let nextControlId = 0;
  const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
  function clone(value, depth = 0) {
    if (depth > MAX_DEPTH) fail("事实内容", "嵌套层数过多，请减少层级");
    if (value === undefined && depth === 0) return undefined;
    if (value === null || typeof value === "string" || typeof value === "boolean") return value;
    if (typeof value === "number") {
      if (!Number.isFinite(value)) fail("事实内容", "请填写有限数值");
      return value;
    }
    if (Array.isArray(value)) return value.map((item) => clone(item, depth + 1));
    if (!value || typeof value !== "object") fail("事实内容", "内容不是可保存的值");
    const result = {};
    Object.keys(value).forEach((key) => Object.defineProperty(result, key, {
      value: clone(value[key], depth + 1), enumerable: true, writable: true, configurable: true,
    }));
    return result;
  }
  const typeOf = (value) => value === null ? "null" : Array.isArray(value) ? "array" : typeof value;
  const label = (key) => own(LABELS, key) ? LABELS[key] : key;
  const enumLabel = (value) => own(ENUM_LABELS, value) ? ENUM_LABELS[value] : value;
  const fail = (path, message) => { throw new Error(`${path || "事实内容"}：${message}`); };
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const matchesType = (value, type) => type === "integer"
    ? typeof value === "number" && Number.isInteger(value)
    : typeOf(value) === type;

  function typesFor(schema) {
    if (schema.type) return Array.isArray(schema.type) ? schema.type : [schema.type];
    if (schema.properties) return ["object"];
    if (schema.items) return ["array"];
    if (schema.oneOf || schema.anyOf) {
      return [...new Set((schema.oneOf || schema.anyOf).flatMap(typesFor))];
    }
    return TYPES;
  }

  // This is deliberately limited to the payload contract's JSON Schema subset.
  // Unknown original fields are retained; final structural/semantic validation is server-side.
  function validate(schema, value, path = "事实内容", depth = 0) {
    if (depth > MAX_DEPTH) fail(path, "嵌套层数过多，请减少层级");
    if (schema === false) fail(path, "该字段不允许填写");
    schema = schema && typeof schema === "object" ? schema : {};
    const actual = typeOf(value);
    if (!TYPES.includes(actual)) fail(path, "内容不是可保存的值");
    if (actual === "number" && !Number.isFinite(value)) fail(path, "请填写有限数值");
    if (schema.type && !typesFor(schema).some((type) => matchesType(value, type))) {
      fail(path, `请选择${typesFor(schema).map((type) => TYPE_LABELS[type] || type).join("或")}`);
    }
    if (schema.enum && !schema.enum.some((option) => same(option, value))) fail(path, "请从选项中选择");
    if (own(schema, "const") && !same(schema.const, value)) fail(path, "该值与固定选项不符");
    const alternatives = schema.oneOf || schema.anyOf;
    if (alternatives) {
      let matching = 0;
      alternatives.forEach((alternative) => {
        try { validate(alternative, value, path, depth); matching += 1; } catch (_) { /* another variant */ }
      });
      if (!matching || (schema.oneOf && matching !== 1)) fail(path, "请检查所选内容类型和字段");
    }
    if (actual === "string") {
      const length = Array.from(value).length;
      if (schema.minLength != null && length < schema.minLength) fail(path, "请填写内容");
      if (schema.maxLength != null && length > schema.maxLength) fail(path, `内容不能超过 ${schema.maxLength} 个字`);
      if (schema.pattern && !new RegExp(schema.pattern).test(value)) {
        fail(path, path.includes("依据事件")
          ? "请选择本章事件，或填写以小写字母开头、仅含小写字母、数字、短横线和下划线的事件标识"
          : "填写格式不正确，请检查内容");
      }
    }
    if (actual === "number") {
      if (schema.minimum != null && value < schema.minimum) fail(path, `不能小于 ${schema.minimum}`);
      if (schema.maximum != null && value > schema.maximum) fail(path, `不能大于 ${schema.maximum}`);
    }
    if (actual === "array") {
      if (value.length > MAX_ITEMS) fail(path, `列表最多支持 ${MAX_ITEMS} 项`);
      if (schema.minItems != null && value.length < schema.minItems) fail(path, `至少需要 ${schema.minItems} 项`);
      if (schema.maxItems != null && value.length > schema.maxItems) fail(path, `最多填写 ${schema.maxItems} 项`);
      if (schema.uniqueItems && value.some((item, index) => value.slice(0, index).some((other) => same(item, other)))) fail(path, "列表不能有重复项");
      value.forEach((item, index) => validate(schema.items || {}, item, `${path} / 第 ${index + 1} 项`, depth + 1));
    }
    if (actual === "object") {
      if (Object.keys(value).length > MAX_ITEMS) fail(path, `对象最多支持 ${MAX_ITEMS} 个字段`);
      (schema.required || []).forEach((key) => { if (!own(value, key)) fail(`${path} / ${label(key)}`, "请填写此项"); });
      Object.keys(value).forEach((key) => {
        const childSchema = own(schema.properties || {}, key) ? schema.properties[key]
          : typeof schema.additionalProperties === "object" ? schema.additionalProperties : {};
        validate(childSchema, value[key], `${path} / ${label(key)}`, depth + 1);
      });
    }
    return value;
  }

  function defaultValue(schema) {
    if (schema.enum?.length) return clone(schema.enum[0]);
    if (own(schema, "const")) return clone(schema.const);
    const type = typesFor(schema)[0];
    if (type === "object") return {};
    if (type === "array") return [];
    if (type === "boolean") return false;
    if (type === "number" || type === "integer") return 0;
    return type === "null" ? null : "";
  }

  function render(container, schema, value, options = {}) {
    const { readOnly = false, entities = [], events = [] } = options;
    const doc = container.ownerDocument || document;
    const original = clone(value);
    let nodeCount = 0;
    const element = (tag, text, className) => {
      const result = doc.createElement(tag);
      if (text != null) result.textContent = text;
      if (className) result.className = className;
      return result;
    };
    const control = (tag, name) => {
      const result = element(tag);
      result.name = `fact_form_${++nextControlId}`;
      result.disabled = readOnly;
      if (name) result.setAttribute("aria-label", name);
      return result;
    };
    const button = (text, handler) => {
      const result = control("button", text);
      result.type = "button";
      result.textContent = text;
      result.addEventListener("click", () => { if (!readOnly) { handler(); changed(); } });
      return result;
    };
    const option = (select, text, value) => {
      const item = element("option", text);
      item.value = value;
      select.appendChild(item);
    };
    const changed = () => {
      if (doc.defaultView?.Event) container.dispatchEvent(new doc.defaultView.Event("input", { bubbles: true }));
    };

    function referenceChoices(key, path) {
      if (key === "source_event_key") return events.filter((event) => event.local_key).map((event) => ({
        value: event.local_key, text: event.summary || event.local_key,
      }));
      if (!["entity_id", "source_entity_id", "target_entity_id", "owner_id", "carrier_id", "item_id",
        "occurrence_location", "from_location", "to_location", "location_id", "member_ids"].includes(key)) return [];
      const kind = key === "item_id" ? "item" : /location/.test(key) ? "location"
        : key === "carrier_id" || key === "member_ids" || /参与人物|在场人物/.test(path) ? "character" : null;
      return entities.filter((entity) => entity.entity_id && (!kind || entity.kind === kind)).map((entity) => ({
        value: entity.entity_id,
        text: `${entity.name || entity.canonical_name || entity.entity_id}${entity.aliases?.length ? `（${entity.aliases.join("、")}）` : ""}`,
      }));
    }

    function node(nodeSchema, initial, key, path, depth) {
      if (depth > MAX_DEPTH || ++nodeCount > MAX_NODES) fail(path, "内容层级或字段过多，请拆分后填写");
      nodeSchema = nodeSchema && typeof nodeSchema === "object" ? nodeSchema : {};
      const box = element("div", null, "fact-form-value");
      box.dataset.factField = key;
      box.dataset.factPath = path;
      const starting = initial === undefined ? defaultValue(nodeSchema) : clone(initial);
      const types = [...typesFor(nodeSchema)];
      let type = types.find((candidate) => matchesType(starting, candidate)) || typeOf(starting);
      if (!types.includes(type)) types.push(type); // Retain original values, including unknown variants.
      const panels = new Map();
      const holder = element("div", null, "fact-form-content");
      let typeSelect = null;

      function getPanel(selectedType) {
        if (!panels.has(selectedType)) {
          const alternatives = nodeSchema.oneOf || nodeSchema.anyOf || [];
          const variant = alternatives.find((candidate) => typesFor(candidate).includes(selectedType));
          const selectedSchema = variant ? { ...nodeSchema, ...variant } : nodeSchema;
          const selectedValue = selectedType === type ? starting : defaultValue({ ...selectedSchema, type: selectedType });
          panels.set(selectedType, typed(selectedType, selectedSchema, selectedValue, key, path, depth, initial === undefined || selectedType !== type));
        }
        return panels.get(selectedType);
      }
      if (types.length > 1 && !nodeSchema.enum) {
        typeSelect = control("select", `${path}的内容类型`);
        typeSelect.className = "fact-form-type";
        types.forEach((candidate) => option(typeSelect, TYPE_LABELS[candidate] || candidate, candidate));
        typeSelect.value = type;
        typeSelect.addEventListener("change", () => {
          if (!readOnly) holder.replaceChildren(getPanel(typeSelect.value).element);
        });
        box.appendChild(typeSelect);
      }
      holder.appendChild(getPanel(type).element);
      box.appendChild(holder);
      return {
        element: box,
        read: () => {
          const selectedType = typeSelect ? typeSelect.value : type;
          return validate(nodeSchema, getPanel(selectedType).read(), path, depth);
        },
      };
    }

    function typed(type, typedSchema, initial, key, path, depth, missing = false) {
      const box = element("div", null, `fact-form-${type}`);
      if (typedSchema.enum || own(typedSchema, "const")) {
        const values = clone(typedSchema.enum || [typedSchema.const]);
        let index = values.findIndex((candidate) => same(candidate, initial));
        if (index < 0) { index = values.length; values.push(clone(initial)); }
        const select = control("select", path);
        if (missing && !own(typedSchema, "const")) option(select, "请选择…", "");
        values.forEach((item, number) => option(select, item === null ? TYPE_LABELS.null : String(enumLabel(item)), String(number)));
        select.value = missing && !own(typedSchema, "const") ? "" : String(index);
        box.appendChild(select);
        return { element: box, read: () => { if (select.value === "") fail(path, "请明确选择"); return clone(values[Number(select.value)]); } };
      }
      if (type === "null") {
        box.appendChild(element("span", "未说明"));
        return { element: box, read: () => null };
      }
      if (["string", "number", "integer", "boolean"].includes(type)) {
        if (type === "boolean" && missing) {
          const select = control("select", path);
          [["", "请选择…"], ["true", "是"], ["false", "否"]].forEach(([value, title]) => option(select, title, value));
          select.value = ""; box.appendChild(select);
          return { element: box, read: () => { if (select.value === "") fail(path, "请明确选择是或否"); return select.value === "true"; } };
        }
        const choices = type === "string" ? referenceChoices(key, path) : [];
        const input = control(type === "string" && !choices.length ? "textarea" : "input", path);
        if (type === "boolean") {
          input.type = "checkbox";
          input.checked = initial;
          const caption = element("label", "是");
          caption.prepend(input);
          box.appendChild(caption);
        } else {
          input.value = missing && (type === "number" || type === "integer") ? "" : Object.is(initial, -0) ? "-0" : String(initial);
          if (type === "number" || type === "integer") {
            input.type = "number";
            input.step = type === "integer" ? "1" : "any";
          } else if (input.tagName === "TEXTAREA") input.rows = 2;
          else input.type = "text";
          box.appendChild(input);
        }
        if (choices.length) {
          const select = control("select", `${path}：选择已有对象`);
          select.className = "fact-form-reference";
          option(select, key === "source_event_key" ? "选择本章事件…" : "选择已有对象或本章新增身份…", "");
          choices.forEach((choice, index) => option(select, choice.text, String(index)));
          select.value = "";
          select.addEventListener("change", () => {
            if (!readOnly && select.value !== "") { input.value = choices[Number(select.value)].value; changed(); }
          });
          box.appendChild(select);
        }
        return { element: box, read: () => {
          if (type === "boolean") return input.checked;
          if (type === "string") return input.value;
          if (!input.value.trim()) fail(path, "请填写数值");
          const numeric = Number(input.value);
          if (!Number.isFinite(numeric)) fail(path, "请填写有限数值");
          if (type === "integer" && !Number.isInteger(numeric)) fail(path, "请填写整数");
          return numeric;
        } };
      }
      if (type === "array") {
        if (initial.length > MAX_ITEMS) fail(path, `列表最多支持 ${MAX_ITEMS} 项`);
        const rows = [];
        const list = element("div", null, "fact-form-list");
        const addRow = (item) => {
          if (rows.length >= MAX_ITEMS) fail(path, `列表最多支持 ${MAX_ITEMS} 项`);
          const row = element("fieldset", null, "fact-form-row");
          const legend = element("legend");
          const editor = node(typedSchema.items || {}, item, key, `${path} / 第 ${rows.length + 1} 项`, depth + 1);
          const entry = { row, legend, editor };
          row.appendChild(legend);
          row.appendChild(editor.element);
          row.appendChild(button("移除此项", () => {
            rows.splice(rows.indexOf(entry), 1);
            row.remove();
            rows.forEach((other, index) => { other.legend.textContent = `第 ${index + 1} 项`; });
          }));
          rows.push(entry);
          legend.textContent = `第 ${rows.length} 项`;
          list.appendChild(row);
        };
        initial.forEach(addRow);
        box.appendChild(list);
        box.appendChild(button("添加一项", () => addRow(undefined)));
        return { element: box, read: () => rows.map((entry) => entry.editor.read()) };
      }
      if (type === "object") {
        if (Object.keys(initial).length > MAX_ITEMS) fail(path, `对象最多支持 ${MAX_ITEMS} 个字段`);
        const entries = [];
        const fields = typedSchema.properties || {};
        const required = new Set(typedSchema.required || []);
        const group = element("div", null, "fact-form-fields");
        const addField = (name, fieldSchema, fieldValue, { fixed, included = true, isNew = false } = {}) => {
          if (entries.length >= MAX_ITEMS) fail(path, `对象最多支持 ${MAX_ITEMS} 个字段`);
          const row = element("fieldset", null, "fact-form-field");
          const legend = element("legend", fixed ? label(name) : "自定义字段");
          row.appendChild(legend);
          let nameInput = null;
          let include = null;
          if (!fixed) {
            nameInput = control("input", `${path}的字段名称`);
            nameInput.type = "text";
            nameInput.value = name;
            row.appendChild(nameInput);
          } else if (!required.has(name)) {
            include = control("input", `填写${label(name)}`);
            include.type = "checkbox";
            include.checked = included;
            const caption = element("label", "填写此项");
            caption.prepend(include);
            row.appendChild(caption);
          }
          const editor = node(fieldSchema, fieldValue, name, `${path} / ${label(name) || "新增字段"}`, depth + 1);
          const contents = element("fieldset", null, "fact-form-field-content");
          contents.disabled = readOnly || Boolean(include && !include.checked);
          contents.appendChild(editor.element);
          row.appendChild(contents);
          if (include) include.addEventListener("change", () => { contents.disabled = readOnly || !include.checked; });
          if (fieldSchema.description) row.appendChild(element("p", fieldSchema.description, "fact-form-hint"));
          const entry = { name, nameInput, include, editor, row, allowEmptyName: !isNew && name === "" };
          if (!fixed) row.appendChild(button("删除字段", () => { entries.splice(entries.indexOf(entry), 1); row.remove(); }));
          entries.push(entry);
          group.appendChild(row);
        };
        Object.keys(fields).forEach((name) => addField(name, fields[name], own(initial, name) ? initial[name] : undefined, { fixed: true, included: own(initial, name) }));
        Object.keys(initial).filter((name) => !own(fields, name)).forEach((name) => addField(name,
          typeof typedSchema.additionalProperties === "object" ? typedSchema.additionalProperties : {}, initial[name]));
        box.appendChild(group);
        if (typedSchema.additionalProperties !== false) box.appendChild(button("添加字段", () => addField("",
          typeof typedSchema.additionalProperties === "object" ? typedSchema.additionalProperties : {}, undefined, { isNew: true })));
        return { element: box, read: () => {
          const result = {};
          entries.forEach((entry) => {
            if (entry.include && !entry.include.checked) return;
            const fieldName = entry.nameInput ? entry.nameInput.value : entry.name;
            if (!fieldName && !entry.allowEmptyName) fail(path, "请填写新增字段的名称");
            if (own(result, fieldName)) fail(path, `字段“${fieldName}”重复，请修改名称`);
            Object.defineProperty(result, fieldName, { value: entry.editor.read(), enumerable: true, writable: true, configurable: true });
          });
          return result;
        } };
      }
      fail(path, "暂不支持该内容类型，请保留原值并检查字段定义");
    }

    const root = element("fieldset", null, "fact-form");
    root.disabled = readOnly;
    const editor = node(schema || {}, value, "proposal", "事实内容", 0);
    root.appendChild(editor.element);
    container.replaceChildren(root);
    return {
      read: () => readOnly && original !== undefined ? clone(original) : editor.read(),
    };
  }

  window.NovelAgentFactForm = Object.freeze({ render, fieldLabel: label, enumLabel });
})();
