import assert from "node:assert/strict";
import test from "node:test";

import { createGateway } from "../src/worker.mjs";


class MemoryStore {
  constructor() {
    this.groups = new Map();
    this.updates = new Map();
    this.pendingDiscards = new Map();
    this.failDiscardPromptBind = false;
  }

  async issueAuthorizationSet(groupKey, records) {
    if (!this.groups.has(groupKey)) {
      this.groups.set(groupKey, records.map((record) => ({ ...record })));
    }
    return this.groups.get(groupKey).map((record) => ({ ...record }));
  }

  async claimUpdate(updateId) {
    if (this.updates.has(updateId)) {
      return false;
    }
    this.updates.set(updateId, { status: "received" });
    return true;
  }

  async consumeAuthorization(token, scope, consumedAt) {
    const record = [...this.groups.values()]
      .flat()
      .find((candidate) => candidate.token === token);
    if (
      !record ||
      record.status === "consumed" ||
      record.actorId !== scope.actorId ||
      record.chatId !== scope.chatId ||
      new Date(record.expiresAt) <= new Date(consumedAt)
    ) {
      return null;
    }
    record.status = "consumed";
    record.consumedAt = consumedAt;
    return { ...record };
  }

  async releaseAuthorization(token) {
    const record = [...this.groups.values()]
      .flat()
      .find((candidate) => candidate.token === token);
    if (!record || record.status !== "consumed") {
      return false;
    }
    record.status = "issued";
    delete record.consumedAt;
    return true;
  }

  async markUpdate(updateId, status, evidence = "") {
    this.updates.set(updateId, { status, evidence });
  }

  async beginDiscardReason(requestId, record) {
    this.pendingDiscards.set(requestId, {
      ...record,
      requestId,
      promptMessageId: null,
      reason: null,
      replyUpdateId: null,
      status: "prompting",
    });
  }

  async bindDiscardReasonPrompt(requestId, promptMessageId) {
    if (this.failDiscardPromptBind) {
      throw new Error("D1 bind failed");
    }
    const record = this.pendingDiscards.get(requestId);
    if (!record || record.status !== "prompting") {
      return false;
    }
    for (const candidate of this.pendingDiscards.values()) {
      if (
        candidate.requestId !== requestId &&
        candidate.status === "pending" &&
        candidate.applicationId === record.applicationId &&
        candidate.vacancyVersion === record.vacancyVersion &&
        candidate.actorId === record.actorId &&
        candidate.chatId === record.chatId
      ) {
        candidate.status = "superseded";
      }
    }
    record.promptMessageId = promptMessageId;
    record.status = "pending";
    return true;
  }

  async failDiscardReasonRequest(requestId) {
    const record = this.pendingDiscards.get(requestId);
    if (record) {
      record.status = "failed";
    }
  }

  async markDiscardReasonUncertain(requestId, promptMessageId = null) {
    const record = this.pendingDiscards.get(requestId);
    if (record) {
      record.promptMessageId = promptMessageId;
      record.status = "uncertain";
    }
  }

  async stageDiscardReason(
    updateId,
    promptMessageId,
    scope,
    reason,
    consumedAt,
  ) {
    if (this.updates.has(updateId)) {
      return { outcome: "duplicate" };
    }
    this.updates.set(updateId, {
      status: "received",
      evidence: reason,
    });
    const record = [...this.pendingDiscards.values()].find(
      (candidate) => candidate.promptMessageId === promptMessageId,
    );
    if (
      !record ||
      (
        record.status !== "pending" &&
        !(
          record.status === "uncertain" &&
          record.reason === null
        )
      ) ||
      record.actorId !== scope.actorId ||
      record.chatId !== scope.chatId ||
      new Date(record.expiresAt) <= new Date(consumedAt)
    ) {
      return { outcome: "unmatched" };
    }
    record.status = "dispatching";
    record.reason = reason;
    record.replyUpdateId = updateId;
    record.consumedAt = consumedAt;
    return { outcome: "staged", ...record };
  }

  async markDiscardReasonDispatched(requestId) {
    const record = this.pendingDiscards.get(requestId);
    if (record) {
      record.status = "dispatched";
    }
  }
}


const env = {
  DB: {},
  INTERNAL_API_TOKEN: "internal-secret",
  TELEGRAM_WEBHOOK_SECRET: "telegram-secret",
  TELEGRAM_BOT_TOKEN: "bot-secret",
  TELEGRAM_ACTOR_ID: "123456789",
  TELEGRAM_CHAT_ID: "123456789",
  GITHUB_DISPATCH_TOKEN: "github-secret",
  GITHUB_REPOSITORY: "example-org/job-agent",
};


test("authorized issuer returns stable short-lived role controls", async () => {
  const store = new MemoryStore();
  let sequence = 0;
  const gateway = createGateway({
    storeFactory: () => store,
    tokenFactory: () => `opaque-token-${++sequence}`,
    now: () => new Date("2026-07-26T10:00:00Z"),
  });
  const body = {
    event_id: "alert:modelco:ai-scientist:top-tier",
    application_id: "approved-25764671169e97eb",
    official_vacancy_version: `sha256:${"a".repeat(64)}`,
    actor_id: "123456789",
    chat_id: "123456789",
  };
  const request = () =>
    new Request("https://gateway.test/v1/authorizations", {
      method: "POST",
      headers: {
        authorization: "Bearer internal-secret",
        "content-type": "application/json",
      },
      body: JSON.stringify(body),
    });

  const first = await gateway.fetch(request(), env);
  const second = await gateway.fetch(request(), env);

  assert.equal(first.status, 200);
  assert.deepEqual(await first.json(), await second.json());
  const payload = await (
    await gateway.fetch(request(), env)
  ).json();
  assert.deepEqual(
    payload.buttons.map((button) => button.text),
    ["👍", "👎", "Dimmi di più"],
  );
  assert.ok(
    payload.buttons.every(
      (button) =>
        button.callback_data.startsWith("ja1:opaque-token-") &&
        button.callback_data.length <= 64,
    ),
  );
  assert.deepEqual(
    store.groups.get(body.event_id).map((record) => record.action),
    ["prepare", "discard", "details"],
  );
  assert.ok(
    store.groups
      .get(body.event_id)
      .every(
        (record) =>
          record.expiresAt === "2026-07-26T10:15:00.000Z" &&
          record.applicationId === body.application_id &&
          record.vacancyVersion === body.official_vacancy_version,
      ),
  );
});


test("valid Telegram callback dispatches the exact decision to GitHub once", async () => {
  const store = new MemoryStore();
  const calls = [];
  const gateway = createGateway({
    storeFactory: () => store,
    tokenFactory: () => "opaque-decision-token",
    now: () => new Date("2026-07-26T10:00:00Z"),
    fetchImpl: async (url, options) => {
      calls.push({ url: String(url), options });
      if (String(url).includes("api.github.com")) {
        return new Response(null, { status: 204 });
      }
      return Response.json({ ok: true, result: true });
    },
  });
  const authorization = await gateway.fetch(
    new Request("https://gateway.test/v1/authorizations", {
      method: "POST",
      headers: {
        authorization: "Bearer internal-secret",
        "content-type": "application/json",
      },
      body: JSON.stringify({
        event_id: "alert:modelco:ai-scientist:top-tier",
        application_id: "approved-25764671169e97eb",
        official_vacancy_version: `sha256:${"a".repeat(64)}`,
        actor_id: "123456789",
        chat_id: "123456789",
      }),
    }),
    env,
  );
  const callbackData = (await authorization.json()).buttons[0].callback_data;
  const update = {
    update_id: 4242,
    callback_query: {
      id: "callback-4242",
      from: { id: 123456789 },
      message: { chat: { id: 123456789 } },
      data: callbackData,
    },
  };
  const telegramRequest = () =>
    new Request("https://gateway.test/telegram", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-telegram-bot-api-secret-token": "telegram-secret",
      },
      body: JSON.stringify(update),
    });

  const first = await gateway.fetch(telegramRequest(), env);
  const duplicate = await gateway.fetch(telegramRequest(), env);

  assert.equal(first.status, 200);
  assert.equal(duplicate.status, 200);
  const githubCalls = calls.filter(({ url }) =>
    url.includes("api.github.com"),
  );
  assert.equal(githubCalls.length, 1);
  assert.equal(
    githubCalls[0].url,
    "https://api.github.com/repos/example-org/job-agent/dispatches",
  );
  assert.equal(
    githubCalls[0].options.headers.authorization,
    "Bearer github-secret",
  );
  assert.deepEqual(JSON.parse(githubCalls[0].options.body), {
    event_type: "telegram-opportunity-decision",
    client_payload: {
      action: "prepare",
      application_id: "approved-25764671169e97eb",
      official_vacancy_version: `sha256:${"a".repeat(64)}`,
      telegram_update_id: "4242",
      actor_id: "123456789",
      chat_id: "123456789",
    },
  });
  assert.equal(store.updates.get(4242).status, "dispatched");
  const telegramCalls = calls.filter(({ url }) =>
    url.includes("api.telegram.org"),
  );
  assert.equal(telegramCalls.length, 1);
  assert.deepEqual(JSON.parse(telegramCalls[0].options.body), {
    callback_query_id: "callback-4242",
    text: "Ricevuto",
  });
});


test("invalid Telegram webhook secret is rejected before state changes", async () => {
  const store = new MemoryStore();
  let called = false;
  const gateway = createGateway({
    storeFactory: () => store,
    fetchImpl: async () => {
      called = true;
      return new Response(null, { status: 204 });
    },
  });
  const response = await gateway.fetch(
    new Request("https://gateway.test/telegram", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-telegram-bot-api-secret-token": "wrong-secret",
      },
      body: JSON.stringify({ update_id: 1 }),
    }),
    env,
  );

  assert.equal(response.status, 401);
  assert.equal(store.updates.size, 0);
  assert.equal(called, false);
});


test("callback from a different Telegram account never reaches GitHub", async () => {
  const store = new MemoryStore();
  const calls = [];
  const gateway = createGateway({
    storeFactory: () => store,
    tokenFactory: () => "opaque-rejected-token",
    now: () => new Date("2026-07-26T10:00:00Z"),
    fetchImpl: async (url, options) => {
      calls.push({ url: String(url), options });
      return Response.json({ ok: true });
    },
  });
  await issueTestAuthorization(gateway, "wrong-actor-event");
  const response = await gateway.fetch(
    callbackRequest({
      updateId: 44,
      actorId: 999,
      token: "opaque-rejected-token",
    }),
    env,
  );

  assert.equal(response.status, 200);
  assert.equal(store.updates.get(44).status, "rejected");
  assert.equal(
    calls.filter(({ url }) => url.includes("api.github.com")).length,
    0,
  );
});


test("uncertain GitHub transport is durable and never retried by Telegram", async () => {
  const store = new MemoryStore();
  let githubCalls = 0;
  const gateway = createGateway({
    storeFactory: () => store,
    tokenFactory: () => "opaque-uncertain-token",
    now: () => new Date("2026-07-26T10:00:00Z"),
    fetchImpl: async (url) => {
      if (String(url).includes("api.github.com")) {
        githubCalls += 1;
        throw new Error("connection reset after write");
      }
      return Response.json({ ok: true });
    },
  });
  await issueTestAuthorization(gateway, "uncertain-event");
  const request = () =>
    callbackRequest({
      updateId: 45,
      token: "opaque-uncertain-token",
    });

  assert.equal((await gateway.fetch(request(), env)).status, 200);
  assert.equal((await gateway.fetch(request(), env)).status, 200);
  assert.equal(githubCalls, 1);
  assert.equal(store.updates.get(45).status, "uncertain");
});


test("definitive GitHub rejection releases the button for a later tap", async () => {
  const store = new MemoryStore();
  let githubCalls = 0;
  const gateway = createGateway({
    storeFactory: () => store,
    tokenFactory: () => "opaque-retryable-token",
    now: () => new Date("2026-07-26T10:00:00Z"),
    fetchImpl: async (url) => {
      if (String(url).includes("api.github.com")) {
        githubCalls += 1;
        return new Response(null, {
          status: githubCalls === 1 ? 503 : 204,
        });
      }
      return Response.json({ ok: true });
    },
  });
  await issueTestAuthorization(gateway, "retryable-event");

  await gateway.fetch(
    callbackRequest({ updateId: 46, token: "opaque-retryable-token" }),
    env,
  );
  await gateway.fetch(
    callbackRequest({ updateId: 47, token: "opaque-retryable-token" }),
    env,
  );

  assert.equal(githubCalls, 2);
  assert.equal(store.updates.get(46).status, "failed");
  assert.equal(store.updates.get(47).status, "dispatched");
});


test("discard asks for a reason and dispatches only the exact reply once", async () => {
  const store = new MemoryStore();
  const calls = [];
  let sequence = 0;
  const gateway = createGateway({
    storeFactory: () => store,
    tokenFactory: () => `discard-flow-token-${++sequence}`,
    now: () => new Date("2026-07-26T10:00:00Z"),
    fetchImpl: async (url, options) => {
      calls.push({ url: String(url), options });
      if (String(url).includes("api.github.com")) {
        return new Response(null, { status: 204 });
      }
      if (String(url).endsWith("/sendMessage")) {
        return Response.json({
          ok: true,
          result: {
            message_id: 777,
            chat: { id: 123456789 },
          },
        });
      }
      return Response.json({ ok: true, result: true });
    },
  });
  const authorization = await issueTestAuthorization(
    gateway,
    "discard-reason-event",
  );
  const discardData = (await authorization.json()).buttons[1].callback_data;

  await gateway.fetch(
    callbackRequest({
      updateId: 48,
      token: discardData.slice("ja1:".length),
    }),
    env,
  );

  assert.equal(
    calls.filter(({ url }) => url.includes("api.github.com")).length,
    0,
  );
  assert.equal(store.updates.get(48).status, "awaiting_reason");
  const prompt = [...store.pendingDiscards.values()].find(
    (record) => record.promptMessageId === 777,
  );
  assert.equal(prompt.applicationId, "approved-25764671169e97eb");
  assert.equal(prompt.status, "pending");
  const promptCall = calls.find(({ url }) => url.endsWith("/sendMessage"));
  assert.deepEqual(JSON.parse(promptCall.options.body), {
    chat_id: "123456789",
    text: "Perché vuoi scartare questa posizione?",
    reply_markup: {
      force_reply: true,
      selective: true,
      input_field_placeholder: "Scrivi il motivo dello scarto",
    },
  });

  const reasonRequest = () =>
    messageRequest({
      updateId: 49,
      messageId: 778,
      replyToMessageId: 777,
      text: "Il ruolo richiede esperienza Lead AI Scientist.",
    });
  assert.equal((await gateway.fetch(reasonRequest(), env)).status, 200);
  assert.equal((await gateway.fetch(reasonRequest(), env)).status, 200);

  const githubCalls = calls.filter(({ url }) =>
    url.includes("api.github.com"),
  );
  assert.equal(githubCalls.length, 1);
  assert.deepEqual(JSON.parse(githubCalls[0].options.body), {
    event_type: "telegram-opportunity-decision",
    client_payload: {
      action: "discard",
      application_id: "approved-25764671169e97eb",
      official_vacancy_version: `sha256:${"a".repeat(64)}`,
      telegram_update_id: "49",
      actor_id: "123456789",
      chat_id: "123456789",
      reason: "Il ruolo richiede esperienza Lead AI Scientist.",
    },
  });
  assert.equal(store.updates.get(49).status, "dispatched");
  assert.equal(prompt.status, "dispatched");
  assert.equal(
    prompt.reason,
    "Il ruolo richiede esperienza Lead AI Scientist.",
  );
});


test("a visible discard prompt remains usable when prompt binding fails", async () => {
  const store = new MemoryStore();
  store.failDiscardPromptBind = true;
  let sequence = 0;
  let githubCalls = 0;
  const gateway = createGateway({
    storeFactory: () => store,
    tokenFactory: () => `bind-failure-token-${++sequence}`,
    now: () => new Date("2026-07-26T10:00:00Z"),
    fetchImpl: async (url) => {
      if (String(url).includes("api.github.com")) {
        githubCalls += 1;
        return new Response(null, { status: 204 });
      }
      if (String(url).endsWith("/sendMessage")) {
        return Response.json({
          ok: true,
          result: { message_id: 888, chat: { id: 123456789 } },
        });
      }
      return Response.json({ ok: true, result: true });
    },
  });
  const authorization = await issueTestAuthorization(
    gateway,
    "discard-bind-failure",
  );
  const discardData = (await authorization.json()).buttons[1].callback_data;

  const response = await gateway.fetch(
    callbackRequest({
      updateId: 50,
      token: discardData.slice("ja1:".length),
    }),
    env,
  );

  assert.equal(response.status, 200);
  assert.equal(store.updates.get(50).status, "uncertain");
  const request = [...store.pendingDiscards.values()][0];
  assert.equal(request.status, "uncertain");
  assert.equal(request.promptMessageId, 888);

  await gateway.fetch(
    messageRequest({
      updateId: 53,
      messageId: 889,
      replyToMessageId: 888,
      text: "Il ruolo è Lead e non ho i requisiti per quel livello.",
    }),
    env,
  );

  assert.equal(githubCalls, 1);
  assert.equal(request.status, "dispatched");
});


test("a definitive discard rejection opens a new force-reply prompt", async () => {
  const store = new MemoryStore();
  const calls = [];
  let tokenSequence = 0;
  let messageSequence = 900;
  const gateway = createGateway({
    storeFactory: () => store,
    tokenFactory: () => `retry-reason-token-${++tokenSequence}`,
    now: () => new Date("2026-07-26T10:00:00Z"),
    fetchImpl: async (url, options) => {
      calls.push({ url: String(url), options });
      if (String(url).includes("api.github.com")) {
        return new Response(null, { status: 503 });
      }
      if (String(url).endsWith("/sendMessage")) {
        return Response.json({
          ok: true,
          result: {
            message_id: ++messageSequence,
            chat: { id: 123456789 },
          },
        });
      }
      return Response.json({ ok: true, result: true });
    },
  });
  const authorization = await issueTestAuthorization(
    gateway,
    "discard-reason-retry",
  );
  const discardData = (await authorization.json()).buttons[1].callback_data;
  await gateway.fetch(
    callbackRequest({
      updateId: 51,
      token: discardData.slice("ja1:".length),
    }),
    env,
  );

  await gateway.fetch(
    messageRequest({
      updateId: 52,
      messageId: 902,
      replyToMessageId: 901,
      text: "Il livello Lead è troppo senior per il mio profilo.",
    }),
    env,
  );

  const prompts = calls
    .filter(({ url }) => url.endsWith("/sendMessage"))
    .map(({ options }) => JSON.parse(options.body))
    .filter((payload) => payload.reply_markup?.force_reply === true);
  assert.equal(prompts.length, 2);
  assert.equal(
    prompts[1].text,
    "GitHub non ha accettato lo scarto. Scrivi di nuovo il motivo.",
  );
  assert.equal(store.pendingDiscards.get("callback:51").status, "failed");
  assert.equal(store.pendingDiscards.get("retry:52").status, "pending");
});


function issueTestAuthorization(gateway, eventId) {
  return gateway.fetch(
    new Request("https://gateway.test/v1/authorizations", {
      method: "POST",
      headers: {
        authorization: "Bearer internal-secret",
        "content-type": "application/json",
      },
      body: JSON.stringify({
        event_id: eventId,
        application_id: "approved-25764671169e97eb",
        official_vacancy_version: `sha256:${"a".repeat(64)}`,
        actor_id: "123456789",
        chat_id: "123456789",
      }),
    }),
    env,
  );
}


function callbackRequest({
  updateId,
  actorId = 123456789,
  chatId = 123456789,
  token,
}) {
  return new Request("https://gateway.test/telegram", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-telegram-bot-api-secret-token": "telegram-secret",
    },
    body: JSON.stringify({
      update_id: updateId,
      callback_query: {
        id: `callback-${updateId}`,
        from: { id: actorId },
        message: { chat: { id: chatId } },
        data: `ja1:${token}`,
      },
    }),
  });
}


function messageRequest({
  updateId,
  messageId,
  replyToMessageId,
  text,
  actorId = 123456789,
  chatId = 123456789,
}) {
  return new Request("https://gateway.test/telegram", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-telegram-bot-api-secret-token": "telegram-secret",
    },
    body: JSON.stringify({
      update_id: updateId,
      message: {
        message_id: messageId,
        from: { id: actorId },
        chat: { id: chatId },
        text,
        reply_to_message: { message_id: replyToMessageId },
      },
    }),
  });
}
