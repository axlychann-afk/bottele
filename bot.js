const TelegramBot = require("node-telegram-bot-api");
const https = require("https");

const BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
const API_KEY = process.env.NROUTER_API_KEY;
const API_URL = "https://trustworthy-solace-production-068d.up.railway.app/v1/chat/completions";
const MODEL = "ssd";

if (!BOT_TOKEN || !API_KEY) {
  console.error("Set TELEGRAM_BOT_TOKEN and NROUTER_API_KEY env vars");
  process.exit(1);
}

const bot = new TelegramBot(BOT_TOKEN, { polling: true });
const conversations = new Map();

function chat(messages) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({ model: MODEL, messages, max_tokens: 2048 });
    const url = new URL(API_URL);
    const req = https.request(
      {
        hostname: url.hostname,
        path: url.pathname,
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${API_KEY}`,
          "Content-Length": Buffer.byteLength(body),
        },
      },
      (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          try {
            const json = JSON.parse(data);
            if (json.choices && json.choices[0]) {
              resolve(json.choices[0].message.content);
            } else {
              console.error("API response:", data);
              reject(new Error(json.error?.message || "Unknown API error"));
            }
          } catch (e) {
            console.error("Raw response:", data);
            reject(e);
          }
        });
      }
    );
    req.on("error", reject);
    req.write(body);
    req.end();
  });
}

bot.onText(/\/start/, (msg) => {
  conversations.delete(msg.chat.id);
  bot.sendMessage(msg.chat.id, "Halo! Kirim pesan apapun, saya akan jawab pakai AI.\n/reset untuk hapus riwayat chat.");
});

bot.onText(/\/reset/, (msg) => {
  conversations.delete(msg.chat.id);
  bot.sendMessage(msg.chat.id, "Riwayat chat dihapus.");
});

bot.on("message", async (msg) => {
  if (!msg.text || msg.text.startsWith("/")) return;

  const chatId = msg.chat.id;
  if (!conversations.has(chatId)) conversations.set(chatId, []);
  const history = conversations.get(chatId);

  history.push({ role: "user", content: msg.text });
  if (history.length > 20) history.splice(0, history.length - 20);

  const typing = setInterval(() => bot.sendChatAction(chatId, "typing"), 4000);
  bot.sendChatAction(chatId, "typing");

  try {
    const reply = await chat([
      { role: "system", content: "Kamu adalah asisten AI yang membantu. Jawab dalam bahasa yang sama dengan user." },
      ...history,
    ]);
    history.push({ role: "assistant", content: reply });
    await bot.sendMessage(chatId, reply).catch((e) =>
      console.error("Send error:", e.message)
    );
  } catch (err) {
    console.error("API error:", err.message);
    await bot.sendMessage(chatId, "Maaf, terjadi error. Coba lagi nanti.");
  } finally {
    clearInterval(typing);
  }
});

console.log("Bot is running...");
