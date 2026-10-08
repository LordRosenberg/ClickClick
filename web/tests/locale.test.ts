import assert from "node:assert/strict";
import test from "node:test";
import { getLocale, setLocale, subscribeLocale, translate } from "../src/lib/locale.ts";

test("switching language notifies mounted views without replacing application state", () => {
  let calls = 0;
  const unsubscribe = subscribeLocale(() => calls++);
  setLocale("en");
  assert.equal(getLocale(), "en");
  setLocale("en");
  assert.equal(calls, 1);
  unsubscribe();
  setLocale("zh-CN");
  assert.equal(calls, 1);
});
test("known labels translate; arbitrary task text stays unchanged", () => {
  assert.equal(translate("设置", "en"), "Settings");
  assert.equal(translate("Keep the user's original task", "zh-CN"), "Keep the user's original task");
});
test("parameter values remain literal and are substituted once", () => {
  assert.equal(translate("{value0} / {value1}", "en", {value0: "{value1}", value1: "$&"}), "{value1} / $&");
});
