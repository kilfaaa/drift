// Живой бот в чате: Telegram шлёт сюда все обновления (webhook).
// 1) Если к боту обращаются (упоминание или ответ на его сообщение) — отвечает нейросетью Workers AI в стиле чата.
// 2) Голоса в опросах и служебные события складывает в KV, прогнозный бот на GitHub забирает их через /updates.
// Секреты (задаются при деплое): TELEGRAM_BOT_TOKEN, PERSONA (описание экипажа, из сейфа), CHAT_ID.

const MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast";
const MODEL_SMALL = "@cf/meta/llama-3.1-8b-instruct-fast";
const SPOTS = [["medvedovka", "Медведовка"], ["krasnaya", "KAPANY"], ["mega", "MEGA"], ["stk", "STK"]];
const RAW = "https://raw.githubusercontent.com/kilfaaa/drift/main/data/";
const KEEP = ["poll_answer", "my_chat_member"];   // что нужно прогнозному боту

const hex = async (s) => [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s)))]
  .map((b) => b.toString(16).padStart(2, "0")).join("");
const hookSecret = (env) => hex("hook:" + env.TELEGRAM_BOT_TOKEN).then((h) => h.slice(0, 48));
const pullKey = (env) => hex("updates:" + env.TELEGRAM_BOT_TOKEN);

async function tg(env, method, params) {
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(params),
  });
  const j = await r.json().catch(() => ({}));
  return j.ok ? j.result : null;
}

let ME = null;
async function me(env) {
  if (!ME) ME = await tg(env, "getMe", {});
  return ME;
}

// ---------- прогноз для ответов ----------
const WD = ["вс", "пн", "вт", "ср", "чт", "пт", "сб"];
const ST = { go: "мокро, едем", maybe: "может быть", dry: "сухо", snow: "снег", ice: "гололёд" };
async function forecast() {
  const lines = [];
  for (const [id, name] of SPOTS) {
    try {
      const r = await fetch(RAW + `fc_${id}.json`, { cf: { cacheTtl: 600, cacheEverything: true } });
      if (!r.ok) continue;
      const j = await r.json();
      const ev = (j.evenings || []).slice(0, 8).map((e) => {
        const d = new Date(e.date + "T12:00:00Z");
        return `${WD[d.getUTCDay()]} ${e.date.slice(8)}.${e.date.slice(5, 7)}: ${e.pct}% (${ST[e.status] || e.status})`;
      });
      lines.push(`${name}: ${ev.join("; ")}`);
    } catch (_) { /* без прогноза по точке */ }
  }
  return lines.length ? lines.join("\n") : "прогноз сейчас недоступен";
}

// ---------- память разговора (только обмены с ботом) ----------
async function history(env, chat) {
  try { return JSON.parse((await env.KV.get(`h:${chat}`)) || "[]"); } catch (_) { return []; }
}
async function remember(env, chat, h) {
  try { await env.KV.put(`h:${chat}`, JSON.stringify(h.slice(-12)), { expirationTtl: 86400 * 3 }); } catch (_) {}
}

const who = (u) => [u?.first_name, u?.last_name].filter(Boolean).join(" ") || u?.username || "кто-то";

function system(env, fc) {
  const now = new Date(Date.now() + 3 * 3600e3);
  return `Ты — бот дрифт-компании в их Telegram-чате. Сейчас ${WD[now.getUTCDay()]} ${now.toISOString().slice(0, 16).replace("T", " ")} по Москве.

${env.PERSONA || ""}

Как отвечать:
- По-русски, коротко: обычно одна-две строки, как пацан в чате. Без вступлений, списков и смайликов через слово.
- Мат можно. Подкалывай жёстко и лично: машины, руки из жопы, гремлинство, кто сколько «шарит». Над собой тоже шути.
- Если спрашивают про погоду, мокро ли, куда ехать — бери цифры ТОЛЬКО из прогноза ниже, не выдумывай. Можно добавить подкол.
- Нельзя: ничего сексуального про чьих-то мам и родственников, ничего сексуального про Тимоху; не обвиняй посторонних людей (в т.ч. «Желтка») в долгах, мошенничестве и преступлениях; без угроз и призывов к насилию; не дискриминируй по национальности. Если просят такое — отшутись про другое.
- Не говори, что ты нейросеть или модель, если не спросили прямо.

Прогноз мокрого асфальта после 20:00 (шанс, статус):
${fc}`;
}

async function reply(env, msg, botName) {
  const chat = msg.chat.id;
  let text = (msg.text || msg.caption || "").replace(new RegExp("@" + botName, "ig"), "").trim();
  if (!text) text = "(пустое сообщение)";
  const h = await history(env, chat);
  const msgs = [{ role: "system", content: system(env, await forecast()) }, ...h];
  const quoted = msg.reply_to_message && !msg.reply_to_message.from?.is_bot && msg.reply_to_message.text
    ? `\n(отвечает на сообщение ${who(msg.reply_to_message.from)}: «${msg.reply_to_message.text.slice(0, 300)}»)` : "";
  const user = `${who(msg.from)}: ${text.slice(0, 800)}${quoted}`;
  msgs.push({ role: "user", content: user });
  let out = null;
  for (const m of [MODEL, MODEL_SMALL]) {
    try {
      const r = await env.AI.run(m, { messages: msgs, max_tokens: 220, temperature: 0.9 });
      out = (r && (r.response || r.result?.response) || "").trim();
      if (out) break;
    } catch (e) { console.log("ai", m, String(e).slice(0, 200)); }
  }
  if (!out) {
    const said = await env.KV.get(`limit:${chat}`);
    if (said) return;
    out = "Всё, мозги на сегодня кончились, бесплатный лимит. Завтра продолжу хуйню нести.";
    await env.KV.put(`limit:${chat}`, "1", { expirationTtl: 6 * 3600 });
  }
  out = out.replace(/^(бот|ассистент)\s*:\s*/i, "").slice(0, 1500);
  await tg(env, "sendMessage", { chat_id: chat, text: out, reply_to_message_id: msg.message_id, allow_sending_without_reply: true });
  h.push({ role: "user", content: user }, { role: "assistant", content: out });
  await remember(env, chat, h);
}

async function onUpdate(env, u) {
  const kind = Object.keys(u).find((k) => k !== "update_id");
  const msg = u.message || u.edited_message;
  // для прогнозного бота: опросы, добавление в чат, первое сообщение группы, миграция в супергруппу
  const needChat = msg && ["group", "supergroup"].includes(msg.chat?.type) && (msg.migrate_to_chat_id || !env.CHAT_ID);
  if (KEEP.includes(kind) || needChat) {
    const slim = needChat ? { update_id: u.update_id, message: { chat: msg.chat, migrate_to_chat_id: msg.migrate_to_chat_id } } : u;
    await env.KV.put(`u:${String(u.update_id).padStart(12, "0")}`, JSON.stringify(slim), { expirationTtl: 86400 * 14 });
  }
  if (!u.message || !u.message.text && !u.message.caption) return;
  const m = u.message;
  if (m.from?.is_bot) return;
  const allowed = env.CHAT_ID ? String(m.chat.id) === String(env.CHAT_ID) : ["group", "supergroup"].includes(m.chat.type);
  if (!allowed) return;   // только наш чат: чужие не съедят лимит
  const bot = await me(env);
  if (!bot) return;
  const t = (m.text || m.caption || "");
  const mention = new RegExp("@" + bot.username + "\\b", "i").test(t) || /^(бот|ботяра|ботик)[,!\s]/i.test(t);
  const toBot = m.reply_to_message?.from?.id === bot.id;
  if (mention || toBot) await reply(env, m, bot.username);
}

export default {
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    if (req.method === "POST" && url.pathname === "/tg") {
      if (req.headers.get("x-telegram-bot-api-secret-token") !== await hookSecret(env)) return new Response("no", { status: 403 });
      const u = await req.json().catch(() => null);
      if (u) ctx.waitUntil(onUpdate(env, u).catch((e) => console.log("update", String(e).slice(0, 300))));
      return new Response("ok");
    }
    if (url.pathname === "/updates") {   // для прогнозного бота, как getUpdates
      if (req.headers.get("authorization") !== "Bearer " + await pullKey(env)) return new Response("no", { status: 403 });
      const offset = Number(url.searchParams.get("offset") || 0);
      const out = []; let cursor;
      do {
        const l = await env.KV.list({ prefix: "u:", cursor });
        for (const k of l.keys) {
          const id = Number(k.name.slice(2));
          if (id < offset) { await env.KV.delete(k.name); continue; }
          const v = await env.KV.get(k.name);
          if (v) out.push(JSON.parse(v));
        }
        cursor = l.list_complete ? null : l.cursor;
      } while (cursor);
      return Response.json({ ok: true, result: out.sort((a, b) => a.update_id - b.update_id) });
    }
    return new Response("medvedrift");
  },
};
