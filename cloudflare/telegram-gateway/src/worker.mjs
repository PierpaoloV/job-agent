const ACTIONS = Object.freeze([
  ["prepare", "👍"],
  ["discard", "👎"],
  ["details", "Dimmi di più"],
]);
const CALLBACK_PREFIX = "ja1:";
const AUTHORIZATION_TTL_MS = 15 * 60 * 1000;
const DISCARD_REASON_TTL_MS = 24 * 60 * 60 * 1000;
const APPLICATION_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{1,127}$/;
const VACANCY_VERSION = /^sha256:[a-f0-9]{64}$/;


export class D1GatewayStore {
  constructor(database) {
    this.database = database;
  }

  async issueAuthorizationSet(groupKey, records) {
    const insert = this.database.prepare(
      "INSERT OR IGNORE INTO callback_authorizations " +
        "(token, group_key, action, application_id, vacancy_version, " +
        "actor_id, chat_id, status, expires_at, created_at) " +
        "VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 'issued', ?8, ?9)",
    );
    await this.database.batch(
      records.map((record) =>
        insert
          .bind(
            record.token,
            groupKey,
            record.action,
            record.applicationId,
            record.vacancyVersion,
            record.actorId,
            record.chatId,
            record.expiresAt,
            record.createdAt,
          ),
      ),
    );
    const result = await this.database
      .prepare(
        "SELECT token, action, application_id, vacancy_version, actor_id, " +
          "chat_id, expires_at, created_at FROM callback_authorizations " +
          "WHERE group_key = ?1 ORDER BY CASE action " +
          "WHEN 'prepare' THEN 1 WHEN 'discard' THEN 2 ELSE 3 END",
      )
      .bind(groupKey)
      .run();
    return result.results.map((row) => ({
      token: String(row.token),
      action: String(row.action),
      applicationId: String(row.application_id),
      vacancyVersion: String(row.vacancy_version),
      actorId: String(row.actor_id),
      chatId: String(row.chat_id),
      expiresAt: String(row.expires_at),
      createdAt: String(row.created_at),
    }));
  }

  async claimUpdate(updateId, receivedAt) {
    const result = await this.database
      .prepare(
        "INSERT OR IGNORE INTO telegram_updates " +
          "(update_id, status, received_at, updated_at) " +
          "VALUES (?1, 'received', ?2, ?2)",
      )
      .bind(updateId, receivedAt)
      .run();
    return result.meta.changes === 1;
  }

  async consumeAuthorization(token, scope, consumedAt) {
    const update = await this.database
      .prepare(
        "UPDATE callback_authorizations SET status = 'consumed', " +
          "consumed_at = ?1 WHERE token = ?2 AND status = 'issued' " +
          "AND actor_id = ?3 AND chat_id = ?4 AND expires_at > ?1",
      )
      .bind(
        consumedAt,
        token,
        scope.actorId,
        scope.chatId,
      )
      .run();
    if (update.meta.changes !== 1) {
      return null;
    }
    const row = await this.database
      .prepare(
        "SELECT action, application_id, vacancy_version FROM " +
          "callback_authorizations WHERE token = ?1",
      )
      .bind(token)
      .first();
    return row
      ? {
          action: String(row.action),
          applicationId: String(row.application_id),
          vacancyVersion: String(row.vacancy_version),
        }
      : null;
  }

  async releaseAuthorization(token) {
    const result = await this.database
      .prepare(
        "UPDATE callback_authorizations SET status = 'issued', " +
          "consumed_at = NULL WHERE token = ?1 AND status = 'consumed'",
      )
      .bind(token)
      .run();
    return result.meta.changes === 1;
  }

  async markUpdate(updateId, status, evidence = "") {
    await this.database
      .prepare(
        "UPDATE telegram_updates SET status = ?1, evidence = ?2, " +
          "updated_at = ?3 WHERE update_id = ?4",
      )
      .bind(status, evidence, new Date().toISOString(), updateId)
      .run();
  }

  async beginDiscardReason(requestId, record) {
    await this.database
      .prepare(
        "INSERT INTO pending_discard_reasons " +
          "(request_id, application_id, vacancy_version, actor_id, " +
          "chat_id, status, created_at, expires_at) " +
          "VALUES (?1, ?2, ?3, ?4, ?5, 'prompting', ?6, ?7)",
      )
      .bind(
        requestId,
        record.applicationId,
        record.vacancyVersion,
        record.actorId,
        record.chatId,
        record.createdAt,
        record.expiresAt,
      )
      .run();
  }

  async bindDiscardReasonPrompt(requestId, promptMessageId) {
    const bindPrompt = this.database
      .prepare(
        "UPDATE pending_discard_reasons SET prompt_message_id = ?1, " +
          "status = 'pending' WHERE request_id = ?2 " +
          "AND status = 'prompting'",
      )
      .bind(promptMessageId, requestId);
    const supersedePrevious = this.database
      .prepare(
        "UPDATE pending_discard_reasons SET status = 'superseded' " +
          "WHERE request_id <> ?1 AND status = 'pending' " +
          "AND application_id = (SELECT application_id FROM " +
          "pending_discard_reasons WHERE request_id = ?1) " +
          "AND vacancy_version = (SELECT vacancy_version FROM " +
          "pending_discard_reasons WHERE request_id = ?1) " +
          "AND actor_id = (SELECT actor_id FROM " +
          "pending_discard_reasons WHERE request_id = ?1) " +
          "AND chat_id = (SELECT chat_id FROM " +
          "pending_discard_reasons WHERE request_id = ?1) " +
          "AND EXISTS (SELECT 1 FROM pending_discard_reasons " +
          "WHERE request_id = ?1 AND status = 'pending' " +
          "AND prompt_message_id = ?2)",
      )
      .bind(requestId, promptMessageId);
    const results = await this.database.batch([
      bindPrompt,
      supersedePrevious,
    ]);
    return results[0].meta.changes === 1;
  }

  async failDiscardReasonRequest(requestId, evidence) {
    await this.database
      .prepare(
        "UPDATE pending_discard_reasons SET status = 'failed', " +
          "status_evidence = ?1 WHERE request_id = ?2 " +
          "AND status IN ('prompting', 'dispatching')",
      )
      .bind(evidence, requestId)
      .run();
  }

  async markDiscardReasonUncertain(
    requestId,
    promptMessageId,
    evidence,
  ) {
    await this.database
      .prepare(
        "UPDATE pending_discard_reasons SET status = 'uncertain', " +
          "prompt_message_id = COALESCE(?1, prompt_message_id), " +
          "status_evidence = ?2 WHERE request_id = ?3",
      )
      .bind(promptMessageId, evidence, requestId)
      .run();
  }

  async stageDiscardReason(
    updateId,
    promptMessageId,
    scope,
    reason,
    consumedAt,
  ) {
    const claimUpdate = this.database
      .prepare(
        "INSERT OR IGNORE INTO telegram_updates " +
          "(update_id, status, evidence, received_at, updated_at) " +
          "VALUES (?1, 'received', ?2, ?3, ?3)",
      )
      .bind(updateId, reason, consumedAt);
    const stageReason = this.database
      .prepare(
        "UPDATE pending_discard_reasons SET status = 'dispatching', " +
          "reason_text = ?1, reply_update_id = ?2, consumed_at = ?3 " +
          "WHERE prompt_message_id = ?4 " +
          "AND (status = 'pending' OR (" +
          "status = 'uncertain' AND reason_text IS NULL " +
          "AND status_evidence LIKE 'discard_prompt_bind_failed:%')) " +
          "AND actor_id = ?5 AND chat_id = ?6 AND expires_at > ?3 " +
          "AND EXISTS (SELECT 1 FROM telegram_updates " +
          "WHERE update_id = ?2 AND status = 'received')",
      )
      .bind(
        reason,
        updateId,
        consumedAt,
        promptMessageId,
        scope.actorId,
        scope.chatId,
      );
    const results = await this.database.batch([claimUpdate, stageReason]);
    if (results[0].meta.changes !== 1) {
      return { outcome: "duplicate" };
    }
    if (results[1].meta.changes !== 1) {
      return { outcome: "unmatched" };
    }
    const row = await this.database
      .prepare(
        "SELECT request_id, application_id, vacancy_version, actor_id, " +
          "chat_id, reason_text FROM pending_discard_reasons " +
          "WHERE reply_update_id = ?1 AND status = 'dispatching'",
      )
      .bind(updateId)
      .first();
    return row
      ? {
          outcome: "staged",
          requestId: String(row.request_id),
          promptMessageId,
          applicationId: String(row.application_id),
          vacancyVersion: String(row.vacancy_version),
          actorId: String(row.actor_id),
          chatId: String(row.chat_id),
          reason: String(row.reason_text),
        }
      : { outcome: "unmatched" };
  }

  async markDiscardReasonDispatched(requestId) {
    await this.database
      .prepare(
        "UPDATE pending_discard_reasons SET status = 'dispatched' " +
          "WHERE request_id = ?1 AND status = 'dispatching'",
      )
      .bind(requestId)
      .run();
  }
}


export function createGateway({
  storeFactory = (env) => new D1GatewayStore(env.DB),
  tokenFactory = randomToken,
  now = () => new Date(),
  fetchImpl = (...args) => fetch(...args),
} = {}) {
  return {
    async fetch(request, env) {
      const url = new URL(request.url);
      if (request.method === "GET" && url.pathname === "/health") {
        return json({ status: "ok" });
      }
      if (
        request.method === "POST" &&
        url.pathname === "/v1/authorizations"
      ) {
        return issueAuthorizations(
          request,
          env,
          storeFactory(env),
          tokenFactory,
          now,
        );
      }
      if (request.method === "POST" && url.pathname === "/telegram") {
        return acceptTelegramUpdate(
          request,
          env,
          storeFactory(env),
          now,
          fetchImpl,
        );
      }
      return json({ error: "not_found" }, 404);
    },
  };
}


async function issueAuthorizations(
  request,
  env,
  store,
  tokenFactory,
  now,
) {
  const bearer = request.headers.get("authorization") || "";
  const expected = `Bearer ${required(env, "INTERNAL_API_TOKEN")}`;
  if (!(await secureEqual(bearer, expected))) {
    return json({ error: "unauthorized" }, 401);
  }
  let value;
  try {
    value = await request.json();
  } catch {
    return json({ error: "invalid_json" }, 400);
  }
  const parsed = parseAuthorizationRequest(value, env);
  if (!parsed.ok) {
    return json({ error: parsed.error }, 400);
  }
  const createdAt = now();
  const expiresAt = new Date(
    createdAt.getTime() + AUTHORIZATION_TTL_MS,
  ).toISOString();
  const records = ACTIONS.map(([action]) => ({
    token: tokenFactory(),
    action,
    applicationId: parsed.value.applicationId,
    vacancyVersion: parsed.value.vacancyVersion,
    actorId: parsed.value.actorId,
    chatId: parsed.value.chatId,
    createdAt: createdAt.toISOString(),
    expiresAt,
  }));
  const issued = await store.issueAuthorizationSet(
    parsed.value.eventId,
    records,
  );
  if (issued.length !== ACTIONS.length) {
    return json({ error: "authorization_set_incomplete" }, 503);
  }
  const labels = new Map(ACTIONS);
  return json({
    buttons: issued.map((record) => ({
      text: labels.get(record.action),
      callback_data: `${CALLBACK_PREFIX}${record.token}`,
    })),
    expires_at: issued[0].expiresAt,
  });
}


async function acceptTelegramUpdate(request, env, store, now, fetchImpl) {
  const suppliedSecret =
    request.headers.get("x-telegram-bot-api-secret-token") || "";
  if (
    !(await secureEqual(
      suppliedSecret,
      required(env, "TELEGRAM_WEBHOOK_SECRET"),
    ))
  ) {
    return json({ error: "unauthorized" }, 401);
  }
  let update;
  try {
    update = await request.json();
  } catch {
    return json({ error: "invalid_json" }, 400);
  }
  const callback = parseCallback(update);
  if (!callback.ok) {
    const reason = parseDiscardReason(update);
    if (reason.ok) {
      return acceptDiscardReason(
        reason.value,
        env,
        store,
        now,
        fetchImpl,
      );
    }
    return json({ accepted: true });
  }
  const receivedAt = now().toISOString();
  if (!(await store.claimUpdate(callback.value.updateId, receivedAt))) {
    return json({ accepted: true, duplicate: true });
  }
  const expectedScope = {
    actorId: required(env, "TELEGRAM_ACTOR_ID"),
    chatId: required(env, "TELEGRAM_CHAT_ID"),
  };
  if (
    callback.value.actorId !== expectedScope.actorId ||
    callback.value.chatId !== expectedScope.chatId
  ) {
    await store.markUpdate(
      callback.value.updateId,
      "rejected",
      "unexpected_actor_or_chat",
    );
    await answerCallback(
      fetchImpl,
      env,
      callback.value.callbackId,
      "Non autorizzato",
    );
    return json({ accepted: true });
  }
  const authorization = await store.consumeAuthorization(
    callback.value.token,
    expectedScope,
    receivedAt,
  );
  if (!authorization) {
    await store.markUpdate(
      callback.value.updateId,
      "rejected",
      "invalid_or_expired_authorization",
    );
    await answerCallback(
      fetchImpl,
      env,
      callback.value.callbackId,
      "Pulsante scaduto",
    );
    return json({ accepted: true });
  }
  if (authorization.action === "discard") {
    return requestDiscardReason(
      callback.value,
      authorization,
      env,
      store,
      now,
      fetchImpl,
    );
  }
  await store.markUpdate(callback.value.updateId, "dispatching");
  const payload = {
    action: authorization.action,
    application_id: authorization.applicationId,
    official_vacancy_version: authorization.vacancyVersion,
    telegram_update_id: String(callback.value.updateId),
    actor_id: callback.value.actorId,
    chat_id: callback.value.chatId,
  };
  let response;
  try {
    response = await dispatchToGitHub(fetchImpl, env, payload);
  } catch (error) {
    await store.markUpdate(
      callback.value.updateId,
      "uncertain",
      String(error),
    );
    await answerCallback(
      fetchImpl,
      env,
      callback.value.callbackId,
      "Ricezione incerta",
    );
    return json({ accepted: true });
  }
  if (response.status !== 204) {
    await store.releaseAuthorization(callback.value.token);
    await store.markUpdate(
      callback.value.updateId,
      "failed",
      `github_http_${response.status}`,
    );
    await answerCallback(
      fetchImpl,
      env,
      callback.value.callbackId,
      "Errore, riprova più tardi",
    );
    return json({ accepted: true });
  }
  await store.markUpdate(callback.value.updateId, "dispatched");
  await answerCallback(
    fetchImpl,
    env,
    callback.value.callbackId,
    "Ricevuto",
  );
  return json({ accepted: true });
}


async function requestDiscardReason(
  callback,
  authorization,
  env,
  store,
  now,
  fetchImpl,
) {
  const requestId = `callback:${callback.updateId}`;
  let prompt;
  try {
    prompt = await openDiscardReasonPrompt({
      requestId,
      applicationId: authorization.applicationId,
      vacancyVersion: authorization.vacancyVersion,
      actorId: callback.actorId,
      chatId: callback.chatId,
      promptText: "Perché vuoi scartare questa posizione?",
      env,
      store,
      now,
      fetchImpl,
    });
  } catch (error) {
    await store.releaseAuthorization(callback.token);
    await store.markUpdate(
      callback.updateId,
      "failed",
      `discard_prompt_begin_failed:${String(error)}`,
    );
    await answerCallback(
      fetchImpl,
      env,
      callback.callbackId,
      "Impossibile chiedere il motivo",
    );
    return json({ accepted: true });
  }
  if (prompt.outcome !== "pending") {
    if (prompt.outcome === "failed") {
      await store.releaseAuthorization(callback.token);
    }
    await store.markUpdate(
      callback.updateId,
      prompt.outcome,
      prompt.evidence,
    );
    await answerCallback(
      fetchImpl,
      env,
      callback.callbackId,
      "Impossibile chiedere il motivo",
    );
    return json({ accepted: true });
  }
  await store.markUpdate(
    callback.updateId,
    "awaiting_reason",
    String(prompt.promptMessageId),
  );
  await answerCallback(
    fetchImpl,
    env,
    callback.callbackId,
    "Ora scrivi il motivo",
  );
  return json({ accepted: true });
}


async function acceptDiscardReason(reason, env, store, now, fetchImpl) {
  const receivedAt = now().toISOString();
  const expectedScope = {
    actorId: required(env, "TELEGRAM_ACTOR_ID"),
    chatId: required(env, "TELEGRAM_CHAT_ID"),
  };
  if (
    reason.actorId !== expectedScope.actorId ||
    reason.chatId !== expectedScope.chatId
  ) {
    if (!(await store.claimUpdate(reason.updateId, receivedAt))) {
      return json({ accepted: true, duplicate: true });
    }
    await store.markUpdate(
      reason.updateId,
      "rejected",
      "unexpected_actor_or_chat",
    );
    return json({ accepted: true });
  }
  const pending = await store.stageDiscardReason(
    reason.updateId,
    reason.replyToMessageId,
    expectedScope,
    reason.text,
    receivedAt,
  );
  if (pending.outcome === "duplicate") {
    return json({ accepted: true, duplicate: true });
  }
  if (pending.outcome !== "staged") {
    await store.markUpdate(
      reason.updateId,
      "rejected",
      "no_matching_discard_prompt",
    );
    return json({ accepted: true });
  }
  await store.markUpdate(reason.updateId, "dispatching");
  let response;
  try {
    response = await dispatchToGitHub(fetchImpl, env, {
      action: "discard",
      application_id: pending.applicationId,
      official_vacancy_version: pending.vacancyVersion,
      telegram_update_id: String(reason.updateId),
      actor_id: reason.actorId,
      chat_id: reason.chatId,
      reason: pending.reason,
    });
  } catch (error) {
    await store.markDiscardReasonUncertain(
      pending.requestId,
      pending.promptMessageId,
      `github_dispatch_uncertain:${String(error)}`,
    );
    await store.markUpdate(reason.updateId, "uncertain", String(error));
    return json({ accepted: true });
  }
  if (response.status !== 204) {
    await store.failDiscardReasonRequest(
      pending.requestId,
      `github_http_${response.status}`,
    );
    await store.markUpdate(
      reason.updateId,
      "failed",
      `github_http_${response.status}`,
    );
    await openDiscardReasonPrompt({
      requestId: `retry:${reason.updateId}`,
      applicationId: pending.applicationId,
      vacancyVersion: pending.vacancyVersion,
      actorId: pending.actorId,
      chatId: pending.chatId,
      promptText:
        "GitHub non ha accettato lo scarto. Scrivi di nuovo il motivo.",
      env,
      store,
      now,
      fetchImpl,
    }).catch(() => {});
    return json({ accepted: true });
  }
  await store.markDiscardReasonDispatched(pending.requestId);
  await store.markUpdate(reason.updateId, "dispatched");
  await sendTelegramMessage(fetchImpl, env, {
    chat_id: reason.chatId,
    text: "Motivo ricevuto. Salvo lo scarto condizionale.",
  }).catch(() => {});
  return json({ accepted: true });
}


async function openDiscardReasonPrompt({
  requestId,
  applicationId,
  vacancyVersion,
  actorId,
  chatId,
  promptText,
  env,
  store,
  now,
  fetchImpl,
}) {
  const createdAt = now();
  await store.beginDiscardReason(requestId, {
    applicationId,
    vacancyVersion,
    actorId,
    chatId,
    createdAt: createdAt.toISOString(),
    expiresAt: new Date(
      createdAt.getTime() + DISCARD_REASON_TTL_MS,
    ).toISOString(),
  });
  let promptMessageId;
  try {
    promptMessageId = await sendTelegramMessage(fetchImpl, env, {
      chat_id: chatId,
      text: promptText,
      reply_markup: {
        force_reply: true,
        selective: true,
        input_field_placeholder: "Scrivi il motivo dello scarto",
      },
    });
  } catch (error) {
    if (error?.definite === true) {
      await store.failDiscardReasonRequest(
        requestId,
        "telegram_prompt_rejected",
      );
      return {
        outcome: "failed",
        evidence: "telegram_prompt_rejected",
      };
    }
    await store.markDiscardReasonUncertain(
      requestId,
      null,
      "telegram_prompt_uncertain",
    );
    return {
      outcome: "uncertain",
      evidence: "telegram_prompt_uncertain",
    };
  }
  try {
    const bound = await store.bindDiscardReasonPrompt(
      requestId,
      promptMessageId,
    );
    if (!bound) {
      throw new Error("discard prompt binding was not applied");
    }
  } catch (error) {
    await store.markDiscardReasonUncertain(
      requestId,
      promptMessageId,
      `discard_prompt_bind_failed:${String(error)}`,
    );
    return {
      outcome: "uncertain",
      promptMessageId,
      evidence: "discard_prompt_bind_failed",
    };
  }
  return { outcome: "pending", promptMessageId };
}


async function dispatchToGitHub(fetchImpl, env, clientPayload) {
  return fetchImpl(
    `https://api.github.com/repos/${required(
      env,
      "GITHUB_REPOSITORY",
    )}/dispatches`,
    {
      method: "POST",
      headers: {
        accept: "application/vnd.github+json",
        authorization: `Bearer ${required(
          env,
          "GITHUB_DISPATCH_TOKEN",
        )}`,
        "content-type": "application/json",
        "user-agent": "job-agent-telegram-gateway",
        "x-github-api-version": "2022-11-28",
      },
      body: JSON.stringify({
        event_type: "telegram-opportunity-decision",
        client_payload: clientPayload,
      }),
    },
  );
}


function parseCallback(update) {
  const query = update?.callback_query;
  const updateId = update?.update_id;
  const callbackId = cleanString(query?.id, 256);
  const actorId = integerId(query?.from?.id);
  const chatId = integerId(query?.message?.chat?.id);
  const data = cleanString(query?.data, 64);
  if (
    !Number.isSafeInteger(updateId) ||
    !callbackId ||
    !actorId ||
    !chatId ||
    !data.startsWith(CALLBACK_PREFIX)
  ) {
    return { ok: false };
  }
  const token = data.slice(CALLBACK_PREFIX.length);
  if (!/^[A-Za-z0-9_-]{8,48}$/u.test(token)) {
    return { ok: false };
  }
  return {
    ok: true,
    value: { updateId, callbackId, actorId, chatId, token },
  };
}


function parseDiscardReason(update) {
  const message = update?.message;
  const updateId = update?.update_id;
  const actorId = integerId(message?.from?.id);
  const chatId = integerId(message?.chat?.id);
  const messageId = message?.message_id;
  const replyToMessageId = message?.reply_to_message?.message_id;
  const text = cleanString(message?.text, 1000);
  if (
    !Number.isSafeInteger(updateId) ||
    !Number.isSafeInteger(messageId) ||
    !Number.isSafeInteger(replyToMessageId) ||
    !actorId ||
    !chatId ||
    !text
  ) {
    return { ok: false };
  }
  return {
    ok: true,
    value: {
      updateId,
      messageId,
      replyToMessageId,
      actorId,
      chatId,
      text,
    },
  };
}


function integerId(value) {
  return Number.isSafeInteger(value) ? String(value) : "";
}


async function answerCallback(fetchImpl, env, callbackId, text) {
  try {
    await fetchImpl(
      `https://api.telegram.org/bot${required(
        env,
        "TELEGRAM_BOT_TOKEN",
      )}/answerCallbackQuery`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          callback_query_id: callbackId,
          text,
        }),
      },
    );
  } catch {
    // A Telegram acknowledgement is cosmetic. The durable dispatch status
    // remains authoritative and must never be changed by an ack failure.
  }
}


async function sendTelegramMessage(fetchImpl, env, payload) {
  let response;
  try {
    response = await fetchImpl(
      `https://api.telegram.org/bot${required(
        env,
        "TELEGRAM_BOT_TOKEN",
      )}/sendMessage`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      },
    );
  } catch (error) {
    throw error;
  }
  let body;
  try {
    body = await response.json();
  } catch {
    const error = new Error("Telegram returned invalid JSON");
    error.definite = response.ok === false;
    throw error;
  }
  const messageId = body?.result?.message_id;
  if (
    response.ok !== true ||
    body?.ok !== true ||
    !Number.isSafeInteger(messageId) ||
    String(body?.result?.chat?.id) !== String(payload.chat_id)
  ) {
    const error = new Error("Telegram rejected sendMessage");
    error.definite = true;
    throw error;
  }
  return messageId;
}


function parseAuthorizationRequest(value, env) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return { ok: false, error: "invalid_request" };
  }
  const eventId = cleanString(value.event_id, 512);
  const applicationId = cleanString(value.application_id, 128);
  const vacancyVersion = cleanString(
    value.official_vacancy_version,
    128,
  );
  const actorId = cleanString(value.actor_id, 32);
  const chatId = cleanString(value.chat_id, 32);
  if (
    !eventId ||
    !APPLICATION_ID.test(applicationId) ||
    !VACANCY_VERSION.test(vacancyVersion) ||
    actorId !== required(env, "TELEGRAM_ACTOR_ID") ||
    chatId !== required(env, "TELEGRAM_CHAT_ID")
  ) {
    return { ok: false, error: "invalid_scope" };
  }
  return {
    ok: true,
    value: { eventId, applicationId, vacancyVersion, actorId, chatId },
  };
}


function cleanString(value, maximum) {
  if (typeof value !== "string") {
    return "";
  }
  const cleaned = value.trim();
  return cleaned && cleaned.length <= maximum ? cleaned : "";
}


function randomToken() {
  const bytes = new Uint8Array(24);
  crypto.getRandomValues(bytes);
  return base64Url(bytes);
}


function base64Url(bytes) {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary)
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/u, "");
}


async function secureEqual(left, right) {
  const encoder = new TextEncoder();
  const [leftHash, rightHash] = await Promise.all(
    [left, right].map((value) =>
      crypto.subtle.digest("SHA-256", encoder.encode(value)),
    ),
  );
  const leftBytes = new Uint8Array(leftHash);
  const rightBytes = new Uint8Array(rightHash);
  let different = leftBytes.length ^ rightBytes.length;
  for (let index = 0; index < leftBytes.length; index += 1) {
    different |= leftBytes[index] ^ rightBytes[index];
  }
  return different === 0;
}


function required(env, name) {
  const value = String(env[name] || "").trim();
  if (!value) {
    throw new Error(`Missing required binding: ${name}`);
  }
  return value;
}


function json(value, status = 200) {
  return Response.json(value, {
    status,
    headers: {
      "cache-control": "no-store",
      "content-type": "application/json; charset=utf-8",
    },
  });
}


export default createGateway();
