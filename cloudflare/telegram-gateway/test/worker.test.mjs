import assert from "node:assert/strict";
import test from "node:test";

import { createGateway } from "../src/worker.mjs";


class MemoryStore {
  constructor() {
    this.groups = new Map();
    this.updates = new Map();
    this.pendingDiscards = new Map();
    this.reviews = new Map();
    this.failDiscardPromptBind = false;
  }

  async issueReviewAuthorizationSet(eventKey, review, records) {
    if (!this.reviews.has(eventKey)) {
      this.reviews.set(eventKey, {
        ...review,
        status: "authorizing",
        authorizations: records.map((record) => ({ ...record })),
      });
    }
    const stored = this.reviews.get(eventKey);
    return {
      review: { ...stored },
      authorizations: stored.authorizations.map((record) => ({ ...record })),
    };
  }

  async bindReviewMessages(reviewId, scope, messageIds) {
    const stored = [...this.reviews.values()].find(
      (candidate) => candidate.reviewId === reviewId,
    );
    if (
      !stored ||
      stored.actorId !== scope.actorId ||
      stored.chatId !== scope.chatId
    ) {
      return null;
    }
    if (stored.status === "pending") {
      return stored.documentMessageIds.join(",") ===
        messageIds.documentMessageIds.join(",") &&
        stored.controlMessageId === messageIds.controlMessageId
        ? { ...stored }
        : null;
    }
    if (stored.status !== "authorizing") {
      return null;
    }
    stored.documentMessageIds = [...messageIds.documentMessageIds];
    stored.controlMessageId = messageIds.controlMessageId;
    stored.status = "pending";
    return { ...stored };
  }

  async consumeReviewAuthorization(token, scope, consumedAt) {
    const review = [...this.reviews.values()].find((candidate) =>
      candidate.authorizations.some((item) => item.token === token),
    );
    const authorization = review?.authorizations.find(
      (item) => item.token === token,
    );
    if (
      !review ||
      !authorization ||
      authorization.status === "consumed" ||
      review.status !== "pending" ||
      review.actorId !== scope.actorId ||
      review.chatId !== scope.chatId ||
      new Date(review.expiresAt) <= new Date(consumedAt)
    ) {
      return null;
    }
    authorization.status = "consumed";
    review.status = "deciding";
    review.decision = authorization.action;
    return {
      reviewId: review.reviewId,
      action: authorization.action,
      applicationId: review.applicationId,
      vacancyVersion: review.vacancyVersion,
      packageHash: review.packageHash,
      actorId: review.actorId,
      chatId: review.chatId,
      documentMessageIds: [...review.documentMessageIds],
      controlMessageId: review.controlMessageId,
    };
  }

  async markReviewDecided(reviewId, status, decidedAt) {
    const review = [...this.reviews.values()].find(
      (candidate) => candidate.reviewId === reviewId,
    );
    review.status = status;
    review.decidedAt = decidedAt;
  }

  async markReviewCleanupUncertain(reviewId, evidence) {
    const review = [...this.reviews.values()].find(
      (candidate) => candidate.reviewId === reviewId,
    );
    review.status = "cleanup_uncertain";
    review.evidence = evidence;
  }

  async markReviewDispatchUncertain(reviewId, evidence) {
    const review = [...this.reviews.values()].find(
      (candidate) => candidate.reviewId === reviewId,
    );
    review.status = "dispatch_uncertain";
    review.evidence = evidence;
  }

  async claimExpiredReviews(observedAt) {
    const expired = [];
    for (const review of this.reviews.values()) {
      if (
        ["pending", "expiring", "expiry_cleanup_uncertain"].includes(
          review.status,
        ) &&
        new Date(review.expiresAt) <= new Date(observedAt)
      ) {
        review.status = "expiring";
        expired.push({
          reviewId: review.reviewId,
          chatId: review.chatId,
          documentMessageIds: [...review.documentMessageIds],
          controlMessageId: review.controlMessageId,
        });
      }
    }
    return expired;
  }

  async claimDecisionCleanupRetries() {
    const retries = [];
    for (const review of this.reviews.values()) {
      if (["cleanup_uncertain", "cleanup_retrying"].includes(review.status)) {
        review.status = "cleanup_retrying";
        retries.push({
          reviewId: review.reviewId,
          action: review.decision,
          applicationId: review.applicationId,
          vacancyVersion: review.vacancyVersion,
          packageHash: review.packageHash,
          actorId: review.actorId,
          chatId: review.chatId,
          documentMessageIds: [...review.documentMessageIds],
          controlMessageId: review.controlMessageId,
        });
      }
    }
    return retries;
  }

  async markReviewExpired(reviewId, expiredAt) {
    const review = [...this.reviews.values()].find(
      (candidate) => candidate.reviewId === reviewId,
    );
    review.status = "expired";
    review.decidedAt = expiredAt;
  }

  async markReviewExpiryUncertain(reviewId, evidence) {
    const review = [...this.reviews.values()].find(
      (candidate) => candidate.reviewId === reviewId,
    );
    review.status = "expiry_cleanup_uncertain";
    review.evidence = evidence;
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

  async getExpiredAuthorization(token, scope, observedAt) {
    const entry = [...this.groups.entries()]
      .flatMap(([groupKey, records]) =>
        records.map((record) => ({ groupKey, record })),
      )
      .find(
        ({ record }) =>
          record.token === token &&
          record.status !== "consumed" &&
          record.actorId === scope.actorId &&
          record.chatId === scope.chatId &&
          new Date(record.expiresAt) <= new Date(observedAt),
      );
    return entry
      ? {
          groupKey: entry.groupKey,
          applicationId: entry.record.applicationId,
          vacancyVersion: entry.record.vacancyVersion,
        }
      : null;
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


test("review issuer returns stable package-scoped approval controls", async () => {
  const store = new MemoryStore();
  let sequence = 0;
  const gateway = createGateway({
    storeFactory: () => store,
    tokenFactory: () => `review-token-${++sequence}`,
    now: () => new Date("2026-08-20T10:00:00Z"),
  });
  const body = {
    event_id: "review:approved-25764671169e97eb:package-a",
    application_id: "approved-25764671169e97eb",
    official_vacancy_version: `sha256:${"a".repeat(64)}`,
    package_hash: `sha256:${"b".repeat(64)}`,
    actor_id: "123456789",
    chat_id: "123456789",
  };
  const request = () =>
    new Request("https://gateway.test/v1/review-authorizations", {
      method: "POST",
      headers: {
        authorization: "Bearer internal-secret",
        "content-type": "application/json",
      },
      body: JSON.stringify(body),
    });

  const first = await (await gateway.fetch(request(), env)).json();
  const replay = await (await gateway.fetch(request(), env)).json();

  assert.deepEqual(replay, first);
  assert.match(first.review_id, /^review-token-/u);
  assert.equal(first.expires_at, "2026-08-21T10:00:00.000Z");
  assert.deepEqual(
    first.buttons.map((button) => button.text),
    ["✅ Approva", "🔄 Rigenera"],
  );
  assert.ok(
    first.buttons.every(
      (button) =>
        button.callback_data.startsWith("jar1:review-token-") &&
        button.callback_data.length <= 64,
    ),
  );
  const stored = store.reviews.get(body.event_id);
  assert.equal(stored.applicationId, body.application_id);
  assert.equal(stored.vacancyVersion, body.official_vacancy_version);
  assert.equal(stored.packageHash, body.package_hash);
  assert.deepEqual(
    stored.authorizations.map((record) => record.action),
    ["approve_artifacts", "regenerate_artifacts"],
  );
});


test("review message receipts bind once to the exact protected review", async () => {
  const store = new MemoryStore();
  let sequence = 0;
  const gateway = createGateway({
    storeFactory: () => store,
    tokenFactory: () => `bind-token-${++sequence}`,
    now: () => new Date("2026-08-20T10:00:00Z"),
  });
  const issued = await (
    await gateway.fetch(reviewAuthorizationRequest(), env)
  ).json();
  const bindRequest = (documentMessageIds = [701, 702]) =>
    new Request(
      `https://gateway.test/v1/artifact-reviews/${issued.review_id}/messages`,
      {
        method: "POST",
        headers: {
          authorization: "Bearer internal-secret",
          "content-type": "application/json",
        },
        body: JSON.stringify({
          document_message_ids: documentMessageIds,
          control_message_id: 703,
        }),
      },
    );

  const first = await gateway.fetch(bindRequest(), env);
  const replay = await gateway.fetch(bindRequest(), env);
  const mismatch = await gateway.fetch(bindRequest([801, 802]), env);

  assert.equal(first.status, 200);
  assert.deepEqual(await first.json(), { status: "pending" });
  assert.equal(replay.status, 200);
  assert.equal(mismatch.status, 409);
  const stored = store.reviews.get(
    "review:approved-25764671169e97eb:package-a",
  );
  assert.deepEqual(stored.documentMessageIds, [701, 702]);
  assert.equal(stored.controlMessageId, 703);
  assert.equal(stored.status, "pending");
});


test("artifact approval deletes protected review then dispatches exact package once", async () => {
  const store = new MemoryStore();
  const calls = [];
  let sequence = 0;
  const gateway = createGateway({
    storeFactory: () => store,
    tokenFactory: () => `approve-token-${++sequence}`,
    now: () => new Date("2026-08-20T10:00:00Z"),
    fetchImpl: async (url, options) => {
      calls.push({ url: String(url), options });
      if (String(url).includes("api.github.com")) {
        return new Response(null, { status: 204 });
      }
      return Response.json({ ok: true, result: true });
    },
  });
  const issued = await (
    await gateway.fetch(reviewAuthorizationRequest(), env)
  ).json();
  await gateway.fetch(
    reviewBindRequest(issued.review_id, [701, 702], 703),
    env,
  );
  const approveToken = issued.buttons[0].callback_data.slice("jar1:".length);
  const request = () =>
    callbackRequest({
      updateId: 61,
      messageId: 703,
      token: approveToken,
      prefix: "jar1:",
    });

  const first = await gateway.fetch(request(), env);
  const duplicate = await gateway.fetch(request(), env);

  assert.equal(first.status, 200);
  assert.equal(duplicate.status, 200);
  const deletes = calls.filter(({ url }) => url.endsWith("/deleteMessage"));
  assert.deepEqual(
    deletes.map(({ options }) => JSON.parse(options.body).message_id),
    [701, 702, 703],
  );
  assert.ok(
    deletes.every(
      ({ options }) => JSON.parse(options.body).chat_id === "123456789",
    ),
  );
  const dispatches = calls.filter(({ url }) =>
    url.includes("api.github.com"),
  );
  assert.equal(dispatches.length, 1);
  assert.deepEqual(JSON.parse(dispatches[0].options.body), {
    event_type: "telegram-opportunity-decision",
    client_payload: {
      action: "approve_artifacts",
      review_id: issued.review_id,
      application_id: "approved-25764671169e97eb",
      official_vacancy_version: `sha256:${"a".repeat(64)}`,
      package_hash: `sha256:${"b".repeat(64)}`,
      telegram_update_id: "61",
      actor_id: "123456789",
      chat_id: "123456789",
    },
  });
  assert.equal(
    store.reviews.get("review:approved-25764671169e97eb:package-a").status,
    "approved",
  );
  assert.equal(store.updates.get(61).status, "dispatched");
});


test("artifact regeneration deletes review and dispatches a fresh generation", async () => {
  const store = new MemoryStore();
  const calls = [];
  let sequence = 0;
  const gateway = createGateway({
    storeFactory: () => store,
    tokenFactory: () => `regenerate-token-${++sequence}`,
    now: () => new Date("2026-08-20T10:00:00Z"),
    fetchImpl: async (url, options) => {
      calls.push({ url: String(url), options });
      return String(url).includes("api.github.com")
        ? new Response(null, { status: 204 })
        : Response.json({ ok: true, result: true });
    },
  });
  const issued = await (
    await gateway.fetch(reviewAuthorizationRequest(), env)
  ).json();
  await gateway.fetch(
    reviewBindRequest(issued.review_id, [721, 722], 723),
    env,
  );
  const token = issued.buttons[1].callback_data.slice("jar1:".length);

  await gateway.fetch(
    callbackRequest({
      updateId: 62,
      messageId: 723,
      token,
      prefix: "jar1:",
    }),
    env,
  );

  const dispatch = calls.find(({ url }) => url.includes("api.github.com"));
  assert.equal(
    JSON.parse(dispatch.options.body).client_payload.action,
    "regenerate_artifacts",
  );
  assert.equal(
    JSON.parse(dispatch.options.body).client_payload.review_id,
    issued.review_id,
  );
  assert.equal(
    store.reviews.get("review:approved-25764671169e97eb:package-a").status,
    "regenerate_requested",
  );
});


test("a partial decision cleanup is reconciled before GitHub dispatch", async () => {
  const store = new MemoryStore();
  let sequence = 0;
  const attempts = new Map();
  const github = [];
  const gateway = createGateway({
    storeFactory: () => store,
    tokenFactory: () => `cleanup-token-${++sequence}`,
    now: () => new Date("2026-08-20T10:00:00Z"),
    fetchImpl: async (url, options) => {
      if (String(url).includes("api.github.com")) {
        github.push(JSON.parse(options.body));
        return new Response(null, { status: 204 });
      }
      if (!String(url).endsWith("/deleteMessage")) {
        return Response.json({ ok: true, result: true });
      }
      const messageId = JSON.parse(options.body).message_id;
      const count = (attempts.get(messageId) || 0) + 1;
      attempts.set(messageId, count);
      if (messageId === 702 && count === 1) {
        throw new Error("temporary Telegram transport failure");
      }
      if (messageId === 701 && count === 2) {
        return Response.json(
          {
            ok: false,
            error_code: 400,
            description: "Bad Request: message to delete not found",
          },
          { status: 400 },
        );
      }
      return Response.json({ ok: true, result: true });
    },
  });
  const issued = await (
    await gateway.fetch(reviewAuthorizationRequest(), env)
  ).json();
  await gateway.fetch(
    reviewBindRequest(issued.review_id, [701, 702], 703),
    env,
  );
  const token = issued.buttons[0].callback_data.slice("jar1:".length);

  await gateway.fetch(
    callbackRequest({
      updateId: 63,
      messageId: 703,
      token,
      prefix: "jar1:",
    }),
    env,
  );
  assert.equal(github.length, 0);

  await gateway.scheduled({}, env, {});

  assert.equal(github.length, 1);
  assert.equal(github[0].client_payload.action, "approve_artifacts");
  assert.equal(github[0].client_payload.review_id, issued.review_id);
  assert.equal(
    store.reviews.get("review:approved-25764671169e97eb:package-a").status,
    "approved",
  );
});


test("scheduled cleanup deletes an undecided protected review after 24 hours once", async () => {
  const store = new MemoryStore();
  const calls = [];
  let sequence = 0;
  let current = new Date("2026-08-20T10:00:00Z");
  const gateway = createGateway({
    storeFactory: () => store,
    tokenFactory: () => `expiry-token-${++sequence}`,
    now: () => current,
    fetchImpl: async (url, options) => {
      calls.push({ url: String(url), options });
      return Response.json({ ok: true, result: true });
    },
  });
  const issued = await (
    await gateway.fetch(reviewAuthorizationRequest(), env)
  ).json();
  await gateway.fetch(
    reviewBindRequest(issued.review_id, [711, 712], 713),
    env,
  );
  current = new Date("2026-08-21T10:00:01Z");

  await gateway.scheduled({}, env, {});
  await gateway.scheduled({}, env, {});

  const deletes = calls.filter(({ url }) => url.endsWith("/deleteMessage"));
  assert.deepEqual(
    deletes.map(({ options }) => JSON.parse(options.body).message_id),
    [711, 712, 713],
  );
  assert.equal(
    calls.filter(({ url }) => url.includes("api.github.com")).length,
    0,
  );
  assert.equal(
    store.reviews.get("review:approved-25764671169e97eb:package-a").status,
    "expired",
  );
});


test("scheduled cleanup resumes safely after a partial Telegram delete", async () => {
  const store = new MemoryStore();
  let sequence = 0;
  let current = new Date("2026-08-20T10:00:00Z");
  const attempts = new Map();
  const gateway = createGateway({
    storeFactory: () => store,
    tokenFactory: () => `resume-token-${++sequence}`,
    now: () => current,
    fetchImpl: async (_url, options) => {
      const messageId = JSON.parse(options.body).message_id;
      const count = (attempts.get(messageId) || 0) + 1;
      attempts.set(messageId, count);
      if (messageId === 712 && count === 1) {
        throw new Error("temporary Telegram transport failure");
      }
      if (messageId === 711 && count === 2) {
        return Response.json(
          {
            ok: false,
            error_code: 400,
            description: "Bad Request: message to delete not found",
          },
          { status: 400 },
        );
      }
      return Response.json({ ok: true, result: true });
    },
  });
  const issued = await (
    await gateway.fetch(reviewAuthorizationRequest(), env)
  ).json();
  await gateway.fetch(
    reviewBindRequest(issued.review_id, [711, 712], 713),
    env,
  );
  current = new Date("2026-08-21T10:00:01Z");

  await gateway.scheduled({}, env, {});
  await gateway.scheduled({}, env, {});

  assert.equal(attempts.get(711), 2);
  assert.equal(attempts.get(712), 2);
  assert.equal(attempts.get(713), 1);
  assert.equal(
    store.reviews.get("review:approved-25764671169e97eb:package-a").status,
    "expired",
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
      message: {
        message_id: 4242,
        chat: { id: 123456789 },
      },
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


test("expired role button refreshes the same card without dispatching", async () => {
  const store = new MemoryStore();
  const calls = [];
  let sequence = 0;
  let current = new Date("2026-07-26T10:00:00Z");
  const gateway = createGateway({
    storeFactory: () => store,
    tokenFactory: () => `refresh-token-${++sequence}`,
    now: () => current,
    fetchImpl: async (url, options) => {
      calls.push({ url: String(url), options });
      if (String(url).includes("api.github.com")) {
        return new Response(null, { status: 204 });
      }
      return Response.json({ ok: true, result: true });
    },
  });
  const authorization = await issueTestAuthorization(
    gateway,
    "expired-role-card",
  );
  const expiredToken = (
    await authorization.json()
  ).buttons[0].callback_data.slice("ja1:".length);
  current = new Date("2026-07-26T10:16:00Z");

  const response = await gateway.fetch(
    callbackRequest({
      updateId: 43,
      messageId: 700,
      token: expiredToken,
    }),
    env,
  );

  assert.equal(response.status, 200);
  assert.equal(
    calls.filter(({ url }) => url.includes("api.github.com")).length,
    0,
  );
  const edit = calls.find(({ url }) =>
    url.endsWith("/editMessageReplyMarkup"),
  );
  assert.ok(edit);
  const editPayload = JSON.parse(edit.options.body);
  assert.equal(editPayload.chat_id, "123456789");
  assert.equal(editPayload.message_id, 700);
  assert.deepEqual(
    editPayload.reply_markup.inline_keyboard[0].map(
      (button) => button.text,
    ),
    ["👍", "👎", "Dimmi di più"],
  );
  assert.ok(
    editPayload.reply_markup.inline_keyboard[0].every(
      (button) =>
        button.callback_data.startsWith("ja1:refresh-token-") &&
        button.callback_data.length <= 64 &&
        button.callback_data !== `ja1:${expiredToken}`,
    ),
  );
  assert.equal(store.updates.get(43).status, "refreshed");
  const answer = calls.find(({ url }) =>
    url.endsWith("/answerCallbackQuery"),
  );
  assert.equal(
    JSON.parse(answer.options.body).text,
    "Pulsante aggiornato: premi di nuovo",
  );

  const freshToken = editPayload.reply_markup.inline_keyboard[0][0]
    .callback_data.slice("ja1:".length);
  await gateway.fetch(
    callbackRequest({
      updateId: 44,
      messageId: 700,
      token: freshToken,
    }),
    env,
  );
  const githubCalls = calls.filter(({ url }) =>
    url.includes("api.github.com"),
  );
  assert.equal(githubCalls.length, 1);
  assert.equal(store.updates.get(44).status, "dispatched");
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


function reviewAuthorizationRequest() {
  return new Request("https://gateway.test/v1/review-authorizations", {
    method: "POST",
    headers: {
      authorization: "Bearer internal-secret",
      "content-type": "application/json",
    },
    body: JSON.stringify({
      event_id: "review:approved-25764671169e97eb:package-a",
      application_id: "approved-25764671169e97eb",
      official_vacancy_version: `sha256:${"a".repeat(64)}`,
      package_hash: `sha256:${"b".repeat(64)}`,
      actor_id: "123456789",
      chat_id: "123456789",
    }),
  });
}


function reviewBindRequest(reviewId, documentMessageIds, controlMessageId) {
  return new Request(
    `https://gateway.test/v1/artifact-reviews/${reviewId}/messages`,
    {
      method: "POST",
      headers: {
        authorization: "Bearer internal-secret",
        "content-type": "application/json",
      },
      body: JSON.stringify({
        document_message_ids: documentMessageIds,
        control_message_id: controlMessageId,
      }),
    },
  );
}


function callbackRequest({
  updateId,
  actorId = 123456789,
  chatId = 123456789,
  messageId = 1,
  token,
  prefix = "ja1:",
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
        message: {
          message_id: messageId,
          chat: { id: chatId },
        },
        data: `${prefix}${token}`,
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
