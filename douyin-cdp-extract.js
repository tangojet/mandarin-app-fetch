#!/usr/bin/env node
/**
 * Douyin post extractor — runs inside a Docker container with a logged-in Chrome.
 * Uses Chrome DevTools Protocol to open a new tab, navigate to douyin.com,
 * fetch post data via web API (in-page fetch reuses browser session), then close tab.
 *
 * Usage: node douyin-cdp-extract.js <aweme_id> [max_comments]
 * Output: JSON to stdout
 */
const http = require("http");
const crypto = require("crypto");

const CDP_PORT = parseInt(process.env.CDP_PORT || "9222", 10);

// ── Minimal raw WebSocket helpers (no deps) ──

function wsConnect(path) {
  return new Promise((resolve, reject) => {
    const key = crypto.randomBytes(16).toString("base64");
    const req = http.request({
      hostname: "127.0.0.1", port: CDP_PORT, path, method: "GET",
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
  const awemeId = process.argv[2];
  const maxComments = parseInt(process.argv[3] || "10", 10);
  if (!awemeId) { process.stderr.write("Usage: node douyin-cdp-extract.js <aweme_id> [max_comments]\n"); process.exit(1); }

  // 1. Get browser WS endpoint
  const version = await new Promise((resolve) => {
    http.get(`http://127.0.0.1:${CDP_PORT}/json/version`, (res) => {
      let d = ""; res.on("data", (c) => (d += c)); res.on("end", () => resolve(JSON.parse(d)));
    });
  });
  const browserPath = new URL(version.webSocketDebuggerUrl).pathname;

  // 2. Create new tab navigating to douyin.com (shares cookies with logged-in session)
  const bs = await wsConnect(browserPath);
  wsSend(bs, 1, "Target.createTarget", { url: "https://www.douyin.com" });
  const { result: { targetId } } = await wsRecv(bs);
  bs.end();

  // 3. Wait for page load, then find the new tab
  await new Promise((r) => setTimeout(r, 5000));
  const pages = await new Promise((resolve) => {
    http.get(`http://127.0.0.1:${CDP_PORT}/json/list`, (res) => {
      let d = ""; res.on("data", (c) => (d += c)); res.on("end", () => resolve(JSON.parse(d)));
    });
  });
  const target = pages.find((p) => p.id === targetId);
  if (!target) throw new Error("Target page not found");

  const ps = await wsConnect(new URL(target.webSocketDebuggerUrl).pathname);

  try {
    // 4. Fetch aweme detail + comments via in-page fetch
    const expr = `(async function() {
      try {
        var r = await fetch("/aweme/v1/web/aweme/detail/?aweme_id=${awemeId}&aid=6383&device_platform=web", {credentials:"include"});
        var d = await r.json();
        var det = d.aweme_detail;
        if (!det) return JSON.stringify({error: "no aweme_detail", keys: Object.keys(d)});

        var authorInfo = det.author || {};
        var avatarThumb = authorInfo.avatar_thumb || {};
        var music = det.music || null;
        var stats = det.statistics || {};
        var video = det.video || {};

        // Extract images (for image/note posts)
        var images = [];
        var imageList = det.images || [];
        for (var i = 0; i < imageList.length; i++) {
          var urls = imageList[i].url_list || [];
          if (urls.length) images.push(urls[urls.length - 1]);
        }

        // Extract video URL
        var videoUrl = null;
        var playAddr = video.play_addr || {};
        var playUrls = playAddr.url_list || [];
        if (playUrls.length) videoUrl = playUrls[0];

        // Video cover as image fallback
        if (!images.length && video) {
          var cover = video.cover || video.origin_cover || {};
          var coverUrls = cover.url_list || [];
          if (coverUrls.length) images.push(coverUrls[coverUrls.length - 1]);
        }

        // Fetch comments
        var comments = [];
        if (${maxComments} > 0) {
          try {
            var cr = await fetch("/aweme/v1/web/comment/list/?aweme_id=${awemeId}&cursor=0&count=${maxComments}&item_type=0", {credentials:"include"});
            var cd = await cr.json();
            var cmts = (cd.comments || []).slice(0, ${maxComments});
            for (var j = 0; j < cmts.length; j++) {
              var c = cmts[j];
              var u = c.user || {};
              comments.push({
                user: u.nickname || "anonymous",
                text: c.text || "",
                likes: c.digg_count || 0,
                time: String(c.create_time || ""),
                ip_location: c.ip_label || null,
                sub_comment_count: c.reply_comment_total || 0,
              });
            }
          } catch(e) { /* ignore comment errors */ }
        }

        // Content type
        var typeMap = {0: "video", 68: "image", 150: "image"};
        var awemeType = det.aweme_type != null ? (typeMap[det.aweme_type] || "type_" + det.aweme_type) : null;

        return JSON.stringify({
          platform: "douyin",
          title: (det.desc || "").substring(0, 80) || "(无标题)",
          author: {
            name: authorInfo.nickname || "unknown",
            id: authorInfo.sec_uid || String(authorInfo.uid || ""),
            avatar: (avatarThumb.url_list || [])[0] || null,
            signature: authorInfo.signature || null,
            ip_location: authorInfo.ip_location || null,
          },
          content: det.desc || "",
          stats: {
            likes: stats.digg_count || 0,
            comments: stats.comment_count || 0,
            shares: stats.share_count || 0,
            plays: stats.play_count || 0,
            favorites: stats.collect_count || 0,
          },
          images: images,
          video_url: videoUrl,
          music: music ? { title: music.title || "", author: music.author || "", duration: music.duration || 0 } : null,
          create_time: det.create_time || null,
          aweme_type: awemeType,
          comments: comments,
          url: "https://www.douyin.com/video/${awemeId}",
          fetched_at: new Date().toISOString(),
        });
      } catch(e) {
        return JSON.stringify({error: e.message});
      }
    })()`;

    wsSend(ps, 1, "Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
    const evalResp = await wsRecv(ps);

    const value = evalResp.result && evalResp.result.result && evalResp.result.result.value;
    if (!value) {
      process.stderr.write("No value in CDP response\n");
      console.log(JSON.stringify({ error: "no value", response: evalResp }));
      process.exit(1);
    }

    const result = JSON.parse(value);
    if (result.error) {
      process.stderr.write("Error: " + result.error + "\n");
      console.log(JSON.stringify(result));
      process.exit(1);
    }

    console.log(JSON.stringify(result));
  } finally {
    ps.end();
    // Close the tab we created
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
