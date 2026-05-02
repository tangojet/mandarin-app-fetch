#!/usr/bin/env node
/**
 * Doubao (豆包) chat completion via CDP — runs inside a Docker container
 * with a logged-in Chrome session on doubao.com.
 *
 * The browser's Argus SDK automatically adds a_bogus/msToken to requests.
 * Uses the v2 /chat/completion endpoint with content_block payload format.
 *
 * Usage: echo '{"text":"hello"}' | node doubao-cdp-chat.js
 *   or:  node doubao-cdp-chat.js '{"text":"hello"}'
 *
 * Input JSON: { text, bot_id? }
 * Output JSON to stdout: { error, content, conversation_id }
 */
const http = require("http");
const crypto = require("crypto");

// ── Minimal raw WebSocket helpers (no deps) ──

function wsConnect(path) {
  return new Promise((resolve, reject) => {
    const key = crypto.randomBytes(16).toString("base64");
    const req = http.request({
      hostname: "127.0.0.1", port: 9222, path, method: "GET",
      headers: { Upgrade: "websocket", Connection: "Upgrade", "Sec-WebSocket-Key": key, "Sec-WebSocket-Version": "13" },
    });
    req.on("upgrade", (_res, socket) => resolve(socket));
    req.on("error", reject);
    req.end();
  });
}

function wsSend(socket, id, method, params) {
  const msg = JSON.stringify({ id, method, params: params || {} });
  const buf = Buffer.from(msg);
  const mask = crypto.randomBytes(4);
  let header;
  if (buf.length < 126) {
    header = Buffer.alloc(6); header[0] = 0x81; header[1] = 0x80 | buf.length; mask.copy(header, 2);
  } else if (buf.length < 65536) {
    header = Buffer.alloc(8); header[0] = 0x81; header[1] = 0x80 | 126; header.writeUInt16BE(buf.length, 2); mask.copy(header, 4);
  } else {
    header = Buffer.alloc(14); header[0] = 0x81; header[1] = 0x80 | 127; header.writeBigUInt64BE(BigInt(buf.length), 2); mask.copy(header, 10);
  }
  const masked = Buffer.alloc(buf.length);
  for (let i = 0; i < buf.length; i++) masked[i] = buf[i] ^ mask[i % 4];
  socket.write(Buffer.concat([header, masked]));
}

function wsRecv(socket) {
  return new Promise((resolve) => {
    let acc = Buffer.alloc(0);
    const handler = (chunk) => {
      acc = Buffer.concat([acc, chunk]);
      try {
        let payloadLen = acc[1] & 0x7f, offset = 2;
        if (payloadLen === 126) { payloadLen = acc.readUInt16BE(2); offset = 4; }
        else if (payloadLen === 127) { offset = 10; payloadLen = Number(acc.readBigUInt64BE(2)); }
        if (acc.length >= offset + payloadLen) {
          socket.removeListener("data", handler);
          resolve(JSON.parse(acc.slice(offset, offset + payloadLen).toString()));
        }
      } catch (_) { /* incomplete frame, wait for more data */ }
    };
    socket.on("data", handler);
  });
}

// ── Browser-side fetch + SSE parse (runs via Runtime.evaluate) ──

const BROWSER_FETCH_JS = `
async (text, botId) => {
  try {
    // Extract device params from localStorage
    let deviceId = "0", webId = "0", fp = "";
    try {
      const sww = JSON.parse(localStorage.getItem("samantha_web_web_id") || "{}");
      deviceId = sww.web_id || "0";
    } catch(e) {}
    try {
      const tc = JSON.parse(localStorage.getItem("__tea_cache_tokens_497858") || "{}");
      webId = tc.web_id || "0";
    } catch(e) {}
    // Get fingerprint from s_v_web_id cookie
    const fpMatch = document.cookie.match(/(?:^|; )s_v_web_id=([^;]*)/);
    fp = fpMatch ? decodeURIComponent(fpMatch[1]) : "";

    // Build URL with required query params (Argus SDK adds a_bogus/msToken)
    const params = new URLSearchParams({
      aid: "497858",
      device_id: deviceId,
      device_platform: "web",
      fp: fp,
      language: "zh",
      pc_version: "3.17.0",
      pkg_type: "release_version",
      real_aid: "497858",
      region: "",
      samantha_web: "1",
      sys_region: "",
      tea_uuid: webId,
      "use-olympus-account": "1",
      version_code: "20800",
      web_id: webId,
      web_tab_id: crypto.randomUUID(),
    });

    // Build v2 payload
    const now = Date.now();
    const payload = {
      client_meta: {
        local_conversation_id: "local_" + now + Math.floor(Math.random() * 10000),
        conversation_id: "",
        bot_id: botId,
        last_section_id: "",
        last_message_index: null,
      },
      messages: [{
        local_message_id: crypto.randomUUID(),
        content_block: [{
          block_type: 10000,
          content: {
            text_block: { text: text, icon_url: "", icon_url_dark: "", summary: "" },
            pc_event_block: "",
          },
          block_id: crypto.randomUUID(),
          parent_id: "",
          meta_info: [],
          append_fields: [],
        }],
        message_status: 0,
      }],
      option: {
        send_message_scene: "",
        create_time_ms: now,
        collect_id: "",
        is_audio: false,
        answer_with_suggest: false,
        tts_switch: false,
        need_deep_think: 0,
        click_clear_context: false,
        from_suggest: false,
        is_regen: false,
        is_replace: false,
        disable_sse_cache: false,
        select_text_action: "",
        resend_for_regen: false,
        scene_type: 0,
        unique_key: crypto.randomUUID(),
        start_seq: 0,
        need_create_conversation: true,
        conversation_init_option: { need_ack_conversation: true },
        regen_query_id: [],
        edit_query_id: [],
        regen_instruction: "",
        no_replace_for_regen: false,
        message_from: 0,
        shared_app_name: "",
        shared_app_id: "",
        sse_recv_event_options: { support_chunk_delta: true },
        is_ai_playground: false,
        recovery_option: {
          is_recovery: false,
          req_create_time_sec: Math.floor(now / 1000),
          append_sse_event_scene: 0,
        },
      },
      ext: {
        use_deep_think: "0",
        fp: fp,
        conversation_init_option: JSON.stringify({ need_ack_conversation: true }),
        commerce_credit_config_enable: "0",
        sub_conv_firstmet_type: "1",
      },
    };

    const resp = await fetch("/chat/completion?" + params.toString(), {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        "Agw-Js-Conv": "str, str",
        "last-event-id": "undefined",
      },
      body: JSON.stringify(payload),
    });

    if (!resp.ok) {
      const errText = await resp.text();
      return JSON.stringify({ error: "HTTP " + resp.status + ": " + errText.substring(0, 200), content: "", conversation_id: null });
    }

    const respText = await resp.text();
    const rawLines = respText.split("\\n");
    let content = "";
    let conversationId = null;
    let errorMsg = null;
    let currentEvent = null;

    for (const line of rawLines) {
      const trimmed = line.trim();

      if (trimmed.startsWith("event:")) {
        currentEvent = trimmed.substring(6).trim();
        continue;
      }

      if (trimmed.startsWith("data:")) {
        const dataStr = trimmed.substring(5).trim();
        if (!dataStr || dataStr === "{}") continue;

        try {
          const data = JSON.parse(dataStr);

          // ── Error events ──
          if (currentEvent === "STREAM_ERROR") {
            errorMsg = (data.error_code || 0) + ": " + (data.error_msg || "Unknown error");
            continue;
          }
          if (currentEvent === "gateway-error") {
            errorMsg = data.code + ": " + data.message;
            continue;
          }

          // ── v2 events (from /chat/completion) ──

          // SSE_ACK — conversation created
          if (currentEvent === "SSE_ACK") {
            const meta = data.ack_client_meta || {};
            conversationId = meta.conversation_id || conversationId;
            continue;
          }

          // CHUNK_DELTA — incremental text content
          if (currentEvent === "CHUNK_DELTA") {
            if (data.text) content += data.text;
            continue;
          }

          // FULL_MSG_NOTIFY — complete message (user echo or bot final)
          if (currentEvent === "FULL_MSG_NOTIFY") {
            const msg = data.message || {};
            // Only capture bot responses (user_type !== 1)
            if (msg.user_type !== 1 && msg.content_block) {
              for (const block of msg.content_block) {
                const tb = block && block.content && block.content.text_block;
                if (tb && tb.text) content = tb.text;
              }
            }
            if (msg.conversation_id) conversationId = msg.conversation_id;
            continue;
          }

          // STREAM_MSG_NOTIFY / STREAM_CHUNK — structured updates
          if (currentEvent === "STREAM_MSG_NOTIFY" || currentEvent === "STREAM_CHUNK") {
            // These use patch_op format; content comes via CHUNK_DELTA
            continue;
          }

          // SSE_REPLY_END — stream complete
          if (currentEvent === "SSE_REPLY_END") {
            continue;
          }

          // ── Legacy v1 events (from /samantha/chat/completion) ──

          if (currentEvent === "SSE_CONVERSATION_CREATED") {
            conversationId = data.conversation_id || conversationId;
            continue;
          }

          if (currentEvent === "SSE_REPLY" || currentEvent === "SSE_CHUNK") {
            const msg = data.message || data;
            if (msg.content_block) {
              for (const block of msg.content_block) {
                const tb = block && block.content && block.content.text_block;
                if (tb && tb.text) content = tb.text;
              }
            }
            if (data.chunk_delta) content += data.chunk_delta;
            if (data.conversation_id) conversationId = data.conversation_id;
            continue;
          }

          // Legacy event_type codes
          const eventType = data.event_type;
          if (eventType === 2005) {
            const ed = typeof data.event_data === "string" ? JSON.parse(data.event_data) : (data.event_data || {});
            errorMsg = (ed.code || 0) + ": " + (ed.message || ed.msg || "error");
          }
          if (eventType === 2002) {
            const ed = typeof data.event_data === "string" ? JSON.parse(data.event_data) : (data.event_data || {});
            conversationId = ed.conversation_id || conversationId;
          }
        } catch(e) {}
      }

      if (trimmed === "") { currentEvent = null; }
    }

    if (errorMsg && !content) {
      return JSON.stringify({ error: errorMsg, content: "", conversation_id: null });
    }
    return JSON.stringify({ error: null, content, conversation_id: conversationId });
  } catch(e) {
    return JSON.stringify({ error: e.message, content: "", conversation_id: null });
  }
}
`;

const DELETE_THREAD_JS = `
async (convId) => {
  try {
    await fetch("/samantha/thread/delete", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conversation_id: convId }),
    });
    return "ok";
  } catch(e) {
    return "err:" + e.message;
  }
}
`;

// ── Main ──

async function main() {
  // Read input: either from argv or stdin
  let inputStr = process.argv[2];
  if (!inputStr) {
    inputStr = await new Promise((resolve) => {
      let d = ""; process.stdin.on("data", (c) => (d += c));
      process.stdin.on("end", () => resolve(d.trim()));
      setTimeout(() => resolve(d.trim()), 1000);
    });
  }
  if (!inputStr) {
    console.log(JSON.stringify({ error: "No input provided", content: "", conversation_id: null }));
    process.exit(1);
  }

  const input = JSON.parse(inputStr);
  const text = input.text || "";
  const botId = input.bot_id || "7338286299411103781";

  if (!text) {
    console.log(JSON.stringify({ error: "Empty text", content: "", conversation_id: null }));
    process.exit(1);
  }

  // Find doubao tab
  const pages = await new Promise((resolve, reject) => {
    http.get("http://127.0.0.1:9222/json/list", (res) => {
      let d = ""; res.on("data", (c) => (d += c));
      res.on("end", () => resolve(JSON.parse(d)));
    }).on("error", reject);
  });

  const tab = pages.find((p) => p.url.includes("doubao.com/chat") && !p.url.includes("worker"));
  if (!tab) {
    console.log(JSON.stringify({ error: "No doubao.com tab found in browser", content: "", conversation_id: null }));
    process.exit(1);
  }

  const wsPath = new URL(tab.webSocketDebuggerUrl).pathname;
  const ws = await wsConnect(wsPath);

  try {
    // Execute fetch in browser context — pass text and botId separately
    // (the payload is built inside the browser where crypto.randomUUID is available)
    wsSend(ws, 1, "Runtime.evaluate", {
      expression: `(${BROWSER_FETCH_JS})(${JSON.stringify(text)}, ${JSON.stringify(botId)})`,
      awaitPromise: true,
      returnByValue: true,
    });

    const evalResp = await wsRecv(ws);

    if (evalResp.result && evalResp.result.result && evalResp.result.result.value) {
      const result = JSON.parse(evalResp.result.result.value);

      // Delete thread if we got a conversation_id
      if (result.conversation_id && !result.error) {
        wsSend(ws, 2, "Runtime.evaluate", {
          expression: `(${DELETE_THREAD_JS})(${JSON.stringify(result.conversation_id)})`,
          awaitPromise: true,
        });
        // Fire and forget — don't wait
        setTimeout(() => {}, 500);
      }

      console.log(JSON.stringify(result));
    } else {
      const errDetail = evalResp.result && evalResp.result.exceptionDetails;
      const errMsg = errDetail ? (errDetail.text || errDetail.exception?.description || "evaluate failed") : "no result";
      console.log(JSON.stringify({ error: errMsg, content: "", conversation_id: null }));
    }
  } finally {
    ws.end();
  }
}

const timer = setTimeout(() => {
  console.log(JSON.stringify({ error: "timeout", content: "", conversation_id: null }));
  process.exit(1);
}, 60000);

main().then(() => {
  clearTimeout(timer);
  process.exit(0);
}).catch((e) => {
  clearTimeout(timer);
  console.log(JSON.stringify({ error: e.message, content: "", conversation_id: null }));
  process.exit(1);
});
