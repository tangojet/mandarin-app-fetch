#!/usr/bin/env node
/**
 * XHS note extractor — runs inside a Docker container with a logged-in Chrome.
 * Uses Chrome DevTools Protocol to open a new tab, navigate, extract data, close tab.
 *
 * Usage: node xhs-cdp-extract.js <url> [max_comments]
 * Output: JSON to stdout
 */
const http = require("http");
const crypto = require("crypto");

// ── Minimal raw WebSocket helpers (no deps) ──

function wsConnect(path) {
  return new Promise((resolve, reject) => {
    const key = crypto.randomBytes(16).toString("base64");
    const req = http.request({
      hostname: "127.0.0.1", port: 9223, path, method: "GET",
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

// ── Main ──

async function main() {
  const noteUrl = process.argv[2];
  const maxComments = parseInt(process.argv[3] || "10", 10);
  if (!noteUrl) { process.stderr.write("Usage: node xhs-cdp-extract.js <url> [max_comments]\n"); process.exit(1); }

  // 1. Get browser WS endpoint
  const version = await new Promise((resolve) => {
    http.get("http://127.0.0.1:9223/json/version", (res) => {
      let d = ""; res.on("data", (c) => (d += c)); res.on("end", () => resolve(JSON.parse(d)));
    });
  });
  const browserPath = new URL(version.webSocketDebuggerUrl).pathname;

  // 2. Create new tab
  const bs = await wsConnect(browserPath);
  wsSend(bs, 1, "Target.createTarget", { url: "about:blank" });
  const { result: { targetId } } = await wsRecv(bs);
  bs.end();

  // 3. Find page WS URL
  const pages = await new Promise((resolve) => {
    http.get("http://127.0.0.1:9223/json/list", (res) => {
      let d = ""; res.on("data", (c) => (d += c)); res.on("end", () => resolve(JSON.parse(d)));
    });
  });
  const target = pages.find((p) => p.id === targetId);
  if (!target) throw new Error("Target page not found");

  const ps = await wsConnect(new URL(target.webSocketDebuggerUrl).pathname);

  try {
    // 4. Navigate
    wsSend(ps, 1, "Page.navigate", { url: noteUrl });
    await wsRecv(ps);
    await new Promise((r) => setTimeout(r, 5000));

    // 5. Extract __INITIAL_STATE__ note data
    wsSend(ps, 2, "Runtime.evaluate", {
      expression: `(() => {
        try {
          const state = window.__INITIAL_STATE__;
          if (!state || !state.note) return JSON.stringify({error: "no __INITIAL_STATE__", url: location.href});
          const map = state.note.noteDetailMap || {};
          let noteData = null;
          for (const k of Object.keys(map)) { noteData = map[k].note; if (noteData) break; }
          if (!noteData) return JSON.stringify({error: "no noteData", url: location.href});

          const images = (noteData.imageList || []).map(img => {
            const info = img.infoList || [];
            if (info.length) {
              const best = info.reduce((a, b) => (a.width||0)*(a.height||0) > (b.width||0)*(b.height||0) ? a : b);
              let u = best.url || "";
              if (u && !u.startsWith("http")) u = "https:" + u;
              return u;
            }
            let u = img.urlDefault || "";
            if (u && !u.startsWith("http")) u = "https:" + u;
            return u;
          }).filter(Boolean);

          let videoUrl = null;
          const video = noteData.video;
          if (video && video.media && video.media.stream) {
            for (const q of ["h264", "h265", "av1"]) {
              const s = video.media.stream[q];
              if (s && s.length) { videoUrl = s[0].masterUrl || (s[0].backupUrls || [""])[0]; break; }
            }
          }

          return JSON.stringify({
            title: noteData.title || "",
            desc: noteData.desc || "",
            user: noteData.user || {},
            interactInfo: noteData.interactInfo || {},
            images,
            videoUrl,
          });
        } catch(e) { return JSON.stringify({error: e.message, url: location.href}); }
      })()`,
    });
    const evalResp = await wsRecv(ps);
    const noteResult = JSON.parse(evalResp.result.result.value);

    if (noteResult.error) {
      process.stderr.write("Error: " + noteResult.error + " url=" + (noteResult.url || "") + "\n");
      console.log(JSON.stringify(noteResult));
      process.exit(1);
    }

    // 6. Extract comments from DOM (API requires signed headers, so we scroll + parse DOM)
    let comments = [];
    if (maxComments > 0) {
      // Scroll to trigger comment loading
      wsSend(ps, 3, "Runtime.evaluate", {
        expression: `(() => {
          const el = document.querySelector('.comment-container') || document.querySelector('[class*="comment"]');
          if (el) el.scrollIntoView(); else window.scrollTo(0, document.body.scrollHeight);
          return "ok";
        })()`,
      });
      await wsRecv(ps);
      await new Promise((r) => setTimeout(r, 2000));

      // Parse comment items from DOM
      wsSend(ps, 4, "Runtime.evaluate", {
        expression: `(() => {
          const items = document.querySelectorAll('.comment-item');
          if (!items.length) return "[]";
          return JSON.stringify(Array.from(items).slice(0, ${maxComments}).map(el => {
            const lines = el.innerText.split("\\n").filter(Boolean);
            // Structure: username, text..., "X天前Region", likeCount, [replyCount | "回复"]
            const user = lines[0] || "anonymous";
            // Find the time line (matches patterns like "2天前辽宁", "昨天 22:52俄罗斯", "7小时前广东")
            let timeIdx = -1;
            for (let i = 1; i < lines.length; i++) {
              if (/\\d+[天小时分钟秒]前|昨天|刚刚|\\d+月\\d+日/.test(lines[i])) { timeIdx = i; break; }
            }
            const text = timeIdx > 1 ? lines.slice(1, timeIdx).join("\\n") : (lines[1] || "");
            const time = timeIdx >= 0 ? lines[timeIdx] : "";
            const likesStr = timeIdx >= 0 && timeIdx + 1 < lines.length ? lines[timeIdx + 1] : "0";
            return { user, text, likes: parseInt(likesStr, 10) || 0, time };
          }));
        })()`,
      });
      const commentResp = await wsRecv(ps);
      try {
        comments = JSON.parse(commentResp.result.result.value);
      } catch (_) {}
    }

    // Output final result
    console.log(JSON.stringify({
      platform: "xhs",
      title: noteResult.title || "(无标题)",
      author: { name: noteResult.user.nickname || "unknown", id: noteResult.user.userId || "" },
      content: noteResult.desc,
      stats: {
        likes: parseInt(noteResult.interactInfo.likedCount || "0", 10),
        comments: parseInt(noteResult.interactInfo.commentCount || "0", 10),
        shares: parseInt(noteResult.interactInfo.shareCount || "0", 10),
        collects: parseInt(noteResult.interactInfo.collectedCount || "0", 10),
      },
      images: noteResult.images,
      video_url: noteResult.videoUrl,
      comments,
      url: noteUrl,
      fetched_at: new Date().toISOString(),
    }));
  } finally {
    ps.end();
    // Close the tab
    const bs2 = await wsConnect(browserPath);
    wsSend(bs2, 1, "Target.closeTarget", { targetId });
    await wsRecv(bs2);
    bs2.end();
  }
}

const timer = setTimeout(() => process.exit(1), 30000);
main().then(() => {
  clearTimeout(timer);
  process.exit(0);
}).catch((e) => {
  clearTimeout(timer);
  process.stderr.write(e.message + "\n");
  process.exit(1);
});
