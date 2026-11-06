import assert from "node:assert/strict";
import { test } from "node:test";
import { parseQuotaAlerts, quotaAlertPayload } from "./quota-alerts";

const response = {
  settings: { enabled: false, has_webhook: true, has_secret: true, threshold: 3, repeat_minutes: 60 },
  status: { last_success_at: null, last_error: null, check_interval_seconds: 30 },
};

test("quota alert payload accepts custom threshold and zero, rejects fractional/empty inputs", () => {
  const { settings } = parseQuotaAlerts(response);
  assert.equal(quotaAlertPayload(settings, "0", "60", false).threshold, 0);
  assert.equal(quotaAlertPayload(settings, "18", "120", true).threshold, 18);
  assert.equal(quotaAlertPayload(settings, "18", "120", true).clear_secret, true);
  for (const value of ["", "-1", "1.5", "NaN", "10001"]) assert.throws(() => quotaAlertPayload(settings, value, "60", false));
  assert.throws(() => quotaAlertPayload(settings, "3", "1", false));
});

test("quota alert parser validates unknown responses and never fills credential inputs", () => {
  assert.equal(parseQuotaAlerts({ ...response, settings: { ...response.settings, secret: "unexpected" } }).settings.secret, "");
  for (const value of [null, {}, { ...response, settings: { ...response.settings, threshold: "3" } },
    { ...response, status: { ...response.status, last_success_at: "yesterday" } }]) assert.throws(() => parseQuotaAlerts(value));
});
