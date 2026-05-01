#!/usr/bin/env node
/**
 * Goofish (闲鱼) item extractor — runs inside a Docker container with a logged-in Chrome.
 * Uses Chrome DevTools Protocol to open a new tab, navigate, extract data, close tab.
 *
 * Usage: node goofish-cdp-extract.js <url>
 * Output: JSON to stdout
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

// ── Main ──

async function main() {
  const itemUrl = process.argv[2];
  if (!itemUrl) { process.stderr.write("Usage: node goofish-cdp-extract.js <url>\n"); process.exit(1); }

  // 1. Get browser WS endpoint
  const version = await new Promise((resolve) => {
    http.get("http://127.0.0.1:9222/json/version", (res) => {
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
    http.get("http://127.0.0.1:9222/json/list", (res) => {
      let d = ""; res.on("data", (c) => (d += c)); res.on("end", () => resolve(JSON.parse(d)));
    });
  });
  const target = pages.find((p) => p.id === targetId);
  if (!target) throw new Error("Target page not found");

  const ps = await wsConnect(new URL(target.webSocketDebuggerUrl).pathname);

  try {
    // 4. Navigate to item page
    wsSend(ps, 1, "Page.navigate", { url: itemUrl });
    await wsRecv(ps);
    await new Promise((r) => setTimeout(r, 5000));

    // 5. Extract item data from DOM
    wsSend(ps, 2, "Runtime.evaluate", {
      expression: `(() => {
        try {
          // Title: strip "_闲鱼" suffix from document.title
          let title = document.title.replace(/_闲鱼$/, "").trim();

          // Seller info
          const nickEl = document.querySelector('[class*="item-user-info-nick"]');
          const sellerName = nickEl ? nickEl.textContent.trim() : "";
          const avatarEl = document.querySelector('[class*="item-user-info-avatar"] img');
          const sellerAvatar = avatarEl ? avatarEl.src : "";

          // Seller location and labels
          const introEl = document.querySelector('[class*="item-user-info-intro"]');
          const introText = introEl ? introEl.textContent.trim() : "";
          const spaceEl = document.querySelector('[class*="item-user-info-space"]');
          const location = spaceEl ? spaceEl.textContent.trim() : "";

          // Price
          const priceEl = document.querySelector('[class*="price--"]');
          const priceText = priceEl ? priceEl.textContent.trim() : "";
          // Extract numeric price
          const priceMatch = priceText.match(/[\\d.]+/);
          const price = priceMatch ? priceMatch[0] : "";
          // Original price
          const origMatch = priceText.match(/原价[¥￥]?([\\d.]+)/);
          const originalPrice = origMatch ? origMatch[1] : "";

          // Want count and views
          const wantEl = document.querySelector('[class*="want--"]');
          const wantText = wantEl ? wantEl.textContent.trim() : "";
          const wantMatch = wantText.match(/(\\d+)\\s*人想要/);
          const wants = wantMatch ? parseInt(wantMatch[1], 10) : 0;
          const viewMatch = wantText.match(/(\\d+)\\s*浏览/);
          const views = viewMatch ? parseInt(viewMatch[1], 10) : 0;

          // Description from item-main-info section
          const mainInfoEl = document.querySelector('[class*="item-main-info"]');
          let desc = "";
          let attributes = {};
          if (mainInfoEl) {
            const fullText = mainInfoEl.innerText;
            // The description comes after stats line and before attributes
            // Pattern: price line, stats, description text, then key-value attributes
            const lines = fullText.split("\\n").filter(Boolean);
            // Find where description starts (after "浏览" line or after stats)
            let descStart = -1;
            let descEnd = -1;
            for (let i = 0; i < lines.length; i++) {
              if (lines[i].includes("浏览") && descStart < 0) { descStart = i + 1; continue; }
              // Attribute section starts with single-char labels like "品", "牌", "："
              if (descStart > 0 && /^[品车里过车源上外上亮变]$/.test(lines[i].trim())) {
                descEnd = i;
                break;
              }
            }
            if (descStart > 0) {
              const end = descEnd > 0 ? descEnd : lines.length;
              // Collect description lines, skip "展开" button text
              desc = lines.slice(descStart, end).filter(l => l.trim() !== "展开" && l.trim() !== "已下架").join("\\n").trim();
            }

            // Extract attributes (品牌, 车系, etc.)
            // Each attr is rendered as: single-char lines forming the key, "：", then value line
            // e.g. "品\\n牌\\n：\\nFOTON/福田\\n车\\n系\\n：\\n福田风景"
            if (descEnd > 0) {
              const attrLines = lines.slice(descEnd);
              const stopWords = ["已下架", "聊一聊", "立即购买", "收藏", "展开"];
              let keyChars = [];
              let state = "key"; // "key" = collecting key chars, "sep" = saw separator, "val" = collecting value
              for (const line of attrLines) {
                const trimmed = line.trim();
                if (!trimmed || stopWords.includes(trimmed)) continue;
                if (trimmed === "：" || trimmed === ":") {
                  state = "sep";
                  continue;
                }
                if (state === "sep") {
                  // This line is the value
                  const key = keyChars.join("");
                  if (key) attributes[key] = trimmed;
                  keyChars = [];
                  state = "key";
                } else {
                  // Single CJK char = part of key; otherwise might be value continuation
                  if (/^[\\u4e00-\\u9fa5]{1,3}$/.test(trimmed)) {
                    keyChars.push(trimmed);
                  } else {
                    keyChars = [];
                  }
                }
              }
            }
          }

          // If no description found from info section, use title
          if (!desc) desc = title;

          // Images from carousel — deduplicate by keeping highest quality
          const allImgs = document.querySelectorAll('[class*="item-main-window"] img');
          const seen = new Set();
          const images = [];
          for (const img of allImgs) {
            const src = img.src || "";
            if (!src.includes("alicdn.com")) continue;
            // Extract base image ID (before resolution suffix)
            const baseMatch = src.match(/(O1CN[a-zA-Z0-9]+)/);
            const baseId = baseMatch ? baseMatch[1] : src;
            if (seen.has(baseId)) continue;
            seen.add(baseId);
            // Prefer highest quality: use _Q90 version
            let bestSrc = src;
            if (src.includes("_220x")) {
              bestSrc = src.replace(/_220x10000Q90/, "_790x10000Q90");
            }
            images.push(bestSrc);
          }

          // Video
          const videoEl = document.querySelector('video');
          let videoUrl = null;
          if (videoEl) {
            videoUrl = videoEl.src || "";
            if (!videoUrl) {
              const sourceEl = videoEl.querySelector('source');
              videoUrl = sourceEl ? sourceEl.src : null;
            }
          }

          return JSON.stringify({
            title,
            seller: { name: sellerName, avatar: sellerAvatar, location, intro: introText },
            price,
            originalPrice,
            desc,
            attributes,
            wants,
            views,
            images,
            videoUrl,
          });
        } catch(e) { return JSON.stringify({error: e.message, url: location.href}); }
      })()`,
    });
    const evalResp = await wsRecv(ps);
    const data = JSON.parse(evalResp.result.result.value);

    if (data.error) {
      process.stderr.write("Error: " + data.error + " url=" + (data.url || "") + "\n");
      console.log(JSON.stringify(data));
      process.exit(1);
    }

    // Build content: price line + description + attributes
    let content = "";
    if (data.price) {
      content += `价格: ¥${data.price}`;
      if (data.originalPrice) content += ` (原价: ¥${data.originalPrice})`;
      content += "\n\n";
    }
    content += data.desc;
    if (data.attributes && Object.keys(data.attributes).length > 0) {
      content += "\n\n";
      for (const [k, v] of Object.entries(data.attributes)) {
        content += `${k}: ${v}\n`;
      }
    }

    // Output final result
    console.log(JSON.stringify({
      platform: "goofish",
      title: data.title || "(无标题)",
      author: {
        name: data.seller.name || "unknown",
        id: "",
        avatar: data.seller.avatar || null,
        ip_location: data.seller.location || null,
        signature: data.seller.intro || null,
      },
      content: content.trim(),
      stats: {
        wants: data.wants,
        views: data.views,
      },
      images: data.images,
      video_url: data.videoUrl || null,
      comments: [],
      url: itemUrl,
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
