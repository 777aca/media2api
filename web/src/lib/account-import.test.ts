import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { parseAccountImportText } from "./account-import";

const firstAccount = {
  email: "first@example.test",
  password: "synthetic-password-one",
  totp_secret: "SYNTHETICTOTPONE",
  access_token: "synthetic-access-token-one",
  source_type: "web",
};

const secondAccount = {
  email: "second@example.test",
  password: "synthetic-password-two",
  totp_secret: "SYNTHETICTOTPTWO",
  access_token: "synthetic-access-token-two",
  source_type: "web",
};

function accountLine(account = firstAccount): string {
  return [
    account.email,
    account.password,
    account.totp_secret,
    account.access_token,
  ].join("----");
}

describe("parseAccountImportText", () => {
  it("keeps legacy opaque tokens without requiring JWT syntax", () => {
    const result = parseAccountImportText(
      "  opaque-token-one  \nopaque-token-two\nopaque-token-one",
    );

    assert.deepEqual(result, {
      tokens: ["opaque-token-one", "opaque-token-two"],
      accounts: [],
      errors: [],
    });
  });

  it("maps four fields to the web account payload", () => {
    const result = parseAccountImportText(accountLine());

    assert.deepEqual(result, {
      tokens: [],
      accounts: [firstAccount],
      errors: [],
    });
  });

  it("removes numbered card and account prefixes", () => {
    const result = parseAccountImportText(
      `卡密 1: ${accountLine()}\n账号2: ${accountLine(secondAccount)}`,
    );

    assert.deepEqual(result, {
      tokens: [],
      accounts: [firstAccount, secondAccount],
      errors: [],
    });
  });

  it("accepts full-width prefix colons", () => {
    const result = parseAccountImportText(`账号 3： ${accountLine()}`);

    assert.deepEqual(result.accounts, [firstAccount]);
    assert.deepEqual(result.tokens, []);
    assert.deepEqual(result.errors, []);
  });

  it("handles BOM, CRLF, empty lines, and whitespace around fields", () => {
    const spacedFields = [
      firstAccount.email,
      firstAccount.password,
      firstAccount.totp_secret,
      firstAccount.access_token,
    ]
      .map((field) => ` ${field} `)
      .join("----");
    const result = parseAccountImportText(
      `\uFEFF\r\n  \r\n${spacedFields}\r\n opaque-token \r\n`,
    );

    assert.deepEqual(result, {
      tokens: ["opaque-token"],
      accounts: [firstAccount],
      errors: [],
    });
  });

  it("supports mixed plain tokens and structured records", () => {
    const result = parseAccountImportText(
      `opaque-token\n${accountLine()}\nother-opaque-token`,
    );

    assert.deepEqual(result, {
      tokens: ["opaque-token", "other-opaque-token"],
      accounts: [firstAccount],
      errors: [],
    });
  });

  it("retains structured metadata when a plain token appears first", () => {
    const result = parseAccountImportText(
      `${firstAccount.access_token}\n${accountLine()}\n${firstAccount.access_token}`,
    );

    assert.deepEqual(result, {
      tokens: [],
      accounts: [firstAccount],
      errors: [],
    });
  });

  it("retains structured metadata when a plain token appears later", () => {
    const result = parseAccountImportText(
      `${accountLine()}\n${firstAccount.access_token}`,
    );

    assert.deepEqual(result, {
      tokens: [],
      accounts: [firstAccount],
      errors: [],
    });
  });

  it("deduplicates structured records by access token", () => {
    const result = parseAccountImportText(
      `${accountLine()}\n${accountLine()}\n${accountLine(secondAccount)}`,
    );

    assert.deepEqual(result, {
      tokens: [],
      accounts: [firstAccount, secondAccount],
      errors: [],
    });
  });

  it("rejects malformed field counts without falling back to plain tokens", () => {
    const result = parseAccountImportText(
      "first@example.test----synthetic-password----synthetic-key\n" +
        "second@example.test----synthetic-password----synthetic-key----synthetic-token----extra",
    );

    assert.deepEqual(result.tokens, []);
    assert.deepEqual(result.accounts, []);
    assert.deepEqual(
      result.errors.map((error) => error.line),
      [1, 2],
    );
  });

  it("rejects every missing required structured field", () => {
    const fields = [
      firstAccount.email,
      firstAccount.password,
      firstAccount.totp_secret,
      firstAccount.access_token,
    ];
    const lines = fields.map((_, emptyIndex) =>
      fields.map((field, index) => (index === emptyIndex ? " " : field)).join("----"),
    );
    const result = parseAccountImportText(lines.join("\n"));

    assert.deepEqual(result.tokens, []);
    assert.deepEqual(result.accounts, []);
    assert.deepEqual(
      result.errors.map((error) => error.line),
      [1, 2, 3, 4],
    );
  });

  it("rejects whitespace inside a structured access token", () => {
    const result = parseAccountImportText(
      `${firstAccount.email}----${firstAccount.password}----${firstAccount.totp_secret}----synthetic token\n` +
        `${secondAccount.email}----${secondAccount.password}----${secondAccount.totp_secret}----synthetic\ttoken`,
    );

    assert.deepEqual(result.tokens, []);
    assert.deepEqual(result.accounts, []);
    assert.deepEqual(
      result.errors.map((error) => error.line),
      [1, 2],
    );
  });

  it("reports original physical line numbers while preserving valid entries", () => {
    const result = parseAccountImportText(
      `\uFEFF\r\nopaque-token\r\n\r\n账号4: ${firstAccount.email}----${firstAccount.password}----${firstAccount.totp_secret}\r\n${accountLine(secondAccount)}`,
    );

    assert.deepEqual(result.tokens, ["opaque-token"]);
    assert.deepEqual(result.accounts, [secondAccount]);
    assert.equal(result.errors.length, 1);
    assert.equal(result.errors[0].line, 4);
  });

  it("never includes credentials or original records in error messages", () => {
    const invalidLine = `${accountLine()}----unexpected-fifth-field`;
    const result = parseAccountImportText(invalidLine);

    assert.equal(result.errors.length, 1);
    assert.equal(typeof result.errors[0].message, "string");
    assert.ok(result.errors[0].message.length > 0);
    for (const credential of [
      firstAccount.email,
      firstAccount.password,
      firstAccount.totp_secret,
      firstAccount.access_token,
      invalidLine,
    ]) {
      assert.ok(!result.errors[0].message.includes(credential));
    }
  });

  it("returns empty collections for empty or blank input", () => {
    for (const value of ["", "\uFEFF", "\uFEFF\r\n  \n\t"]) {
      assert.deepEqual(parseAccountImportText(value), {
        tokens: [],
        accounts: [],
        errors: [],
      });
    }
  });
});
