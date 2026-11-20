from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "workbench" / "static" / "fact-form.js"
SCHEMA = ROOT / "schemas" / "chapter_fact_payloads.schema.json"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")

# A small DOM surface runs the actual renderer and handlers, rather than extracting
# a second implementation from its source. Browser integration is checked separately.
HARNESS = r'''
const fs = require("fs");
const vm = require("vm");
const assert = require("assert/strict");
class Element {
  constructor(tag, doc) {
    this.tagName = tag.toUpperCase(); this.ownerDocument = doc; this.children = [];
    this.dataset = {}; this.attributes = {}; this.handlers = {}; this.parentNode = null;
    this.value = ""; this.checked = false; this.disabled = false; this.textContent = "";
  }
  appendChild(child) {
    child.remove(); this.children.push(child); child.parentNode = this; return child;
  }
  prepend(child) { child.remove(); this.children.unshift(child); child.parentNode = this; }
  replaceChildren(...children) {
    this.children.forEach(child => { child.parentNode = null; }); this.children = [];
    children.forEach(child => this.appendChild(child));
  }
  remove() {
    if (this.parentNode) {
      this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1);
      this.parentNode = null;
    }
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(event, listener) { (this.handlers[event] ||= []).push(listener); }
  dispatch(event) { (this.handlers[event] || []).forEach(listener => listener({target: this})); }
}
const doc = {createElement: tag => new Element(tag, doc)};
const container = new Element("div", doc);
const sandbox = {window: {}, document: doc};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), sandbox);
const Form = sandbox.window.NovelAgentFactForm;
const schemas = JSON.parse(fs.readFileSync(process.argv[2], "utf8")).properties;
const plain = value => JSON.parse(JSON.stringify(value));
const walk = node => [node, ...node.children.flatMap(walk)];
const find = (predicate, root = container) => {
  const node = walk(root).find(predicate); assert.ok(node, "control not found"); return node;
};
const named = (name, root = container) => find(node => node.attributes["aria-label"] === name, root);
const button = (text, root = container) => find(node => node.tagName === "BUTTON" && node.textContent === text, root);
const field = (key, root = container) => find(node => node.dataset.factField === key, root);
const set = (node, value) => { node.value = value; node.dispatch("change"); };
const event = {
  event_type: "交接", status: "completed", occurrence_location: "北门",
  onsite_participants: [{entity_id: "char_1", role: "actor"}],
  remote_participants: [{entity_id: "char_2", role: "observer", channel: null}],
  transported_items: [{item_id: "item_1", from_location: null, to_location: "北门", carrier_id: null}],
  related_entities: [{entity_id: "local:guard", relation: "reference"}],
};
'''


def run_dom(code: str) -> None:
    result = subprocess.run(
        [shutil.which("node"), "-e", HARNESS + "\n" + code, str(SCRIPT), str(SCHEMA)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("kind", ["event", "character_state", "relationship", "roster", "location", "inventory", "counter"])
def test_seven_fact_kinds_round_trip_without_silent_conversion(kind: str) -> None:
    run_dom(r'''
const fixtures = {
  event,
  character_state: {entity_id: "char_1", canonical_name: "赵原", field: "心理", after: {"": "保留空字段", nested: [null, true, 3.25, "  原文\n"]}, source_event_key: "arrival"},
  relationship: {relationship_id: "rel_1", source_entity_id: "char_1", target_entity_id: "char_2", relation_type: "friend", field: "信任", after: false, source_event_key: "arrival"},
  roster: {roster_id: "roster_1", operation: "add", member_ids: ["char_1", "local:guard"], source_event_key: "arrival"},
  location: {entity_id: "char_1", from_location: null, to_location: "北门", source_event_key: "arrival"},
  inventory: {owner_id: "char_1", item_id: "item_1", delta: -2, source_event_key: "arrival"},
  counter: {counter_id: "days", operation: "set", value: 3.125, source_event_key: "arrival"},
};
const kind = KIND;
const original = fixtures[kind];
original.unrecognized_original_field = {keep: ["x", null, false]};
const expected = plain(original);
const instance = Form.render(container, schemas[kind], original);
assert.deepEqual(plain(instance.read()), original);
assert.deepEqual(original, expected);
const result = instance.read(); result.unrecognized_original_field.keep.push("changed");
assert.equal(original.unrecognized_original_field.keep.length, 3);
assert.deepEqual(plain(instance.read()), original);
'''.replace("KIND", repr(kind)))


def test_nullable_values_and_optional_fields_remain_explicit() -> None:
    run_dom(r'''
const schema = {type: "object", required: ["from_location"], properties: {
  from_location: {type: ["string", "null"]}, note: {type: "string"}
}, additionalProperties: false};
const form = Form.render(container, schema, {from_location: "  原地  "});
assert.deepEqual(plain(form.read()), {from_location: "  原地  "});
const type = named("事实内容 / 出发地点的内容类型");
set(type, "null"); assert.equal(form.read().from_location, null);
set(type, "string"); assert.equal(form.read().from_location, "  原地  ");
const include = named("填写note"); include.checked = true; include.dispatch("change");
assert.deepEqual(plain(form.read()), {from_location: "  原地  ", note: ""});
include.checked = false; include.dispatch("change");
assert.deepEqual(plain(form.read()), {from_location: "  原地  "});
''')


def test_nested_object_and_array_edit_without_json_text_input() -> None:
    run_dom(r'''
const form = Form.render(container, {}, {nested: [1, 2], keep: {x: true}});
const nested = field("nested");
button("添加一项", nested).dispatch("click");
const types = walk(nested).filter(node => node.className === "fact-form-type");
const newest = types[types.length - 1];
set(newest, "boolean");
const check = named("事实内容 / nested / 第 3 项", nested);
set(check, "true");
assert.deepEqual(plain(form.read()), {nested: [1, 2, true], keep: {x: true}});
button("移除此项", nested).dispatch("click");
assert.deepEqual(plain(form.read()), {nested: [2, true], keep: {x: true}});
const fields = find(node => node.className === "fact-form-object");
const add = fields.children.find(node => node.tagName === "BUTTON" && node.textContent === "添加字段");
add.dispatch("click");
assert.throws(() => form.read(), /字段的名称/);
const names = walk(container).filter(node => node.attributes["aria-label"] === "事实内容的字段名称");
names[names.length - 1].value = "nested";
assert.throws(() => form.read(), /重复/);
names[names.length - 1].value = "作者补充";
assert.equal(form.read()["作者补充"], "");
assert.ok(!walk(container).some(node => (node.attributes["aria-label"] || "").includes("JSON")));
''')


def test_reference_selectors_preserve_aliases_and_use_entity_ids() -> None:
    run_dom(r'''
const entities = [
  {entity_id: "char_1", kind: "character", name: "赵原", aliases: ["小赵"]},
  {entity_id: "local:gate", kind: "location", name: "北门", aliases: []},
  {entity_id: "item_1", kind: "item", name: "钥匙", aliases: []},
];
const form = Form.render(container, schemas.location,
  {entity_id: "小赵", from_location: null, to_location: "待确认地点", source_event_key: "arrival"},
  {entities, events: [{local_key: "arrival", summary: "抵达北门"}, {local_key: "departure", summary: "离开东门"}]});
assert.equal(form.read().entity_id, "小赵");
assert.equal(form.read().to_location, "待确认地点");
const places = named("事实内容 / 到达地点：选择已有对象");
assert.equal(places.children.length, 2);
assert.equal(places.children[1].textContent, "北门");
set(places, "0"); assert.equal(form.read().to_location, "local:gate");
set(named("事实内容 / 依据事件：选择已有对象"), "1");
assert.equal(form.read().source_event_key, "departure");
set(named("事实内容 / 人物或对象：选择已有对象"), "0");
assert.equal(form.read().entity_id, "char_1");
named("事实内容 / 到达地点").value = "用户输入的原值";
assert.equal(form.read().to_location, "用户输入的原值");
''')


@pytest.mark.parametrize(
    ("schema_expression", "value_expression", "input_value", "message"),
    [
        ('{type: "integer"}', "2", "2.2", "整数"),
        ('{type: "number"}', "2", "", "请填写数值"),
        ('{type: "number"}', "2", "Infinity", "有限数值"),
        ('{type: "string", minLength: 1}', '"旧值"', "", "请填写内容"),
        ('{type: "string", maxLength: 2}', '"原值"', "三个字", "不能超过"),
    ],
)
def test_human_validation_never_treats_empty_numeric_input_as_zero(
    schema_expression: str, value_expression: str, input_value: str, message: str
) -> None:
    import json

    run_dom(
        f"const form = Form.render(container, {schema_expression}, {value_expression});\n"
        f"named('事实内容').value = {json.dumps(input_value)};\n"
        f"assert.throws(() => form.read(), /{message}/);\n"
    )


def test_readonly_disables_every_control_and_returns_an_independent_original() -> None:
    run_dom(r'''
const initial = {...event, extra: {nested: [true, null]}};
const original = plain(initial);
const form = Form.render(container, schemas.event, initial, {readOnly: true,
  entities: [{entity_id: "char_1", name: "赵原", kind: "character"}]});
const controls = walk(container).filter(node => ["INPUT", "TEXTAREA", "SELECT", "BUTTON"].includes(node.tagName));
assert.ok(controls.length > 10);
controls.forEach(node => assert.equal(node.disabled, true));
named("事实内容 / 事件类型").value = "外部脚本改动";
button("添加一项").dispatch("click");
assert.deepEqual(plain(form.read()), original);
form.read().extra.nested.push("x");
assert.deepEqual(plain(form.read()), original);
''')


def test_new_baseline_numbers_and_decisions_require_explicit_input() -> None:
    run_dom(r'''
const schema = {type: "object", required: ["quantity", "status", "confirmed"], properties: {
  quantity: {type: "integer", minimum: 0}, status: {type: "string", enum: ["started", "completed"]}, confirmed: {type: "boolean"}
}};
const form = Form.render(container, schema, {});
assert.equal(named("事实内容 / 数量").value, "");
assert.throws(() => form.read(), /请填写数值/);
set(named("事实内容 / 数量"), "0");
assert.throws(() => form.read(), /请明确选择/);
set(named("事实内容 / 事件进度"), "1");
assert.throws(() => form.read(), /请明确选择/);
set(named("事实内容 / confirmed"), "false");
assert.deepEqual(plain(form.read()), {quantity: 0, status: "completed", confirmed: false});
const known = Form.render(container, schema, {quantity: 0, status: "started", confirmed: false});
assert.deepEqual(plain(known.read()), {quantity: 0, status: "started", confirmed: false});
''')


def test_unknown_and_special_object_keys_are_not_dropped_or_prototype_mutations() -> None:
    run_dom(r'''
const original = JSON.parse('{"__proto__":{"polluted":true},"constructor":1,"":false,"<img src=x onerror=alert(1)>":"<script>text</script>"}');
const form = Form.render(container, {}, original);
assert.deepEqual(plain(form.read()), original);
assert.equal({}.polluted, undefined);
assert.ok(Object.prototype.hasOwnProperty.call(form.read(), "__proto__"));
assert.ok(!walk(container).some(node => node.tagName === "SCRIPT" || node.tagName === "IMG"));
assert.equal(Form.fieldLabel("__proto__"), "__proto__");
assert.equal(Form.enumLabel("constructor"), "constructor");
''')


def test_limits_reject_oversized_values_without_replacing_existing_form() -> None:
    run_dom(r'''
Form.render(container, {}, "原表单");
const originalRoot = container.children[0];
assert.throws(() => Form.render(container, {}, Array(201).fill(1)), /最多支持 200/);
assert.equal(container.children[0], originalRoot);
let value = "末端"; for (let index = 0; index < 14; index++) value = {nested: value};
assert.throws(() => Form.render(container, {}, value), /层级|层数/);
assert.equal(container.children[0], originalRoot);
''')


def test_enum_labels_and_fresh_forms_match_the_contract() -> None:
    run_dom(r'''
const form = Form.render(container, schemas.counter, undefined);
assert.equal(named("事实内容 / 变更方式").children[0].textContent, "请选择…");
assert.equal(named("事实内容 / 变更方式").children[1].textContent, "增加或减少");
set(named("事实内容 / 变更方式"), "0");
named("事实内容 / 计数项标识").value = "days";
named("事实内容 / 依据事件").value = "arrival";
named("事实内容 / 数值").value = "1";
assert.deepEqual(plain(form.read()), {counter_id: "days", operation: "increment", value: 1, source_event_key: "arrival"});
named("事实内容 / 依据事件").value = "BAD EVENT";
assert.throws(() => form.read(), /依据事件.*小写字母/);
''')


def test_numbers_are_not_changed_to_null_or_zero_by_cloning() -> None:
    run_dom(r'''
const form = Form.render(container, {}, {signed_zero: -0, fraction: 0.000000125});
assert.ok(Object.is(form.read().signed_zero, -0));
assert.equal(form.read().fraction, 0.000000125);
assert.throws(() => Form.render(container, {}, {invalid: NaN}), /有限数值/);
assert.throws(() => Form.render(container, {}, {invalid: Infinity}), /有限数值/);
''')


def test_simple_schema_variants_keep_values_when_switching() -> None:
    run_dom(r'''
const form = Form.render(container, {anyOf: [{type: "string"}, {type: "number"}]}, "原值");
assert.equal(form.read(), "原值");
const type = named("事实内容的内容类型");
set(type, "number");
named("事实内容").value = "4.5";
assert.equal(form.read(), 4.5);
set(type, "string"); assert.equal(form.read(), "原值");
set(type, "number"); assert.equal(form.read(), 4.5);
''')
